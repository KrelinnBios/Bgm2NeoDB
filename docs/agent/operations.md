# 凭证、服务运行、限流、数据库和迁移边界

此文件是项目内部的专项维护规则。根目录 [AGENTS.md](../../AGENTS.md) 仍然适用。

## 关键约束

### 凭证管理

- Bangumi 使用官方个人授权凭证（Access Token）
- NeoDB 使用动态注册的 OAuth 应用
- 凭证优先保存在操作系统凭证库（Windows Credential Manager / macOS Keychain / Linux Secret Service）
- 凭证库不可用时只保存在内存，程序重启后需要重新连接
- **禁止**将凭证写入数据库、备份文件、日志或任何持久化存储
- **禁止**在日志中输出完整的 Authorization 头或 access_token

### 本地运行

- 服务固定监听 `127.0.0.1:8765`，不接受外部连接
- OAuth 回调使用 `http://127.0.0.1:8765/callback`
- 请求必须校验 Host、Referer 和 CSRF 令牌
- 程序不会将用户数据上传到项目维护者或其他无关的远程服务器；与 Bangumi、NeoDB 的必要接口请求除外

### 限流与礼节

- Bangumi API 请求间隔至少 50ms
- NeoDB 普通请求间隔至少 50ms
- NeoDB 抓取请求间隔至少 3 秒
- 收到 429 或 503 时，遵守 `Retry-After` 头，默认至少等待 15 秒
- 同一来源的抓取请求，收到"正在抓取"响应后至少等待 15 秒再查
- 并发限制：解析最多 6 个，写入最多 10 个，HTTP 连接池最多 64 个连接

### 数据库

- 使用 SQLite 保存迁移状态、条目映射和人工确认
- 数据库文件：`data/migration.db`
- 不同账号和 NeoDB 实例的组合使用独立的 profile 隔离
- 数据库 schema 变更必须提供迁移脚本，保持向后兼容

### 迁移规则

- Bangumi 的"搁置"状态迁移为 NeoDB "在看"，但不导入评分
- 评分范围 1-10，0 表示未评分
- 短评非空时迁移，空短评保留 NeoDB 原文
- 标签去重后与 NeoDB 现有标签合并，按不区分大小写识别同名标签；同名时始终保留 NeoDB 已有标签的名称，只有 NeoDB 不存在对应标签时才使用 Bangumi 标签名称
- 隐私设置：Bangumi 私密→NeoDB 私密，不降低已有隐私级别
- 不自动向联邦时间线发布迁移动态
