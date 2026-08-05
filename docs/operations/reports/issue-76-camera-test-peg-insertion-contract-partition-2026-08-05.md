# Issue 76 — Camera_test_peg_insertion contract partition

## 결과

`cell005/Camera_test_peg_insertion`의 214 recordings를 실제 MCAP에서 해석한
recording contract 기준으로 분할했다. frame resize나 metadata 추측은 사용하지
않았다.

| raw task | wrist geometry | raw | converted | terminal failure |
| --- | --- | ---: | ---: | ---: |
| `cell005/Camera_test_peg_insertion` | `640x480` | 106 | 98 | 8 |
| `cell005/Camera_test_peg_insertion__wrist_480x270` | `480x270` | 108 | 107 | 1 |

기존 task의 8개와 새 task의 1개는 camera Hz 또는 frame extraction preflight를
통과하지 못했다. geometry/layout 오류는 두 task 모두에서 재현되지 않았다.

## Exact contract manifest

- manifest: `issue76-camera-test-peg-insertion-contract.json`
- SHA-256: `dca168cd35a229f8fdb5eb5b6418841d6b96ee53764c61b20e1b6f73bd8ccdc2`
- summary: total 214, resolved 214, invalid 0, partitions 2
- source/keep digest: `87403df750775c27b69dde16604b96623aad79bc824116bc1ecc191cc7e70cbd`
- destination digest: `e6801b30c294879d66c3b25bbe42379323f61461171101767538c63fbe9e6176`

manifest는 raw root를 read-only mount한 networkless converter container에서 생성했다.
manifest file은 mode `0600`, parent는 mode `0700`이다.

## Partition과 state reconciliation

- partition journal: `issue76-camera-test-peg-insertion-partition.journal.json`
- journal SHA-256: `684207ed20f864151657a01b5f327594e5d23bd860ccf8df5ec7907deb4de348`
- state log SHA-256: `ae34f0308e656bbee2b61e980e7432e05048b405875093103af4dea78ee6ff3d`
- plan SHA-256: `fa2cf43a246cc836fa796237d77b34d5e85e6455700894aa35822751854a0d1e`
- rename strategy: `isolated_nfs_plain_rename`
- wrapper terminal phase: `committed`
- pre-reconcile state backup:
  `convert_state.json.partition-reconcile-fa2cf43a246cc836fa796237d77b34d5e85e6455700894aa35822751854a0d1e.bak`
- backup SHA-256: `2313f525aa9d0d03b4615d4afadeb2f4c2862be8c89d2c7c1d205508ce9e8d44`

partition wrapper가 `app`, `curation-worker`, `converter`를 중지하고 active convert
job 0건을 확인한 뒤에만 rename을 수행했다. recording directory inode와 그 아래
MCAP/metacard는 복사하거나 재작성하지 않았다. 분할 전후 serial 합집합 SHA-256은
동일하다.

```text
be10d59d8bd72378e0f70ea3381057b5c10d80326716850e736ddff5cd549124
```

state reconcile 후 source는 converted 98 / failed 8, destination은 converted 0 /
failed 0으로 시작했다. 변환 후 destination은 converted 107 / failed 1이다.

## Dataset validation

### Source — 640x480 wrist

- episodes: 98
- frames: 116,924
- quick: passed, 0 warnings
- full: passed, official LeRobot loader smoke passed
- `meta/info.json` SHA-256:
  `b1076e98eb3875c1441054d30b50903eade1edf899e023a79390550c0166dab4`
- curation dataset key: `37bd831984c800f4`

### Destination — 480x270 wrist

- episodes: 107
- frames: 123,360
- quick: passed, 0 warnings
- full: passed, official LeRobot loader smoke passed
- `meta/info.json` SHA-256:
  `bf007e73d4b21e80775655a02459e0ccc25e97777dea20069cb54f43cf62c7b0`
- curation dataset key: `9105e7992fadc9e1`

curation API에서 두 dataset의 listing과 load를 각각 확인했다. 최종 `app`, `nginx`,
`db`, `converter`는 healthy이고 `curation-worker`도 running이다.

## 검증 순서

1. pytest: partition/reconcile/recovery wrapper targeted suite `73 passed`
2. Docker: networkless, read-only raw contract probe로 214 recordings exact-once 확인
3. 실제 데이터: isolated partition, state reconcile, source retry, destination convert,
   quick/full validation, curation listing/load

## Rollback

rollback은 새 변환 또는 curation job이 없는 시점에 수행한다. 아래 wrapper들은 mutation
service를 중지하고 active convert job 0건을 다시 확인한다. 먼저 state를 복원하고 raw
partition을 되돌린다.

```bash
issue76_data_root=/mnt/synology/data/data_div/2026_1
issue76_journal="${issue76_data_root}/.robodata-contract-manifests/issue76-camera-test-peg-insertion-partition.journal.json"

scripts/run_partition_state_reconcile.sh rollback \
  "${issue76_data_root}/raw" \
  "${issue76_data_root}/lerobot" \
  "${issue76_journal}" \
  cell005/Camera_test_peg_insertion \
  e6801b30c294879d66c3b25bbe42379323f61461171101767538c63fbe9e6176=cell005/Camera_test_peg_insertion__wrist_480x270

scripts/run_raw_contract_partition.sh rollback \
  "${issue76_data_root}/raw/cell005/Camera_test_peg_insertion" \
  "${issue76_journal}"

docker compose -f docker/compose.yml --profile convert --profile curator \
  up -d app curation-worker converter
docker compose -f docker/compose.yml restart nginx
```

rollback 후 raw serial count/hash, `convert_state.json`, 두 output의 quick/full validation과
curation listing을 다시 확인한다. 이미 생성된 destination output은 자동 삭제하지 않는다.
