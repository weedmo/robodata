from pydantic import BaseModel, field_validator, model_validator


class DatasetInfo(BaseModel):
    path: str
    dataset_key: str | None = None
    name: str
    fps: int
    total_episodes: int
    total_tasks: int
    robot_type: str | None = None
    features: dict = {}


class Episode(BaseModel):
    episode_index: int
    length: int
    task_index: int
    task_instruction: str = ""
    chunk_index: int = 0
    file_index: int = 0
    dataset_from_index: int = 0
    dataset_to_index: int = 0
    grade: str | None = None
    tags: list[str] = []
    reason: str | None = None
    created_at: str | None = None
    raw_recording: str | None = None


class Task(BaseModel):
    task_index: int
    task_instruction: str


class EpisodeUpdate(BaseModel):
    dataset_path: str
    grade: str | None = None
    tags: list[str] | None = None
    reason: str | None = None

    @field_validator("grade")
    @classmethod
    def validate_grade(cls, v: str | None) -> str | None:
        if v is not None and v not in ("good", "normal", "bad"):
            raise ValueError("Grade must be one of: Good, Normal, Bad")
        return v

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            v = [t.strip() for t in v if t.strip()]
        return v

    @model_validator(mode="after")
    def _reason_required_for_bad_or_normal(self):
        if self.grade in ("bad", "normal"):
            if self.reason is None or not self.reason.strip():
                raise ValueError("reason is required when grade is 'bad' or 'normal'")
        return self


class BulkGradeRequest(BaseModel):
    dataset_path: str
    episode_indices: list[int]
    grade: str
    reason: str | None = None

    @field_validator("grade")
    @classmethod
    def validate_grade(cls, v: str) -> str:
        if v not in ("good", "normal", "bad"):
            raise ValueError("Grade must be one of: good, normal, bad")
        return v

    @model_validator(mode="after")
    def _reason_required_for_bad_or_normal(self):
        if self.grade in ("bad", "normal"):
            if self.reason is None or not self.reason.strip():
                raise ValueError("reason is required when grade is 'bad' or 'normal'")
        return self


class TaskUpdate(BaseModel):
    dataset_path: str
    task_instruction: str


class DatasetLoadRequest(BaseModel):
    path: str


class DatasetExportRequest(BaseModel):
    dataset_path: str
    output_path: str
    exclude_grades: list[str] = ["bad"]


class CellInfo(BaseModel):
    name: str
    path: str
    mount_root: str
    dataset_count: int
    active: bool


class DatasetSourceInfo(BaseModel):
    name: str
    path: str
    cell_count: int
    active: bool


class DatasetSummary(BaseModel):
    name: str
    path: str
    total_episodes: int
    graded_count: int
    good_count: int = 0
    normal_count: int = 0
    bad_count: int = 0
    robot_type: str | None = None
    fps: int
    total_duration_sec: float = 0
    good_duration_sec: float = 0
    normal_duration_sec: float = 0
    bad_duration_sec: float = 0


class DistributionRequest(BaseModel):
    dataset_path: str
    field: str
    chart_type: str = "auto"  # "auto", "histogram", "bar"


class FieldInfo(BaseModel):
    name: str
    dtype: str  # "int64", "float64", "string", "bool", etc.
    is_system: bool  # True = read-only system column


class DistributionBin(BaseModel):
    label: str
    count: int


class DistributionResponse(BaseModel):
    field: str
    dtype: str
    chart_type: str  # "histogram" or "bar"
    bins: list[DistributionBin]
    total: int


class InfoFieldUpdate(BaseModel):
    key: str
    value: str | int | float | bool | None  # None = delete


class EpisodeColumnAdd(BaseModel):
    dataset_path: str
    column_name: str
    dtype: str  # "string", "int64", "float64", "bool"
    default_value: str | int | float | bool = ""
