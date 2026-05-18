"""Operation payload for `auto_bad_camera_flicker` jobs."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from backend.datasets.services.camera_flicker_detector import DEFAULT_CAMERA_FLICKER_BAD_REASON


class AutoBadCameraFlickerPayload(BaseModel):
    dataset_path: str = Field(min_length=1)
    dry_run: bool = True
    allow_overwrite_grades: list[str] = Field(default_factory=lambda: ["normal"])
    reason: str = DEFAULT_CAMERA_FLICKER_BAD_REASON
    tile_grid: tuple[int, int] = (4, 4)

    model_config = {"extra": "ignore"}

    @field_validator("allow_overwrite_grades")
    @classmethod
    def _normalize_grades(cls, value: list[str]) -> list[str]:
        return [grade.strip().lower() for grade in value if grade and grade.strip()]

    @field_validator("tile_grid")
    @classmethod
    def _validate_tile_grid(cls, value: tuple[int, int]) -> tuple[int, int]:
        if value[0] <= 0 or value[1] <= 0:
            raise ValueError("tile_grid dimensions must be positive")
        return value

    def dedupe_key(self) -> str:
        return f"auto_bad_camera_flicker:{self.dataset_path}:{self.dry_run}"
