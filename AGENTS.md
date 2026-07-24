# 에이전트 작업 지침

이 저장소에서 코드를 변경하는 모든 에이전트는 작업 전에 다음 문서를 읽고 따른다.

1. [저장소 작업·검증 규칙](CLAUDE.md)
2. [에이전트 GitHub 운영 규칙](docs/engineering/agent-operations.md)
3. [도메인 용어집](CONTEXT.md)
4. 변경 영역과 관련된 [아키텍처 결정 기록](docs/adr/)

## 핵심 계약

- GitHub Issue와 GitHub Project가 작업의 유일한 운영 원장이다. 별도 작업 추적 도구를 사용하지 않는다.
- 이슈 계층은 `Spec → Requirement/Bug` 두 단계뿐이다. Spec은 상위 계획이며 직접 구현하지 않는다.
- 코드 변경은 Ready gate를 통과한 Requirement 또는 Bug leaf에서만 시작한다.
- 구현 전에 versioned control comment로 claim을 요청하고 `accepted` receipt를 확인한다. assignee는 공유 GitHub 계정 때문에 참고 표시일 뿐, 소유권 근거가 아니다.
- 한 leaf에는 동시에 열린 PR 하나만 허용한다.
- 브랜치 이름은 `agent/<issue>-a<attempt>-<slug>` 형식을 사용한다.
- 상태, 크기, 우선순위, 영역은 label이 아니라 GitHub Project 필드를 기준으로 판단한다.
- 같은 요청을 재시도할 때는 새 Claim-ID를 만들지 않는다. receipt가 없으면 동일한 marker를 다시 게시해 idempotent replay로 처리한다.
- 저장소 데이터, NAS 경로, parquet/video 파일, DB, submodule을 변경하는 작업은 검증과 롤백 방법을 PR에 명시한다.

운영 상태, claim 형식, lease와 복구, 이슈/PR 작성 규칙은
`docs/engineering/agent-operations.md`가 최종 기준이다.
