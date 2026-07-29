# Issue #56 전체 변환 운영 결과

## 실행 식별자

- Issue: `weedmo/robodata#56`
- Claim-ID: `issue-56-a2-full-pending-recovery`
- Agent-Run: `codex/full-conversion-20260729-a5`
- 실행일: 2026-07-28 ~ 2026-07-29 (KST)
- 범위: 접근 가능한 잔여 1,237개 recording의 변환 또는 명시적 terminal data error 확정

## 결과 요약

시작 시 접근 가능한 여섯 task의 2,397개 recording은 기존 output 1,142개,
기존 terminal failure 18개, 미결 1,237개였다. 미결 1,237개를 모두 처리한 결과는
신규 output 618개와 신규 terminal data error 619개이며 pending/retry는 0개다.

| Task | 접근 가능 raw | 최종 output | 최종 failed | scoped 신규 output | scoped 신규 failed | pending/retry |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `cell002/archive` | 1,308 | 1,019 | 289 | 18 | 271 | 0 / 0 |
| `cell004/Amore_toner` | 200 | 140 | 60 | 0 | 60 | 0 / 0 |
| `cell004/Amore_toner__rby1a__10650d20` | 288 | 0 | 288 | 0 | 288 | 0 / 0 |
| `cell007/SD_panel_placement_5_0720` | 269 | 269 | 0 | 268 | 0 | 0 / 0 |
| `cell007/SD_panel_placement__ffw_bg2_follower__b44bdb74` | 55 | 55 | 0 | 55 | 0 | 0 / 0 |
| `cell007/SD_panel_placement__ffw_bg2_follower__4aede9a5` | 277 | 277 | 0 | 277 | 0 | 0 / 0 |
| **합계** | **2,397** | **1,760** | **637** | **618** | **619** | **0 / 0** |

`SD_panel_placement_5_0720`에는 실행 시작 전에 output 한 개가 이미 있었으므로,
raw/output 최종 수량 269개와 scoped 신규 output 268개가 모두 맞다.

## 데이터 계약 검증

output이 있는 다섯 dataset의 `meta/info.json`, parquet, video와 공식 LeRobot loader를
검사했다. task마다 robot type, action/state 차원, 카메라 수와 영상 geometry를 별도
계약으로 유지했다.

| Dataset | Robot | FPS | Camera | state / action | 영상 geometry (H×W) | episode / frame |
| --- | --- | ---: | ---: | ---: | --- | ---: |
| `cell002/archive` | `rby1a` | 30 | 4 | 16 / 16 | head 2개 376×672, wrist 2개 240×424 | 1,019 / 1,324,510 |
| `cell004/Amore_toner` | `rby1a` | 30 | 4 | 16 / 16 | head 2개 720×1280, wrist 2개 240×424 | 140 / 208,613 |
| `cell007/SD_panel_placement_5_0720` | `RBY1_M_v1.2` | 30 | 3 | 14 / 18 | head 376×672, wrist 2개 270×480 | 269 / 126,128 |
| FFW `b44bdb74` | `ffw_bg2_follower` | 30 | 3 | 16 / 19 | head 188×336, wrist 2개 270×480 | 55 / 37,960 |
| FFW `4aede9a5` | `ffw_bg2_follower` | 30 | 3 | 16 / 19 | head 376×672, wrist 2개 270×480 | 277 / 177,133 |

카메라 feature는 4-camera dataset에서 `cam_head`, `cam_head_right`,
`cam_wrist_left`, `cam_wrist_right`, 3-camera dataset에서 `cam_head`,
`cam_wrist_left`, `cam_wrist_right`로 확인했다. output 전체 frame 수는
1,874,344개다.

`Amore_toner__rby1a__10650d20` 288개는 output을 만들 수 없는 frame-rate
terminal data error만 포함하므로 dataset directory와 DB row를 만들지 않았다.
기존 `Amore_toner`의 미결 60개도 같은 frame-rate terminal data error로 확정했다.
허용 오차를 벗어난 Hz, 프레임이 없는 입력, 손상되거나 비어 있는 MCAP은 임의로
force-convert하지 않았다.

## validation과 DB 동기화

- output이 있는 다섯 dataset 모두 quick validation 통과:
  episode 전수 검사, warning 0개
- output이 있는 다섯 dataset 모두 full validation 통과:
  dataset 구조 검사와 official LeRobot loader smoke test 통과
- parquet episode/frame 수와 `meta/info.json`의 합계가 일치
- DB dataset/`episode_serials` 수가 각각 정확히 일치:
  - DB 428: `archive` 1,019
  - DB 22: `Amore_toner` 140
  - DB 47: `SD_panel_placement_5_0720` 269
  - DB 756: FFW `b44bdb74` 55
  - DB 755: FFW `4aede9a5` 277
- no-output partition에는 DB dataset row가 없음
- 최종 active/in-flight job 0

## 0-byte MCAP 분류 복구

독립 운영 검증에서 `cell002/archive/20260506_160307_582829`의 마지막 과거 이벤트가
storage 초기화 `UNKNOWN_ERROR`로 남아 있는 것을 발견했다. 선택된 MCAP은 실제로
0 byte regular file이었다.

scanner가 rosbag2를 호출하기 전에 verified file descriptor의 크기를 검사해 빈 MCAP을
`RecordingDataError`로 분류하도록 수정하고 회귀 테스트를 추가했다. 해당 serial 하나만
`last_updated` compare guard로 requeue한 뒤 raw task를 read-only로 mount한 단일 probe를
실행했다. `convert_events.jsonl` line 219546에 다음 terminal event가 남았다.

- `error_code=RECORDING_DATA_ERROR`
- `error_category=DATA_ERROR`
- `reason=selected MCAP file is empty`
- `source=scanner`

probe가 함께 발견한 범위 밖 owner-only 입력은 운영 state에 섞지 않았다. probe 전
versioned backup으로 `convert_state.json`을 정확히 복원했으며, terminal event는 보존했다.
최종 state SHA-256은
`8f27e9c566d1950150c43ff63f2cd2bd0ac2c1b52e07ac3e75e5479dd293daec`이고
`archive`는 output 1,019, failed 289, transient 0이다.

## 원본·권한 안전

- raw recording의 내용, mode, owner, ACL을 변경하지 않았다.
- partition은 원본 바이트를 재작성하지 않고 검증된 recording directory rename만 사용했다.
- 권한 오류는 자동 chmod/chown하지 않고 retryable permission failure로 기록한다.
- Issue #34의 owner-only `archive` 입력 490개(디렉터리 481개, 파일 9개)는 접근 가능한
  1,308개에서 제외했고 이번 작업에서 변경하지 않았다.
- zero-byte 재분류 probe는 대상 raw task를 read-only로 mount했다.
- NAS state와 journal은 mode `0600`, single-link regular file 계약으로 기록했다.

## 운영 증거와 해시

운영 manifest는 데이터 루트의 private `.robodata-contract-manifests/`에 보존한다.

| Artifact | SHA-256 |
| --- | --- |
| `issue56-archive-20260729T1928KST.json` | `42d3863ca79cfa1ab97fa1eecdec52d03d79b11c56bcbff3fded57c436880f26` |
| `issue56-amore-toner-20260729T1721KST.json` | `d381f23ae1371091bb14be63bb17c4eb8fde119ca9845e17f1a7b4eec8cfc87e` |
| `issue56-amore-toner-partition-20260729T1752KST.journal.json` | `a7807675a417f27fbf7c1b9621975998eac5b630f413985e52ae56aa888f1150` |
| `issue56-ffw-20260728T1911KST.json` | `fb0edb0418c81791a3b86a727865fb9fe5c6fc0f095deb58fe1bcf103af3df70` |
| `issue56-ffw-partition-20260728T1919KST.journal.json` | `6904731e50463cbc40b9a1ca6dc6e01060b4dcfa510df7bee88a988cb493df2c` |
| zero-byte state backup | `8f27e9c566d1950150c43ff63f2cd2bd0ac2c1b52e07ac3e75e5479dd293daec` |
| zero-byte reclass journal | `34499b9b45d612873b7040d5790be03d3bbb4f9d54531d59442652b67149ba1e` |
| zero-byte outcome | `ab42d8b03134556dba7624be7aa5455f9cce231c9badc28dc0c60012df96690b` |

serial-set SHA-256:

- archive accessible raw:
  `37afb7ac2bd89450f32f43608f45a4954b9255d180d7fd0eb3c9768c3783f527`
- archive output:
  `675da41ac0650930b731f4abec94a4952897322a31ef94d85148d581722f991a`
- archive failed:
  `79ec5f4239b41fcb1e204da559d1c940be84052db7bac038b49e9ebacb0b4d88`
- Amore source raw/output/failed:
  `294a96d71ca0a63358d09d0a0874a275c5893958981206346e1002f941512ee6` /
  `fbda7a2ecb53f27c4a5799beff45902d2c54b6957a17edc9c9b82321d5769014` /
  `e69f04b72e3c70a07291c869fef0cabd32d88630dba3d2d79d90845487105673`
- Amore terminal partition raw/failed:
  `234e7f6445db5635b44dbcb0e1dcf32ccfd57ef671061a889434f9a9154dcd10`
- FFW source raw/output:
  `40631acdc225bc26327c18bf82b04e21105513f03d672bb66c031f660d940ee1`
- FFW destination raw/output:
  `b5f85229672e3e958397b422cfd6af6046a261f52aa73e261fd54cec8b0124d`

## 코드 검증과 서비스 상태

- root targeted raw/queue tests: 55 passed
- root 전체 pytest: 840 passed, 72 skipped, default-config 1건은 env 격리를 위해
  분리 실행해 1 passed(총 841 passed)
- submodule 전체 Docker pytest: 487 passed, 25 skipped
- frontend production build 통과
- Compose config, Python compile, shell syntax, `git diff --check`,
  host-control 금지 검사 통과
- 최종 converter image:
  `sha256:88c555006691f5dbb46b353949e9b59107795753d0559d8994fe92bf7ca031d5`
- app, nginx, db, converter 정상; converter OOM=false, restart=0
- curation-worker도 desired/actual `running`, heartbeat 최신, in-flight job 없음
- 독립 read-only 운영 재검증: 신규 failure 619개 모두 `DATA_ERROR` event 존재,
  여섯 task의 pending/retry 0, active job 0 확인

## 롤백

- 코드: root PR과 submodule PR을 각각 revert한다.
- FFW/Amore partition: 위 versioned partition journal과
  `scripts/run_raw_contract_partition.sh rollback`을 사용한다. 서비스 격리와
  active convert job 0을 다시 증명해야 한다.
- converter state: zero-byte probe 전 backup과 SHA-256을 확인한 뒤 atomic state
  복원 절차를 사용한다.
- 이미 검증된 output은 삭제하거나 덮어쓰지 않는다.
- rollback 전후에 raw/output/failed serial-set hash, quick/full validation,
  DB episode 수를 다시 비교한다.
