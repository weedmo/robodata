from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


DEFAULT_DATASET_ROOT_BASE = "/mnt/synology/data/data_div/2026_1"
DEFAULT_DATASET_SOURCES = ["lerobot", "lerobot_test"]
CURATION_TOOLS_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONVERSION_REPO_PATH = str((CURATION_TOOLS_ROOT / "rosbag2lerobot-svt").resolve())


def _default_dataset_sources() -> list[str]:
    return list(DEFAULT_DATASET_SOURCES)


def _default_allowed_dataset_roots() -> list[str]:
    # Path validation scope: anywhere under the shared base is writable as a destination.
    # Curation scope (what the UI scans) is narrower — see configured_dataset_roots().
    return [str(Path(DEFAULT_DATASET_ROOT_BASE).resolve())]


class Settings(BaseSettings):
    dataset_root_base: str = DEFAULT_DATASET_ROOT_BASE
    # Add a new directory name here to expose it as a top-level source in the UI.
    dataset_sources: list[str] = Field(default_factory=_default_dataset_sources)
    dataset_path: str = f"{DEFAULT_DATASET_ROOT_BASE}/lerobot"
    allowed_dataset_roots: list[str] = Field(default_factory=_default_allowed_dataset_roots)
    rosbag_to_lerobot_repo_path: str = DEFAULT_CONVERSION_REPO_PATH
    host: str = "127.0.0.1"
    fastapi_port: int = 8001
    rerun_grpc_port: int = 9876
    rerun_web_port: int = 9090
    db_url: str = "postgresql://curation:dev-only-change-me@127.0.0.1:5433/curation"
    rerun_grpc_url: str = "rerun+grpc://127.0.0.1:9876"
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3000"]
    annotations_path: str = ""
    db_path: str = ""  # empty = default ~/.local/share/curation-tools/metadata.db
    enable_rerun: bool = False
    debug: bool = False
    cell_name_pattern: str = "cell*"

    @model_validator(mode="after")
    def _sync_allowed_dataset_roots(self):
        if "allowed_dataset_roots" not in self.model_fields_set:
            self.allowed_dataset_roots = [str(Path(self.dataset_root_base).resolve())]
        return self

    def configured_dataset_roots(self) -> list[str]:
        """UI curation roots — what shows up in the cell/dataset picker."""
        base = Path(self.dataset_root_base)
        return [str((base / source_name).resolve()) for source_name in self.dataset_sources]

    model_config = {"env_prefix": "CURATION_"}


settings = Settings()
