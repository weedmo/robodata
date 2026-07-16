import json
import struct
from pathlib import Path

import pytest
from mcap.reader import make_reader
from mcap.writer import CompressionType, Writer

from backend.converter.mcap_to_fb_converter import (
    ConversionCancelled,
    convert_episode,
)


def _write_source_mcap(path: Path, *, include_camera: bool = True) -> None:
    writer = Writer(str(path), compression=CompressionType.ZSTD)
    writer.start(profile="ros2")
    image_schema = writer.register_schema(
        "sensor_msgs/msg/CompressedImage",
        "ros2msg",
        b"std_msgs/Header header\nstring format\nuint8[] data\n",
    )
    state_schema = writer.register_schema(
        "sensor_msgs/msg/JointState",
        "ros2msg",
        b"std_msgs/Header header\nstring[] name\nfloat64[] position\n",
    )
    image_channel = writer.register_channel(
        "/cam/image/compressed",
        "cdr",
        image_schema,
    )
    state_channel = writer.register_channel("/joint_states", "cdr", state_schema)
    if include_camera:
        for index, timestamp in enumerate((100, 200, 300), start=1):
            writer.add_message(
                image_channel,
                log_time=timestamp,
                publish_time=timestamp,
                sequence=index,
                data=f"image-{index}".encode(),
            )
    for index, timestamp in enumerate((110, 210), start=1):
        writer.add_message(
            state_channel,
            log_time=timestamp,
            publish_time=timestamp,
            sequence=index,
            data=f"state-{index}".encode(),
        )
    writer.finish()


def _write_metacard(path: Path) -> None:
    path.write_text(
        json.dumps({
            "task_name": "test",
            "fps": 30,
            "camera_topic_map": {"cam_head": "/cam/image/compressed"},
            "state_topic": "/joint_states",
            "joint_names": ["joint_1"],
        }),
        encoding="utf-8",
    )


def _read_u16(buffer: bytes, offset: int) -> int:
    return struct.unpack_from("<H", buffer, offset)[0]


def _read_u32(buffer: bytes, offset: int) -> int:
    return struct.unpack_from("<I", buffer, offset)[0]


def _field(buffer: bytes, table: int, slot: int) -> int:
    vtable = table - struct.unpack_from("<i", buffer, table)[0]
    voffset = 4 + slot * 2
    assert voffset < _read_u16(buffer, vtable)
    return table + _read_u16(buffer, vtable + voffset)


def _read_string(buffer: bytes, field: int) -> str:
    start = field + _read_u32(buffer, field)
    length = _read_u32(buffer, start)
    return buffer[start + 4:start + 4 + length].decode()


def _read_bytes(buffer: bytes, field: int) -> bytes:
    start = field + _read_u32(buffer, field)
    length = _read_u32(buffer, start)
    return buffer[start + 4:start + 4 + length]


def _read_fb_messages(path: Path) -> list[tuple[int, str, str, bytes]]:
    buffer = path.read_bytes()
    root = _read_u32(buffer, 0)
    messages_field = _field(buffer, root, 1)
    vector = messages_field + _read_u32(buffer, messages_field)
    count = _read_u32(buffer, vector)
    result = []
    for index in range(count):
        element = vector + 4 + index * 4
        table = element + _read_u32(buffer, element)
        result.append((
            struct.unpack_from("<q", buffer, _field(buffer, table, 0))[0],
            _read_string(buffer, _field(buffer, table, 1)),
            _read_string(buffer, _field(buffer, table, 2)),
            _read_bytes(buffer, _field(buffer, table, 3)),
        ))
    return result


def test_converts_whole_episode_into_camera_chunks_and_state_mcap(tmp_path):
    source = tmp_path / "raw" / "serial_001"
    source.mkdir(parents=True)
    _write_source_mcap(source / "serial_001_0.mcap")
    _write_metacard(source / "metacard.json")

    result = convert_episode(
        source,
        tmp_path / "output",
        messages_per_chunk=2,
    )

    episode = tmp_path / "output" / "serial_001"
    assert result.episode_dir == episode
    assert result.camera_message_counts == {"cam_head": 3}
    assert result.state_message_count == 2
    assert result.chunk_count == 2
    chunks = sorted((episode / "images" / "cam_head").glob("*.fb"))
    assert [path.name for path in chunks] == ["chunk_0001.fb", "chunk_0002.fb"]
    assert _read_fb_messages(chunks[0]) == [
        (100, "/cam/image/compressed", "sensor_msgs/msg/CompressedImage", b"image-1"),
        (200, "/cam/image/compressed", "sensor_msgs/msg/CompressedImage", b"image-2"),
    ]
    assert _read_fb_messages(chunks[1]) == [
        (300, "/cam/image/compressed", "sensor_msgs/msg/CompressedImage", b"image-3"),
    ]

    state_topics = []
    with (episode / "state" / "state_0.mcap").open("rb") as stream:
        for _, channel, message in make_reader(stream).iter_messages():
            state_topics.append((channel.topic, bytes(message.data)))
    assert state_topics == [
        ("/joint_states", b"state-1"),
        ("/joint_states", b"state-2"),
    ]
    manifest = json.loads((episode / "conversion_manifest.json").read_text())
    assert manifest["camera_message_counts"] == {"cam_head": 3}
    assert manifest["state_message_count"] == 2


def test_missing_camera_fails_without_publishing_partial_episode(tmp_path):
    source = tmp_path / "raw" / "serial_001"
    source.mkdir(parents=True)
    _write_source_mcap(source / "serial_001_0.mcap", include_camera=False)
    _write_metacard(source / "metacard.json")

    with pytest.raises(ValueError, match="no messages for camera"):
        convert_episode(source, tmp_path / "output")

    assert not (tmp_path / "output" / "serial_001").exists()


def test_cancel_fails_without_publishing_partial_episode(tmp_path):
    source = tmp_path / "raw" / "serial_001"
    source.mkdir(parents=True)
    _write_source_mcap(source / "serial_001_0.mcap")
    _write_metacard(source / "metacard.json")

    with pytest.raises(ConversionCancelled):
        convert_episode(
            source,
            tmp_path / "output",
            cancel_requested=lambda: True,
        )

    assert not (tmp_path / "output" / "serial_001").exists()
