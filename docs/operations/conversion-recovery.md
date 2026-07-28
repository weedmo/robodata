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

운영 NAS는 NFS라 `RENAME_NOREPLACE`를 지원하지 않는다. 일반 복구기는 전역
LEROBOT-root advisory lock과 task lock을 모두 잡은 뒤, wrapper가 세 mutation
service의 정지를 확인한 격리 컨테이너에서만 destination 부재를 재검증하고 plain
NFS rename fallback을 사용한다. 격리 환경 표시가 없으면 fallback은 fail-closed한다.
아래 raw materializer의 기존 경로 교체 모드는 이 fallback을 사용하지 않으며,
atomic no-replace 지원을 증명하지 못하면 첫 namespace mutation 전에 중단한다.

Contract manifest 기준 raw partition도 recording directory rename에는 같은 격리
경계를 사용한다. 실행은 반드시 `scripts/run_raw_contract_partition.sh`로만 하며,
wrapper는 host-local 전역 lock, 세 mutation service 정지, active convert job 0을
확인한다. partition journal은 교체하지 않는 mode-0600 base inode와 상호 결합된
append-only state log를 사용하고, source/destination root descriptor와 recording
inode를 rename 직전과 직후에 검증한다.

Synology NFS의 plain rename 경로는 다른 NAS client까지 syscall 수준에서 배제하는
atomic no-clobber가 아니다. 따라서 격리 시간에는 동일 export에 쓰는 외부 client가
없다는 운영 전제가 필요하다. 이 전제를 보장할 수 없으면 contract partition을
실행하지 않는다. atomic `RENAME_NOREPLACE`를 지원하는 filesystem에서는 이 제한 없이
strict no-clobber 경로를 사용한다.

```bash
scripts/run_raw_contract_partition.sh apply \
  /data/raw/cell004/example_task \
  /secure/contract-partitions.json \
  /data/raw/.robodata-materialization-manifests/example-partition.json \
  <keep-contract-sha256> \
  <move-contract-sha256>=/data/raw/cell004/example_task__high
```

중단 후에는 인자를 바꾸지 않고 같은 `apply`를 다시 실행한다. 반대 방향으로
복원해야 할 때만 아래처럼 명시적으로 rollback한다. wrapper는 두 경우 모두 서비스를
자동 재시작하지 않는다.

```bash
scripts/run_raw_contract_partition.sh rollback \
  /data/raw/cell004/example_task \
  /data/raw/.robodata-materialization-manifests/example-partition.json
```

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

## 혼합 raw link view 정규화

일부 legacy task는 recording 디렉터리 symlink와 plain recording 디렉터리가
섞여 있고, plain 디렉터리 안에서도 MCAP 또는 `metadata.yaml`만 backing
디렉터리를 가리키는 symlink일 수 있다. 일반 복구기의 raw no-follow 계약은 이
상태를 의도적으로 거부한다. 이때는 복구를 실행하기 전에 전용 wrapper로 각
recording을 plain 디렉터리와 regular-file hardlink로 materialize한다.

```bash
DETACHED_RAW=/data/raw/.recovery-materialized/example-op
install -d -m 0700 "${DETACHED_RAW}" "${DETACHED_RAW}/cell007"

scripts/run_raw_materialization.sh \
  /data/raw/cell007/example_task \
  /data/raw/cell007/.example_task__legacy_backing \
  "${DETACHED_RAW}/cell007/example_task" \
  "${DETACHED_RAW}/example_task.hardlink-materialization.json"

scripts/run_conversion_recovery.sh inspect cell007/example_task \
  --raw-root "${DETACHED_RAW}"

scripts/run_conversion_recovery.sh commit-verified cell007/example_task \
  --raw-root "${DETACHED_RAW}"
```

wrapper는 일반 복구 wrapper와 마찬가지로 `app`, `curation-worker`,
`converter`를 정지하고 running 목록에서 격리를 확인한다. 이어 DB를 read-only로
조회해 active convert job이 0인지 확인한 뒤, 네트워크와 DB 연결 없이
`conversion-recovery` 컨테이너를 실행한다. 인자는 원래 task view, 검증할 backing
root, 새 canonical destination, task 외부의 durable manifest 경로 순서다. 모든
경로는 같은 filesystem에 둔다. destination parent는 이 attempt만 사용하는
빈 plain directory여야 하며 mode를 `0700`으로 고정한다. manifest는 이 private
parent 밖에 둔다. destination 자체는 최초 실행 시 없어야 한다. detached root
아래에는 원본과 같은 `cell/task` 상대 경로를 만들고, 이어지는 모든 recovery
명령에 같은 `--raw-root`를 전달한다. materializer는 private parent의
path/device/inode/mode/owner를 manifest에 고정하고 모든 build/replay 단계에서
다시 확인한다.

materializer는 먼저 전체 view와 backing의 no-follow identity를 manifest에
고정한다. 원본 source, backing, `.conversion-quarantine-*`은 rename하거나
삭제하지 않는다. 별도 destination에 recording별 plain 디렉터리와 원본 inode를
공유하는 hardlink만 설치한다. 빌드 중에는 manifest에 identity가 결합된 reservation
marker를 사용하지만 finalizing 단계에서 이를 제거하므로 committed destination에는
recording 외 숨김 entry가 없다. 성공 시 manifest version은 3, operation은
`materialize_link_view_detached_as_hardlinks`, phase는 `committed`다.
`preparing` 또는 `finalizing` 중 process가 중단되면 동일한 manifest 경로를 다시
전달해 durable phase에서 재개한다. 검증 오류로 `recovery_failed`가 기록되면
해당 attempt는 terminal이며 자동 재개·채택·삭제하지 않는다. 다른 manifest로
재계획하거나 partial artifact를 수동으로 정리하지 않는다.

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

## Contract manifest로 legacy output 검증

계약 metadata가 output에 기록되기 전에 만들어진 legacy dataset은 구조 검증만
통과했다고 채택하지 않는다. 변환 전에 별도 운영 원장에 고정한 exact-once contract
manifest와 그 파일 전체 바이트의 SHA-256을 함께 제공해 action/observation schema,
robot type, target FPS, camera 개수·이름·순서·HWC geometry를 검증한다. SHA-256은
현재 NAS 파일을 보고 즉석에서 승인값으로 만들지 않고, mutation 전에 독립적으로
고정한 값을 사용한다.

manifest는 복구 대상 raw/output과 분리된 안전한 경로의 regular file이어야 하며
POSIX mode `0600`, link count `1`이어야 한다. 기본 recovery container에서는
`CURATION_DATA_ROOT`만 같은 절대 경로로 mount되므로 manifest도 그 아래의 전용
private 디렉터리(예:
`/mnt/synology/data/data_div/2026_1/.robodata-contract-manifests`)에 둔다.
raw가 symlink 기반 legacy view라면 먼저 위의 hardlink materializer로 detached
raw를 만들고, 이후 모든 명령에서 같은 `--raw-root`를 사용한다.

```bash
scripts/run_conversion_recovery.sh inspect cell007/example_task \
  --raw-root /data/raw/.recovery-materialized/example-op \
  --contract-manifest /mnt/synology/data/data_div/2026_1/.robodata-contract-manifests/example-task.contract-manifest.json \
  --authorize-contract-manifest-sha256 <independently-fixed-full-file-sha256>

scripts/run_conversion_recovery.sh quarantine-restart cell007/example_task \
  --raw-root /data/raw/.recovery-materialized/example-op \
  --contract-manifest /mnt/synology/data/data_div/2026_1/.robodata-contract-manifests/example-task.contract-manifest.json \
  --authorize-contract-manifest-sha256 <independently-fixed-full-file-sha256>
```

두 contract 옵션은 항상 한 쌍으로 전달한다. crash 후 replay나 다음 복구 phase에서도
manifest 경로, 승인 SHA-256, detached `--raw-root`를 포함해 정확히 같은 인자를 다시
전달해야 한다. manifest의 bytes 또는 inode identity가 바뀌거나 raw serial 집합이
달라지면 복구는 fail-closed한다.

계약 불일치가 확인된 output의 보존 격리는 운영자가 승인한 offline recovery에서만
수행한다. 일반 conversion queue나 worker는 legacy output을 자동 quarantine하거나
덮어쓰면 안 된다.

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
