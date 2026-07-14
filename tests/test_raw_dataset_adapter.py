import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from backend.core.db import _reset, close_db, get_db, init_db
from backend.main import app

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest.fixture(autouse=True)
async def reset_db():
    _reset()
    await init_db()
    db = await get_db()
    await db.execute(
        "TRUNCATE TABLE jobs, dataset_stats, episode_serials, datasets, annotations "
        "RESTART IDENTITY CASCADE"
    )
    await db.commit()
    yield
    await close_db()


@pytest.fixture
async def client():
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac


@pytest.fixture
def raw_task(tmp_path, monkeypatch):
    base = tmp_path / "data_div" / "2026_1"
    raw_root = base / "raw"
    task = raw_root / "cell006" / "Mamonde_toner_sy"
    _make_recording(task, "20260226_170029", task_name="toner")
    _make_recording(task, "20260226_164701", task_name="toner")

    from backend.core.config import settings
    from backend.datasets.routers import cells as cells_router
    from backend.datasets.routers import fields as fields_router
    from backend.datasets.services import raw_dataset_adapter

    for target in (settings, cells_router.settings, fields_router.settings, raw_dataset_adapter.settings):
        monkeypatch.setattr(target, "dataset_root_base", str(base), raising=False)
        monkeypatch.setattr(target, "dataset_sources", ["raw"], raising=False)
        monkeypatch.setattr(target, "allowed_dataset_roots", [str(base.resolve())], raising=False)
    return task


def _make_recording(task: Path, serial: str, *, task_name: str):
    rec = task / serial
    rec.mkdir(parents=True)
    (rec / "metacard.json").write_text(json.dumps({"task_name": task_name}), encoding="utf-8")
    (rec / f"{serial}_0.mcap").write_bytes(b"")


@pytest.mark.anyio
async def test_raw_dataset_load_matches_dataset_info_shape(client, raw_task):
    resp = await client.post("/api/datasets/load", json={"path": str(raw_task)})

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["path"] == str(raw_task.resolve())
    assert payload["dataset_key"].startswith("raw-")
    assert payload["name"] == "raw"
    assert payload["total_episodes"] == 2
    assert payload["total_tasks"] == 1
    assert payload["robot_type"] == "raw"
    assert payload["features"]["raw.rerun"]["viewer"] == "rerun_raw"


@pytest.mark.anyio
async def test_raw_episode_list_matches_episode_shape(client, raw_task):
    resp = await client.get("/api/episodes", params={"dataset_path": str(raw_task)})

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert [ep["episode_index"] for ep in payload] == [0, 1]
    assert payload[0]["task_instruction"] == "toner"
    assert payload[0]["raw_recording"].endswith("cell006/Mamonde_toner_sy/20260226_164701")
    assert payload[1]["raw_recording"].endswith("cell006/Mamonde_toner_sy/20260226_170029")


@pytest.mark.anyio
async def test_raw_grade_update_matches_lerobot_reason_rules_and_db(client, raw_task):
    bad_without_reason = await client.patch(
        "/api/episodes/0",
        json={"dataset_path": str(raw_task), "grade": "bad", "tags": []},
    )
    assert bad_without_reason.status_code == 422

    updated = await client.patch(
        "/api/episodes/0",
        json={
            "dataset_path": str(raw_task),
            "grade": "bad",
            "tags": ["needs-review"],
            "reason": "mcap incomplete",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["grade"] == "bad"
    assert updated.json()["tags"] == ["needs-review"]
    assert updated.json()["reason"] == "mcap incomplete"

    db = await get_db()
    async with db.execute(
        "SELECT serial_number FROM annotations ORDER BY serial_number"
    ) as cursor:
        rows = await cursor.fetchall()
    assert rows[0]["serial_number"].startswith("raw:cell006/Mamonde_toner_sy/")

    good = await client.patch(
        "/api/episodes/0",
        json={
            "dataset_path": str(raw_task),
            "grade": "good",
            "tags": ["needs-review"],
            "reason": "ignored",
        },
    )
    assert good.status_code == 200, good.text
    assert good.json()["reason"] is None


@pytest.mark.anyio
async def test_raw_fields_and_tasks_are_read_only(client, raw_task):
    fields = await client.get("/api/datasets/info-fields", params={"dataset_path": str(raw_task)})
    assert fields.status_code == 200, fields.text
    assert fields.json()
    assert all(item["is_system"] is True for item in fields.json())

    columns = await client.get("/api/datasets/episode-columns", params={"dataset_path": str(raw_task)})
    assert columns.status_code == 200, columns.text
    assert all(item["is_system"] is True for item in columns.json())

    tasks = await client.get("/api/tasks", params={"dataset_path": str(raw_task)})
    assert tasks.status_code == 200, tasks.text
    assert tasks.json() == [{"task_index": 0, "task_instruction": "toner"}]

    field_write = await client.patch(
        "/api/datasets/info-fields",
        params={"dataset_path": str(raw_task)},
        json={"key": "operator", "value": "kim"},
    )
    assert field_write.status_code == 409
    assert field_write.json()["error"] == "raw_read_only"

    column_write = await client.post(
        "/api/datasets/episode-columns",
        json={"dataset_path": str(raw_task), "column_name": "operator", "dtype": "string", "default_value": ""},
    )
    assert column_write.status_code == 409
    assert column_write.json()["error"] == "raw_read_only"

    task_write = await client.patch(
        "/api/tasks/0",
        json={"dataset_path": str(raw_task), "task_instruction": "new task"},
    )
    assert task_write.status_code == 409
    assert task_write.json()["error"] == "raw_read_only"


@pytest.mark.anyio
async def test_raw_cell_listing_exposes_task_dataset_cards_with_annotation_counts(client, raw_task):
    cell = raw_task.parent

    cells = await client.get("/api/cells", params={"root": str(cell.parent)})
    assert cells.status_code == 200, cells.text
    assert cells.json()[0]["name"] == "cell006"
    assert cells.json()[0]["dataset_count"] == 1

    encoded = str(cell)
    datasets = await client.get(f"/api/cells/{encoded}/datasets")
    assert datasets.status_code == 200, datasets.text
    assert datasets.json()[0]["name"] == "Mamonde_toner_sy"
    assert datasets.json()[0]["path"] == str(raw_task.resolve())
    assert datasets.json()[0]["robot_type"] == "raw"
    assert datasets.json()[0]["graded_count"] == 0

    updated = await client.patch(
        "/api/episodes/0",
        json={
            "dataset_path": str(raw_task),
            "grade": "good",
            "tags": [],
        },
    )
    assert updated.status_code == 200, updated.text

    refreshed = await client.get(f"/api/cells/{encoded}/datasets")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()[0]["graded_count"] == 1
    assert refreshed.json()[0]["good_count"] == 1


def test_raw_cell_listing_skips_unreadable_nested_task(raw_task, monkeypatch):
    from backend.datasets.services.raw_dataset_adapter import raw_dataset_summaries_for_cell

    cell = raw_task.parent
    blocked = cell / "blocked_task"
    blocked.mkdir()
    original_iterdir = Path.iterdir

    def fake_iterdir(path: Path):
        if path == blocked:
            raise PermissionError(f"permission denied: {path}")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)

    summaries = raw_dataset_summaries_for_cell(cell)

    assert [item["name"] for item in summaries] == ["Mamonde_toner_sy"]


@pytest.mark.anyio
async def test_raw_adapter_rejects_traversal_and_non_recording_paths(client, raw_task):
    traversal = await client.post("/api/datasets/load", json={"path": str(raw_task / ".." / "..")})
    assert traversal.status_code in {400, 404}

    empty_task = raw_task.parent / "empty"
    empty_task.mkdir()
    empty = await client.post("/api/datasets/load", json={"path": str(empty_task)})
    assert empty.status_code in {400, 404}
