"""E2E tests for the curation tools UI using Playwright against a real running app.

Requires backend on :8000 and frontend on :5173.
"""

import json
import os
import urllib.parse

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
Page = playwright_sync.Page
expect = playwright_sync.expect

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session")
def browser_type_launch_args():
    return {"executable_path": "/snap/bin/chromium", "headless": True}


BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:5173")

MOCK_SOURCE = {
    "name": "mock-source",
    "path": "/mock",
    "cell_count": 1,
    "active": True,
}

MOCK_CELL = {
    "name": "mock-cell",
    "path": "/mock/cell",
    "mount_root": "/mock",
    "active": True,
    "dataset_count": 1,
}

MOCK_DATASET = {
    "name": "broken-dataset",
    "path": "/mock/cell/broken-dataset",
    "robot_type": "testbot",
    "total_episodes": 3,
    "total_duration_sec": 15,
    "good_count": 0,
    "normal_count": 0,
    "bad_count": 0,
    "good_duration_sec": 0,
    "normal_duration_sec": 0,
    "bad_duration_sec": 0,
}

MOCK_OK_DATASET = {
    "name": "working-dataset",
    "path": "/mock/cell/working-dataset",
    "robot_type": "testbot",
    "total_episodes": 0,
    "graded_count": 0,
    "good_count": 0,
    "normal_count": 0,
    "bad_count": 0,
    "fps": 30,
    "total_duration_sec": 0,
    "good_duration_sec": 0,
    "normal_duration_sec": 0,
    "bad_duration_sec": 0,
}

MOCK_DATASET_INFO = {
    "path": "/mock/cell/working-dataset",
    "name": "testbot",
    "fps": 30,
    "total_episodes": 0,
    "total_tasks": 0,
    "robot_type": "testbot",
    "features": {},
}

MOCK_RAW_SOURCE = {
    "name": "raw",
    "path": "/mock/raw",
    "cell_count": 1,
    "active": True,
}

MOCK_RAW_CELL = {
    "name": "cell006",
    "path": "/mock/raw/cell006",
    "mount_root": "/mock/raw",
    "active": True,
    "dataset_count": 1,
}

MOCK_RAW_DATASET = {
    "name": "Mamonde_toner_sy",
    "path": "/mock/raw/cell006/Mamonde_toner_sy",
    "robot_type": "raw",
    "total_episodes": 1,
    "graded_count": 0,
    "good_count": 0,
    "normal_count": 0,
    "bad_count": 0,
    "fps": 0,
    "total_duration_sec": 0,
    "good_duration_sec": 0,
    "normal_duration_sec": 0,
    "bad_duration_sec": 0,
}

MOCK_RAW_DATASET_INFO = {
    "path": "/mock/raw/cell006/Mamonde_toner_sy",
    "dataset_key": "raw-abc123",
    "name": "raw",
    "fps": 0,
    "total_episodes": 1,
    "total_tasks": 1,
    "robot_type": "raw",
    "features": {},
}

MOCK_RAW_EPISODE = {
    "episode_index": 0,
    "length": 0,
    "task_index": 0,
    "task_instruction": "toner",
    "chunk_index": 0,
    "file_index": 0,
    "dataset_from_index": 0,
    "dataset_to_index": 0,
    "grade": None,
    "tags": [],
    "reason": None,
    "created_at": "2026-02-26",
    "raw_recording": "cell006/Mamonde_toner_sy/20260226_170029",
}


def _fulfill_json(route, payload, status: int = 200):
    route.fulfill(
        status=status,
        content_type="application/json",
        body=json.dumps(payload),
    )


def _select_dataset(page: Page, name: str):
    """Click a dataset by its exact name text."""
    page.get_by_text(name, exact=True).click()


def _mock_dataset_load_failure(page: Page):
    def handler(route):
        url = route.request.url
        path = urllib.parse.urlparse(url).path
        if path.endswith("/api/converter/status"):
            _fulfill_json(route, {
                "container_state": "stopped",
                "docker_available": False,
                "tasks": [],
                "summary": "",
            })
            return
        if path.endswith("/api/cells/sources"):
            _fulfill_json(route, [MOCK_SOURCE])
            return
        if path.endswith("/api/cells"):
            _fulfill_json(route, [MOCK_CELL])
            return
        if "/api/cells/" in path and path.endswith("/datasets"):
            _fulfill_json(route, [MOCK_DATASET])
            return
        if path.endswith("/api/datasets/load"):
            _fulfill_json(route, {"detail": "Dataset mount unavailable"}, status=500)
            return
        if path.endswith("/api/episodes"):
            pytest.fail("Unexpected /api/episodes request after dataset load failure")
        route.continue_()

    page.route("**/api/**", handler)


def _mock_dataset_load_success(page: Page):
    def handler(route):
        url = route.request.url
        path = urllib.parse.urlparse(url).path
        if path.endswith("/api/converter/status"):
            _fulfill_json(route, {
                "container_state": "stopped",
                "docker_available": False,
                "tasks": [],
                "summary": "",
            })
            return
        if path.endswith("/api/cells/sources"):
            _fulfill_json(route, [MOCK_SOURCE])
            return
        if path.endswith("/api/cells"):
            _fulfill_json(route, [MOCK_CELL])
            return
        if "/api/cells/" in path and path.endswith("/datasets"):
            _fulfill_json(route, [MOCK_OK_DATASET])
            return
        if path.endswith("/api/datasets/load"):
            _fulfill_json(route, MOCK_DATASET_INFO)
            return
        if path.endswith("/api/episodes"):
            _fulfill_json(route, [])
            return
        if "/api/datasets/fields" in path:
            _fulfill_json(route, [])
            return
        route.continue_()

    page.route("**/api/**", handler)


def _mock_raw_dataset_page(page: Page):
    def handler(route):
        url = route.request.url
        path = urllib.parse.urlparse(url).path
        if path.endswith("/api/converter/status"):
            _fulfill_json(route, {
                "container_state": "stopped",
                "docker_available": False,
                "tasks": [],
                "summary": "",
            })
            return
        if path.endswith("/api/cells/sources"):
            _fulfill_json(route, [MOCK_RAW_SOURCE])
            return
        if path.endswith("/api/cells"):
            _fulfill_json(route, [MOCK_RAW_CELL])
            return
        if "/api/cells/" in path and path.endswith("/datasets"):
            _fulfill_json(route, [MOCK_RAW_DATASET])
            return
        if path.endswith("/api/datasets/load"):
            _fulfill_json(route, MOCK_RAW_DATASET_INFO)
            return
        if path.endswith("/api/episodes"):
            _fulfill_json(route, [MOCK_RAW_EPISODE])
            return
        if path.endswith("/api/datasets/fields"):
            _fulfill_json(route, [
                {"name": "grade", "dtype": "string", "is_system": True},
                {"name": "task_instruction", "dtype": "string", "is_system": True},
            ])
            return
        if path.endswith("/api/datasets/distribution"):
            _fulfill_json(route, {"field": "grade", "dtype": "string", "chart_type": "bar", "bins": [], "total": 0})
            return
        if path.endswith("/api/datasets/info-fields"):
            _fulfill_json(route, [{"key": "source_type", "value": "raw", "dtype": "str", "is_system": True}])
            return
        if path.endswith("/api/datasets/episode-columns"):
            _fulfill_json(route, [{"name": "recording", "dtype": "string", "is_system": True}])
            return
        route.continue_()

    page.route("**/api/**", handler)


def _mock_converter_validation(page: Page):
    calls: list[tuple[str, str]] = []

    def handler(route):
        url = route.request.url
        path = urllib.parse.urlparse(url).path
        method = route.request.method

        if path.endswith("/api/converter/status"):
            _fulfill_json(route, {
                "container_state": "stopped",
                "docker_available": True,
                "summary": "1 task | 5 recordings | 5 done | 0 pending | 0 failed",
                "tasks": [
                    {
                        "cell_task": "cell001/task_a",
                        "total": 5,
                        "done": 5,
                        "pending": 0,
                        "failed": 0,
                        "retry": 0,
                        "validation": {
                            "quick": {
                                "status": "passed",
                                "summary": "Quick passed: 5 episodes, 0 warnings",
                                "checked_at": "2026-04-18T11:00:00+09:00",
                            },
                            "full": {
                                "status": "partial",
                                "summary": "Full partial: dataset OK, official loader skipped",
                                "checked_at": "2026-04-18T11:03:00+09:00",
                            },
                        },
                    }
                ],
            })
            return

        if path.endswith("/api/converter/validate/quick") and method == "POST":
            calls.append(("POST", "quick"))
            _fulfill_json(route, {
                "status": "passed",
                "summary": "Quick passed: 5 episodes, 0 warnings",
                "checked_at": "2026-04-18T11:04:00+09:00",
            })
            return

        if path.endswith("/api/converter/validate/full") and method == "POST":
            calls.append(("POST", "full"))
            _fulfill_json(route, {
                "status": "partial",
                "summary": "Full partial: dataset OK, official loader skipped",
                "checked_at": "2026-04-18T11:05:00+09:00",
            })
            return

        if path.endswith("/api/cells/sources"):
            _fulfill_json(route, [])
            return
        if path.endswith("/api/cells"):
            _fulfill_json(route, [])
            return

        route.continue_()

    page.route("**/api/**", handler)
    return calls


# ---------------------------------------------------------------------------
# Page load
# ---------------------------------------------------------------------------

class TestPageLoad:
    def test_app_loads(self, page: Page):
        page.goto(BASE_URL)
        expect(page.get_by_text("Datasets", exact=True)).to_be_visible()

    def test_shows_dataset_list(self, page: Page):
        page.goto(BASE_URL)
        expect(page.get_by_text("basic_aic_cheetcode_dataset", exact=True)).to_be_visible()
        expect(page.get_by_text("hojun", exact=True)).to_be_visible()

    def test_no_episodes_shown_initially(self, page: Page):
        page.goto(BASE_URL)
        # Before selecting a dataset, no episode list should be visible
        expect(page.get_by_text("#0", exact=True)).not_to_be_visible()


# ---------------------------------------------------------------------------
# Dataset selection
# ---------------------------------------------------------------------------

class TestDatasetSelection:
    def test_click_dataset_loads_it(self, page: Page):
        page.goto(BASE_URL)
        _select_dataset(page, "basic_aic_cheetcode_dataset")
        # Should show dataset info after loading
        expect(page.get_by_text("FPS")).to_be_visible(timeout=5000)
        expect(page.get_by_text("20", exact=True)).to_be_visible()

    def test_episode_list_populated_after_load(self, page: Page):
        page.goto(BASE_URL)
        _select_dataset(page, "basic_aic_cheetcode_dataset")
        # Wait for episodes to appear
        expect(page.get_by_text("#0", exact=True)).to_be_visible(timeout=5000)

    def test_switch_datasets(self, page: Page):
        page.goto(BASE_URL)
        _select_dataset(page, "basic_aic_cheetcode_dataset")
        expect(page.get_by_text("#0", exact=True)).to_be_visible(timeout=5000)

        # Switch to hojun
        _select_dataset(page, "hojun")
        page.wait_for_timeout(1000)
        expect(page.get_by_text("#0", exact=True)).to_be_visible()

    def test_dataset_load_failure_is_visible(self, page: Page):
        _mock_dataset_load_failure(page)

        page.goto(BASE_URL)
        page.get_by_text("mock-source", exact=True).click()
        page.get_by_text("mock-cell", exact=True).click()
        _select_dataset(page, "broken-dataset")

        expect(page.get_by_text("Dataset mount unavailable", exact=True)).to_be_visible(timeout=5000)

    def test_dataset_navigation_renders_overview(self, page: Page):
        _mock_dataset_load_success(page)

        page.goto(BASE_URL)
        page.get_by_text("mock-source", exact=True).click()
        page.get_by_text("mock-cell", exact=True).click()
        _select_dataset(page, "working-dataset")

        expect(page.get_by_text("Overview", exact=True)).to_be_visible(timeout=5000)
        expect(page.get_by_text("Curate", exact=True)).to_be_visible(timeout=5000)
        expect(page.get_by_text("Fields", exact=True)).to_be_visible(timeout=5000)
        expect(page.get_by_text("Select fields from the left panel", exact=True)).to_be_visible(timeout=5000)

    def test_raw_source_uses_dataset_page_tabs(self, page: Page):
        _mock_raw_dataset_page(page)

        page.goto(BASE_URL)
        page.get_by_text("raw", exact=True).click()
        page.get_by_text("cell006", exact=True).click()
        _select_dataset(page, "Mamonde_toner_sy")

        expect(page.get_by_text("Overview", exact=True)).to_be_visible(timeout=5000)
        expect(page.get_by_text("Curate", exact=True)).to_be_visible(timeout=5000)
        expect(page.get_by_text("Fields", exact=True)).to_be_visible(timeout=5000)

        page.get_by_text("Curate", exact=True).click()
        page.get_by_text("ep_000", exact=True).click()
        expect(page.get_by_text("Raw recording", exact=True)).to_be_visible(timeout=5000)
        expect(page.get_by_text("Preview unavailable for raw recordings", exact=True)).to_be_visible(timeout=5000)
        expect(page.get_by_text("cell006/Mamonde_toner_sy/20260226_170029", exact=True)).to_be_visible(timeout=5000)

        page.get_by_text("Fields", exact=True).click()
        expect(page.get_by_text("Raw fields are read-only in v1.", exact=True)).to_be_visible(timeout=5000)


# ---------------------------------------------------------------------------
# Episode interaction
# ---------------------------------------------------------------------------

class TestEpisodeInteraction:
    def test_click_episode_shows_editor(self, page: Page):
        page.goto(BASE_URL)
        _select_dataset(page, "basic_aic_cheetcode_dataset")
        expect(page.get_by_text("#0", exact=True)).to_be_visible(timeout=5000)
        page.get_by_text("#0", exact=True).click()
        # Right panel should show episode editor
        expect(page.get_by_text("Grade", exact=True)).to_be_visible(timeout=3000)

    def test_episode_shows_frame_count(self, page: Page):
        page.goto(BASE_URL)
        _select_dataset(page, "basic_aic_cheetcode_dataset")
        expect(page.get_by_text("#0", exact=True)).to_be_visible(timeout=5000)
        # Episodes should display frame counts
        expect(page.get_by_text("frames").first).to_be_visible()


# ---------------------------------------------------------------------------
# Converter validation card
# ---------------------------------------------------------------------------

class TestConverterValidation:
    def test_converter_card_shows_validation_summary_and_buttons(self, page: Page):
        _mock_converter_validation(page)

        page.goto(BASE_URL)
        page.get_by_title("Converter: stopped").click()

        expect(page.get_by_text("Quick passed: 5 episodes, 0 warnings", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Quick Check")).to_be_visible()
        expect(page.get_by_role("button", name="Full Check")).to_be_visible()
        expect(page.get_by_text("passed", exact=True)).to_be_visible()
        expect(page.get_by_text("partial", exact=True)).to_be_visible()

    def test_converter_validation_buttons_post_to_api(self, page: Page):
        calls = _mock_converter_validation(page)

        page.goto(BASE_URL)
        page.get_by_title("Converter: stopped").click()
        page.get_by_role("button", name="Quick Check").click()
        page.get_by_role("button", name="Full Check").click()

        expect(page.get_by_text("Full partial: dataset OK, official loader skipped", exact=True)).to_be_visible(timeout=5000)
        assert calls == [("POST", "quick"), ("POST", "full")]
