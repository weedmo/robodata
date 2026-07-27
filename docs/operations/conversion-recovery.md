# 변환 트랜잭션 복구

중단된 변환은 일반 변환 워커가 임의로 이어서 처리하지 않는다. 먼저 복구 전용
컨테이너에서 상태를 확인하고, 확인 결과에 맞는 단일 복구 모드를 실행한다. 복구
컨테이너는 DB와 네트워크를 사용하지 않으며 NAS에 쓸 수 있는 유일한 서비스다.
복구 wrapper는 이 격리 사실을 컨테이너에
`CURATION_RECOVERY_ISOLATED=true`로 고정한다.

## 실행 전 격리

반드시 저장소의 격리 wrapper를 사용한다. wrapper는 `app`,
`curation-worker`, `converter`를 먼저 정지하고 실제 running service 목록에서
세 서비스가 모두 사라졌는지 확인한 다음에만 복구 컨테이너를 시작한다.
`conversion-recovery`만 NAS를 읽기/쓰기로 마운트한다.

```bash
scripts/run_conversion_recovery.sh inspect cell007/example_task
```

wrapper는 복구가 끝나도 세 서비스를 자동 재시작하지 않는다. 여러 task를 복구할
때마다 격리 상태를 다시 검증한다. 내부 `run --no-deps`는 DB를 포함한 의존
서비스 없이 일회성 컨테이너 하나만 시작한다. 항상 `inspect`부터 실행하고 JSON
결과와 대상 경로를 확인한다. 명령의 표준 출력은 자동화가 읽는 JSON 하나뿐이며,
실패 설명은 표준 오류로 출력되고 종료 코드는 0이 아니다.

운영 NAS는 NFS라 `RENAME_NOREPLACE`를 지원하지 않는다. 복구기는 전역
LEROBOT-root advisory lock과 task lock을 모두 잡은 뒤, wrapper가 세 mutation
service의 정지를 확인한 격리 컨테이너에서만 destination 부재를 재검증하고 plain
NFS rename fallback을 사용한다. 격리 환경 표시가 없으면 fallback은 fail-closed한다.

데이터 루트가 기본값과 다르면 Compose 호출에 `CURATION_DATA_ROOT`를 설정한다.
호스트에서 직접 실행할 때는 `--raw-root`, `--lerobot-root`, `--state-file`로
명시할 수 있다.

```bash
.venv/bin/python scripts/recover_conversion.py inspect cell007/example_task \
  --raw-root /data/raw \
  --lerobot-root /data/lerobot \
  --state-file /data/lerobot/convert_state.json
```

직접 실행은 주변 mutation process를 자동 정지하지 않으므로 격리된 테스트
filesystem에서만 사용한다. 운영 NAS에서는 wrapper만 사용한다.

유실된 v1 producer가 만든 일부 marker의 내부 snapshot 알고리즘은 현재 serial
digest와 다르다. 이 marker는 현재 파일에서 즉석 계산한 값으로 승인하면 안 된다.
별도 보관본이나 mutation 전에 외부 운영 원장에 고정한 marker 전체 파일
SHA-256만 반복 옵션으로 전달한다.

```bash
scripts/run_conversion_recovery.sh inspect cell007/example_task \
  --authorize-legacy-marker-sha256 <pre-incident-marker-file-sha256>
```

승인값은 marker 전체 바이트에 결합되며, marker가 하나라도 바뀌면 재실행도
거부된다. legacy finalization marker에 `raw_serials_before`가 있으면 현재 raw
serial 목록과도 정확히 일치해야 한다. Crash replay에도 같은 승인 옵션을 다시
전달한다.

## 복구 모드

| 모드 | 용도 |
| --- | --- |
| `inspect` | 파일을 변경하지 않고 트랜잭션·원본·출력·격리 상태를 보고한다. |
| `rollback` | 커밋되지 않은 변경을 이전의 검증된 상태로 되돌린다. |
| `adopt-finalization` | 이미 완성된 finalization 결과를 복구 트랜잭션에 편입한다. |
| `quarantine-restart` | 불완전한 출력을 보존 격리하고 원본에서 다시 시작할 상태로 만든다. |
| `commit-verified` | 검증이 끝난 결과만 최종 커밋 상태로 전환한다. |

복구 모드는 삭제 대신 이름 변경과 격리를 사용한다. 실행 뒤 같은 명령에
`inspect`를 다시 사용해 terminal phase와 보존 경로를 확인한다. 성공을 확인하기 전에는
격리 디렉터리, rollback 경로, recovery state file을 수동으로 삭제하거나 이동하지 않는다.
`convert_state.json` 교체본은 원본의 POSIX mode, uid, gid를 보존한다. 새 inode를
만들기 때문에 별도 ACL 또는 xattr는 복제하지 않으며, 운영 state에 확장 ACL/xattr가
있다면 복구 전에 별도로 기록하고 복구 뒤 다시 확인한다. Dataset output, archive,
marker는 새 파일을 만들지 않고 rename하므로 기존 inode metadata가 그대로 유지된다.
Intent는 mode `0600`, link count 1, 현재 temporary inode의 dev/ino와 최초
temporary inode에 실제로 부여된 uid/gid를 payload에 결합한다. 이후 phase
rewrite는 새 inode의 dev/ino를 다시 결합하고 owner가 최초 결합값과 같을 때만
진행하며, receipt replay도 payload와 실제 inode identity를 다시 대조한다. 따라서
cell별 parent owner가 달라도 NFS root-squash가 컨테이너 uid 0에 실제로 부여한
owner를 보존하면서 다른 owner나 교체된 intent를 거부한다.

Marker가 없는 기존 output도 queue 진입 전에 side-effect 없는 quick validation을
통과해야 한다. 손상 JSON/parquet, 빈 episodes/data, 필수 컬럼 누락은
`incomplete-output` recovery blocker가 된다. Recovery intent는 raw 전체 tree의
경로·inode·mode·uid/gid·size·mtime/ctime metadata fingerprint를 저장해 첫 rename
직전과 모든 replay에서 다시 확인한다.

## 정상 변환 재개

복구 receipt가 `receipt_durable`이고 canonical recovery blocker가 없으며 output,
state backup, audit marker를 확인한 뒤에만 일반 Compose 구성으로 서비스를 다시
배포한다. 복구 overlay를 일반 운영 배포에 계속 포함하지 않는다.

```bash
docker compose -f docker/compose.yml --profile convert --profile curator \
  up -d app curation-worker converter
```
