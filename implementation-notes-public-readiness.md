# Implementation Notes

## Design Decisions
- Treat “E2E” as a hermetic, no-credential application-to-HTTP-boundary test; real Telegram delivery is deliberately kept outside CI and named separately in documentation.
- Preserve the existing dirty worktree and confine this task's changes to public-project documentation, CI/test wiring, and isolated test support.

## Deviations

## Tradeoffs

## Open Questions
- The existing repository already contains unrelated, uncommitted pipeline changes. Any proposed public-readiness changes must not rewrite or subsume them.

## Deviations
- Added an MIT LICENSE to meet the requested public-reuse goal; it makes the licensing decision explicit.

## Tradeoffs
- The public-readiness patch scopes Ruff/MyPy to the incremental boundary, avoiding a speculative whole-repository style/type migration.

- Added a GPT Image 2 generated, non-sensitive overview diagram at the README entry point; it supplements the newcomer-oriented explanation without documenting operational secrets.
