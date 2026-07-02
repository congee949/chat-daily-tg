---
title: Raycast Dictionary AI 加成
status: draft
date: 2026-07-02
---

# Raycast Dictionary AI 加成

## 背景 / 问题

用户在 Raycast 中使用官方 Dictionary 插件查词，该插件基于系统本地词典（Apple Dictionary），
对一些冷门单词、词组搭配、俚语、专业术语经常查不到，体验不佳。用户希望增加 AI 加成来补足查词能力。

## 环境约束

- 使用的是 Raycast 官方 Dictionary 插件（非第三方词典插件）
- 安装的是 Raycast app 的 beta 发布通道，与 Raycast Pro/AI 订阅无关
- 未订阅 Raycast Pro，因此无法直接使用官方 Raycast AI / AI Extensions 机制
- 愿意自带 LLM API Key（如 OpenAI）来驱动 AI 查词

## 方案

分两阶段推进，先以最小成本验证现成方案是否够用，不够用再决定是否自建。

### 阶段一：安装并试用 Easydict（本轮要做的）

- 从 Raycast Store 安装开源插件 [Easydict](https://github.com/tisfeng/Raycast-Easydict)
- 在插件设置中配置自己的 OpenAI（或其他 Easydict 支持的）API Key
- 用 Easydict 替代/补充官方 Dictionary 的查词入口，重点测试官方 Dictionary 查不到的词组、俚语、专业术语场景

**验收标准**：
连续实测 5-10 个「官方 Dictionary 查不到」的真实案例，Easydict + AI 后端给出可用释义的比例 ≥ 80%，
且响应速度、UI 呈现方式用户能接受。

### 阶段二（备选，仅当阶段一未达标时触发）：自建轻量 Raycast 命令

- 不修改/fork 官方 Dictionary 插件（维护成本高，官方更新可能随时覆盖自定义改动）
- 新建独立的 TypeScript Raycast 命令，例如 "AI Dictionary Fallback"：
  输入词/词组 → 调用用户自己的 API Key（OpenAI/Claude 等）→ 返回释义 + 例句
- 待细化的定制点（阶段二启动时再单独过一轮设计）：
  - 提示词风格（是否需要词源、例句、中英双解）
  - 结果展示格式
  - 是否需要与官方 Dictionary 结果拼接展示

此阶段目前只明确方向，不作为本次交付范围。

## 不做的事

- 不修改/fork 官方 Dictionary 插件源码
- 不依赖 Raycast Pro / 官方 AI Extensions 机制（用户未订阅）
- 阶段一不涉及任何代码开发

## 开放问题

- 阶段二的具体交互设计（提示词、展示格式）留待阶段一验收后再确定
