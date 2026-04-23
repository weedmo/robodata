# Out 탭: Split/Merge 통합 설계

**Date:** 2026-04-22
**Status:** Draft (awaiting user review)
**Owner:** `curation-tools` 프론트엔드 `TrimPanel` + `backend/datasets/routers/dataset_ops.py`

## 배경

`curation-tools` TrimPanel에는 현재 네 개 탭이 있다 — `Split / Merge / Delete / Cycles`. 이 중 Split과 Merge는 의미가 중첩된다:

- **Split 탭**은 `POST /api/datasets/split-into`를 호출하고, 내부적으로 `sync_selected_episodes`가 destination 존재 여부에 따라 **create / merge**를 자동 분기한다. 즉 destination이 비어 있으면 새 데이터셋을 만들고, 기존 LeRobot 데이터셋이면 Serial_number 기준으로 dedup하면서 merge한다.
- **Merge 탭**은 `POST /api/datasets/merge`를 호출해 **여러 개의 기존 데이터셋 전체**를 새 데이터셋 하나로 합친다. 쓰임새가 다르며 별도 UI 흐름을 탄다.

운영상 Split의 "create vs merge" 분기가 이미 백엔드에서 일어나고 있으므로 탭을 쪼개는 것은 혼란만 준다. Merge 탭(여러 데이터셋 → 새 데이터셋)은 실사용 빈도가 낮고, "Out"의 의미와도 맞지 않는다.

## 목표

- Split 탭과 Merge 탭을 하나의 **Out 탭**으로 합친다.
- 사용자가 대상 데이터셋을 **입력이 아닌 선택**으로 지정하게 한다.
- Destination이 기존 LeRobot 데이터셋이면 "어떤 데이터셋에 합치는가"를 실행 전에 명확히 보여준다.
- 필터를 `good 고정 + 태그`에서 **grade 다중 선택 + 태그 다중 선택**으로 확장한다.
- 백엔드 핵심 로직(`sync_selected_episodes`)은 손대지 않는다.

## Non-goals

- `POST /api/datasets/merge` 엔드포인트 / `MergeTab` 백엔드 서비스 제거는 **이번 스펙에 포함하지 않는다.** UI에서만 제거하고, 엔드포인트는 dead code로 남긴 뒤 별도 cleanup PR로 삭제한다.
- Merge 호환성 사전 검증(source와 destination의 robot_type/fps/feature 비교)은 포함하지 않는다. 잡 실행 시 `_validate_merge_compatibility`가 raise하면 잡 `failed` 상태의 error 메시지로 노출된다.
- Serial_number 중복 사전 계산(프리플라이트)은 포함하지 않는다. 안내 문구로만 "중복은 자동 skip됨"을 전달한다.
- E2E 자동 테스트 추가는 없음(저장소에 인프라 부재). 수동 체크리스트로 대체.

## 변경 요약

### 백엔드

새 엔드포인트 1개만 추가한다.

```
GET /api/datasets/summary?path=<absolute-path>
```

- `path`는 `settings.allowed_dataset_roots` 안이어야 함 (아니면 400).
- `path`가 존재하지 않거나 `meta/info.json`이 없으면 404 `"Not a LeRobot dataset"`.
- 성공 시 200:
  ```json
  {
    "path": "/mnt/.../cell01_sync",
    "total_episodes": 42,
    "robot_type": "panda",
    "fps": 30,
    "features_count": 18
  }
  ```
- 읽기 전용, 파일시스템 mutation 없음.

구현은 `backend/datasets/routers/dataset_ops.py`에 추가. `info.json`을 열어 필드를 뽑는 단순한 함수.

**기존 엔드포인트 재사용**
- `POST /api/datasets/split-into`: 그대로 사용. 요청/응답 형식 동변.
- `GET /api/datasets/browse-dirs`: 그대로 사용. 각 entry의 `is_lerobot_dataset` 플래그가 UI의 drill/select 분기에 쓰인다.

### 프론트엔드

`frontend/src/components/TrimPanel.tsx`를 수정한다.

#### 탭 구성 변경
- `TabId` 타입: `'split' | 'merge' | 'delete' | 'cycles'` → `'out' | 'delete' | 'cycles'`.
- `MergeTab` 함수와 관련 상태/스타일 모두 제거.
- `SplitTab` → `OutTab`으로 재설계 (같은 시그니처 `{ datasetPath, episodes }`).

#### OutTab 구조

```
┌─ OutTab ─────────────────────────────────────────────┐
│  Grade 필터       good  normal  bad  Ungraded         │ ← chip 다중 선택
│  Tag 필터         tag1  tag2  tag3                    │ ← chip 다중 선택
│  N episode(s) selected · Episodes: 0-5, 8, 12-15      │
│                                                        │
│  Destination                                           │
│  ┌─ DestinationPicker ─────────────────────────────┐  │
│  │  Breadcrumb: / mnt / … / 2026_1                  │  │
│  │  ↑ ..                                             │  │
│  │  ▸ lerobot         (드릴-인)                      │  │
│  │  ▸ lerobot_test    (드릴-인)                      │  │
│  │  ◆ cell01_sync     (merge target — 선택 가능)     │  │
│  │  ─────────────                                    │  │
│  │  ➕ Create new dataset here                       │  │
│  │     (확장 시 name input + Select 버튼)             │  │
│  └──────────────────────────────────────────────────┘  │
│                                                        │
│  TargetSummary (target 선택 시 노출):                   │
│    • merge 모드: "🔗 Merge into {name} — {N} eps …"  │
│    • create 모드: "📄 Create new dataset: {path}"     │
│                                                        │
│  [ Run Out ]                                           │
└───────────────────────────────────────────────────────┘
```

#### DestinationPicker 재설계

기존 `DirectoryPicker`를 확장/교체한다.

- **엔트리 타입별 동작**
  - `is_lerobot_dataset === false`: `▸` 아이콘, 클릭 시 드릴-인(`fetchDir(entry.path)`).
  - `is_lerobot_dataset === true`: `◆` 아이콘, 클릭 시 드릴-인 없이 `setTarget({ mode: 'merge', path: entry.path })`. 선택 상태는 배경색 강조.
- **Create new 버튼** — 리스트 하단 고정. 클릭 시 인라인 input 확장:
  - 자동 제안값: `${sourceDatasetName}__out_${YYYYMMDD}` (소스 basename + `__out_` + 8자리 날짜).
  - 현재 디렉토리의 자식 이름과 충돌하면 `_2`, `_3` suffix 자동 증가.
  - `Select` 버튼 → `setTarget({ mode: 'create', path: `${currentDir}/${name}` })`.
- **props 타입 변경**
  ```ts
  type Target = { mode: 'create'; path: string } | { mode: 'merge'; path: string }
  <DestinationPicker
    sourceDatasetName: string      // 자동 제안용
    value: Target | null
    onChange: (t: Target | null) => void
    disabled?: boolean
  />
  ```
- 단 하나의 target만 선택 가능. 다른 것 선택 시 교체.

#### TargetSummary 컴포넌트 (신규, 소형)

```ts
function TargetSummary({ target }: { target: Target }) {
  // create: 즉시 라벨 표시, API 호출 없음
  // merge: useEffect → GET /api/datasets/summary?path=target.path
  //        로딩/에러/성공 각각 처리
}
```

#### 필터 리팩토링

- `selectedGrades: Set<string>` + `selectedTags: Set<string>` 두 상태 공존 (DeleteTab의 multi-filter와 동일 패턴).
- matchingEpisodes 계산:
  ```
  episodes
    .filter(e => selectedGrades.size === 0 || selectedGrades.has(e.grade ?? 'Ungraded'))
    .filter(e => selectedTags.size === 0 || (e.tags ?? []).some(t => selectedTags.has(t)))
  ```
- 둘 다 비면 전체 에피소드 대상. 결과가 0이면 Run 버튼 disabled + "No episodes match the selected filter" 표시.

#### 제출 흐름

- `Run Out` 클릭 →
  ```
  POST /api/datasets/split-into
  { source_path, episode_ids, destination_path: target.path }
  ```
- 응답 `{ job_id }` → 기존 `useJobPoller`로 1초 폴링.
- `JobProgress` / 완료 summary 표시 로직 그대로.

## 데이터 플로우

1. OutTab mount → `GET /datasets/browse-dirs` (no path) → base root 하위 리스트.
2. 유저 드릴-인 → `GET /datasets/browse-dirs?path=<dir>` → 리스트 갱신.
3. LeRobot 데이터셋 선택 → target 상태 update → TargetSummary가 `GET /datasets/summary?path=<target>` 호출.
4. 또는 Create new → 이름 확정 → target 상태 update → summary 호출 없이 라벨만 표시.
5. Run → `POST /datasets/split-into` → 폴링 → 완료 표시.

## 에러 처리

| 상황 | 레이어 | 표시 |
|---|---|---|
| destination path 허용 범위 밖 | `split-into` 400 | Run 버튼 위 인라인 에러 |
| `source == destination` | `split-into` 400 | 동일 |
| merge 호환 불가 (robot_type/fps/features 불일치) | 잡 `failed` | JobProgress 빨간 박스 + error 메시지 |
| summary 호출 시 LeRobot 아님/삭제됨 | summary 404 | TargetSummary 안에 "Not a LeRobot dataset" fallback (실무적으로 race 상황) |
| 필터 결과 0개 | 클라이언트 | Run 버튼 disabled + 힌트 |
| Create 모드 이름 공란 | 클라이언트 | Select 버튼 disabled |
| Create 모드 이름 중복 | 클라이언트 | suffix 자동 제안 + 경고 |

## 테스트 계획

### 백엔드 (`tests/test_dataset_ops_router.py`)

새 `TestDatasetSummary` 클래스:

- `test_summary_returns_metadata_for_lerobot_dataset`: `meta/info.json` 있는 tmp dir → 200 + 모든 필드.
- `test_summary_returns_404_if_not_lerobot`: 일반 dir → 404.
- `test_summary_returns_404_if_missing`: 없는 path → 404.
- `test_summary_rejects_outside_allowed_roots`: `/etc` → 400.

기존 split-into / merge / delete / browse-dirs 테스트는 수정 없음.

### 프론트엔드

수동 QA 체크리스트 (PR 설명에 스크린샷/녹화 첨부):

- [ ] 탭 목록이 `Out / Delete / Cycles` 3개.
- [ ] Grade 필터 chip 다중 선택 → matchingEpisodes 카운트 정확.
- [ ] Tag 필터 chip 다중 선택 → AND 조건 동작.
- [ ] 두 필터 모두 비우면 전체 에피소드.
- [ ] Picker에서 일반 폴더 클릭 → 하위 이동.
- [ ] LeRobot 데이터셋 클릭 → 드릴-인 없이 선택됨, TargetSummary 노출.
- [ ] TargetSummary가 에피소드 수/robot_type/fps 표시.
- [ ] Create new 클릭 → 자동 제안 이름 표시 + 편집 가능.
- [ ] 제안 이름이 이미 있으면 suffix `_2` 등으로 증가.
- [ ] Create 모드 확정 시 summary 호출 없이 "Create new dataset" 라벨 표시.
- [ ] Run Out → 잡 폴링 → 완료 시 created/skipped_duplicates 표시.
- [ ] 호환 불가 merge 시도 → 잡 failed + 에러 메시지 노출.

### 회귀

- `tests/test_config.py`, `tests/test_cells_api.py`, `tests/test_dataset_ops_router.py` 기존 케이스 그대로 통과해야 함.
- 변경점이 엔드포인트 추가 + 프론트 교체뿐이라 다른 테스트에 영향 없음.

## 롤아웃

- 이번 PR: 백엔드 엔드포인트 + 프론트엔드 UI 교체.
- 후속 PR(별도): `POST /api/datasets/merge`, `dataset_ops_service.merge_datasets`, 관련 테스트 제거. UI 참조가 없어진 뒤 안전하게 제거 가능.

## 열려 있는 이슈

- **호환 사전 검증**: 향후 summary 엔드포인트에 `compatible_with?: source_path` 쿼리를 추가해 source와의 호환성을 미리 반환할 수 있음. 이번엔 포함하지 않음.
- **Delete 탭과의 일관성**: Delete 탭은 grade XOR tag 토글 방식이고, OutTab은 grade AND tag이다. 장기적으로 Delete 탭도 같은 방식으로 맞추는 게 바람직하나 이번 스펙 범위 밖.
