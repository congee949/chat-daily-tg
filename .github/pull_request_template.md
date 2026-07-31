## Problem

<!-- Identify the concrete behavior, file/function and user impact. -->

## Minimal change

<!-- Explain why this is the smallest safe boundary and what alternatives were rejected. -->

## Evidence

- [ ] Unit tests run
- [ ] Hermetic E2E run
- [ ] Build/package check run
- [ ] Microbenchmark run when making a performance claim
- [ ] Real integration/production verification is clearly separated or marked not run

Commands and redacted results:

```text
<commands and results>
```

## Invariants and impact

- [ ] Enhancements cannot block body delivery
- [ ] seen remains write-after-send; album IDs are all recorded
- [ ] `--no-push` does not write delivery completion
- [ ] LLM output has code-level parsing/normalization/fallback
- [ ] Bilibili direct-connect and proxy boundaries are unchanged or explicitly tested
- [ ] No secret, real message, route table, database or production configuration is included
- [ ] Dependency and `uv.lock` impact is explained
- [ ] Deployment/state migration impact and rollback are explained

## Not verified

<!-- List external services, platforms or environments not exercised. -->
