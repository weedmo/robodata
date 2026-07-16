"""Convert a legacy MCAP recording into the Data Foundry FB episode layout."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import flatbuffers
from mcap.reader import make_reader
from mcap.records import Channel, Message, Schema
from mcap.writer import CompressionType, Writer


COMPRESSED_IMAGE_TYPE = "sensor_msgs/msg/CompressedImage"


class ConversionCancelled(Exception):
    """Raised before promotion when an MCAP-to-FB conversion is cancelled."""


@dataclass(frozen=True)
class FBMessage:
    timestamp_ns: int
    topic: str
    msg_type: str
    data: bytes


@dataclass(frozen=True)
class ConversionResult:
    episode_dir: Path
    camera_message_counts: dict[str, int]
    state_message_count: int
    chunk_count: int


def _build_fb_message(
    builder: flatbuffers.Builder,
    message: FBMessage,
) -> int:
    topic = builder.CreateString(message.topic)
    msg_type = builder.CreateString(message.msg_type)
    data = builder.CreateByteVector(message.data)

    builder.StartObject(4)
    builder.PrependInt64Slot(0, message.timestamp_ns, 0)
    builder.PrependUOffsetTRelativeSlot(1, topic, 0)
    builder.PrependUOffsetTRelativeSlot(2, msg_type, 0)
    builder.PrependUOffsetTRelativeSlot(3, data, 0)
    return builder.EndObject()


def build_message_bag(source: str, messages: list[FBMessage]) -> bytes:
    """Serialize messages with the canonical tachybridge MessageBag schema."""
    builder = flatbuffers.Builder(1024)
    offsets = [_build_fb_message(builder, message) for message in messages]

    builder.StartVector(4, len(offsets), 4)
    for offset in reversed(offsets):
        builder.PrependUOffsetTRelative(offset)
    messages_offset = builder.EndVector()

    source_offset = builder.CreateString(source)
    builder.StartObject(2)
    builder.PrependUOffsetTRelativeSlot(0, source_offset, 0)
    builder.PrependUOffsetTRelativeSlot(1, messages_offset, 0)
    bag_offset = builder.EndObject()
    builder.Finish(bag_offset)
    return bytes(builder.Output())


def _normalized_message_type(schema: Schema | None) -> str:
    if schema is None:
        return ""
    name = schema.name.replace("/msg/", "/")
    if name == "sensor_msgs/CompressedImage":
        return COMPRESSED_IMAGE_TYPE
    return schema.name


def _is_compressed_image(schema: Schema | None) -> bool:
    return _normalized_message_type(schema) == COMPRESSED_IMAGE_TYPE


class StateMCAPWriter:
    """Rewrite non-camera messages while preserving their ROS schemas."""

    def __init__(self, output: Path):
        self._writer = Writer(str(output), compression=CompressionType.ZSTD)
        self._writer.start(profile="ros2")
        self._schema_ids: dict[tuple[str, str, bytes], int] = {}
        self._channel_ids: dict[tuple[Any, ...], int] = {}
        self.message_count = 0

    def _schema_id(self, schema: Schema | None) -> int:
        if schema is None:
            return 0
        key = (schema.name, schema.encoding, bytes(schema.data))
        if key not in self._schema_ids:
            self._schema_ids[key] = self._writer.register_schema(*key)
        return self._schema_ids[key]

    def _channel_id(self, schema: Schema | None, channel: Channel) -> int:
        schema_id = self._schema_id(schema)
        metadata = dict(channel.metadata or {})
        key = (
            channel.topic,
            channel.message_encoding,
            schema_id,
            tuple(sorted(metadata.items())),
        )
        if key not in self._channel_ids:
            self._channel_ids[key] = self._writer.register_channel(
                topic=channel.topic,
                message_encoding=channel.message_encoding,
                schema_id=schema_id,
                metadata=metadata,
            )
        return self._channel_ids[key]

    def add(self, schema: Schema | None, channel: Channel, message: Message) -> None:
        self._writer.add_message(
            channel_id=self._channel_id(schema, channel),
            log_time=message.log_time,
            publish_time=message.publish_time,
            sequence=message.sequence,
            data=bytes(message.data),
        )
        self.message_count += 1

    def finish(self) -> None:
        self._writer.finish()


def _load_metacard(path: Path) -> dict[str, Any]:
    try:
        metacard = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read metacard {path}: {exc}") from exc
    camera_map = metacard.get("camera_topic_map")
    if not isinstance(camera_map, dict) or not camera_map:
        raise ValueError("metacard.json requires a non-empty camera_topic_map")
    if not all(isinstance(name, str) and isinstance(topic, str) for name, topic in camera_map.items()):
        raise ValueError("metacard camera_topic_map must map strings to strings")
    if len(set(camera_map.values())) != len(camera_map):
        raise ValueError("each camera in camera_topic_map must use a unique topic")
    return metacard


def _discover_source(
    source: Path,
    metacard_path: Path | None,
    serial: str | None,
) -> tuple[list[Path], Path, str]:
    source = source.resolve()
    if source.is_dir():
        episode_dir = source
        mcap_paths = sorted(source.glob("*.mcap"))
        inferred_serial = source.name
    elif source.is_file() and source.suffix == ".mcap":
        episode_dir = source.parent
        mcap_paths = [source]
        inferred_serial = re.sub(r"_\d+$", "", source.stem)
    else:
        raise ValueError(f"source must be an episode directory or MCAP file: {source}")

    if not mcap_paths:
        raise ValueError(f"no root MCAP files found in {episode_dir}")
    metacard = (metacard_path or episode_dir / "metacard.json").resolve()
    if not metacard.is_file():
        raise ValueError(f"metacard.json not found: {metacard}")
    return mcap_paths, metacard, serial or inferred_serial


def _write_chunk(
    camera_dir: Path,
    chunk_index: int,
    source: str,
    messages: list[FBMessage],
) -> None:
    output = camera_dir / f"chunk_{chunk_index:04d}.fb"
    output.write_bytes(build_message_bag(source, messages))


def _promote_episode(staged: Path, destination: Path, *, force: bool) -> None:
    if destination.exists() and not force:
        raise FileExistsError(
            f"destination already exists: {destination}; pass --force to replace it"
        )
    backup = destination.parent / f".{destination.name}.backup-{uuid.uuid4().hex[:8]}"
    moved_existing = False
    try:
        if destination.exists():
            os.replace(destination, backup)
            moved_existing = True
        os.replace(staged, destination)
    except Exception:
        if moved_existing and not destination.exists() and backup.exists():
            os.replace(backup, destination)
        raise
    else:
        if moved_existing:
            shutil.rmtree(backup)


def convert_episode(
    source: Path,
    output_root: Path,
    *,
    metacard_path: Path | None = None,
    serial: str | None = None,
    messages_per_chunk: int = 300,
    force: bool = False,
    cancel_requested: Callable[[], bool] | None = None,
) -> ConversionResult:
    """Convert every camera message and state message from one episode."""
    if messages_per_chunk < 1:
        raise ValueError("messages_per_chunk must be at least 1")
    mcap_paths, metacard_path, serial = _discover_source(
        source,
        metacard_path,
        serial,
    )
    metacard = _load_metacard(metacard_path)
    camera_map: dict[str, str] = metacard["camera_topic_map"]
    topic_to_camera = {topic: camera for camera, topic in camera_map.items()}

    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / serial
    source_episode_dir = mcap_paths[0].parent.resolve()
    if destination.resolve() == source_episode_dir:
        raise ValueError("output episode must not replace the source episode")
    if destination.exists() and not force:
        raise FileExistsError(
            f"destination already exists: {destination}; pass --force to replace it"
        )

    with tempfile.TemporaryDirectory(prefix=f".{serial}.staging-", dir=output_root) as temp:
        staged = Path(temp) / serial
        images_dir = staged / "images"
        state_dir = staged / "state"
        images_dir.mkdir(parents=True)
        state_dir.mkdir()
        shutil.copy2(metacard_path, staged / "metacard.json")

        buffers: dict[str, list[FBMessage]] = {camera: [] for camera in camera_map}
        camera_counts = {camera: 0 for camera in camera_map}
        camera_chunks = {camera: 0 for camera in camera_map}
        for camera in camera_map:
            (images_dir / camera).mkdir()

        state_writer = StateMCAPWriter(state_dir / "state_0.mcap")
        try:
            for mcap_path in mcap_paths:
                if cancel_requested is not None and cancel_requested():
                    raise ConversionCancelled(f"conversion cancelled before reading {mcap_path}")
                with mcap_path.open("rb") as stream:
                    reader = make_reader(stream)
                    for schema, channel, message in reader.iter_messages():
                        if cancel_requested is not None and cancel_requested():
                            raise ConversionCancelled(
                                f"conversion cancelled while reading {mcap_path}"
                            )
                        camera = topic_to_camera.get(channel.topic)
                        if camera is None:
                            state_writer.add(schema, channel, message)
                            continue
                        if not _is_compressed_image(schema):
                            schema_name = schema.name if schema is not None else "<missing>"
                            raise ValueError(
                                f"camera topic {channel.topic} uses unsupported schema "
                                f"{schema_name}; only sensor_msgs/msg/CompressedImage is supported"
                            )
                        buffer = buffers[camera]
                        buffer.append(FBMessage(
                            timestamp_ns=message.publish_time,
                            topic=channel.topic,
                            msg_type=_normalized_message_type(schema),
                            data=bytes(message.data),
                        ))
                        camera_counts[camera] += 1
                        if len(buffer) >= messages_per_chunk:
                            camera_chunks[camera] += 1
                            _write_chunk(
                                images_dir / camera,
                                camera_chunks[camera],
                                str(mcap_path),
                                buffer,
                            )
                            buffer.clear()
        finally:
            state_writer.finish()

        for camera, buffer in buffers.items():
            if buffer:
                camera_chunks[camera] += 1
                _write_chunk(
                    images_dir / camera,
                    camera_chunks[camera],
                    str(mcap_paths[-1]),
                    buffer,
                )

        missing_cameras = [camera for camera, count in camera_counts.items() if count == 0]
        if missing_cameras:
            raise ValueError(
                "MCAP contains no messages for camera(s): " + ", ".join(missing_cameras)
            )
        if state_writer.message_count == 0:
            raise ValueError("MCAP contains no non-camera state messages")

        chunk_count = sum(camera_chunks.values())
        manifest = {
            "serial": serial,
            "source_mcaps": [str(path) for path in mcap_paths],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "camera_message_counts": camera_counts,
            "state_message_count": state_writer.message_count,
            "chunk_count": chunk_count,
            "messages_per_chunk": messages_per_chunk,
        }
        (staged / "conversion_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if cancel_requested is not None and cancel_requested():
            raise ConversionCancelled("conversion cancelled before output promotion")
        _promote_episode(staged, destination, force=force)

    return ConversionResult(
        episode_dir=destination,
        camera_message_counts=camera_counts,
        state_message_count=state_writer.message_count,
        chunk_count=chunk_count,
    )
