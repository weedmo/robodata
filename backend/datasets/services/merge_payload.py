"""Operation Payload for `merge` jobs. See CONTEXT.md → Operation Payload."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class MergePayload(BaseModel):
    source_paths: list[str] = Field(min_length=1)
    target_name: str = Field(min_length=1)
    output_dir: str | None = None

    model_config = {"extra": "ignore"}

    @field_validator("source_paths", mode="before")
    @classmethod
    def _coerce_source_paths(cls, v: object) -> list[str]:
        if not isinstance(v, list):
            raise ValueError("source_paths must be a non-empty list")
        return [str(item) for item in v]

    def dedupe_key(self) -> str:
        return f"merge:{','.join(self.source_paths)}:{self.target_name}"

    def output_path(self) -> Path:
        if self.output_dir:
            return Path(self.output_dir)
        return Path(self.source_paths[0]).parent / self.target_name
