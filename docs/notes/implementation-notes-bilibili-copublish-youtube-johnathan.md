# Implementation Notes — Bilibili 联合投稿与 Johnathan Bi YouTube 订阅

## Design Decisions

- A Bilibili card keeps the platform-reported uploader as its primary author, while adding a provenance line only when that uploader's immutable UID differs from the whitelisted subscription UID. This makes a collaboration legible without misrepresenting who published the video.
- The Johnathan Bi YouTube source uses the immutable channel ID `UCCrl9a26fDCZvofnCnA5A8g` and the existing default `youtube` topic. The public RSS feed was verified before the configuration change.

## Tradeoffs

- The provenance label does not try to enumerate every co-author because the Bilibili list API provides a reliable uploader but not a complete collaboration roster. It states only the fact the pipeline can prove: the subscribed creator surfaced the video.

## Open Questions

- None.
