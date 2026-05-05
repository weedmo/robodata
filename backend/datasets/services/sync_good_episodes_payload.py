"""Operation Payload for `sync_good_episodes` jobs. See CONTEXT.md → Operation Payload."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class SyncGoodEpisodesPayload(BaseModel):
    source_path: str = Field(min_length=1)
    episode_ids: list[int] = Field(min_length=1)
    destination_path: str = Field(min_length=1)

    model_config = {"extra": "ignore"}

    @field_validator("episode_ids", mode="before")
    @classmethod
    def _coerce_episode_ids(cls, v: object) -> list[int]:
        if not isinstance(v, list):
            raise ValueError("episode_ids must be a non-empty list")
        return [int(item) for item in v]

    def dedupe_key(self) -> str:
        return f"sync_good_episodes:{self.source_path}:{self.destination_path}"
