# Dataset Path-Aware API Implementation Plan

> **Verified:** 2026-04-28T03:00Z · Codex(gpt-5.5/xhigh) ↔ Claude · 2 codex passes + 1 claude pass · PASS · fixes=17

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 백엔드의 “현재 로드된 데이터셋” 전역 singleton을 제거하고, 모든 데이터셋 의존 API가 명시적으로 dataset path를 받아 cell005/cell002 같은 다중 데이터셋이 한 서버에서 섞이지 않도록 한다.

**Architecture:** `DatasetService` 모듈 싱글톤(`dataset_service`)을 path 키 LRU cache로 관리되는 `DatasetRegistry` + 데이터셋 단위 `DatasetContext`로 교체한다. 모든 read/write 라우터는 `dataset_path` 쿼리/경로 파라미터를 받아 `dataset_registry.get(path)`로 컨텍스트를 꺼내 쓰고, 비디오 스트림 URL은 `/api/datasets/{dataset_key}/videos/...` 형태로 dataset key를 포함시켜 브라우저 캐시까지 분리한다. 프론트는 `datasetPath`를 `useEpisodes`/`VideoPlayer`/`ScalarChart`/`OverviewTab` 등 모든 하위 컴포넌트로 전달한다.

**Tech Stack:** Python >=3.12 (Docker app image: Python 3.13) / FastAPI / asyncpg + Postgres / pyarrow / pytest + pytest-asyncio + httpx / TypeScript / React / Vite / axios

---

## 배경 (Why this plan)

다음 흐름이 cell005 화면에 cell002 데이터를 노출시키는 근본 원인이다.

- `backend/datasets/services/dataset_service.py` — 파일 하단의 `dataset_service = DatasetService()`가 모듈 import 시 단 1개 인스턴스로 만들어져 서버 전체가 공유한다.
- `backend/datasets/routers/datasets.py` — `/api/datasets/load`가 이 singleton의 `_dataset_path`/`_episodes`/`_info`/`_episode_file_index` 등 "현재 로드 상태"를 통째로 갈아엎는다.
- `backend/datasets/routers/episodes.py` / `videos.py` / `scalars.py` — 모두 `episode_index`만 받고 `dataset_service` singleton을 그대로 읽는다.
- `frontend/src/hooks/useEpisodes.ts` / `frontend/src/components/VideoPlayer.tsx` / `frontend/src/components/ScalarChart.tsx` — 데이터셋 식별자 없이 `/episodes`, `/videos/{idx}/...`, `/scalars/{idx}` 호출.

같은 서버에서 여러 탭(혹은 여러 사용자)이 서로 다른 데이터셋을 로드하면, 마지막으로 `/api/datasets/load`를 호출한 데이터셋의 데이터가 모든 탭의 응답에 섞일 수 있다. 비디오는 추가로 URL 자체가 dataset 식별자를 안 가지므로 브라우저 캐시까지 cell 사이를 넘나든다.

해결 방향은 “요청마다 dataset path를 명시” + “서버는 path를 키로 multi-dataset 컨텍스트를 LRU 캐시”다.

---

## 파일 구조 (File Structure)

### 새로 만드는 파일

- `backend/datasets/services/dataset_registry.py` — `DatasetContext` dataclass와 `DatasetRegistry`(LRU). 기존 `DatasetService`의 인스턴스 상태를 `DatasetContext`에 옮기고, registry는 path-resolved key로 컨텍스트 LRU 캐시를 관리한다.
- `tests/test_dataset_registry.py` — registry LRU/eviction/lock 격리 단위 테스트.
- `tests/test_dataset_isolation_e2e.py` — 두 데이터셋(A=cell005, B=cell002 모사)을 띄워두고 episodes/videos/scalars/bulk-grade가 절대 섞이지 않는지 확인하는 회귀 테스트.

### 수정하는 파일

- `backend/datasets/services/dataset_service.py` — `DatasetService` 클래스와 모듈 레벨 싱글톤 `dataset_service`를 제거하고 parquet helper만 남긴다.
- `backend/datasets/services/episode_service.py` — 모든 `dataset_service.foo()` 호출을 `ctx: DatasetContext` 인자로 받아 `ctx.foo()`로 호출하도록 변환. `episode_service`도 모듈 싱글톤이지만 메서드 시그니처에 `ctx`가 추가된다.
- `backend/datasets/services/task_service.py` — 동일.
- `backend/datasets/services/distribution_service.py` — `dataset_service.distribution_cache` 의존을 `ctx.distribution_cache`로 변경.
- `backend/datasets/services/export_service.py` — `export_dataset(output_path, exclude_grades, dataset_path: str)`로 시그니처 변경.
- `backend/datasets/services/rerun_service.py` — `dataset_service` 직접 참조를 `ctx` 인자로 교체.
- `backend/datasets/routers/datasets.py` — `/api/datasets/load`는 검증 + 메타데이터 반환 전용으로 축소. `/api/datasets/info`도 `dataset_path` 쿼리 필수.
- `backend/datasets/routers/episodes.py` — list/get/patch 엔드포인트에 `dataset_path: str = Query(...)` 추가. bulk-grade는 body의 `dataset_path`를 필수로 받는다.
- `backend/datasets/routers/videos.py` — URL을 `/api/datasets/{dataset_key}/videos/{episode_index}/...`로 재배치. `dataset_key` ↔ `dataset_path` 매핑 helper 제공. 응답은 `Cache-Control: private, no-store`로 변경.
- `backend/datasets/routers/scalars.py` — `dataset_path` 쿼리 추가.
- `backend/datasets/routers/tasks.py` — `dataset_path` 쿼리 추가.
- `backend/datasets/routers/distribution.py` — 이미 `dataset_path`는 받지만, 캐시 키만 ctx로 옮기면 됨.
- `backend/datasets/routers/fields.py` — 변경 없음(이미 path-aware) — 검증만.
- `backend/datasets/routers/__init__.py` (1라인 빈 파일) — 변경 없음.
- `frontend/src/hooks/useEpisodes.ts` — `useEpisodes(datasetPath)`로 시그니처 변경, 모든 호출에 `?dataset_path=...` query parameter를 붙인다.
- `frontend/src/hooks/useTasks.ts` — 동일.
- `frontend/src/components/VideoPlayer.tsx` — `datasetKey` prop 추가, 카메라 fetch / 비디오 URL에 dataset key 사용.
- `frontend/src/components/ScalarChart.tsx` — `datasetPath` prop 추가.
- `frontend/src/components/OverviewTab.tsx` — bulk-grade / patch 호출에 dataset_path 포함.
- `frontend/src/components/DatasetPage.tsx` — 하위 컴포넌트로 `datasetPath` 전달 정리.
- `frontend/src/components/EpisodeEditor.tsx` — 이미 episode 객체만 다루므로 직접 변경은 거의 없으나 props chain 확인.
- `backend/services/dataset_service.py` — legacy shim이 `DatasetService`/`dataset_service`를 재수출하지 않도록 Task 14에서 helper-only shim으로 축소하거나 삭제.
- 기존 테스트 중 `DatasetService`/`dataset_service` singleton을 직접 patch하는 파일들(`tests/test_api_real.py`, `tests/test_dataset_list.py`, `tests/test_grade_reason.py`, `tests/test_task_service_real.py`, `tests/test_task_parquet_compat.py`, `tests/test_episode_service_real.py`, `tests/test_dataset_service_real.py`, `tests/test_security.py`, `tests/test_mockup.py`, `tests/test_split_dataset_scalar_indices.py`, `tests/test_rerun_service.py`, `tests/test_rerun_router.py`)은 각 관련 Task 또는 Task 14에서 registry/context 기반으로 갱신한다.

각 파일은 한 가지 책임을 갖도록 유지하며, registry/context 분리는 새 파일로 떼낸다. 기존 `dataset_service.py`는 Task 14에서 helper-only module로 축소한다.

---

## URL/HTTP 설계 결정

이번 리팩터에서 두 가지 path-aware 패턴이 가능하다:

1. **Query parameter 방식** — `GET /api/episodes?dataset_path=/abs/path/to/cell005`
2. **Path prefix 방식** — `GET /api/datasets/{dataset_key}/episodes` 단, `dataset_key`는 dataset path의 sha256 16자 prefix

비디오 캐시 격리(브라우저가 URL을 캐시 키로 쓰는 문제) 때문에 비디오만큼은 path prefix 방식이 안전하다. 다른 엔드포인트는 query parameter가 변경 폭이 작다. 본 계획은 **하이브리드**를 채택한다:

- 비디오: `/api/datasets/{dataset_key}/videos/{episode_index}/cameras` / `/stream/{camera}` (path prefix)
- 그 외 read API(episodes/scalars/tasks): `?dataset_path=...` (query)
- write API: `PATCH /episodes/{idx}?dataset_path=...`는 query parameter를 사용하고, `POST /episodes/bulk-grade`는 JSON body의 `dataset_path`를 사용한다.

`dataset_key`는 `_dataset_key_from_path(path: str) -> str = sha256(resolved).hexdigest()[:16]`로 결정. 서버에 `dataset_key -> dataset_path` 단방향 매핑(registry에 자동 등록)을 두고, URL의 key를 받아 path를 복원.

`/api/datasets/load`는 검증/메타데이터/등록 엔드포인트로 남겨, 프론트가 이 응답에서 `dataset_key`를 받아 비디오 URL 조립에 쓴다.

---

## 검증된 프로젝트 전제 (Do not guess)

- FastAPI 앱 factory는 없다. API 테스트는 `from backend.main import app` + `httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")` 패턴을 사용한다. `backend.app.create_app` 또는 `fastapi.testclient.TestClient`를 새로 도입하지 않는다.
- DB 계층은 `backend/core/db.py`의 asyncpg/Postgres compatibility wrapper다. 기존 `_db_path_override` fixture 이름은 legacy SQLite-era marker일 뿐 실제 DB는 Postgres test DB(`CURATION_TEST_DB_URL` 또는 기본 `127.0.0.1:5433/curation_test`)를 쓴다.
- Frontend `package.json`에는 `dev`, `build`, `preview`만 있다. TypeScript 검증은 `cd frontend && npm run build`로 수행한다. `npm run typecheck`와 `npm test`는 존재하지 않는다.
- Docker compose 파일은 `docker/compose.yml` 하나다. `docker/docker-compose.dev.yml`과 `tests` compose service는 없다. 자동 Docker 검증은 `docker compose --env-file docker/.env.example -f docker/compose.yml config >/dev/null`로 한다.
- 현재 worktree가 dirty일 수 있다. 각 Task 시작 전 `git diff -- <task files>`로 기존 변경을 확인하고, 같은 파일에 사용자/WIP 변경이 있으면 되돌리지 말고 계획의 목표에 맞게 이어서 수정한다.

## Rollback / Undo Guidance

- 각 Task는 명시된 커밋 하나가 rollback 단위다. 실패 시 다음 Task로 넘어가지 말고 `git revert <commit>`으로 해당 Task만 되돌린다.
- Task 2/6/12는 grade/tags parquet와 annotation DB를 쓴다. 자동 테스트는 `tmp_path` 데이터셋만 사용하고, 실데이터 수동 검증에서는 변경 전 grade/reason을 기록한 뒤 같은 UI/API로 원복한다.
- Task 3은 `meta/tasks.parquet`를 rewrite한다. 실패 시 해당 Task 커밋을 revert하고, 테스트 중 생성된 temp dataset만 삭제한다. 운영 데이터셋에서 직접 실행하지 않는다.
- Docker smoke cleanup은 `docker compose --env-file docker/.env.example -f docker/compose.yml down`만 사용한다. 운영/개발 DB volume을 지울 수 있는 `down -v`는 이 계획에서 금지한다.

---

## Self-Contained Task List

각 Task는 2~5분짜리 step의 묶음이다. TDD 순서(failing test → minimal impl → green → commit)를 지킨다. 한 checkbox 안에 numbered edit가 여러 개 있으면 각 번호를 별도 micro-step으로 수행하고, 해당 Task의 다음 verification command를 통과하기 전에는 다음 Task로 넘어가지 않는다.

---

### Task 1: `DatasetContext` + `DatasetRegistry` 신규 모듈

**Files:**
- Create (or replace existing WIP): `backend/datasets/services/dataset_registry.py`
- Create: `tests/test_dataset_registry.py`

**Why this task:** 모든 path-aware 작업의 기반. 기존 `DatasetService` 인스턴스 상태를 path 단위로 분리한 `DatasetContext`로 옮기고, path → context LRU 캐시를 제공한다.

- [ ] **Step 1: 실패하는 테스트 작성 (registry basic load + cache hit)**

`tests/test_dataset_registry.py` 생성:

```python
"""Tests for DatasetRegistry path-keyed LRU cache."""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


def _make_min_dataset(root: Path, name: str) -> Path:
    ds = root / name
    (ds / "meta" / "episodes" / "chunk-000").mkdir(parents=True, exist_ok=True)
    (ds / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
    info = {
        "fps": 30, "total_episodes": 1, "total_tasks": 0,
        "robot_type": "test_robot", "features": {},
    }
    (ds / "meta" / "info.json").write_text(json.dumps(info))
    pq.write_table(
        pa.table({"task_index": pa.array([], type=pa.int64()),
                  "task": pa.array([], type=pa.string())}),
        ds / "meta" / "tasks.parquet",
    )
    pq.write_table(
        pa.table({
            "episode_index": pa.array([0], type=pa.int64()),
            "task_index": pa.array([0], type=pa.int64()),
            "data/chunk_index": pa.array([0], type=pa.int64()),
            "data/file_index": pa.array([0], type=pa.int64()),
            "dataset_from_index": pa.array([0], type=pa.int64()),
            "dataset_to_index": pa.array([10], type=pa.int64()),
        }),
        ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
    )
    return ds


@pytest.fixture
def two_datasets(tmp_path, monkeypatch):
    a = _make_min_dataset(tmp_path, "cell005_ds")
    b = _make_min_dataset(tmp_path, "cell002_ds")
    from backend.core import config as _cfg
    monkeypatch.setattr(
        _cfg.settings, "allowed_dataset_roots",
        [str(tmp_path)],
    )
    return a, b


def test_registry_get_returns_path_specific_context(two_datasets):
    from backend.datasets.services.dataset_registry import DatasetRegistry
    a, b = two_datasets
    reg = DatasetRegistry(max_size=4)
    ctx_a = reg.get(a)
    ctx_b = reg.get(b)
    assert ctx_a is not ctx_b
    assert ctx_a.dataset_path == a.resolve()
    assert ctx_b.dataset_path == b.resolve()


def test_registry_caches_same_path(two_datasets):
    from backend.datasets.services.dataset_registry import DatasetRegistry
    a, _ = two_datasets
    reg = DatasetRegistry(max_size=4)
    assert reg.get(a) is reg.get(str(a))


def test_registry_evicts_lru_when_full(two_datasets, tmp_path):
    from backend.datasets.services.dataset_registry import DatasetRegistry
    a, b = two_datasets
    c = _make_min_dataset(tmp_path, "cell007_ds")
    reg = DatasetRegistry(max_size=2)
    ctx_a = reg.get(a)
    reg.get(b)
    reg.get(c)  # should evict a
    ctx_a2 = reg.get(a)
    assert ctx_a2 is not ctx_a


def test_registry_rejects_path_outside_allowed_roots(tmp_path, monkeypatch):
    from backend.datasets.services.dataset_registry import DatasetRegistry
    from backend.core import config as _cfg
    monkeypatch.setattr(_cfg.settings, "allowed_dataset_roots", [str(tmp_path / "allowed")])
    reg = DatasetRegistry(max_size=2)
    with pytest.raises(ValueError):
        reg.get(tmp_path)


def test_registry_dataset_key_is_stable_for_same_path(two_datasets):
    from backend.datasets.services.dataset_registry import DatasetRegistry, dataset_key_for
    a, _ = two_datasets
    k1 = dataset_key_for(a)
    k2 = dataset_key_for(str(a))
    assert k1 == k2
    assert len(k1) == 16


def test_registry_dataset_key_survives_context_eviction(two_datasets, tmp_path):
    from backend.datasets.services.dataset_registry import DatasetRegistry, dataset_key_for
    a, b = two_datasets
    c = _make_min_dataset(tmp_path, "cell007_ds")
    reg = DatasetRegistry(max_size=1)
    key_a = dataset_key_for(a)
    ctx_a = reg.get(a)
    reg.get(b)
    reg.get(c)
    reloaded = reg.get_by_key(key_a)
    assert reloaded.dataset_path == ctx_a.dataset_path
    assert reloaded is not ctx_a
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `pytest tests/test_dataset_registry.py -v`
Expected: FAIL — if the file is absent, `ModuleNotFoundError`; if a WIP `dataset_registry.py` already exists, the eviction/key assertions fail. Do not proceed on a green result unless all six tests already exist and pass unchanged.

- [ ] **Step 3: 최소 구현 작성**

`backend/datasets/services/dataset_registry.py`:

```python
"""Path-keyed LRU registry for dataset contexts.

Replaces the previous module-level singleton DatasetService. Each loaded
dataset path resolves to its own DatasetContext, so concurrent requests for
different datasets do not contaminate each other's data.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from backend.core.config import settings
from backend.datasets.services.task_parquet import normalize_task_records


def dataset_key_for(path: str | Path) -> str:
    """Stable short key derived from the resolved dataset path."""
    resolved = str(Path(path).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


@dataclass
class DatasetContext:
    """All state previously held by the DatasetService singleton, scoped to one path."""
    dataset_path: Path
    info: dict
    episodes: list[dict]
    tasks: list[dict]
    episode_file_index: dict[int, dict]
    episode_parquet_files: list[Path]
    episode_to_file_map: dict[int, Path]
    file_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    episodes_cache: dict[int, dict] | None = None
    distribution_cache: dict[str, dict] = field(default_factory=dict)

    def get_info(self) -> dict:
        return self.info

    def get_episodes(self) -> list[dict]:
        return self.episodes

    def get_tasks(self) -> list[dict]:
        return self.tasks

    def get_features(self) -> dict:
        return self.features

    def get_dataset_path(self) -> str:
        return str(self.dataset_path)

    @property
    def features(self) -> dict:
        return self.info.get("features", {})

    @property
    def fps(self) -> int:
        return int(self.info.get("fps", 0) or 0)

    def get_file_lock(self, file_path: str | Path) -> asyncio.Lock:
        key = str(file_path)
        if key not in self.file_locks:
            self.file_locks[key] = asyncio.Lock()
        return self.file_locks[key]

    def get_episode_file_location(self, episode_index: int) -> dict:
        if episode_index not in self.episode_file_index:
            raise KeyError(f"Episode index {episode_index!r} not found.")
        return self.episode_file_index[episode_index]

    def get_file_for_episode(self, episode_index: int) -> Path | None:
        return self.episode_to_file_map.get(episode_index)

    def iter_episode_parquet_files(self) -> list[Path]:
        return list(self.episode_parquet_files)

    @property
    def file_lock(self) -> asyncio.Lock:
        return self.get_file_lock("__tasks_parquet__")

    def reload_tasks(self) -> None:
        tasks_path = self.dataset_path / "meta" / "tasks.parquet"
        if not tasks_path.exists():
            self.tasks = []
            return
        from backend.datasets.services.dataset_service import _table_to_list_of_dicts
        table = pq.read_table(str(tasks_path))
        self.tasks = normalize_task_records(_table_to_list_of_dicts(table), table)

    async def get_tasks_map(self) -> dict[int, str]:
        return {
            int(t["task_index"]): str(t.get("task", ""))
            for t in self.tasks
        }


class DatasetRegistry:
    """Thread-safe LRU registry mapping resolved dataset paths to DatasetContext."""

    def __init__(self, max_size: int = 8) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._max_size = max_size
        self._items: OrderedDict[Path, DatasetContext] = OrderedDict()
        self._lock = threading.Lock()
        self._key_to_path: dict[str, Path] = {}

    def get(self, path: str | Path) -> DatasetContext:
        resolved = self._resolve_and_validate(path)
        with self._lock:
            ctx = self._items.get(resolved)
            if ctx is not None:
                self._items.move_to_end(resolved)
                return ctx

            # Load under the registry lock so two first requests for the same
            # path cannot create separate contexts with separate file locks.
            ctx = self._load(resolved)
            self._items[resolved] = ctx
            self._items.move_to_end(resolved)
            key = dataset_key_for(resolved)
            existing = self._key_to_path.get(key)
            if existing is not None and existing != resolved:
                raise RuntimeError(f"dataset_key collision for {resolved} and {existing}")
            self._key_to_path[key] = resolved
            while len(self._items) > self._max_size:
                # Keep key->path entries so stable dataset-key URLs can reload
                # an evicted context by calling get_by_key().
                self._items.popitem(last=False)
            return ctx

    def get_by_key(self, key: str) -> DatasetContext:
        with self._lock:
            path = self._key_to_path.get(key)
        if path is None:
            raise KeyError(f"Unknown dataset_key: {key}")
        return self.get(path)

    def invalidate(self, path: str | Path) -> None:
        resolved = Path(path).resolve()
        with self._lock:
            self._items.pop(resolved, None)
            self._key_to_path = {
                k: v for k, v in self._key_to_path.items() if v != resolved
            }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _resolve_and_validate(self, path: str | Path) -> Path:
        resolved = Path(path).resolve()
        allowed = [Path(r).resolve() for r in settings.allowed_dataset_roots]
        if not any(resolved.is_relative_to(root) for root in allowed):
            raise ValueError(
                f"Dataset path is not under any allowed root: {resolved}"
            )
        if not resolved.exists():
            raise FileNotFoundError(f"Dataset path does not exist: {resolved}")
        if not resolved.is_dir():
            raise ValueError(f"Dataset path is not a directory: {resolved}")
        return resolved

    def _load(self, root: Path) -> DatasetContext:
        info = self._load_info(root)
        episodes, parquet_files, ep_to_file = self._load_episodes(root)
        tasks = self._load_tasks(root)
        episode_file_index = self._build_episode_file_index(episodes, info)
        return DatasetContext(
            dataset_path=root,
            info=info,
            episodes=episodes,
            tasks=tasks,
            episode_file_index=episode_file_index,
            episode_parquet_files=parquet_files,
            episode_to_file_map=ep_to_file,
        )

    def _load_info(self, root: Path) -> dict:
        info_path = root / "meta" / "info.json"
        with info_path.open("r", encoding="utf-8") as fh:
            content = fh.read().rstrip("\x00")
            return json.loads(content)

    def _load_episodes(
        self, root: Path,
    ) -> tuple[list[dict], list[Path], dict[int, Path]]:
        pattern = str(root / "meta" / "episodes" / "chunk-*" / "file-*.parquet")
        parquet_files = sorted(glob(pattern))
        if not parquet_files:
            return [], [], {}

        from backend.datasets.services.dataset_service import (
            _normalize_compatible_string_widths, _table_to_list_of_dicts,
        )

        tables: list[pa.Table] = []
        ep_to_file: dict[int, Path] = {}
        for f in parquet_files:
            table = pq.read_table(f)
            tables.append(table)
            for idx in table.column("episode_index").to_pylist():
                ep_to_file[int(idx)] = Path(f)

        combined = pa.concat_tables(
            _normalize_compatible_string_widths(tables),
            promote_options="default",
        )
        return _table_to_list_of_dicts(combined), [Path(f) for f in parquet_files], ep_to_file

    def _load_tasks(self, root: Path) -> list[dict]:
        from backend.datasets.services.dataset_service import _table_to_list_of_dicts
        tasks_path = root / "meta" / "tasks.parquet"
        if not tasks_path.exists():
            return []
        table = pq.read_table(str(tasks_path))
        return normalize_task_records(_table_to_list_of_dicts(table), table)

    def _build_episode_file_index(
        self, episodes: list[dict], info: dict,
    ) -> dict[int, dict]:
        features: dict = info.get("features", {})
        camera_keys: list[str] = [
            k for k in features
            if k.startswith("observation.images.") or k.startswith("observation.image.")
        ]
        index: dict[int, dict] = {}
        for ep in episodes:
            ep_idx = int(ep["episode_index"])
            entry: dict = {
                "data_chunk_index": ep.get("data/chunk_index", 0),
                "data_file_index": ep.get("data/file_index", 0),
                "dataset_from_index": ep.get("dataset_from_index", 0),
                "dataset_to_index": ep.get("dataset_to_index", 0),
                "videos": {},
            }
            for cam_key in camera_keys:
                chunk_val = ep.get(f"videos/{cam_key}/chunk_index")
                file_val = ep.get(f"videos/{cam_key}/file_index")
                if chunk_val is not None or file_val is not None:
                    entry["videos"][cam_key] = {
                        "chunk_index": chunk_val,
                        "file_index": file_val,
                        "from_timestamp": ep.get(f"videos/{cam_key}/from_timestamp"),
                        "to_timestamp": ep.get(f"videos/{cam_key}/to_timestamp"),
                    }
            index[ep_idx] = entry
        return index


dataset_registry = DatasetRegistry(max_size=8)
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_dataset_registry.py -v`
Expected: 6 passed.

- [ ] **Step 5: 커밋**

```bash
git add backend/datasets/services/dataset_registry.py tests/test_dataset_registry.py
git commit -m "feat(datasets): add path-keyed DatasetRegistry and DatasetContext

Introduces a thread-safe LRU registry that loads each dataset path into its
own DatasetContext. Replaces the global DatasetService singleton's role as
'currently loaded dataset', preparing every dataset-dependent service and
router to become path-aware."
```

---

### Task 2: `episode_service`를 `DatasetContext` 인자 기반으로 변환

**Files:**
- Modify: `backend/datasets/services/episode_service.py`
- Modify: `tests/test_episode_annotations_db.py`

**Why this task:** `episode_service`의 모든 `dataset_service.foo()` 호출을 `ctx.foo()`로 바꿔, episode 관련 모든 read/write가 명시적 컨텍스트에서 동작하게 한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_episode_annotations_db.py`의 `_make_services`를 ctx 기반으로 바꾸고 새 회귀 케이스를 추가한다. 기존 `services` fixture는 `(ctx, es)`를 반환하도록 바꾸고, 이 파일 안의 기존 호출부는 모두 `await es.update_episode(ctx, ...)`, `await es.get_episodes(ctx)`, `await es.bulk_grade(ctx, ...)` 형태로 같이 수정한다.

> **중요 — fixture cleanup:** 기존 `services` fixture의 마지막 줄
> `monkeypatch.setattr("backend.datasets.services.episode_service.dataset_service", ds)`는
> Task 2의 episode_service refactor 이후 `dataset_service` attribute 자체가 사라져 **AttributeError**를 일으킨다. 이 fixture에서 해당 monkeypatch 라인을 **삭제**하고, ctx 인자 직접 주입 방식으로만 동작시킨다.

```python
def _make_ctx(dataset_path: Path):
    """Build a DatasetContext directly via the registry."""
    from backend.core.config import settings
    from backend.datasets.services.dataset_registry import DatasetRegistry

    if str(dataset_path.parent) not in settings.allowed_dataset_roots:
        settings.allowed_dataset_roots = list(settings.allowed_dataset_roots) + [str(dataset_path.parent)]
    reg = DatasetRegistry(max_size=4)
    return reg.get(dataset_path)


def _make_services(dataset_path: Path):
    """Create a DatasetContext + EpisodeService pointing at dataset_path."""
    from backend.datasets.services.episode_service import EpisodeService
    return _make_ctx(dataset_path), EpisodeService()
```

기존 `services` fixture는 다음과 같이 갱신한다 (monkeypatch 제거):

```python
@pytest.fixture
def services(mock_dataset):
    return _make_services(mock_dataset)
```

새 테스트(같은 파일 끝부분에 추가):

```python
class TestEpisodeServiceTakesContext:
    @pytest.mark.asyncio
    async def test_get_episode_uses_passed_ctx(self, tmp_db, mock_dataset):
        await init_db()
        from backend.datasets.services.episode_service import EpisodeService
        ctx = _make_ctx(mock_dataset)
        es = EpisodeService()
        ep = await es.get_episode(ctx, episode_index=0)
        assert ep["episode_index"] == 0

    @pytest.mark.asyncio
    async def test_two_contexts_do_not_share_episodes(self, tmp_db, tmp_path):
        await init_db()
        a = _create_mock_dataset(tmp_path / "a")
        b = _create_mock_dataset(tmp_path / "b")
        from backend.datasets.services.episode_service import EpisodeService
        ctx_a = _make_ctx(a)
        ctx_b = _make_ctx(b)
        es = EpisodeService()
        await es.update_episode(ctx_a, episode_index=0, grade="good", tags=[])
        ep_b = await es.get_episode(ctx_b, episode_index=0)
        assert ep_b.get("grade") in (None, "")
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `pytest tests/test_episode_annotations_db.py::TestEpisodeServiceTakesContext -v`
Expected: FAIL — `EpisodeService.get_episode()` does not accept `ctx`.

- [ ] **Step 3: `episode_service.py` 시그니처 변환**

`backend/datasets/services/episode_service.py`에서 다음을 변경:

1. 상단 `from backend.datasets.services.dataset_service import dataset_service` 줄을 `from backend.datasets.services.dataset_registry import DatasetContext`로 교체.
2. 모듈 레벨 helper들에 `ctx: DatasetContext` 인자 추가:

```python
async def _write_annotations_to_parquet(
    ctx: DatasetContext,
    updates: dict[int, tuple[str | None, list[str]]],
) -> None:
    file_groups: dict[Path, dict[int, tuple[str | None, list[str]]]] = {}
    for ep_idx, (grade, tags) in updates.items():
        fp = ctx.get_file_for_episode(ep_idx)
        if fp is None:
            continue
        file_groups.setdefault(fp, {})[ep_idx] = (grade, tags)

    for file_path, group in file_groups.items():
        lock = ctx.get_file_lock(str(file_path))
        async with lock:
            table = await asyncio.to_thread(pq.read_table, file_path)
            indices = table.column("episode_index").to_pylist()
            old_grades = (
                table.column("grade").to_pylist()
                if "grade" in table.schema.names
                else [None] * table.num_rows
            )
            old_tags = (
                table.column("tags").to_pylist()
                if "tags" in table.schema.names
                else [None] * table.num_rows
            )
            new_grades = list(old_grades)
            new_tags = list(old_tags)
            for i, ep_idx in enumerate(indices):
                if ep_idx in group:
                    g, t = group[ep_idx]
                    new_grades[i] = g
                    new_tags[i] = t
            drop_cols = [c for c in ("grade", "tags") if c in table.schema.names]
            if drop_cols:
                table = table.drop(drop_cols)
            table = table.append_column("grade", pa.array(new_grades, type=pa.string()))
            table = table.append_column(
                "tags", pa.array(new_tags, type=pa.list_(pa.string())),
            )
            await asyncio.to_thread(pq.write_table, table, file_path)
```

3. `_read_episode_serial_from_parquet`도 ctx를 받도록:

```python
def _read_episode_serial_from_parquet(ctx: DatasetContext, episode_index: int) -> str | None:
    file_path = ctx.get_file_for_episode(episode_index)
    if file_path is None:
        return None
    schema = pq.read_schema(file_path)
    if "Serial_number" not in schema.names:
        return None
    table = pq.read_table(file_path, columns=["episode_index", "Serial_number"])
    indices = table.column("episode_index").to_pylist()
    serials = table.column("Serial_number").to_pylist()
    for idx, serial in zip(indices, serials):
        if int(idx) == episode_index and serial not in (None, ""):
            return str(serial)
    return None


async def _get_episode_serial(ctx: DatasetContext, dataset_id: int, episode_index: int) -> str | None:
    db = await get_db()
    serial = await _get_serial(db, dataset_id, episode_index)
    if serial is not None:
        return serial
    return await asyncio.to_thread(_read_episode_serial_from_parquet, ctx, episode_index)


async def _save_annotation_to_db(
    ctx: DatasetContext,
    dataset_id: int,
    episode_index: int,
    grade: str | None,
    tags: list[str],
    reason: str | None,
) -> None:
    db = await get_db()
    serial = await _get_episode_serial(ctx, dataset_id, episode_index)
    if serial is None:
        raise ValueError(
            f"no serial_number for dataset_id={dataset_id} episode={episode_index}; "
            "cannot persist annotation"
        )
    await db.execute(
        """INSERT INTO annotations (serial_number, grade, tags, reason, updated_at)
           VALUES (?, ?, ?, ?, NOW())
           ON CONFLICT (serial_number) DO UPDATE SET
             grade=excluded.grade, tags=excluded.tags, reason=excluded.reason,
             updated_at=excluded.updated_at""",
        (serial, grade, _json.dumps(tags), reason),
    )
    await db.commit()
```

4. `EpisodeService` 메서드 시그니처를 ctx를 받도록 변경:

```python
class EpisodeService:
    async def get_episodes(self, ctx: DatasetContext) -> list[dict[str, Any]]:
        if ctx.episodes_cache is not None:
            return list(ctx.episodes_cache.values())
        episodes: dict[int, dict[str, Any]] = {}
        tasks_map = await ctx.get_tasks_map()
        dataset_id = await _ensure_dataset_registered(ctx.dataset_path)
        await _ensure_migrated(dataset_id, ctx.dataset_path)
        annotations = await _load_annotations_from_db(dataset_id)
        serial_annotations: dict[str, dict] | None = None

        for file_path in ctx.episode_parquet_files:
            table = await asyncio.to_thread(pq.read_table, file_path)
            for row in _iter_rows(table):
                ep = _row_to_episode(row, tasks_map)
                ann = annotations.get(ep["episode_index"])
                if ann is None and row.get("Serial_number") not in (None, ""):
                    if serial_annotations is None:
                        serials = [
                            str(r.get("Serial_number"))
                            for fp in ctx.episode_parquet_files
                            for r in _iter_rows(await asyncio.to_thread(pq.read_table, fp))
                            if r.get("Serial_number") not in (None, "")
                        ]
                        serial_annotations = await _load_annotations_by_serials(serials)
                    ann = serial_annotations.get(str(row.get("Serial_number")))
                if ann:
                    ep["grade"] = ann.get("grade")
                    ep["tags"] = ann.get("tags", [])
                    ep["reason"] = ann.get("reason")
                episodes[ep["episode_index"]] = ep
        ctx.episodes_cache = episodes
        return list(episodes.values())

    async def get_episode(self, ctx: DatasetContext, episode_index: int) -> dict[str, Any]:
        if ctx.episodes_cache is not None:
            try:
                return ctx.episodes_cache[episode_index]
            except KeyError:
                raise EpisodeNotFoundError(f"Episode {episode_index} not found in cache.")
        tasks_map = await ctx.get_tasks_map()
        file_path = ctx.get_file_for_episode(episode_index)
        if file_path is None:
            raise EpisodeNotFoundError(
                f"Episode {episode_index} not found in any parquet file."
            )
        dataset_id = await _ensure_dataset_registered(ctx.dataset_path)
        await _ensure_migrated(dataset_id, ctx.dataset_path)
        annotations = await _load_annotations_from_db(dataset_id)
        table = await asyncio.to_thread(pq.read_table, file_path)
        for row in _iter_rows(table):
            if row.get("episode_index") == episode_index:
                ep = _row_to_episode(row, tasks_map)
                ann = annotations.get(episode_index)
                if ann is None and row.get("Serial_number") not in (None, ""):
                    serial_annotations = await _load_annotations_by_serials(
                        [str(row.get("Serial_number"))],
                    )
                    ann = serial_annotations.get(str(row.get("Serial_number")))
                if ann:
                    ep["grade"] = ann.get("grade")
                    ep["tags"] = ann.get("tags", [])
                    ep["reason"] = ann.get("reason")
                return ep
        raise EpisodeNotFoundError(
            f"Episode {episode_index} not found in {file_path}."
        )

    async def update_episode(
        self,
        ctx: DatasetContext,
        episode_index: int,
        grade: str | None,
        tags: list[str],
        reason: str | None = None,
    ) -> dict[str, Any]:
        if ctx.episodes_cache is not None:
            if episode_index not in ctx.episodes_cache:
                raise EpisodeNotFoundError(f"Episode {episode_index} not found.")
        else:
            file_path = ctx.get_file_for_episode(episode_index)
            if file_path is None:
                raise EpisodeNotFoundError(f"Episode {episode_index} not found.")

        effective_reason = reason if grade in ("bad", "normal") else None
        dataset_id = await _ensure_dataset_registered(ctx.dataset_path)
        await _ensure_migrated(dataset_id, ctx.dataset_path)
        await _save_annotation_to_db(ctx, dataset_id, episode_index, grade, tags, effective_reason)
        await _refresh_dataset_stats(dataset_id)
        await _write_annotations_to_parquet(ctx, {episode_index: (grade, tags)})

        ctx.distribution_cache.clear()
        if ctx.episodes_cache is not None:
            ep = ctx.episodes_cache.get(episode_index)
            if ep:
                ep["grade"] = grade
                ep["tags"] = tags
                ep["reason"] = effective_reason
                return ep
        return await self.get_episode(ctx, episode_index)

    async def bulk_grade(
        self,
        ctx: DatasetContext,
        episode_indices: list[int],
        grade: str,
        reason: str | None = None,
    ) -> int:
        dataset_id = await _ensure_dataset_registered(ctx.dataset_path)
        await _ensure_migrated(dataset_id, ctx.dataset_path)
        effective_reason = reason if grade in ("bad", "normal") else None
        existing = await _load_annotations_from_db(dataset_id)
        parquet_updates: dict[int, tuple[str | None, list[str]]] = {}
        for idx in episode_indices:
            tags = existing.get(idx, {}).get("tags", [])
            await _save_annotation_to_db(ctx, dataset_id, idx, grade, tags, effective_reason)
            parquet_updates[idx] = (grade, tags)
        await _refresh_dataset_stats(dataset_id)
        await _write_annotations_to_parquet(ctx, parquet_updates)

        ctx.distribution_cache.clear()
        if ctx.episodes_cache is not None:
            for idx in episode_indices:
                ep = ctx.episodes_cache.get(idx)
                if ep:
                    ep["grade"] = grade
                    ep["reason"] = effective_reason
        return len(episode_indices)
```

5. 기존 모듈 레벨 import에 `from backend.datasets.services.dataset_registry import DatasetContext`도 추가.

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_episode_annotations_db.py -v`
Expected: 기존 케이스가 모두 통과 + 새 `TestEpisodeServiceTakesContext` 2건 통과. 기존 `services` fixture와 이 파일의 모든 `EpisodeService` 호출부는 이 Task 안에서 ctx 첫 인자 형태로 바뀌어 있어야 한다.

> NOTE: 기존 테스트(`TestUpdateEpisode.test_writes_grade_and_tags_to_db` 등)는 `ds, es = services`가 아니라 `ctx, es = services`를 사용하고, DB 검증용 path는 `str(ctx.dataset_path.resolve())`로 읽는다.

- [ ] **Step 5: 커밋**

```bash
git add backend/datasets/services/episode_service.py tests/test_episode_annotations_db.py
git commit -m "refactor(episodes): take DatasetContext as explicit argument

Removes implicit dependency on the global dataset_service singleton from
EpisodeService and its parquet-write helpers. Each method now accepts a
DatasetContext, so two simultaneously-loaded datasets can never alias their
file maps, locks, or episode caches."
```

---

### Task 3: `task_service`를 `DatasetContext` 인자 기반으로 변환

**Files:**
- Modify: `backend/datasets/services/task_service.py`
- Create: `tests/test_task_service_ctx.py`
- Modify: `tests/test_task_service_real.py`
- Modify: `tests/test_task_parquet_compat.py`
- Modify: `tests/test_mockup.py`

**Why this task:** task 갱신도 dataset path를 모르고 동작하면 안 된다. 같은 패턴으로 ctx 인자 추가.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_task_service_ctx.py`(새 파일):

```python
"""Verify task_service is path-aware via DatasetContext."""
import pytest

from tests.test_episode_annotations_db import (
    _create_mock_dataset, _make_ctx,
)


@pytest.mark.asyncio
async def test_get_tasks_uses_ctx(tmp_path):
    ds = _create_mock_dataset(tmp_path / "ds")
    ctx = _make_ctx(ds)
    from backend.datasets.services import task_service
    tasks = task_service.get_tasks(ctx)
    assert tasks == [{"task_index": 0, "task_instruction": "test task"}]
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `pytest tests/test_task_service_ctx.py -v`
Expected: FAIL — `task_service.get_tasks()` takes no positional argument.

- [ ] **Step 3: `task_service.py` 시그니처 변환**

```python
"""Service for reading and writing task instructions in meta/tasks.parquet."""
from __future__ import annotations

import os
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from backend.datasets.services.dataset_registry import DatasetContext, dataset_registry
from backend.datasets.services.task_parquet import get_task_text_column_name


def get_tasks(ctx: DatasetContext) -> list[dict]:
    return [
        {"task_index": int(t["task_index"]), "task_instruction": str(t.get("task", ""))}
        for t in ctx.tasks
    ]


def get_task(ctx: DatasetContext, task_index: int) -> dict:
    for t in ctx.tasks:
        if int(t["task_index"]) == task_index:
            return {
                "task_index": int(t["task_index"]),
                "task_instruction": str(t.get("task", "")),
            }
    raise KeyError(f"task_index {task_index!r} not found")


async def update_task(ctx: DatasetContext, task_index: int, task_instruction: str) -> dict:
    file_path = ctx.dataset_path / "meta" / "tasks.parquet"
    lock = ctx.get_file_lock(file_path)

    async with lock:
        table: pa.Table = pq.read_table(file_path)
        task_indices = table.column("task_index").to_pylist()
        task_column = get_task_text_column_name(table)

        if task_index not in task_indices:
            raise KeyError(f"task_index {task_index!r} not found")
        if task_column is None:
            raise KeyError("tasks.parquet is missing a task text column")

        row_pos = task_indices.index(task_index)
        old_tasks: list[str] = table.column(task_column).to_pylist()
        old_tasks[row_pos] = task_instruction

        updated_table = table.set_column(
            table.schema.get_field_index(task_column),
            task_column,
            pa.array(old_tasks, type=table.schema.field(task_column).type),
        )

        tmp_fd, tmp_path = tempfile.mkstemp(dir=file_path.parent, suffix=".tmp")
        os.close(tmp_fd)
        try:
            pq.write_table(updated_table, tmp_path)
            os.replace(tmp_path, file_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        # Force registry to reload tasks for this path on next access.
        dataset_registry.invalidate(ctx.dataset_path)

    return {"task_index": task_index, "task_instruction": task_instruction}
```

- [ ] **Step 4: 테스트 통과 확인**

기존 task-service 테스트와 mockup 호출부도 ctx 인자를 넘기도록 같이 수정한다. `tests/test_task_service_real.py`와 `tests/test_task_parquet_compat.py`의 `DatasetService()` setup은 Task 1의 `DatasetRegistry(max_size=...)` + `ctx = reg.get(dataset_path)` setup으로 바꾼다.

Run: `pytest tests/test_task_service_ctx.py tests/test_task_service_real.py tests/test_task_parquet_compat.py tests/test_mockup.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add backend/datasets/services/task_service.py tests/test_task_service_ctx.py tests/test_task_service_real.py tests/test_task_parquet_compat.py tests/test_mockup.py
git commit -m "refactor(tasks): make task_service path-aware via DatasetContext

update_task now invalidates the registry entry for the dataset's path so
subsequent reads see the rewritten tasks.parquet without leaking into other
datasets' caches."
```

---

### Task 4: `distribution_service`의 캐시를 ctx로 이동

**Files:**
- Modify: `backend/datasets/services/distribution_service.py`
- Modify: `tests/test_distribution_service.py`

**Why this task:** distribution은 이미 `dataset_path`를 받지만 cache lifecycle은 아직 `dataset_service.distribution_cache` singleton에 묶여 있다. grade update가 모든 path의 distribution cache를 같이 날리는 구조를 없애고, DatasetContext별 cache로 통일한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_distribution_service.py`에 회귀 케이스 추가. 이 파일에는 이미 `_write_distribution_dataset(root, grades)` helper가 있으므로 새 helper를 만들지 않는다:

```python
def test_distribution_cache_is_stored_on_dataset_context(tmp_path, monkeypatch):
    a = _write_distribution_dataset(tmp_path / "a", ["good"])
    from backend.core import config as _cfg
    monkeypatch.setattr(_cfg.settings, "allowed_dataset_roots", [str(tmp_path)])
    from backend.datasets.services.dataset_registry import dataset_registry
    from backend.datasets.services.distribution_service import compute_distribution
    ctx = dataset_registry.get(a)
    assert ctx.distribution_cache == {}
    compute_distribution(str(a), "grade", "auto")
    assert "grade:auto" in ctx.distribution_cache
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_distribution_service.py::test_distribution_cache_is_stored_on_dataset_context -v`
Expected: FAIL — `ctx.distribution_cache` remains empty because `compute_distribution` still writes to `dataset_service.distribution_cache`.

- [ ] **Step 3: 캐시 위치를 ctx로 이동**

`distribution_service.py`의 `compute_distribution` 내부:

```python
def compute_distribution(
    dataset_path: str,
    field: str,
    chart_type: str = "auto",
) -> DistributionResponse:
    from backend.datasets.services.dataset_registry import dataset_registry

    ctx = dataset_registry.get(dataset_path)
    cache_key = f"{field}:{chart_type}"
    cached = ctx.distribution_cache.get(cache_key)
    if cached is not None:
        return cached

    if field in ("grade", "tags"):
        result = _compute_annotation_distribution(dataset_path, field, chart_type)
    elif field == "collection_date":
        result = _compute_collection_date_distribution(dataset_path)
    else:
        result = _compute_parquet_distribution(dataset_path, field, chart_type)

    ctx.distribution_cache[cache_key] = result
    return result
```

같은 파일의 autouse fixture는 더 이상 `dataset_service.distribution_cache.clear()`를 import하지 않는다. 필요하면 `dataset_registry.invalidate(path)` 또는 새 registry 인스턴스를 사용해 테스트별 cache를 격리한다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_distribution_service.py -v`
Expected: 모두 PASS.

- [ ] **Step 5: 커밋**

```bash
git add backend/datasets/services/distribution_service.py tests/test_distribution_service.py
git commit -m "refactor(distribution): scope distribution cache to DatasetContext

Each dataset path now owns its distribution cache, eliminating cross-path
leaks where computing a chart for cell005 could return cell002's bins."
```

---

### Task 5: `export_service` / `rerun_service`를 명시적 path로 전환

**Files:**
- Modify: `backend/datasets/services/export_service.py`
- Modify: `backend/datasets/services/rerun_service.py`
- Modify: `backend/datasets/routers/datasets.py` (export 호출부)
- Modify: `backend/datasets/routers/rerun.py`
- Modify: `backend/datasets/schemas.py`
- Modify: `tests/test_mockup.py`
- Modify: `tests/test_rerun_router.py`
- Modify: `tests/test_rerun_service.py`

**Why this task:** export/rerun은 “현재 로드된” 데이터셋을 가정하지 말고 항상 호출자가 path를 명시해야 한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_mockup.py`의 export 호출은 dataset path를 명시하도록 바꾸기 전에, 먼저 다음 expectation을 추가해 현재 시그니처가 부족함을 드러낸다:

```python
result = export_dataset(str(export_dir), exclude_grades=["bad"], dataset_path=str(ds.dataset_path))
assert result["excluded_count"] == 1
```

`tests/test_rerun_router.py`에는 router가 `dataset_path`를 요구하고 서비스에 전달하는지 확인하는 케이스를 추가한다:

```python
@pytest.mark.asyncio
async def test_visualize_requires_dataset_path(monkeypatch):
    app = FastAPI()
    app.include_router(rerun_router.router)
    monkeypatch.setattr(rerun_router.rerun_service, "visualize_episode", AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/rerun/visualize/12")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_visualize_passes_dataset_path(monkeypatch):
    app = FastAPI()
    app.include_router(rerun_router.router)
    visualize = AsyncMock()
    monkeypatch.setattr(rerun_router.rerun_service, "visualize_episode", visualize)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/rerun/visualize/12", params={"dataset_path": "/tmp/ds"})
    assert response.status_code == 200
    visualize.assert_awaited_once_with("/tmp/ds", 12)
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_mockup.py tests/test_rerun_router.py -v`
Expected: FAIL — export does not accept `dataset_path`, and rerun router does not require/pass it.

- [ ] **Step 3: 시그니처 변경**

`export_service.export_dataset(output_path, exclude_grades, dataset_path: str)`로 변경:

```python
def export_dataset(output_path: str, exclude_grades: list[str], dataset_path: str) -> dict:
    from backend.datasets.services.dataset_registry import dataset_registry
    ctx = dataset_registry.get(dataset_path)
    ds_path = ctx.dataset_path
    info = ctx.info
    episodes = ctx.episodes
    features = info.get("features", {})
```

같은 함수의 기존 본문에서 남은 `dataset_service.iter_episode_parquet_files()`는 `ctx.iter_episode_parquet_files()`로, `dataset_service.get_*()`는 위 변수(`ctx`, `info`, `episodes`)로 바꾼다. `_copy_data_files`와 `_copy_video_files` helper 시그니처는 그대로 둔다.

`rerun_service.visualize_episode`도 동일하게 `async def visualize_episode(dataset_path: str, episode_index: int) -> None`로 변경하고, 내부의 `dataset_service.get_*()` 호출은 `ctx = dataset_registry.get(dataset_path)`에서 얻은 `ctx.get_episode_file_location(...)`, `ctx.dataset_path`, `ctx.info`, `ctx.features`로 바꾼다. 이 함수는 async API로 유지하므로 `tests/test_rerun_router.py`의 `AsyncMock`과 `assert_awaited_once_with(...)`를 사용한다.

- [ ] **Step 4: 라우터 호출부 수정**

`backend/datasets/routers/datasets.py:80-91` `export_dataset_endpoint`:

```python
@router.post("/export")
async def export_dataset_endpoint(req: DatasetExportRequest):
    if not req.dataset_path:
        raise HTTPException(status_code=400, detail="dataset_path is required")
    try:
        result = export_dataset(req.output_path, req.exclude_grades, req.dataset_path)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return result
```

`DatasetExportRequest`에 `dataset_path: str` 필드 추가:

```python
class DatasetExportRequest(BaseModel):
    output_path: str
    exclude_grades: list[str] = ["bad"]
    dataset_path: str
```

`backend/datasets/routers/rerun.py`:

```python
from fastapi import APIRouter, HTTPException, Query

@router.post("/visualize/{episode_index}")
async def visualize_episode(episode_index: int, dataset_path: str = Query(...)):
    try:
        await rerun_service.visualize_episode(dataset_path, episode_index)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"status": "ok", "episode_index": episode_index}
```

- [ ] **Step 5: 기존 테스트 호환 점검**

`tests/test_mockup.py`, `tests/test_rerun_service.py`의 모든 직접 호출부를 새 시그니처로 갱신한다.

Run: `pytest tests/test_mockup.py tests/test_rerun_router.py tests/test_rerun_service.py -v`
Expected: PASS.

- [ ] **Step 6: 커밋**

```bash
git add backend/datasets/services/export_service.py backend/datasets/services/rerun_service.py backend/datasets/routers/datasets.py backend/datasets/routers/rerun.py backend/datasets/schemas.py tests/test_mockup.py tests/test_rerun_router.py tests/test_rerun_service.py
git commit -m "refactor(export, rerun): require explicit dataset_path

export_dataset and rerun_service no longer rely on a 'currently loaded'
dataset; all entry points now pass dataset_path explicitly so cross-tab
exports cannot accidentally reference the wrong dataset."
```

---

### Task 6: `episodes` 라우터에 `dataset_path` 쿼리 추가

**Files:**
- Modify: `backend/datasets/routers/episodes.py`
- Create: `tests/test_episodes_router_path_aware.py`

**Why this task:** 프론트가 path를 보내고 백엔드가 그 path 컨텍스트를 사용하게 한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_episodes_router_path_aware.py`:

```python
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.core.db import _reset, close_db, db, init_db
from backend.main import app
from tests.test_episode_annotations_db import _create_mock_dataset

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest_asyncio.fixture(autouse=True)
async def reset_db():
    _reset()
    await init_db()
    # _DBFacade.execute() auto-commits outside transactions, so no explicit
    # commit (and `_DBFacade` has no `commit()` method).
    await db.execute(
        "TRUNCATE TABLE jobs, dataset_stats, episode_serials, datasets, annotations "
        "RESTART IDENTITY CASCADE"
    )
    yield
    await close_db()


@pytest.fixture
def two_datasets(tmp_path, monkeypatch):
    a = _create_mock_dataset(tmp_path / "a")
    b = _create_mock_dataset(tmp_path / "b")
    from backend.core import config as _cfg
    monkeypatch.setattr(_cfg.settings, "allowed_dataset_roots", [str(tmp_path)])
    return a, b


@pytest.mark.asyncio
async def test_episodes_endpoint_requires_dataset_path(two_datasets):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/episodes")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_episodes_endpoint_returns_path_specific_data(two_datasets):
    a, b = two_datasets
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_a = await client.get("/api/episodes", params={"dataset_path": str(a)})
        resp_b = await client.get("/api/episodes", params={"dataset_path": str(b)})
        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        await client.patch(
            "/api/episodes/0",
            params={"dataset_path": str(a)},
            json={"grade": "good", "tags": []},
        )
        resp_b_after = await client.get("/api/episodes", params={"dataset_path": str(b)})
    grades_b = [e["grade"] for e in resp_b_after.json()]
    assert all(g in (None, "") for g in grades_b)
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_episodes_router_path_aware.py -v`
Expected: FAIL — endpoint accepts no `dataset_path`.

- [ ] **Step 3: 라우터 수정**

`backend/datasets/routers/episodes.py`:

```python
from fastapi import APIRouter, HTTPException, Query

from backend.datasets.schemas import BulkGradeRequest, Episode, EpisodeUpdate
from backend.datasets.services.dataset_registry import dataset_registry
from backend.datasets.services.episode_service import episode_service, EpisodeNotFoundError

router = APIRouter(prefix="/api/episodes", tags=["episodes"])


def _ctx(dataset_path: str):
    try:
        return dataset_registry.get(dataset_path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", response_model=list[Episode])
async def list_episodes(dataset_path: str = Query(...)):
    ctx = _ctx(dataset_path)
    return await episode_service.get_episodes(ctx)


@router.get("/{episode_index}", response_model=Episode)
async def get_episode(episode_index: int, dataset_path: str = Query(...)):
    ctx = _ctx(dataset_path)
    try:
        return await episode_service.get_episode(ctx, episode_index)
    except EpisodeNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/{episode_index}", response_model=Episode)
async def update_episode(
    episode_index: int,
    update: EpisodeUpdate,
    dataset_path: str = Query(...),
):
    ctx = _ctx(dataset_path)
    try:
        if update.tags is not None:
            tags = update.tags
        else:
            current = await episode_service.get_episode(ctx, episode_index)
            tags = current.get("tags", [])
        return await episode_service.update_episode(
            ctx,
            episode_index=episode_index,
            grade=update.grade,
            tags=tags,
            reason=update.reason,
        )
    except EpisodeNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/bulk-grade")
async def bulk_grade_episodes(req: BulkGradeRequest):
    ctx = _ctx(req.dataset_path)
    try:
        count = await episode_service.bulk_grade(
            ctx, req.episode_indices, req.grade, reason=req.reason,
        )
        return {"updated": count}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
```

`backend/datasets/schemas.py`의 `BulkGradeRequest`에 `dataset_path: str`를 추가:

```python
class BulkGradeRequest(BaseModel):
    dataset_path: str
    episode_indices: list[int]
    grade: str
    reason: str | None = None
```

기존 `BulkGradeRequest`의 `grade` validator와 bad/normal reason validator는 그대로 유지한다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_episodes_router_path_aware.py tests/test_grade_reason.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add backend/datasets/routers/episodes.py backend/datasets/schemas.py tests/test_episodes_router_path_aware.py
git commit -m "feat(episodes-api): require dataset_path on every episodes endpoint

list/get/patch take dataset_path as a query param; bulk-grade takes it in
the JSON body. Removes the multi-tab contamination where two open tabs on
different datasets could share results."
```

---

### Task 7: `videos` 라우터를 path-prefix URL로 재설계

**Files:**
- Modify: `backend/datasets/routers/videos.py`
- Create: `tests/test_videos_router_dataset_key.py`

**Why this task:** 비디오 URL이 dataset key를 포함해야 브라우저 캐시까지 격리된다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_videos_router_dataset_key.py`:

```python
import json
import pytest
import pytest_asyncio
import pyarrow as pa
import pyarrow.parquet as pq
from httpx import ASGITransport, AsyncClient

from backend.core.db import _reset, close_db, db, init_db
from backend.main import app
from tests.test_episode_annotations_db import _create_mock_dataset

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest_asyncio.fixture(autouse=True)
async def reset_db():
    _reset()
    await init_db()
    # _DBFacade.execute() auto-commits outside transactions, so no explicit
    # commit (and `_DBFacade` has no `commit()` method).
    await db.execute(
        "TRUNCATE TABLE jobs, dataset_stats, episode_serials, datasets, annotations "
        "RESTART IDENTITY CASCADE"
    )
    yield
    await close_db()


def _add_video_feature(ds):
    info_path = ds / "meta" / "info.json"
    info = json.loads(info_path.read_text())
    info["features"] = {"observation.images.front": {"dtype": "video"}}
    info_path.write_text(json.dumps(info))

    ep_file = ds / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
    table = pq.read_table(ep_file)
    rows = table.num_rows
    table = table.append_column(
        "videos/observation.images.front/chunk_index",
        pa.array([0] * rows, type=pa.int64()),
    )
    table = table.append_column(
        "videos/observation.images.front/file_index",
        pa.array([0] * rows, type=pa.int64()),
    )
    table = table.append_column(
        "videos/observation.images.front/from_timestamp",
        pa.array([0.0] * rows, type=pa.float64()),
    )
    table = table.append_column(
        "videos/observation.images.front/to_timestamp",
        pa.array([1.0] * rows, type=pa.float64()),
    )
    pq.write_table(table, ep_file)
    video_file = ds / "videos" / "observation.images.front" / "chunk-000" / "file-000.mp4"
    video_file.parent.mkdir(parents=True)
    video_file.write_bytes(b"")


@pytest.fixture
def app_with_two(tmp_path, monkeypatch):
    a = _create_mock_dataset(tmp_path / "a")
    b = _create_mock_dataset(tmp_path / "b")
    _add_video_feature(a)
    _add_video_feature(b)
    from backend.core import config as _cfg
    monkeypatch.setattr(_cfg.settings, "allowed_dataset_roots", [str(tmp_path)])
    return a, b


@pytest.mark.asyncio
async def test_videos_cameras_url_uses_dataset_key(app_with_two):
    a, _ = app_with_two
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        info = (await client.post("/api/datasets/load", json={"path": str(a)})).json()
        key = info["dataset_key"]
        assert len(key) == 16
        cams = (await client.get(f"/api/datasets/{key}/videos/0/cameras")).json()
    assert isinstance(cams, list)
    assert cams
    for cam in cams:
        assert cam["url"].startswith(f"/api/datasets/{key}/videos/0/stream/")


@pytest.mark.asyncio
async def test_videos_unknown_dataset_key_404(app_with_two):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/datasets/0000000000000000/videos/0/cameras")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_video_response_disables_cross_tab_cache(app_with_two):
    a, _ = app_with_two
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        info = (await client.post("/api/datasets/load", json={"path": str(a)})).json()
        key = info["dataset_key"]
        cams = (await client.get(f"/api/datasets/{key}/videos/0/cameras")).json()
        resp = await client.get(cams[0]["url"])
    cache = resp.headers.get("cache-control", "")
    assert "private" in cache
    assert "no-store" in cache
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_videos_router_dataset_key.py -v`
Expected: FAIL — endpoint not found.

- [ ] **Step 3: 라우터 재구성**

`backend/datasets/routers/videos.py`:

```python
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from backend.datasets.services.dataset_registry import dataset_registry

router = APIRouter(prefix="/api/datasets", tags=["videos"])


def _ctx_by_key(dataset_key: str):
    try:
        return dataset_registry.get_by_key(dataset_key)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{dataset_key}/videos/{episode_index}/cameras")
async def list_cameras(dataset_key: str, episode_index: int):
    ctx = _ctx_by_key(dataset_key)
    try:
        loc = ctx.get_episode_file_location(episode_index)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))

    features = ctx.features
    video_keys = [k for k, m in features.items() if m.get("dtype") == "video"]
    dataset_path = ctx.dataset_path
    cameras = []
    for vkey in video_keys:
        vid_info = loc.get("videos", {}).get(vkey, {})
        chunk_idx = vid_info.get("chunk_index", loc["data_chunk_index"])
        file_idx = vid_info.get("file_index", loc["data_file_index"])
        video_path = dataset_path / f"videos/{vkey}/chunk-{chunk_idx:03d}/file-{file_idx:03d}.mp4"
        if video_path.exists():
            cameras.append({
                "key": vkey,
                "label": vkey.replace("observation.images.", "").replace("observation.image.", ""),
                "url": f"/api/datasets/{dataset_key}/videos/{episode_index}/stream/{vkey}",
                "from_timestamp": vid_info.get("from_timestamp", 0.0),
                "to_timestamp": vid_info.get("to_timestamp"),
            })
    return cameras


@router.get("/{dataset_key}/videos/{episode_index}/stream/{camera_key:path}")
async def stream_video(dataset_key: str, episode_index: int, camera_key: str):
    ctx = _ctx_by_key(dataset_key)
    try:
        loc = ctx.get_episode_file_location(episode_index)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))

    features = ctx.features
    video_keys = [k for k, m in features.items() if m.get("dtype") == "video"]
    if camera_key not in video_keys:
        raise HTTPException(status_code=404, detail=f"Unknown camera: {camera_key}")

    dataset_path = ctx.dataset_path
    vid_info = loc.get("videos", {}).get(camera_key, {})
    chunk_idx = vid_info.get("chunk_index", loc["data_chunk_index"])
    file_idx = vid_info.get("file_index", loc["data_file_index"])
    video_path = dataset_path / f"videos/{camera_key}/chunk-{chunk_idx:03d}/file-{file_idx:03d}.mp4"

    if not video_path.resolve().is_relative_to(dataset_path.resolve()):
        raise HTTPException(status_code=400, detail="Invalid camera path")
    if not video_path.exists():
        raise HTTPException(status_code=404, detail=f"Video not found: {camera_key}")

    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=f"episode_{episode_index}_{camera_key.replace('/', '_')}.mp4",
        headers={"Cache-Control": "private, no-store"},
    )
```

`/api/videos` 레거시 prefix는 제거. 이전 prefix를 import하던 코드(있다면)를 추적해 모두 제거한다(`rg -n "/api/videos" backend frontend tests` → 프론트만 남아있으면 다음 task에서 처리).

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_videos_router_dataset_key.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add backend/datasets/routers/videos.py tests/test_videos_router_dataset_key.py
git commit -m "feat(videos-api): include dataset key in URL and disable cross-tab cache

Video endpoints now live under /api/datasets/{dataset_key}/videos/... and
respond with Cache-Control: private, no-store. Browsers can no longer reuse
a cached video from a different dataset that happens to share an episode
index, and the dataset key in the URL guarantees unique cache identity."
```

---

### Task 8: `scalars` 라우터에 `dataset_path` 쿼리 추가

**Files:**
- Modify: `backend/datasets/routers/scalars.py`
- Create: `tests/test_scalars_router_path_aware.py`

**Why this task:** scalar 차트도 데이터셋 별로 분리되어야 한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_scalars_router_path_aware.py`:

```python
import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from tests.test_episode_annotations_db import _create_mock_dataset


@pytest.mark.asyncio
async def test_scalars_endpoint_requires_dataset_path(tmp_path, monkeypatch):
    a = _create_mock_dataset(tmp_path / "a")
    from backend.core import config as _cfg
    monkeypatch.setattr(_cfg.settings, "allowed_dataset_roots", [str(tmp_path)])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get("/api/scalars/0")
        ok = await client.get("/api/scalars/0", params={"dataset_path": str(a)})
    assert missing.status_code == 422
    assert ok.status_code in (200, 404)
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_scalars_router_path_aware.py -v`
Expected: FAIL.

- [ ] **Step 3: 라우터 수정**

`backend/datasets/routers/scalars.py`:

```python
"""Endpoint to return per-frame scalar data (observations, actions) for charts."""
import asyncio
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
from fastapi import APIRouter, HTTPException, Query

from backend.datasets.services.dataset_registry import dataset_registry
from backend.datasets.services.episode_rows import resolve_episode_rows

router = APIRouter(prefix="/api/scalars", tags=["scalars"])


@router.get("/{episode_index}")
async def get_scalars(episode_index: int, dataset_path: str = Query(...)):
    try:
        ctx = dataset_registry.get(dataset_path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        loc = ctx.get_episode_file_location(episode_index)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))

    dataset_path_obj = ctx.dataset_path
    features = ctx.features
    from_idx = loc["dataset_from_index"]
    to_idx = loc["dataset_to_index"]
    chunk_idx = loc["data_chunk_index"]
    file_idx = loc["data_file_index"]

    data_path = dataset_path_obj / f"data/chunk-{chunk_idx:03d}/file-{file_idx:03d}.parquet"
    if not data_path.exists():
        raise HTTPException(status_code=404, detail=f"Data file not found: {data_path}")
    # Keep the existing scalar extraction body below this point.
    # The only required changes are the source of loc/dataset_path/features
    # and removal of the dataset_service import.
```

(스칼라 추출 로직은 동일 — 함수 윗부분의 `loc`, `dataset_path`, `features` 변수 출처만 ctx로 대체.)

- [ ] **Step 4: 테스트 통과 확인**

Run: `pytest tests/test_scalars_router_path_aware.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add backend/datasets/routers/scalars.py tests/test_scalars_router_path_aware.py
git commit -m "feat(scalars-api): require dataset_path query parameter

Scalar charts now resolve per-frame observation/action data via the
DatasetRegistry rather than the global singleton."
```

---

### Task 9: `tasks` 라우터 + `/api/datasets/load` 정리

**Files:**
- Modify: `backend/datasets/routers/tasks.py`
- Modify: `backend/datasets/routers/datasets.py`
- Modify: `backend/datasets/schemas.py` (DatasetInfo에 `dataset_key` 추가)
- Create: `tests/test_datasets_load_returns_key.py`
- Modify: `tests/test_api_real.py`
- Modify: `tests/test_dataset_list.py`

**Why this task:** `/api/datasets/load` 응답에 `dataset_key`를 포함해 프론트가 비디오 URL을 만들 수 있게 한다. tasks 라우터도 path-aware로 통일.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_datasets_load_returns_key.py`:

```python
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.core.db import _reset, close_db, db, init_db
from backend.main import app
from tests.test_episode_annotations_db import _create_mock_dataset

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest_asyncio.fixture(autouse=True)
async def reset_db():
    _reset()
    await init_db()
    # _DBFacade.execute() auto-commits outside transactions, so no explicit
    # commit (and `_DBFacade` has no `commit()` method).
    await db.execute(
        "TRUNCATE TABLE jobs, dataset_stats, episode_serials, datasets, annotations "
        "RESTART IDENTITY CASCADE"
    )
    yield
    await close_db()


@pytest.mark.asyncio
async def test_load_returns_dataset_key(tmp_path, monkeypatch):
    a = _create_mock_dataset(tmp_path / "a")
    from backend.core import config as _cfg
    monkeypatch.setattr(_cfg.settings, "allowed_dataset_roots", [str(tmp_path)])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/datasets/load", json={"path": str(a)})
    body = res.json()
    assert "dataset_key" in body
    assert len(body["dataset_key"]) == 16


@pytest.mark.asyncio
async def test_load_does_not_alter_global_state(tmp_path, monkeypatch):
    a = _create_mock_dataset(tmp_path / "a")
    b = _create_mock_dataset(tmp_path / "b")
    from backend.core import config as _cfg
    monkeypatch.setattr(_cfg.settings, "allowed_dataset_roots", [str(tmp_path)])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/datasets/load", json={"path": str(a)})
        await client.post("/api/datasets/load", json={"path": str(b)})
        info_no_path = await client.get("/api/datasets/info")
        info_a = (await client.get("/api/datasets/info", params={"dataset_path": str(a)})).json()
    assert info_no_path.status_code == 422
    assert info_a["path"] == str(a.resolve())
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_datasets_load_returns_key.py -v`
Expected: FAIL.

- [ ] **Step 3: 스키마/라우터 수정**

`backend/datasets/schemas.py`:

```python
class DatasetInfo(BaseModel):
    path: str
    dataset_key: str
    name: str
    fps: int
    total_episodes: int
    total_tasks: int
    robot_type: str | None = None
    features: dict = {}
```

`backend/datasets/routers/datasets.py`의 `load_dataset` / `get_info`를 등록 + 메타데이터만 반환하도록:

```python
from backend.datasets.services.dataset_registry import dataset_registry, dataset_key_for


@router.post("/load", response_model=DatasetInfo)
async def load_dataset(req: DatasetLoadRequest):
    try:
        ctx = dataset_registry.get(req.path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    info = ctx.info
    return DatasetInfo(
        path=str(ctx.dataset_path),
        dataset_key=dataset_key_for(ctx.dataset_path),
        name=info.get("robot_type", ctx.dataset_path.name),
        fps=info.get("fps", 0),
        total_episodes=info.get("total_episodes", len(ctx.episodes)),
        total_tasks=info.get("total_tasks", len(ctx.tasks)),
        robot_type=info.get("robot_type"),
        features=info.get("features", {}),
    )


@router.get("/info", response_model=DatasetInfo)
async def get_info(dataset_path: str = Query(...)):
    return await load_dataset(DatasetLoadRequest(path=dataset_path))
```

`backend/datasets/routers/tasks.py`도 같은 패턴으로 수정 (모든 엔드포인트에 `dataset_path: str = Query(...)` 추가, ctx로 위임).

- [ ] **Step 4: 테스트 통과 확인**

기존 API 테스트의 `/api/datasets/info`, `/api/tasks` 호출도 `dataset_path`를 넘기도록 갱신한다.

Run: `pytest tests/test_datasets_load_returns_key.py tests/test_dataset_list.py tests/test_api_real.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add backend/datasets/routers/datasets.py backend/datasets/routers/tasks.py backend/datasets/schemas.py tests/test_datasets_load_returns_key.py tests/test_dataset_list.py tests/test_api_real.py
git commit -m "feat(datasets-api): expose dataset_key, drop singleton current state

/api/datasets/load now warms the registry and returns dataset_key alongside
metadata. /api/datasets/info and tasks endpoints take dataset_path
explicitly. The server no longer holds a 'current dataset' that leaks across
tabs and users."
```

---

### Task 10: 프론트엔드 — `datasetPath` / `datasetKey` 스레딩 (hooks)

**Files:**
- Modify: `frontend/src/hooks/useEpisodes.ts`
- Modify: `frontend/src/hooks/useTasks.ts`
- Modify: `frontend/src/hooks/useDataset.ts`
- Modify: `frontend/src/types/index.ts`
- Create: `frontend/tests/pathAwareRequests.test.mjs`

**Why this task:** 모든 dataset-dependent fetch에 path를 실어보낸다.

- [ ] **Step 1: 실패하는 frontend source test 작성**

`frontend/tests/pathAwareRequests.test.mjs`:

```javascript
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const root = new URL('..', import.meta.url).pathname
const useEpisodes = readFileSync(join(root, 'src/hooks/useEpisodes.ts'), 'utf8')
const useTasks = readFileSync(join(root, 'src/hooks/useTasks.ts'), 'utf8')
const types = readFileSync(join(root, 'src/types/index.ts'), 'utf8')

function assertIncludes(actual, expected, label) {
  if (!actual.includes(expected)) {
    throw new Error(`[${label}] expected source to include: ${expected}`)
  }
}

assertIncludes(useEpisodes, 'useEpisodes(datasetPath: string | null)', 'useEpisodes requires datasetPath')
assertIncludes(useEpisodes, "params: { dataset_path: datasetPath }", 'episodes requests send dataset_path')
assertIncludes(useTasks, 'useTasks(datasetPath: string | null)', 'useTasks requires datasetPath')
assertIncludes(useTasks, "params: { dataset_path: datasetPath }", 'tasks requests send dataset_path')
assertIncludes(types, 'dataset_key: string', 'DatasetInfo exposes dataset_key')
console.log('pathAwareRequests: OK')
```

- [ ] **Step 2: 실패 확인**

Run: `node frontend/tests/pathAwareRequests.test.mjs`
Expected: FAIL — hooks do not yet accept `datasetPath` and `DatasetInfo` lacks `dataset_key`.

- [ ] **Step 3: `useEpisodes`를 path-aware로 변경**

```typescript
// frontend/src/hooks/useEpisodes.ts
import { useState, useCallback } from 'react'
import client from '../api/client'
import type { Episode, EpisodeUpdate } from '../types'

interface UseEpisodesReturn {
  episodes: Episode[]
  selectedEpisode: Episode | null
  loading: boolean
  error: string | null
  fetchEpisodes: () => Promise<void>
  selectEpisode: (index: number) => void
  updateEpisode: (
    index: number,
    grade: string | null,
    tags: string[],
    reason?: string | null,
  ) => Promise<void>
}

export function useEpisodes(datasetPath: string | null): UseEpisodesReturn {
  const [episodes, setEpisodes] = useState<Episode[]>([])
  const [selectedEpisode, setSelectedEpisode] = useState<Episode | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchEpisodes = useCallback(async () => {
    if (!datasetPath) {
      setEpisodes([])
      return
    }
    setLoading(true)
    setError(null)
    try {
      const response = await client.get<Episode[]>('/episodes', {
        params: { dataset_path: datasetPath },
      })
      setEpisodes(response.data)
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to fetch episodes'
      setError(message)
      throw err instanceof Error ? err : new Error(message)
    } finally {
      setLoading(false)
    }
  }, [datasetPath])

  const selectEpisode = useCallback((index: number) => {
    setEpisodes(prev => {
      const ep = prev.find(e => e.episode_index === index) ?? null
      setSelectedEpisode(ep)
      return prev
    })
  }, [])

  const updateEpisode = useCallback(
    async (index: number, grade: string | null, tags: string[], reason?: string | null) => {
      if (!datasetPath) throw new Error('datasetPath is required')
      const update: EpisodeUpdate = { grade, tags }
      if (reason !== undefined) update.reason = reason
      const response = await client.patch<Episode>(`/episodes/${index}`, update, {
        params: { dataset_path: datasetPath },
      })
      const updated = response.data
      setEpisodes(prev => prev.map(e => e.episode_index === index ? updated : e))
      setSelectedEpisode(prev => prev?.episode_index === index ? updated : prev)
    },
    [datasetPath],
  )

  return { episodes, selectedEpisode, loading, error, fetchEpisodes, selectEpisode, updateEpisode }
}
```

- [ ] **Step 4: `useDataset`에 `datasetKey` 노출**

```typescript
// frontend/src/types/index.ts
export interface DatasetInfo {
  path: string
  dataset_key: string
  name: string
  fps: number
  total_episodes: number
  total_tasks: number
  robot_type: string | null
  features: Record<string, unknown>
}
```

`useDataset.ts`는 응답을 그대로 reuse — `dataset.dataset_key`로 접근 가능하게 된다.

- [ ] **Step 5: `useTasks` 동일 패턴 적용**

```typescript
// useTasks.ts
export function useTasks(datasetPath: string | null) {
  const fetchTasks = useCallback(async () => {
    if (!datasetPath) {
      setTasks([])
      return
    }
    setLoading(true)
    setError(null)
    try {
      const response = await client.get<Task[]>('/tasks', {
        params: { dataset_path: datasetPath },
      })
      setTasks(response.data)
    } finally {
      setLoading(false)
    }
  }, [datasetPath])

  const updateTask = useCallback(async (taskIndex: number, instruction: string) => {
    if (!datasetPath) throw new Error('datasetPath is required')
    const update: TaskUpdate = { task_instruction: instruction }
    await client.patch<Task>(`/tasks/${taskIndex}`, update, {
      params: { dataset_path: datasetPath },
    })
  }, [datasetPath])
}
```

- [ ] **Step 6: 호출부 타입 에러 확인**

Run: `cd frontend && npm run build`
Expected: FAIL — `useEpisodes()`/`useTasks()` 호출부가 새 required argument를 넘기지 않아 TypeScript가 막는다.

- [ ] **Step 7: `DatasetPage` 호출부 수정**

`frontend/src/components/DatasetPage.tsx:34-35`:

```typescript
const { dataset, loading: datasetLoading, error: datasetError, loadDataset } = useDataset()
const { episodes, loading: epLoading, error: epError, fetchEpisodes, updateEpisode } = useEpisodes(datasetPath)
```

(`datasetPath`는 이미 props로 받고 있다.)

- [ ] **Step 8: frontend 검증**

Run: `node frontend/tests/pathAwareRequests.test.mjs && cd frontend && npm run build`
Expected: PASS.

- [ ] **Step 9: 커밋**

```bash
git add frontend/src/hooks/useEpisodes.ts frontend/src/hooks/useTasks.ts frontend/src/types/index.ts frontend/src/components/DatasetPage.tsx frontend/tests/pathAwareRequests.test.mjs
git commit -m "feat(frontend): thread datasetPath through episodes/tasks hooks

useEpisodes and useTasks now require an explicit datasetPath; every fetch
attaches it as a query param. DatasetInfo carries the new dataset_key so
downstream components can build dataset-scoped URLs."
```

---

### Task 11: 프론트엔드 — 비디오 URL과 ScalarChart에 datasetPath/datasetKey 적용

**Files:**
- Modify: `frontend/src/components/VideoPlayer.tsx`
- Modify: `frontend/src/components/ScalarChart.tsx`
- Modify: `frontend/src/components/DatasetPage.tsx`
- Create: `frontend/tests/datasetScopedMedia.test.mjs`

**Why this task:** 비디오는 dataset_key URL prefix를 사용해야 브라우저 캐시까지 분리된다. 스칼라는 path 쿼리.

- [ ] **Step 1: 실패하는 frontend source test 작성**

`frontend/tests/datasetScopedMedia.test.mjs`:

```javascript
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const root = new URL('..', import.meta.url).pathname
const video = readFileSync(join(root, 'src/components/VideoPlayer.tsx'), 'utf8')
const scalar = readFileSync(join(root, 'src/components/ScalarChart.tsx'), 'utf8')
const page = readFileSync(join(root, 'src/components/DatasetPage.tsx'), 'utf8')

function assertIncludes(actual, expected, label) {
  if (!actual.includes(expected)) {
    throw new Error(`[${label}] expected source to include: ${expected}`)
  }
}

assertIncludes(video, 'datasetKey: string | null', 'VideoPlayer prop')
assertIncludes(video, '`/datasets/${datasetKey}/videos/${episodeIndex}/cameras`', 'VideoPlayer scoped cameras URL')
assertIncludes(scalar, 'datasetPath: string | null', 'ScalarChart prop')
assertIncludes(scalar, "params: { dataset_path: datasetPath }", 'ScalarChart sends dataset_path')
assertIncludes(page, 'datasetKey={dataset?.dataset_key ?? null}', 'DatasetPage passes datasetKey')
assertIncludes(page, 'datasetPath={datasetPath}', 'DatasetPage passes datasetPath')
console.log('datasetScopedMedia: OK')
```

- [ ] **Step 2: 실패 확인**

Run: `node frontend/tests/datasetScopedMedia.test.mjs`
Expected: FAIL — VideoPlayer/ScalarChart do not yet accept dataset identifiers.

- [ ] **Step 3: `VideoPlayer.tsx` 수정**

prop 추가:

```typescript
interface VideoPlayerProps {
  episodeIndex: number | null
  fps: number
  datasetKey: string | null
  onFrameChange?: (frame: number) => void
  terminalFrames?: number[]
}
```

카메라 fetch 부분(현재 87줄):

```typescript
useEffect(() => {
  if (episodeIndex === null || !datasetKey) {
    setCameras([])
    return
  }
  setLoading(true)
  setPlaying(false)
  setReady(false)
  setCurrentTime(0)
  setDuration(0)
  videoRefs.current.clear()
  camInfoByKey.current.clear()
  primaryKeyRef.current = null
  client.get<Camera[]>(`/datasets/${datasetKey}/videos/${episodeIndex}/cameras`)
    .then(res => {
      setCameras(res.data)
      res.data.forEach(cam => camInfoByKey.current.set(cam.key, cam))
      if (res.data.length > 0) {
        const cam = res.data[0]
        setVideoStartTime(cam.from_timestamp ?? 0)
        setVideoEndTime(cam.to_timestamp ?? 0)
      }
    })
    .catch(() => setCameras([]))
    .finally(() => setLoading(false))
}, [episodeIndex, datasetKey])
```

`Camera.url`은 이미 백엔드가 dataset_key가 들어간 절대경로를 내려주므로 `<video src={cam.url}>`은 그대로 OK.

- [ ] **Step 4: `ScalarChart.tsx` 수정**

```typescript
interface ScalarChartProps {
  episodeIndex: number | null
  datasetPath: string | null
  currentFrame: number
  onTerminalFrames?: (frames: number[], timestamps: number[]) => void
}
client.get<ScalarData>(`/scalars/${episodeIndex}`, {
  params: { dataset_path: datasetPath },
})
```

`useEffect` deps에 `datasetPath` 추가.

- [ ] **Step 5: `DatasetPage.tsx`에서 prop 전달**

```typescript
<VideoPlayer
  ref={videoRef}
  episodeIndex={selectedEpisode?.episode_index ?? null}
  fps={dataset?.fps ?? 30}
  datasetKey={dataset?.dataset_key ?? null}
  onFrameChange={setCurrentFrame}
  terminalFrames={terminalFrames}
/>
<ScalarChart
  episodeIndex={selectedEpisode?.episode_index ?? null}
  datasetPath={datasetPath}
  currentFrame={currentFrame}
  onTerminalFrames={(frames, timestamps) => {
    setTerminalFrames(frames)
    setTerminalTimestamps(timestamps)
  }}
/>
```

- [ ] **Step 6: 빌드/타입체크**

Run: `node frontend/tests/datasetScopedMedia.test.mjs && cd frontend && npm run build`
Expected: PASS.

- [ ] **Step 7: 수동 검증 — 한 브라우저에 두 탭 띄우기**

1. 탭 A에서 cell005 데이터셋 로드 → 에피소드 0 비디오 재생.
2. 탭 B에서 cell002 데이터셋 로드 → 에피소드 0 비디오 재생.
3. 탭 A로 돌아가 새로고침. 비디오가 cell005의 에피소드 0임을 확인 (URL에 cell005 dataset_key prefix 포함, DevTools Network에서 200 OK 응답이 cache가 아닌 server에서 옴).

- [ ] **Step 8: 커밋**

```bash
git add frontend/src/components/VideoPlayer.tsx frontend/src/components/ScalarChart.tsx frontend/src/components/DatasetPage.tsx frontend/tests/datasetScopedMedia.test.mjs
git commit -m "feat(frontend): scope video and scalar requests to active dataset

VideoPlayer accepts datasetKey and fetches cameras from
/api/datasets/{key}/videos/.... ScalarChart accepts datasetPath and sends
it as query param. With dataset key in the URL, browsers cannot reuse a
cached video stream from another dataset."
```

---

### Task 12: 프론트엔드 — `OverviewTab`의 bulk-grade/patch에 datasetPath 포함

**Files:**
- Modify: `frontend/src/components/OverviewTab.tsx`
- Create: `frontend/tests/overviewDatasetPathRequests.test.mjs`

**Why this task:** OverviewTab의 모든 grade write가 백엔드 변경(`BulkGradeRequest.dataset_path` 필수)에 맞춰야 한다.

- [ ] **Step 1: 실패하는 frontend source test 작성**

`frontend/tests/overviewDatasetPathRequests.test.mjs`:

```javascript
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const root = new URL('..', import.meta.url).pathname
const overview = readFileSync(join(root, 'src/components/OverviewTab.tsx'), 'utf8')

function assertIncludes(actual, expected, label) {
  if (!actual.includes(expected)) {
    throw new Error(`[${label}] expected source to include: ${expected}`)
  }
}

assertIncludes(overview, 'dataset_path: datasetPath', 'bulk-grade body sends datasetPath')
assertIncludes(overview, 'params: { dataset_path: datasetPath }', 'undo patch sends datasetPath query')
console.log('overviewDatasetPathRequests: OK')
```

- [ ] **Step 2: 실패 확인**

Run: `node frontend/tests/overviewDatasetPathRequests.test.mjs`
Expected: FAIL — bulk-grade/patch calls do not yet include dataset_path.

- [ ] **Step 3: 호출부 4곳 수정**

`OverviewTab.tsx:149`(good 분기), `:202`(modal 분기), `:269`(undo 분기), `:280`(undo patch 분기) — `datasetPath`는 이미 prop으로 들어와 있다 (`OverviewTabProps`).

```typescript
await client.post('/episodes/bulk-grade', {
  dataset_path: datasetPath,
  episode_indices: indices,
  grade: 'good',
  reason: null,
})

// modal submit
await client.post('/episodes/bulk-grade', {
  dataset_path: datasetPath,
  episode_indices: m.episodeIndices,
  grade: targetGrade,
  reason,
})

// undo grouped
await client.post('/episodes/bulk-grade', {
  dataset_path: datasetPath,
  episode_indices: group.episodeIndices,
  grade: group.grade,
  reason: group.reason,
})

// undo patch
await client.patch(`/episodes/${episodeIndex}`, {
  grade: null,
  reason: null,
}, {
  params: { dataset_path: datasetPath },
})
```

- [ ] **Step 4: frontend 검증**

Run: `node frontend/tests/overviewDatasetPathRequests.test.mjs && node frontend/tests/overviewBulkGradeWiring.test.mjs && cd frontend && npm run build`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add frontend/src/components/OverviewTab.tsx frontend/tests/overviewDatasetPathRequests.test.mjs
git commit -m "feat(overview): include dataset_path on bulk-grade and patch calls

Aligns OverviewTab grade write paths with the path-aware backend so a
right-click bulk grade in cell005 cannot accidentally annotate cell002."
```

---

### Task 13: Cross-dataset 격리 회귀 테스트

**Files:**
- Create: `tests/test_dataset_isolation_e2e.py`

**Why this task:** “두 데이터셋 동시에 띄워두고 어느 한쪽도 다른 쪽 데이터를 보지 않는다”를 영구적으로 보장한다. 사용자가 보고한 cell005 ↔ cell002 contamination이 다시 들어오면 즉시 실패한다.

- [ ] **Step 1: 테스트 작성**

```python
"""Cross-dataset isolation regression tests."""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.core.db import _reset, close_db, db, init_db
from backend.main import app
from tests.test_episode_annotations_db import _create_mock_dataset

pytestmark = pytest.mark.usefixtures("_point_settings_at_test_db")


@pytest_asyncio.fixture(autouse=True)
async def reset_db():
    _reset()
    await init_db()
    # _DBFacade.execute() auto-commits outside transactions, so no explicit
    # commit (and `_DBFacade` has no `commit()` method).
    await db.execute(
        "TRUNCATE TABLE jobs, dataset_stats, episode_serials, datasets, annotations "
        "RESTART IDENTITY CASCADE"
    )
    yield
    await close_db()


@pytest.fixture
def two_datasets(tmp_path, monkeypatch):
    a = _create_mock_dataset(tmp_path / "cell005_ds")
    b = _create_mock_dataset(tmp_path / "cell002_ds")
    from backend.core import config as _cfg
    monkeypatch.setattr(_cfg.settings, "allowed_dataset_roots", [str(tmp_path)])
    return a, b


@pytest.mark.asyncio
async def test_episodes_grade_does_not_leak_across_datasets(two_datasets):
    a, b = two_datasets
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/datasets/load", json={"path": str(a)})
        await client.post("/api/datasets/load", json={"path": str(b)})
        res = await client.patch(
            "/api/episodes/0",
            params={"dataset_path": str(a)},
            json={"grade": "good", "tags": []},
        )
        assert res.status_code == 200, res.text
        listed = (await client.get("/api/episodes", params={"dataset_path": str(b)})).json()
    ep0 = next(e for e in listed if e["episode_index"] == 0)
    assert ep0.get("grade") in (None, "")


@pytest.mark.asyncio
async def test_videos_use_distinct_keys(two_datasets):
    a, b = two_datasets
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        info_a = (await client.post("/api/datasets/load", json={"path": str(a)})).json()
        info_b = (await client.post("/api/datasets/load", json={"path": str(b)})).json()
    assert info_a["dataset_key"] != info_b["dataset_key"]


@pytest.mark.asyncio
async def test_bulk_grade_targets_only_requested_dataset(two_datasets):
    a, b = two_datasets
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/api/episodes/bulk-grade",
            json={
                "dataset_path": str(a),
                "episode_indices": [0, 1, 2],
                "grade": "bad",
                "reason": "regression test",
            },
        )
        assert res.status_code == 200, res.text
        listed_b = (await client.get("/api/episodes", params={"dataset_path": str(b)})).json()
    grades_b = [e.get("grade") for e in listed_b]
    assert all(g in (None, "") for g in grades_b)


@pytest.mark.asyncio
async def test_load_two_then_get_first_returns_first(two_datasets):
    """The exact scenario the user reported: load A, then B, then ask for A."""
    a, b = two_datasets
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        info_a = (await client.post("/api/datasets/load", json={"path": str(a)})).json()
        await client.post("/api/datasets/load", json={"path": str(b)})
        info_re = (await client.get(
            "/api/datasets/info", params={"dataset_path": str(a)},
        )).json()
    assert info_re["path"] == info_a["path"]
    assert info_re["dataset_key"] == info_a["dataset_key"]
```

- [ ] **Step 2: 테스트 실행**

Run: `pytest tests/test_dataset_isolation_e2e.py -v`
Expected: 4 PASS.

- [ ] **Step 3: compose 파일 검증**

Run: `docker compose --env-file docker/.env.example -f docker/compose.yml config >/dev/null`
Expected: PASS. There is no compose `tests` service in this repo, so pytest stays a host command.

- [ ] **Step 4: 실데이터 sanity 확인 (수동)**

`/mnt/synology/data/data_div/2026_1/lerobot/`에 있는 cell005, cell002 두 데이터셋을 한 서버에서 두 탭으로 띄우고:
1. 각 탭에서 episode 0을 선택.
2. 각 탭에서 비디오/스칼라/grade가 “해당 cell의” 데이터인지 육안 확인.
3. 한쪽에서 grade 변경 → 다른 쪽 새로고침 시 변하지 않는지 확인.

- [ ] **Step 5: 커밋**

```bash
git add tests/test_dataset_isolation_e2e.py
git commit -m "test: regression test for cross-dataset request isolation

Captures the cell005 ↔ cell002 contamination the user reported. Two
datasets are loaded into the same server; grade writes, episode reads, and
bulk-grade in one dataset must not affect the other, and dataset keys must
be distinct."
```

---

### Task 14: 레거시 `dataset_service` 싱글톤 제거 / shim 정리

**Files:**
- Modify: `backend/datasets/services/dataset_service.py`
- Modify or delete: `backend/services/dataset_service.py`
- Modify: legacy tests that still import `DatasetService`/`dataset_service` (`tests/test_dataset_service_real.py`, `tests/test_security.py`, `tests/test_task_service_real.py`, `tests/test_task_parquet_compat.py`, `tests/test_api_real.py`, `tests/test_dataset_list.py`, `tests/test_grade_reason.py`, `tests/test_episode_service_real.py`, `tests/test_mockup.py`, `tests/test_split_dataset_scalar_indices.py`)
- Audit: `rg -n "DatasetService|dataset_service" backend tests`

**Why this task:** 마지막에 남은 레거시 import를 모두 정리한다. 더 이상 누구도 `dataset_service.foo()`로 전역 상태에 접근하지 않게 한다.

- [ ] **Step 1: 잔존 import 식별**

```bash
rg -n "DatasetService|dataset_service" backend tests
```

기대 결과: `backend/datasets/services/dataset_service.py` helper docstring/function names와 `backend/datasets/services/dataset_registry.py`의 helper import만 남는다. 한 건이라도 singleton/class 사용이 남아 있으면 해당 파일을 수정해 `DatasetRegistry`/`DatasetContext` 기반으로 변환한다.

- [ ] **Step 2: `dataset_service.py` 슬림화**

`DatasetService` 클래스 + 모듈 인스턴스를 제거하고, 다른 모듈들이 의존하는 두 helper(`_normalize_compatible_string_widths`, `_table_to_list_of_dicts`)만 유지:

```python
"""Compatibility helpers for legacy parquet shape normalization.

The DatasetService singleton lived here historically. It has been replaced
by backend.datasets.services.dataset_registry.DatasetRegistry, which
manages per-path DatasetContext objects. This module retains only the
arrow-table helpers that the registry still relies on.
"""
from __future__ import annotations

import pyarrow as pa


def _table_to_list_of_dicts(table: pa.Table) -> list[dict]:
    column_names = table.schema.names
    columns = [table.column(name).to_pylist() for name in column_names]
    return [dict(zip(column_names, row)) for row in zip(*columns)] if columns else []


def _normalize_compatible_string_widths(tables: list[pa.Table]) -> list[pa.Table]:
    compatible_types: dict[str, pa.DataType] = {}
    field_types: dict[str, list[pa.DataType]] = {}
    for table in tables:
        for field in table.schema:
            field_types.setdefault(field.name, []).append(field.type)
    for field_name, types in field_types.items():
        if _has_only_string_width_mismatch(types):
            compatible_types[field_name] = pa.large_string()
    if not compatible_types:
        return tables
    normalized_tables: list[pa.Table] = []
    for table in tables:
        normalized_table = table
        for index, field in enumerate(normalized_table.schema):
            target_type = compatible_types.get(field.name)
            if target_type is None or field.type.equals(target_type):
                continue
            normalized_table = normalized_table.set_column(
                index,
                pa.field(field.name, target_type, nullable=field.nullable, metadata=field.metadata),
                normalized_table.column(field.name).cast(target_type),
            )
        normalized_tables.append(normalized_table)
    return normalized_tables


def _has_only_string_width_mismatch(types: list[pa.DataType]) -> bool:
    if len(types) < 2:
        return False
    distinct_types: list[pa.DataType] = []
    for data_type in types:
        if any(existing.equals(data_type) for existing in distinct_types):
            continue
        distinct_types.append(data_type)
    return (
        len(distinct_types) > 1
        and all(
            data_type.equals(pa.string()) or data_type.equals(pa.large_string())
            for data_type in distinct_types
        )
    )
```

`backend/services/dataset_service.py`는 `DatasetService`나 `dataset_service`를 재수출하지 않는다. 유지가 필요하면 helper-only shim으로 축소한다:

```python
"""Backwards-compatibility shim for parquet helper imports only."""
from backend.datasets.services.dataset_service import (  # noqa: F401
    _normalize_compatible_string_widths,
    _table_to_list_of_dicts,
)
```

기존 `tests/test_dataset_service_real.py`와 `tests/test_security.py`는 `DatasetService()` 직접 생성 대신 `DatasetRegistry(max_size=...)`와 `reg.get(path)`를 검증하도록 이름/본문을 갱신한다. 기존 singleton monkeypatch fixture들은 삭제한다.

- [ ] **Step 3: 풀 테스트 실행**

```bash
pytest tests/ -q
```

Expected: 전체 그린.

- [ ] **Step 4: 커밋**

```bash
git add backend/datasets/services/dataset_service.py backend/services/dataset_service.py tests/test_dataset_service_real.py tests/test_security.py tests/test_task_service_real.py tests/test_task_parquet_compat.py tests/test_api_real.py tests/test_dataset_list.py tests/test_grade_reason.py tests/test_episode_service_real.py tests/test_mockup.py tests/test_split_dataset_scalar_indices.py
git commit -m "refactor(datasets): drop DatasetService singleton

All call sites now obtain a DatasetContext from dataset_registry. The
remaining helpers in dataset_service.py are kept only because the registry
re-uses the parquet shape-normalization logic."
```

---

### Task 15: 최종 검증 + 문서 갱신

**Files:**
- Modify: `docs/superpowers/plans/2026-04-28-dataset-path-aware-apis.md` (이 파일에 “shipped” 표시)
- Verify: `frontend/src/api/client.ts`는 변경 없음 (baseURL 그대로)

**Why this task:** 모든 회귀 테스트 + compose smoke + 실데이터 검증을 한꺼번에 통과시키고 finishing-a-development-branch 단계 직전 마무리.

- [ ] **Step 1: pytest 전체 실행**

```bash
pytest tests/ -q
```

Expected: 전체 그린. 새 테스트(`test_dataset_registry`, `test_episodes_router_path_aware`, `test_videos_router_dataset_key`, `test_scalars_router_path_aware`, `test_datasets_load_returns_key`, `test_dataset_isolation_e2e`) 6개 파일이 모두 PASS.

- [ ] **Step 2: frontend + compose smoke**

```bash
node frontend/tests/pathAwareRequests.test.mjs
node frontend/tests/datasetScopedMedia.test.mjs
node frontend/tests/overviewDatasetPathRequests.test.mjs
node frontend/tests/overviewBulkGradeWiring.test.mjs
cd frontend && npm run build
cd ..
docker compose --env-file docker/.env.example -f docker/compose.yml config >/dev/null
```

Expected: all commands PASS. There is no compose `tests` service in this repo; host pytest in Step 1 is authoritative for automated tests.

- [ ] **Step 3: 실데이터 수동 검증**

위 Task 13의 “실데이터 sanity 확인” 시나리오 재실행:
- 한 서버, 두 탭, cell005 + cell002 동시.
- /api/episodes, /api/videos, /api/scalars 모두 정상 응답.
- 한쪽 grade 변경이 다른 쪽에 누설되지 않음.
- DevTools Network 탭에서 비디오 응답 헤더가 `Cache-Control: private, no-store`인지 확인.

- [ ] **Step 4: 변경 영향 받는 기존 PR/branch 점검**

```bash
git diff main..HEAD --stat
git log main..HEAD --oneline
```

특히 다음을 확인:
- `useEpisodes`/`VideoPlayer`/`ScalarChart` import하는 모든 곳 — 새 prop 누락 시 `npm run build`가 잡지 못한 동적 호출이 없는지 대비.
- `BulkGradeRequest`/`DatasetExportRequest` schema 호환 — 기존 클라이언트가 dataset_path 없이 호출하면 422.

- [ ] **Step 5: 커밋 + finishing-a-development-branch**

```bash
git add docs/superpowers/plans/2026-04-28-dataset-path-aware-apis.md
git commit -m "docs(plan): mark dataset path-aware APIs plan as shipped"
```

이후 `superpowers:finishing-a-development-branch` 스킬을 호출해 통합 옵션(merge/PR/cleanup)을 결정한다.

---

## Self-Review Notes

- **Spec coverage:** 사용자가 지목한 6가지 권장 방향 — (1) episodes/{idx}에 dataset 식별자, (2) path별 LRU cache, (3) 비디오 URL 캐시 키 분리, (4) 프론트 datasetPath 스레딩, (5) /datasets/load의 전역 상태 제거, (6) 회귀 테스트 — 가 각각 Task 6, Task 1, Task 7, Task 10–12, Task 9·14, Task 13에 대응한다.
- **Order rationale:** Registry/Context(Task 1) → service 레벨 ctx 변환(Task 2–5) → 라우터(Task 6–9) → 프론트(Task 10–12) → 회귀 테스트(Task 13) → 레거시 청소(Task 14) → 최종 검증(Task 15). 읽기 전용 API부터 잡고 grade write를 마지막에 잡는 사용자 권장 순서를 그대로 따름.
- **No backward-compat singleton shims:** `/api/videos/...` 옛 prefix는 제거한다(테스트와 프론트가 모두 새 URL을 쓰므로 죽은 코드를 남기지 않음). `dataset_service` 싱글톤도 Task 14에서 제거하고, helper-only compatibility import만 남긴다.
- **Risk:** `DatasetRegistry`가 `_load_episodes`에서 `dataset_service.py`의 helper를 import하는 순환 가능성 — Task 1의 Step 3에서 lazy import(함수 안에서 import)로 회피.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-28-dataset-path-aware-apis.md`. Execute in task order; do not skip red/green verification or commit boundaries.

**1. Subagent-Driven (recommended)** — task당 fresh subagent dispatch, task 사이 리뷰, 빠른 iteration.

**2. Inline Execution** — 같은 세션에서 batch 실행 + checkpoint 리뷰.
