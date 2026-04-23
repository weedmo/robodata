# out/ 폴더 구조 지원 및 out 데이터 검증 재활용

작성일: 2026-04-22
선행 plan:
- `2026-04-18-good-only-dataset-sync.md` — rosbag2lerobot 측 sync 모듈 재사용
- `2026-04-18-converter-validation.md` — Quick/Full validation 서비스
- `2026-04-22-out-tab-unified-split-merge.md` — 프런트 Out 탭 통합

## 1. 배경

현재 동기화(sync)된 데이터는 `/mnt/synology/data/data_div/2026_1/lerobot/` 아래 `cell*/<task>/` 형태로만 떨어지고, 검증(Quick/Full)도 이 경로를 상수로 사용한다.

신규로 정해진 산출물 레이아웃은 다음과 같다.

```
/mnt/synology/data/data_div/2026_1/out/
├── Amore/               # 프로젝트 폴더 (다양한 시도별 하위 dataset)
├── habilis_brain_zero/  # 프로젝트 폴더
└── pre/                 # 사전학습(pretrain) 통합 데이터 루트
    ├── ffw_bg2_rev4/    # 동일 robot_type, 다중 task 병합된 단일 dataset
    └── rby1a/
```

규칙 요약:
- `pre/{robot_type}/` 는 **한 개의 embodiment** 안에서 여러 task 를 합쳐 놓은 단일 dataset. 즉 `pre/<robot_type>` 자체가 `meta/info.json` 을 보유한 dataset 루트.
- `out/<project>/` 는 각 프로젝트 이름을 쓰고, 그 안에 **여러 시도(dataset)** 가 하위 디렉터리로 존재. 즉 `out/<project>` 는 cell 유사 컨테이너이며 dataset 은 한 단계 더 깊다.
- `cell*` 네이밍 규칙은 out/ 아래에서는 적용되지 않는다 (`Amore`, `habilis_brain_zero`, `pre` 등 자유 이름).

필요 작업:
1. 기존 sync 대상 루트를 `out/` 쪽으로 옮긴다 (= 항상 `out/` 아래로 향하도록 고정).
2. UI에서 `out/` 을 새로운 source 로 노출하고, pre/project 두 성격을 구분해 탐색하도록 한다.
3. converter 가 만든 dataset 뿐 아니라, `out/` 에 있는 임의 경로의 dataset 도 기존 Quick/Full validation 을 그대로 재활용할 수 있게 만든다.

## 2. 현재 코드 지형 (요약)

- `backend/core/config.py:7-50`
  - `DEFAULT_DATASET_ROOT_BASE = "/mnt/synology/data/data_div/2026_1"`
  - `DEFAULT_DATASET_SOURCES = ["lerobot", "lerobot_test"]`
  - `configured_dataset_roots()` 은 `base / source_name` 만 뱉음.
  - `cell_name_pattern: str = "cell*"` 단일 글로벌 패턴.
- `backend/datasets/services/cell_service.py`
  - `_find_dataset_roots(cell_dir)` 는 이미 `meta/info.json` 이 나올 때까지 재귀 탐색 후 해당 서브트리에서 멈춤 → **depth-agnostic**. pre/<robot> (깊이 1) 과 project/<try> (깊이 1) 모두 지원 가능.
  - `scan_cells(roots, pattern)` 은 `fnmatch` 로 cell 이름 필터링. `pattern="*"` 로 주면 사실상 필터링 없음.
- `backend/datasets/routers/cells.py`
  - `/api/cells/sources`, `/api/cells?root=`, `/api/cells/{cell_path}/datasets` 세 엔드포인트.
  - `_resolve_allowed_root` 가 `configured_dataset_roots()` 만 허용.
- `backend/converter/service.py:24-30`
  - `_DATA_ROOT = /mnt/synology/data/data_div/2026_1` (env 오버라이드 가능)
  - `LEROBOT_BASE = _DATA_ROOT / "lerobot"` — 하드코드.
- `backend/converter/validation_service.py:14,23,147`
  - `from backend.converter.service import LEROBOT_BASE` 직접 임포트.
  - `VALIDATION_STATE_FILE = LEROBOT_BASE / "convert_validation_state.json"` — 상수.
  - `_dataset_dir_for(cell_task) = LEROBOT_BASE / cell_task` — 상수.
- sync 모듈: `backend/datasets/services/rosbag_dataset_sync.py` + `TrimPanel` (`2026-04-22-out-tab-unified-split-merge.md` 로 Out 탭 통합 진행 중). dst 는 `allowed_dataset_roots` 로만 검증.

## 3. 설계 방침

### 3.1 source 구조 확장

`out` 을 새 source 로 추가한다. `lerobot`, `lerobot_test` 는 유지 (기존 converter 산출물/시험용을 계속 브라우즈).

`dataset_sources` 는 단순 리스트(string) 를 유지하되, **source 별 cell 이름 패턴 오버라이드**를 추가한다 — out 에서는 `cell*` 이 맞지 않으므로 `*` 로 허용.

설정 변경 (`backend/core/config.py`):
```python
DEFAULT_DATASET_SOURCES = ["lerobot", "lerobot_test", "out"]
DEFAULT_DATASET_SOURCE_PATTERNS = {
    "lerobot": "cell*",
    "lerobot_test": "cell*",
    "out": "*",  # pre, Amore, habilis_brain_zero 등 이름 자유
}

class Settings(BaseSettings):
    ...
    dataset_source_patterns: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_DATASET_SOURCE_PATTERNS))

    def pattern_for_source(self, source_name: str) -> str:
        return self.dataset_source_patterns.get(source_name, self.cell_name_pattern)
```

`allowed_dataset_roots` 는 `base` 전체를 그대로 허용하므로 out/ 은 자동 커버.

### 3.2 cell 탐색 패턴 주입

`cell_service.scan_cells` / `list_dataset_sources` 에 source 별 패턴을 넘긴다.

```python
# list_dataset_sources 수정
def list_dataset_sources(base_root, source_names, pattern_lookup: Callable[[str], str]) -> list[DatasetSourceInfo]:
    for source_name in source_names:
        pattern = pattern_lookup(source_name)
        ...
        cell_count = len(scan_cells([str(source_path)], pattern=pattern))
```

`/api/cells?root=out` 로 들어오면 `settings.pattern_for_source("out")` = `"*"` 를 그대로 사용.

주의: 프로젝트 폴더(`Amore`)처럼 이름 앞에 `.` 가 붙지 않은 모든 디렉터리가 매칭되므로 `_find_dataset_roots` 로 빈 폴더(=dataset 없음) 는 자연스럽게 `dataset_count=0` 로 걸러진다. UI 에서 0 개인 셀은 회색 처리.

### 3.3 pre/ vs project 구분

pre/ 안의 각 `robot_type` 은 자체가 dataset(= `meta/info.json` 보유). project/ 안의 각 시도는 한 단계 아래 dataset.

현재 `_find_dataset_roots` 재귀가 이미 두 경우를 모두 평탄화해서 `dataset_count` 를 계산한다. 단, **UI 표시상의 카테고리 구분**이 필요하다.

두 가지 방식을 검토:

- **안 A (추천)**: source 한 단계 아래를 `cell` 로 취급하는 기존 구조는 유지하되, 응답 `CellInfo` 에 `kind: Literal["pretrain", "project", "cell"]` 을 추가한다. out 이면서 이름이 `pre` 면 `pretrain`, 그 외 out 이면 `project`, lerobot/lerobot_test 면 `cell`. 프런트는 이 필드로 그룹 헤더를 나눠 표시.
- **안 B**: source → category(`pre`|`project`) → cell 3단계로 확장. 백엔드/프런트 모두 URL 구조와 상태가 커짐. 과한 변경.

→ **안 A 채택.** 데이터 모델 변화 최소.

프런트에서 out 진입 시:
- `pre` cell 은 "사전학습 데이터" 섹션에 표시. 클릭 시 곧장 내부의 robot_type 단위 dataset 목록(현재 DatasetPage 경로). `pre/<robot_type>` 자체가 dataset 이므로 dataset 목록 1개짜리 cell 로 자연스럽게 보인다.
- project cell 들은 "프로젝트 데이터" 섹션에 별도 그룹으로 표시. 내부 탐색은 기존 CellPage → DatasetPage 와 동일.

### 3.4 validation 재사용을 위한 경로 일반화

목표: `/api/converter/validate/quick` 과 동일한 검사 로직을 `out/` 경로에도 사용.

단계별 리팩터:

1. `validation_service._dataset_dir_for(cell_task)` 를 제거. 대신 `_validate_dataset(dataset_dir: Path, key: str, mode)` 처럼 **절대경로를 받는 내부 함수**를 만든다. 기존 converter 쪽 진입점(`run_quick_validation_sync(cell_task)`) 은 `LEROBOT_BASE / cell_task` 를 계산해 새 내부 함수를 호출하는 얇은 어댑터로 바꾼다.
2. `VALIDATION_STATE_FILE` 을 상수에서 **함수**로 전환:
   ```python
   def _state_file_for(source_root: Path) -> Path:
       return source_root / "convert_validation_state.json"
   ```
   source root 별로 상태 파일을 분리 (`lerobot/convert_validation_state.json`, `out/convert_validation_state.json`). 서로 섞이지 않게 한다.
3. `read_validation_state() / write_validation_state()` 는 `source_root` 인자를 받는 형태로 바꿔 state 파일 분리에 대응. 기존 converter 라우터는 `source_root = LEROBOT_BASE` 고정.
4. 신규 dataset-scope 엔드포인트 추가 (가칭):
   ```
   POST /api/datasets/validate/quick    {"dataset_path": "...", "source_root": "..."}
   POST /api/datasets/validate/full     {"dataset_path": "...", "source_root": "..."}
   GET  /api/datasets/validation?source_root=...
   ```
   - `dataset_path` 와 `source_root` 는 둘 다 `allowed_dataset_roots` 안에 있는지 검증 후 통과.
   - `key` = `dataset_path` 의 `source_root` 기준 상대경로. 예: `pre/ffw_bg2_rev4`, `Amore/try_001`.
   - lock 키도 `(source_root, key, mode)` 로 확장.
5. 기존 converter 라우터(`/api/converter/validate/*`) 는 내부적으로 새 라우터 구현을 재사용 (중복 로직 제거).

### 3.5 sync 목적지 out/ 강제

`2026-04-22-out-tab-unified-split-merge.md` 의 destination picker 를 다음 정책으로 보강:

- 목적지 루트는 항상 `settings.dataset_root_base + "/out/"` 이하 (하드 제약).
- 사용자에게는 두 카테고리 셀렉터를 제공:
  - **사전학습 (pre)**: robot_type 을 선택. 선택한 robot_type 경로가 이미 존재하면 "기존 pretrain 에 병합" 흐름 (merge), 없으면 새로 생성.
  - **프로젝트**: 프로젝트명(드롭다운/새로 입력) + 시도명(자유 입력). 결과 경로 = `out/<project>/<try_name>/`.
- 백엔드 sync 엔드포인트에서 dst 가 `base/out/` 시작이 아니면 400 반환.
- sync 직후 Quick validation 자동 실행 옵션은 플래그로 (기본 on).

### 3.6 state 파일 마이그레이션

기존 `/mnt/synology/data/data_div/2026_1/lerobot/convert_validation_state.json` 은 그대로 두고 출처가 `lerobot` 인 경우에만 읽는다. out/ 은 새 파일. 기존 converter 흐름에는 영향 없음.

## 4. 구현 Phase

### Phase 1 — config / 탐색 확장
- [ ] `config.py`: `DEFAULT_DATASET_SOURCES` 에 `"out"` 추가, `dataset_source_patterns` 필드 및 `pattern_for_source()` 헬퍼 추가.
- [ ] `cell_service.list_dataset_sources` / `scan_cells` 호출부에 source 별 패턴 전달.
- [ ] `/api/cells/sources`, `/api/cells?root=` 가 out 에 대해 올바르게 응답 (통합테스트).
- [ ] `CellInfo` 스키마에 `kind: Literal["pretrain","project","cell"]` 추가. `scan_cells` 단계에서 source name + cell name 으로 분류.

### Phase 2 — validation 경로 일반화
- [ ] `validation_service` 리팩터: `_dataset_dir_for` 제거, 절대경로 기반 내부 API.
- [ ] state 파일 `source_root` 별 분리 (`_state_file_for`).
- [ ] 기존 `run_quick_validation_sync(cell_task)` 는 내부 API 에 위임하는 어댑터로 축소.
- [ ] 신규 라우터 `/api/datasets/validate/*` 추가. `allowed_dataset_roots` 경계 검사 재사용.
- [ ] 단위테스트: out/pre/<robot>, out/<project>/<try> 구조의 fixture dataset 에 대해 Quick/Full 성공 케이스 + 누락 파일 실패 케이스.

### Phase 3 — sync 목적지 고정
- [ ] `Out 탭 통합` plan 의 destination picker 에 두 모드(pretrain / project) UI 추가.
- [ ] 백엔드 sync 요청 핸들러에 `dst startswith base/out/` 검증.
- [ ] 기본값: robot_type 선택기는 `info.json.robot_type` 기반으로 기존 pre/ 자동 매칭.
- [ ] sync 완료 후 Quick validation 자동 트리거 옵션.

### Phase 4 — 프런트 out 탐색 UX
- [ ] `LibraryPage` / `SourcePage`: `kind=pretrain` 과 `kind=project` 를 두 섹션으로 분리해 표시.
- [ ] `CellPage` 에서 pretrain cell 을 눌렀을 때 바로 dataset 화면으로 이동하도록 short-circuit (dataset 1 개뿐인 경우 네비 스킵).
- [ ] dataset 목록 행에 Quick/Full 버튼 추가 (Phase 2 라우터 사용). 상태 뱃지(`passed`/`failed`/`running`/`not_run`) 표시.

### Phase 5 — 검증 및 마이그레이션
- [ ] 실제 NAS 에서 `out/` 구조를 대상으로 스모크 테스트 (pre/ffw_bg2_rev4, pre/rby1a, Amore, habilis_brain_zero).
- [ ] converter 기존 흐름 regression 확인 (lerobot/ 쪽 Quick/Full 변화 없음).
- [ ] README / 문서 갱신: "sync 는 항상 out/ 으로 간다" 명시.

## 5. 열린 질문

1. `pre/<robot_type>/` 에 새 배치 데이터를 sync 할 때 기본 정책은 "기존 dataset 에 append merge" 인가, 아니면 새 시도를 따로 두는가? (UX 에 영향)
2. 프로젝트 이름(`Amore` 등) 목록은 사전에 정의된 whitelist 인가, 자유 생성 허용인가? (destination picker drop-down 구성)
3. `out/` 하위 dataset 도 `cell*/task` 형태의 `cell_task` key 규약을 유지해야 하는가? (validation state key 호환성)
4. 기존 lerobot/ 쪽 cell 들 중 사실상 완료된 것을 `out/` 으로 옮기는 일회성 이관 계획이 필요한가?

위 항목은 Phase 1 시작 전에 사용자 확인 필요.
