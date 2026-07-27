# GitHub 기반 에이전트 운영 규칙

이 문서는 사람과 코딩 에이전트가 같은 GitHub 계정을 사용할 수 있는 개인 저장소에서
작업 충돌 없이 이슈를 선택하고 구현하고 복구하는 기준이다. GitHub Issue, Pull Request,
GitHub Project가 유일한 운영 원장이다.

## 1. 작업 계층

작업은 정확히 두 단계로 관리한다.

```text
Spec (kind:spec, 상위 계획, 비실행)
└── Requirement 또는 Bug (실행 가능한 leaf)
```

- **Spec**: 문제, 목표, 범위, 비목표, 전체 수용 기준과 leaf 분해를 기록한다. Spec 브랜치나
  구현 PR을 만들지 않는다.
- **Requirement**: 새 동작이나 변경을 구현하는 leaf다. `kind:requirement` label을 사용한다.
- **Bug**: 재현 가능한 결함을 수정하는 leaf다. 기존 `bug` label을 사용한다.
- Requirement와 Bug는 반드시 부모 Spec 하나에 연결한다. GitHub sub-issue 기능을 우선
  사용하고, 사용할 수 없으면 본문의 `Parent Spec` 링크를 기준으로 삼는다.
- leaf를 다시 하위 이슈로 쪼개지 않는다. M보다 크면 여러 S/M leaf로 나눈다.

`kind:spec`, `kind:requirement`, `bug`는 종류만 나타낸다. `needs-triage`, `security`,
`regression`, `release-blocker`는 보조 신호다. status, priority, area label은 만들거나
사용하지 않는다. 상태·우선순위·영역의 기준은 GitHub Project 필드다.

## 2. 상태와 Ready gate

GitHub의 open/closed는 **기록 상태**, Project의 `Status`는 **작업 흐름 상태**다.
Project `Status`는 다음 값을 사용한다.

| Status | 의미 |
| --- | --- |
| Inbox | 새로 들어와 분류 전 |
| Needs spec | 부모 Spec 또는 Ready gate 내용을 보완해야 함 |
| Ready | Ready gate를 통과해 claim 가능 |
| In progress | 유효한 claim lease로 구현 중 |
| In review | leaf를 닫는 열린 PR이 있음 |
| Recovery | 만료·중단된 실행을 점검하고 새 attempt를 준비 중 |
| Blocked | 외부 의존성, 권한 또는 3회 실패로 진행 중단 |
| Done | PR 병합과 leaf 종료가 완료됨 |

Spec은 `Inbox`, `Needs spec`, `Ready`, `Done`만 사용한다. `Ready`인 Spec도 직접 실행하지 않는다.

leaf가 `Ready`가 되려면 아래 항목이 모두 구체적이어야 한다.

- Objective: 사용자가 얻게 될 결과
- Scope: 변경할 동작과 경계
- Non-goals: 이번 작업에서 하지 않을 것
- Acceptance criteria: 관찰 가능한 완료 조건
- Validation profile: `backend`, `frontend`, `fullstack`, `db`, `docker`, `docs`,
  `submodule` 중 주 검증 프로필
- Dependencies: 선행 이슈, 외부 조건, submodule 영향 또는 `없음`
- Size: Project `Size`가 `S` 또는 `M`
- Priority, Area, Risk: Issue Form과 Project 필드의 값이 일치

빈 항목, `TBD`, M보다 큰 작업, 열린 선행 의존성이 있으면 `Ready`로 옮기지 않는다.

## 3. claim과 lease

`Agent Control` workflow가 Issue comment를 직렬 처리하고 Project 필드를 갱신한다. triage를
마친 leaf에는 아래 marker로 Ready 판정을 요청한다.

```html
<!-- robodata-agent-control:v1
{"command":"ready"}
-->
```

Ready receipt를 확인한 뒤 agent는 충돌하지 않는 Claim-ID와 실행별 Agent-Run을 만들어
claim을 요청한다. 두 값에는 영문자, 숫자, `. _ : / -`만 사용한다.

```html
<!-- robodata-agent-control:v1
{"command":"claim","claim_id":"issue-42-a1-7f3c","run_id":"codex/task-019f"}
-->
```

workflow는 leaf가 open/Ready 또는 Recovery이고, 다른 유효 lease와 열린 PR이 없으며,
attempt가 1~3인지 다시 검사한다. mutation 전 `result=pending` receipt로 attempt를
예약하고, 성공하면 `robodata-agent-receipt:v1` comment에 `result=accepted`, attempt,
lease 만료, 브랜치 이름을 기록하고 Project
`Status=In progress`, `Claim ID`, `Agent Run`, `Lease Until`, `Attempt`를 맞춘다.
코드 작성과 브랜치 생성은 반드시 이 receipt를 확인한 뒤 시작한다. GitHub assignee는 표시
편의를 위한 값일 뿐이다. 공유 계정 환경에서는 Claim-ID와 lease만이 실행 소유권을 증명한다.

같은 Claim-ID/Agent-Run 요청은 attempt를 늘리지 않는 idempotent replay다. Actions
concurrency는 `queue: max`로 대기 실행을 보존한다. queue 한도, 실행 중단 또는 API 부분
실패로 receipt가 생기지 않으면 값을 바꾸지 말고 같은 요청을 다시 게시한다. 다른 Claim-ID가
승인된 receipt를 확인한 agent는 즉시 중단한다.

lease는 최대 2시간이다. 같은 실행이 계속 작업 중이면 만료 전에 heartbeat를 요청한다.

```html
<!-- robodata-agent-control:v1
{"command":"heartbeat","claim_id":"issue-42-a1-7f3c","run_id":"codex/task-019f"}
-->
```

갱신은 attempt를 늘리지 않는다. lease가 만료된 뒤에는 기존 실행이 push를 계속하면 안 된다.
명시적으로 중단하거나 막힐 때는 각각 `release`, `block` command를 사용한다. `block`에는
500자 이하 `reason`이 필수다.

브랜치는 `agent/<issue>-a<attempt>-<slug>` 형식으로 만들며 `<slug>`는 짧은 소문자
kebab-case다. 예: `agent/42-a2-fix-job-cancel`.

## 4. PR 계약

- 하나의 leaf는 동시에 하나의 열린 PR만 가진다.
- PR은 하나의 leaf만 `Closes #<leaf>`로 닫는다.
- 부모는 `Relates to #<spec>`로 연결하며 Spec을 닫지 않는다.
- PR의 `Claim-ID`와 `Agent-Run`은 현재 유효 claim과 일치해야 한다.
- PR을 열면 Project `Status=In review`로 바꾼다.
- summary, scope, validation, gaps, data safety, submodule, risk/rollback을 모두 작성한다.
  해당 사항이 없으면 빈칸 대신 `없음`과 판단 근거를 적는다.
- 병합 뒤 leaf를 `Done`으로 옮기고 닫는다. 모든 leaf가 완료되어 전체 수용 기준이 충족될
  때만 부모 Spec을 `Done`으로 옮기고 닫는다.

## 5. 만료와 실패 복구

claim 또는 실행이 끊겼을 때 다음 순서로 복구한다.

1. leaf, Project 필드, claim/renew comment, 열린 PR, 원격 브랜치와 CI 결과를 확인한다.
2. lease가 아직 유효하면 takeover하지 않는다. 명백히 같은 Agent-Run이면 기존 claim을
   갱신하고 계속한다.
3. lease가 만료됐고 열린 PR이 없으면 hourly reconciler가 `Status=Recovery`로 옮기고
   receipt에 기존 Claim-ID와 확인한 상태를 기록한다. 재사용할 커밋과 폐기할 변경은 leaf에
   별도 설명으로 남긴다.
4. attempt를 1 올려 새 claim을 만든다. 새 브랜치를 사용하고 필요한 커밋만 명시적으로
   가져온다. 이전 브랜치에 강제 push하지 않는다.
5. 열린 PR이 있으면 새 PR을 만들지 않는다. 현재 PR을 이어받을 수 있는지 먼저 확인하고,
   불가능하면 이유를 comment로 남긴 뒤 기존 PR을 닫고 새 attempt를 시작한다.
6. attempt 3이 실패하거나 lease가 만료되면 `Status=Blocked`로 옮기고 사람이 원인과
   범위를 재검토한다. 네 번째 attempt는 새 leaf나 명시적인 수동 재승인 없이는 시작하지 않는다.

복구 후에는 처음부터 검증을 다시 실행한다. 이전 실행의 성공 로그만으로 완료를 주장하지 않는다.

## 6. 데이터와 submodule 안전

- parquet, video, NAS 원본, DB migration/backup, 대량 파일 작업은 기본적으로 데이터 변경으로
  취급한다. 샘플/임시 경로에서 먼저 검증하고 원본 보존 및 롤백 절차를 PR에 적는다.
- 토큰, 인증서, `.env`, 데이터셋의 민감 경로 또는 내용을 issue, comment, 로그, commit에
  넣지 않는다.
- `lerobot`, `rosbag2lerobot-svt`는 submodule이다. 포인터 변경이 의도한 것인지 확인하고
  submodule 내부 commit과 상위 저장소 포인터를 각각 검증한다.
- 범위 밖 submodule 변경이나 다른 실행의 작업 트리 변경을 되돌리지 않는다.

## 7. 저장소 관리자 수동 설정

### Labels

다음 label만 운영 분류에 사용한다.

| Label | 용도 |
| --- | --- |
| `kind:spec` | 상위 Spec |
| `kind:requirement` | 실행 leaf 요구사항 |
| `bug` | 실행 leaf 결함 |
| `needs-triage` | Ready gate 검토 전 |
| `security` | 보안 관련(민감 내용은 Security Advisory 사용) |
| `regression` | 이전에 동작하던 기능의 회귀 |
| `release-blocker` | 배포를 막는 결함 |

status, priority, area label은 만들지 않는다.

GitHub CLI로 처음 설정할 때는 저장소 루트에서 다음 label을 만든다. 이미 존재하는 label은
설명과 색을 확인해 같은 의미로 유지한다.

```bash
gh label create "kind:spec" --color "8250DF" --description "비실행 상위 Spec"
gh label create "kind:requirement" --color "0969DA" --description "실행 가능한 Requirement leaf"
gh label create "bug" --color "D1242F" --description "실행 가능한 Bug leaf"
gh label create "needs-triage" --color "FBCA04" --description "Ready gate 검토 필요"
gh label create "security" --color "B60205" --description "공개 가능한 보안 후속 작업"
gh label create "regression" --color "D93F0B" --description "기존 동작의 회귀"
gh label create "release-blocker" --color "B60205" --description "배포 차단"
```

default branch에 이 설정이 병합된 뒤에는 `Bootstrap GitHub Operations` workflow를 수동
실행해 같은 label을 additive하게 reconcile할 수 있다. 기존 label은 삭제하지 않는다.

### GitHub Project

`.github/project.json`이 필드 이름과 option의 최종 기준이다. `PROJECTS_CLASSIC_PAT`을
현재 shell에만 주입한 뒤 먼저 dry-run, 이어서 apply를 실행한다.

```bash
python scripts/github/bootstrap_project.py
python scripts/github/bootstrap_project.py --apply
```

manifest는 `Status`, `Type`, `Priority`, `Area`, `Risk`, `Size`, `Validation Profile`,
`Claim ID`, `Agent Run`, `Lease Until`, `Attempt`를 정의한다. `Size`에는 manifest 정합성을
위해 `L`도 있지만 Ready leaf는 `S` 또는 `M`만 허용한다. bootstrap은 Project view를 만들지
않으므로 `.github/project.json`에 선언된 `Dispatch`, `Active`, `Review`, `Recovery`,
`Specs`, `Release` view를 웹 UI에서 만든다. Project built-in automation은 새 issue 자동
추가, item 추가 시 `Status=Inbox`, issue close 시 `Done`, reopen 시 `Inbox`, Done 30일 후
archive만 활성화한다. Agent Control과 같은 상태 전이를 중복 설정하지 않는다. 상태, 종류,
크기, 우선순위, 영역, 위험도와 검증 프로필은 Project 필드 값이 최종 기준이다.

### Actions secrets와 variables

Project 자동화용 classic PAT를 Actions secret `PROJECTS_CLASSIC_PAT`으로 등록한다.
현재 public 저장소에서는 `project` scope만 부여하고, 실제로 private 저장소의 issue를
Project에서 읽어야 할 때만 `repo` scope를 추가한다.
비공개 `rosbag2lerobot-svt` submodule을 CI에서 checkout하기 위해서는 두 저장소를
읽을 수 있는 토큰을 Actions secret `SUBMODULE_PAT`으로 등록한다. 운영 토큰은
fine-grained PAT의 contents read-only 권한으로 제한한다. CI checkout은
`persist-credentials: false`를 유지하며 토큰을 후속 단계나 로그에 전달하지 않는다.
저장소 Actions variables에는 `PROJECT_OWNER=weedmo`와
`PROJECT_NUMBER=<bootstrap 출력값>`을 등록한다. 기본 `GITHUB_TOKEN`은 저장소의
issue/comment 작업에만 사용하며 Project 접근에는 사용하지 않는다. 토큰 값은 문서, issue,
workflow 입력 또는 로그에 복사하지 않고 만료일을 설정한다.

### Ruleset

기본 브랜치에 다음 repository ruleset을 적용한다.

- pull request를 통한 변경만 허용하고 직접 push와 force push를 차단
- `CI / required`, `PR Contract / contract` status check를 required로 지정
- 모든 review conversation 해결 요구
- branch deletion 제한과 linear history 여부를 저장소 정책에 맞게 지정
- 관리자 우회는 복구 상황으로 제한하고 사유를 issue에 기록

개인 저장소라 필수 승인자 수는 0으로 둘 수 있지만, 열린 PR 하나/leaf 하나와 required
checks는 유지한다. Actions의 workflow permissions는 기본 read로 두고, 필요한 workflow에만
`contents`, `issues`, `pull-requests` 권한을 최소 범위로 부여한다. Project API 호출은
`GITHUB_TOKEN` 권한을 넓히지 않고 `PROJECTS_CLASSIC_PAT`을 사용하는 단계로 분리한다.

## 8. 보안 이슈

토큰 노출, 권한 상승, 공개하면 위험한 취약점은 일반 Bug issue로 작성하지 않는다.
Issue chooser의 Security Advisory 링크로 비공개 보고한다. 공개 추적이 안전해진 뒤에만
내용을 정제한 Bug leaf와 `security` label을 만든다.

## 9. GitHub 공식 참고 자료

- [Actions로 Projects 자동화](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions)
- [Projects API 사용](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects)
- [Issue Form 구성](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-githubs-form-schema)
- [Ruleset에서 사용할 수 있는 규칙](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
- [Actions 보안 강화](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions)
