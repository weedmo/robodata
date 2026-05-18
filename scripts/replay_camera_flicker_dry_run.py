"""Replay the head/ZED localized flicker detector on a known dataset.

Example:
    python3 scripts/replay_camera_flicker_dry_run.py \
      /mnt/synology/data/data_div/2026_1/lerobot/cell002/habilis_zero_joint_10h_split \
      --grade bad --expected-matches 120
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.datasets.services.camera_flicker_detector import scan_dataset_for_camera_flicker
from backend.datasets.services.camera_flicker_handler import _decode_video_window_frames
from backend.datasets.services.dataset_registry import DatasetContext, dataset_registry
from backend.datasets.services.episode_service import EpisodeService


DEFAULT_REASON_REGEX = r"head|헤드|zed|제드|깜"


@dataclass
class FilteredDatasetContext:
    base: DatasetContext
    episodes: list[dict[str, Any]]

    def get_dataset_path(self) -> str:
        return self.base.get_dataset_path()

    def get_episodes(self) -> list[dict[str, Any]]:
        return self.episodes

    def get_features(self) -> dict:
        return self.base.get_features()

    def get_episode_file_location(self, episode_index: int) -> dict:
        return self.base.get_episode_file_location(episode_index)


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset_path")
    parser.add_argument("--grade", default="bad")
    parser.add_argument("--reason-regex", default=DEFAULT_REASON_REGEX)
    parser.add_argument(
        "--episode-indices",
        help="Comma-separated explicit episode indices to replay instead of grade/reason filtering.",
    )
    parser.add_argument("--expected-matches", type=int)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    ctx = dataset_registry.get(args.dataset_path)
    episodes = await EpisodeService().get_episodes(ctx)
    selected = _select_episodes(
        episodes,
        grade=args.grade,
        reason_regex=args.reason_regex,
        episode_indices=args.episode_indices,
    )
    if args.limit is not None:
        selected = selected[: args.limit]

    filtered_ctx = FilteredDatasetContext(ctx, selected)
    result = scan_dataset_for_camera_flicker(
        filtered_ctx,
        frame_provider=_decode_video_window_frames,
        dry_run=True,
    )
    output = {
        "dataset_path": args.dataset_path,
        "candidate_episode_count": len(selected),
        "matched_episode_count": len(result.matched_episode_indices),
        "matched_episode_indices": result.matched_episode_indices,
        "camera_keys": result.camera_keys,
        "tile_grid": result.tile_grid,
        "warnings": result.warnings,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if args.expected_matches is not None and len(result.matched_episode_indices) != args.expected_matches:
        return 1
    return 0


def _select_episodes(
    episodes: list[dict[str, Any]],
    *,
    grade: str,
    reason_regex: str,
    episode_indices: str | None,
) -> list[dict[str, Any]]:
    if episode_indices:
        selected_indices = {int(item.strip()) for item in episode_indices.split(",") if item.strip()}
        return [
            episode for episode in episodes
            if int(episode["episode_index"]) in selected_indices
        ]

    reason_pattern = re.compile(reason_regex, re.IGNORECASE)
    return [
        episode
        for episode in episodes
        if episode.get("grade") == grade
        and reason_pattern.search(str(episode.get("reason") or ""))
    ]


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
