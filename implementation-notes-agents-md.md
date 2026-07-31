# Implementation Notes

## Design Decisions

- 项目此前只有 `CLAUDE.md`，本次新增 `AGENTS.md` 作为跨 agent 的唯一事实源；
  `CLAUDE.md` 保留为兼容入口，避免两份规则继续漂移。
- 主文件只保留会造成真实事故的运行边界、网络、状态和部署约束。架构流程、故障案例与
  历史设计继续引用现有 `docs/`，不复制到新的 `kb/docs/`。
- 模型 ID、话题号、具体触发时间和历史事故叙述属于易过期快照，不再常驻 agent 规则。

## Deviations

- 未修改已存在且有用户未提交内容的 `implementation-notes.md`，改用本任务专属文件。

## Tradeoffs

- `CLAUDE.md` 不再内嵌项目规则；依赖 agent 跟随其明确链接读取 `AGENTS.md`，换取单一维护入口。

## Open Questions

- None.
