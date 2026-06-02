# Implementation Notes

## Design Decisions

- Evidence indexing is narrowed at chunk-selection time: keep high-risk messages and adjacent informative context, instead of embedding every exported chat line.
- Fact-risk reporting is generated from verifier JSON after the verified summary is parsed, so it reflects the final verifier decision rather than the draft.

## Deviations

## Tradeoffs

- Adjacent short context is kept when it sits next to a high-risk message, because terse chat replies like “这个能直接读x” can be crucial evidence even though they are short.

## Open Questions
