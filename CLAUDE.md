## gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Available skills: `/office-hours`, `/plan-ceo-review`, `/plan-eng-review`, `/plan-design-review`, `/design-consultation`, `/design-shotgun`, `/design-html`, `/review`, `/ship`, `/land-and-deploy`, `/canary`, `/benchmark`, `/browse`, `/connect-chrome`, `/qa`, `/qa-only`, `/design-review`, `/setup-browser-cookies`, `/setup-deploy`, `/retro`, `/investigate`, `/document-release`, `/codex`, `/cso`, `/autoplan`, `/careful`, `/freeze`, `/guard`, `/unfreeze`, `/gstack-upgrade`, `/learn`.

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review

## UI Design Rules

- **Font**: sans-serif(기본) for UI labels/text, `var(--font-mono)` for data values/identifiers only. Inline style: `fontFamily: 'var(--font-mono)'`
- **Colors**: CSS custom properties only (`var(--text)`, `var(--c-green)` 등). 하드코딩 금지
- **No noise**: 경로, n=count, fps 같은 기술 메타데이터는 UI에 노출하지 않음. 사용자가 실제 읽는 정보만 표시
- **No redundancy**: 같은 정보를 두 곳에 표시하지 않음
- **No unnecessary toggles**: 탭으로 이미 진입했으면 내부에 접기/펼치기 추가 금지

## Test 순서

테스트는 항상 다음 순서로 진행:
1. `pytest` (단위 테스트)
2. Docker 내에서 mockup data 테스트
3. 실제 data 테스트

앞 단계가 통과해야 다음 단계로 진행.

## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- After modifying code files in this session, run `python3 -c "from graphify.watch import _rebuild_code; from pathlib import Path; _rebuild_code(Path('.'))"` to keep the graph current

항상 한글로 spec, plan 작성해줘
