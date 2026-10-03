# ChatDaily 重写规格

ChatDaily 把已有的微信、Telegram 消息及视频、文章订阅整理成可追溯的内容，投递到 Telegram，并保留本地归档、机会库和个人学习资料。

本目录是新实现的产品与验收规格，版本 **1.0，2026-09-27**。可以整体复制到新项目：实现者只需本目录、合成样例和接入阶段另行提供的配置与数据导出。规格描述目标行为；新程序、性能结果和生产切换尚未交付。

## 从哪里开始

1. 读 [产品范围](01-product.md)，明确要保留什么、分几步交付。
2. 读 [功能合同](02-capabilities.md) 和 [领域与投递状态](03-domain-and-delivery.md)，理解正常行为与失败行为。
3. 按 [外部接口](04-integrations.md)、[架构选择](05-architecture.md)、[操作界面](06-interfaces.md) 设计实现。
4. 用 [质量与性能](07-quality.md)、[迁移与回滚](08-migration.md)、[验收用例](09-acceptance.md) 判断是否可替换原系统。
5. 在 [决策登记](10-decisions.md) 中记录影响行为的取舍；[启动说明](IMPLEMENTATION_BRIEF.md) 可直接交给新项目的实现者。

## 规格怎么使用

“必须”约束结果和业务安全；“建议”是可替换的起点；“候选”需要在指定阶段形成决定。语言、框架、数据库产品、队列产品、模型品牌、部署主机和代码目录均可重新选择。变更技术选择无需保持旧实现形状，但必须通过关联的功能和失败用例。

[功能合同](02-capabilities.md) 为每项能力分配 F 编号。[验收用例](09-acceptance.md) 为观察结果分配 AT 编号。[覆盖表](contracts/coverage.json) 只保存两者的映射，不复制规则正文。[样例](contracts/examples.json) 全为合成数据，可用于编写测试；它不是未来 API 或数据库的固定结构。

复制本目录时保留 [LICENSE](LICENSE) 与 [第三方归属](NOTICE.md)。校验文件见 [MANIFEST.sha256](MANIFEST.sha256)。本目录没有旧实现代码、真实消息、账号、密钥或生产数据库。
