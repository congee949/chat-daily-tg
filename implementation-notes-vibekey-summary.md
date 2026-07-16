# Implementation Notes

## Design Decisions

- VibeKey is registered as an explicit summary-model alias instead of replacing the default summary model, allowing an isolated comparison without changing scheduled runs.
- After the key moved from the `vip` group to `default`, the provider advertised eight models and `gpt-5.6-sol` completed a Chinese chat-completions probe successfully. The alias now targets that exact advertised model name.
- On 2026-07-16, the user promoted `gpt-5.6-sol` to the scheduled `models.summary` default after reviewing the delivered comparison digest. Vision and growth judging remain on CLIProxyAPI.

## Deviations

- The initial push was deferred while the key's `vip` group had no channels. Testing resumed after the user moved it to the `default` group.

## Tradeoffs

- The promoted model was materially slower in the comparison run, but its successful long-context draft, evidence-assisted verifier pass, and delivered output were accepted over the faster prior default.

## Open Questions

- Whether repeated scheduled runs stay within the 600-second per-call timeout; streaming remains unnecessary unless idle timeouts or mid-response disconnects appear.
