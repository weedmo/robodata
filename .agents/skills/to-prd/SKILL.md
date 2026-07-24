---
name: to-prd
description: 현재 대화 맥락을 GitHub Spec과 실행 가능한 Requirement/Bug leaf로 정리해 게시합니다.
---

이 skill은 현재 대화와 코드베이스 이해를 PRD 성격의 GitHub Spec으로 정리한다.
사용자를 다시 인터뷰하지 말고 이미 확인된 내용만 합성한다.

작업 원장은 GitHub Issue와 GitHub Project뿐이다. 운영 규칙은
`AGENTS.md`와 `docs/engineering/agent-operations.md`를 따른다.

## Process

1. 아직 확인하지 않았다면 저장소의 현재 상태를 탐색한다. PRD 전체에서 `CONTEXT.md`의
   도메인 용어를 사용하고 관련 `docs/adr/` 결정을 존중한다.

2. 구현하거나 수정할 주요 모듈과 외부 동작을 정리한다. 독립적으로 검증할 수 있는 깊은
   모듈 경계를 우선하되, 구체적인 파일 경로나 코드 조각은 Spec에 넣지 않는다.

3. 아래 템플릿으로 `kind:spec` + `needs-triage` GitHub Issue를 게시한다. Spec은 비실행
   상위 이슈이며 브랜치나 구현 PR을 만들지 않는다.

4. 구현 작업을 S/M 크기의 `kind:requirement` 또는 `bug` leaf로 나누고 Spec의
   sub-issue로 연결한다. 각 leaf에는 Objective, Scope, Non-goals, Acceptance criteria,
   Validation profile, Dependencies, Size, Priority, Area, Risk를 채우고 `needs-triage`를
   붙인다. Ready 판정과 claim은 triage 단계에서 수행하며 이 skill은 구현을 시작하지 않는다.

5. 생성한 Spec과 leaf 링크, 아직 결정되지 않아 triage가 필요한 항목을 결과로 보고한다.

<prd-template>

## Objective

사용자가 얻게 될 결과.

## Scope

포함되는 동작과 시스템 경계.

## Non-goals

의도적으로 하지 않을 일.

## Acceptance criteria

관찰 가능한 완료 조건의 체크리스트.

## Validation profile

`backend`, `frontend`, `fullstack`, `db`, `docker`, `docs`, `submodule` 중 주 검증 프로필.

## Dependencies

선행 이슈, 외부 조건, submodule 영향 또는 `없음`.

## Project classification

Size(S/M), Priority(P0-P3), Area, Risk를 선택한다.

## Planned leaf issues

Requirement/Bug 제목, 종류, S/M 크기와 핵심 수용 기준. 생성 후 issue 링크로 교체한다.

</prd-template>
