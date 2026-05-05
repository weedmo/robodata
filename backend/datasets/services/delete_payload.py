"""Operation Payload for `delete` jobs. See CONTEXT.md → Operation Payload."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class DeletePayload(BaseModel):
    source_path: str = Field(min_length=1)
    episode_ids: list[int] = Field(min_length=1)
    output_dir: str | None = None

    model_config = {"extra": "ignore"}

    @field_validator("episode_ids", mode="before")
    @classmethod
    def _coerce_episode_ids(cls, v: object) -> list[int]:
        if not isinstance(v, list):
            raise ValueError("episode_ids must be a non-empty list")
        return [int(item) for item in v]

    def dedupe_key(self) -> str:
        return f"delete:{self.source_path}:{','.join(map(str, self.episode_ids))}"

    def output_path_or_none(self) -> Path | None:
        return Path(self.output_dir) if self.output_dir else None
