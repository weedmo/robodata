# 변환기 복구 안전잠금

커널 패닉이나 강제 종료 뒤 NAS에 미완료 transaction 표식이 남아 있으면 이 절차로
변환기를 `paused + NAS read-only` 상태로 먼저 복구한다. 기본 Compose 설정도
`CURATION_CONVERSION_MUTATIONS_ENABLED=false`이므로, 명시적으로 활성화하기 전에는
변환 작업을 enqueue·claim·startup requeue하거나 validation 상태를 NAS에 쓰지 않는다.

## 안전잠금 적용

먼저 병합 설정이 세 서비스의 mutation을 끄고 converter의 NAS mount를 read-only로
만드는지 확인한다.

```bash
docker compose \
  --env-file docker/.env \
  -p curation-tools \
  -f docker/compose.yml \
  -f docker/compose.conversion-hold.yml \
  --profile curator \
  --profile convert \
  config
```

기존 converter가 실행 중이더라도 이전 환경값이나 RW mount를 유지하지 않도록 app,
curation-worker, converter를 모두 강제 재생성한다. DB와 NAS 파일은 건드리지 않는다.

```bash
docker compose \
  --env-file docker/.env \
  -p curation-tools \
  -f docker/compose.yml \
  -f docker/compose.conversion-hold.yml \
  --profile curator \
  --profile convert \
  up -d --build --force-recreate --no-deps \
  app curation-worker converter
```

적용 직후 다음을 확인한다.

- converter 환경값이 `CURATION_CONVERSION_MUTATIONS_ENABLED=false`이다.
- converter의 NAS bind가 `RW=false`이다.
- converter heartbeat가 `actual_state=paused`와 안전잠금 사유를 보고한다.
- 적용 전후 queued/running job ID, status, attempts가 동일하다.
- converter 로그에 claim, scan, conversion 실행이 없다.
- 대표 원본 recording과 production dataset fingerprint가 동일하다.

표식 파일, rebuild archive, 현재 output은 안전잠금 중 이동·삭제·chmod하지 않는다.
`*.finalization-pending.json` 또는 terminal로 입증되지 않은 `*.rebuild-journal.json`이
있는 task는 queue adapter가 변환 전에 차단한다.

## 명시적 재개

다음 조건을 모두 만족한 뒤에만 재개한다.

1. 고정된 submodule commit으로 Docker image build와 대표 robot/camera smoke가 통과했다.
2. 변환 전후 read-only fingerprint가 동일하다.
3. DB에 running/queued conversion job이 없고 converter desired state가 `stopped`이다.
4. 미완료 transaction task는 복구됐거나 queue adapter 차단이 실제 NAS 표식에서
   검증됐다.
5. rollback용 DB/NAS snapshot 위치와 담당자가 기록됐다.

먼저 mutation은 활성화하되 desired state는 `stopped`로 유지해 claim이 없는지 검증한다.
`docker/.env`의 값을 명시적으로 `true`로 설정한 뒤 hold overlay 없이 세 서비스를
재생성한다.

```bash
docker compose \
  --env-file docker/.env \
  -p curation-tools \
  -f docker/compose.yml \
  --profile curator \
  --profile convert \
  up -d --build --force-recreate --no-deps \
  app curation-worker converter
```

환경값 `true`, converter NAS `RW=true`, queued/running job 0건을 확인한 다음에만
`PATCH /api/workers/converter`로 `desired_state=running`과 복구 사유 note를 기록한다.
첫 작업은 단일 `cell_task`만 enqueue하고 fingerprint와 로그를 확인한다. 전체 자동
변환은 단일 작업 검증 후에만 사용한다.

## 즉시 rollback

새 enqueue를 중단하고 converter desired state를 `stopped`로 바꾼 뒤, 위의
`compose.conversion-hold.yml` 명령으로 app, curation-worker, converter를 다시 강제
재생성한다. converter가 `paused`, NAS가 read-only, job attempts가 불변인지 확인한다.
NAS transaction 표식과 archive는 별도 복구 승인 전까지 그대로 보존한다.
