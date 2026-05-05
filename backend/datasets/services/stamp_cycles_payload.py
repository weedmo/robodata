"""Operation Payload for `stamp_cycles` jobs. See CONTEXT.md → Operation Payload."""
from __future__ import annotations

from pydantic import BaseModel, Field


class StampCyclesPayload(BaseModel):
    source_path: str = Field(min_length=1)
    overwrite: bool = False

    model_config = {"extra": "ignore"}

    def dedupe_key(self) -> str:
        return f"stamp_cycles:{self.source_path}"
