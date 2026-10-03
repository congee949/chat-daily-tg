# Jev 布尔判断（TypeSafe 直连）

Node.js 22+ 服务端示例通过 [TypeSafe HTTP API](https://docs.typesafe.ai/api)
调用 `jev-latest`。目录名保留以兼容现有命令，示例使用 Node 原生 fetch，无 SDK 依赖。

在 [TypeSafe 控制台](https://console.typesafe.ai/) 创建密钥，将以下变量合并到
`~/chat-daily/.env`，权限设为 600；参考根目录 [.env.example](../../.env.example)。

```dotenv
TYPESAFE_API_KEY=
TYPESAFE_BASE_URL=https://api.typesafe.ai/v1
```

Base URL 不含末尾的 `/systemone`，程序自动追加。当前仅接受官方地址，
不将凭据发送到其他主机。Vercel 的 API key 不适用于此接口。
环境变量优先于本地文件；密钥仅由服务端读取。

```bash
npm --prefix examples/jev-sdk test
npm --prefix examples/jev-sdk start
```

示例发送固定退款句子和 Noul 问题，将返回的 `noul` 与 0.5 比较生成
`decision`。单次请求不重试，30 秒超时；失败输出状态码，隐藏响应正文与凭据。
缺少密钥时返回 `missing_api_key`，退出码 1；真实成功返回 `status: ok`。
`npm test` 使用模拟 HTTP，不是线上回执。

本项目部署在 Mac launchd，真实密钥填写到 `~/chat-daily/.env`。
若另行在 Vercel 部署，在项目 Settings → Environment Variables 填入
`TYPESAFE_API_KEY` 和 `TYPESAFE_BASE_URL`，选择部署环境后重新部署。
不要使用 `NEXT_PUBLIC_` 或 `VITE_` 前缀。

原生请求不使用 Vercel 的 `providerOptions`；没有文档化的 ZDR 请求选项。
频道主裁判配置和回退规则见 [Jev 接入指南](../../docs/jev-dedup.md)。
