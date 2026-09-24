# r4s 运行副本核对（2026-09-24）

r4s 使用 /root/chat-daily-tg 运行 Bilibili、YouTube 订阅，运行配置和状态位于 /root/chat-daily。源码目录没有 Git；[源码哈希清单](r4s-runtime-manifest-2026-09-24.json)用于核对部署副本。

该机器的 topic.enabled=false。Mac 的 X 镜像导入、向量检索和频道夜间排期由 Mac 独立运行。r4s 的 media_sent_ledger.jsonl 是已投递媒体的权威源，Mac 只读同步其副本。

本次修复 ledger 第 177 行的格式前缀，保留其余内容和原始备份。2026-09-24 10:57 的 Mac ledger-sync 记录确认同步 370 条媒体记录并正常退出。该数字仅用于本次核验。

共享 Telegram bot 的按钮接收由 /opt/r4sbot 中的 CC98 poller 负责，桥接实现与部署补丁在 [X Monitor 的 r4s 目录](https://github.com/congee949/x_monitor/tree/46ba0fcaa392076fcc9117b12f948b22c97080d3/r4s)。它向本地 outbox 写入 owner 私聊与 xreview callback，BWG读取并更新X事件复核账本。
