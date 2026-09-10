# 接口依据与兼容性

核实日期：2026-09-08。开发时先下载官方定义并阅读对应源码，实际个人账号请求仍需用户自行连接后验证。

## Bangumi

- [官方 OpenAPI v0](https://github.com/bangumi/api/blob/master/open-api/v0.yaml)，快照为本目录 `bangumi-v0.yaml`。
- [官方授权说明](https://github.com/bangumi/api/blob/master/docs-raw/How-to-Auth.md)。授权码交换明确要求 `client_secret`；未找到已文档化、可用于无共享密钥本地客户端的 PKCE 流程。因此采用需求中允许的个人凭证降级方式。
- [官方凭证入口来源](https://github.com/bangumi/api/blob/master/open-api/api.yml)：`https://next.bgm.tv/demo/access-token`。
- [User-Agent 要求](https://github.com/bangumi/api/blob/master/docs-raw/user%20agent.md)。当前包含仓库维护者 `KrelinnBios`、应用名与版本、项目地址，以及 Codex 开发标识。

实际调用：`GET /v0/me` 自动识别当前用户，`GET /v0/users/{username}/collections?limit=50&offset=...` 分页读取。分页上限为 50。每页字段会保存到本地数据库，重复条目或不完整分页会阻止替换当前扫描记录。

官方 `UserSubjectCollection.updated_at` 不等于可靠的首次收藏时间。当前由“导入收藏日期”开关控制：开启时，新收藏将其写入 NeoDB `created_time`，界面明确提示日期可能不准确；关闭时不导入。NeoDB 原有的收藏日期始终保留。

## NeoDB

- [官方 OAuth 文档](https://neodb.net/api/)。使用 `POST /api/v1/apps`、`GET /oauth/authorize`、`POST /oauth/token`、`GET /api/me`。
- [兼容的应用注册字段](https://docs.joinmastodon.org/methods/apps/)：`client_name`、`redirect_uris`、`scopes`；权限为 `read write`。
- [目标实例开发页](https://neodb.social/developer/) 暴露 [OpenAPI 定义](https://neodb.social/api/openapi.json)，快照为本目录 `neodb-openapi.json`。核实的实例版本是 `0.18.1-d79b305-20260906195616`。
- [官方 catalog 实现](https://github.com/neodb-social/neodb/blob/main/neodb/catalog/apis.py)。`GET /api/catalog/fetch` 的 302 指向作品 API（而不是假设为 HTML 页面）；202 表示后台抓取，每次至少等待 15 秒，约 120 秒停止；422 是不支持的来源。已登录用户的 catalog/fetch 限流锁约 3 秒，触发时返回 429。
- 本工具没有直接创建 NeoDB 条目的接口调用，也不会提交自定义标题、类型或简介来创建条目；外部链接只作为 `catalog/fetch` 的参数交给目标实例处理。目标实例是否能抓取或创建，取决于它支持的来源和自身权限。
- [官方 shelf 实现](https://github.com/neodb-social/neodb/blob/main/neodb/journal/apis/shelf.py)。`GET /api/me/shelf/item/{item_uuid}` 读取当前收藏；同路径 POST 一次写入 `shelf_type`、`visibility`、`rating_grade`、`comment_text`、`tags`，而不是虚构多个独立评分／短评接口。**省略评分、短评或标签会清空它们**，因此显式回填应保留的现有值。
- [官方可见性定义](https://github.com/neodb-social/neodb/blob/main/neodb/journal/models/common.py)：0 公开、1 关注者、2 私密；本工具不降低已有可见性，且 `post_to_fediverse=false`。
- [官方进度模型](https://github.com/neodb-social/neodb/blob/main/neodb/journal/models/mark.py)：虽然存在独立进度接口，但允许的进度类型由实际 catalog item 决定。当前版本不猜测条目对应关系，进度仅存档。

连接及每次任务开始时读取目标实例当前 OpenAPI，确认必要写入字段与可见性范围仍可用。若结构不兼容，阻止迁移；不会猜测替代字段。

GET 同实例跳转可跟随，最多 8 次。POST 的 307/308 先校验地址，再解析合并后的作品并回读状态，确认仍符合预览才重新写入；不会自动将原 payload 不加检查地覆盖到合并后的收藏。跨实例、降级到 HTTP、带用户信息的跳转均拒绝。

手工“按链接导入”使用同一个 `GET /api/catalog/fetch?url=...` 接口，接受 NeoDB 条目链接及实例支持的外部作品来源链接。302 跟随至同实例作品 API；202/429 表示后台抓取，界面先返回抓取中的状态，并在至少 15 秒后轮询 `/api/map/{sid}/pending`；429 同时尊重 `Retry-After`。页面从提交链接起最多等待 60 秒，并显示剩余秒数；到时停止等待和轮询，不启动迁移。超时或解析失败会保留具体原因及输入链接，提示用户在 NeoDB 创建条目后粘贴链接。已提交给 NeoDB 的后台抓取不受页面超时取消控制。404/422 明确失败，401/403 要求重新授权；失败不保存新映射。外链作为参数发送，不改变 HTTP 客户端请求目标或放宽跳转检查。详情页提供按 Bangumi 类型跳转到 NeoDB 创建页面的按钮，但本工具不会代替 NeoDB 提交创建表单。

## 并发与验证边界

每个程序单进程、单任务；网络请求受限并发执行（解析最多 6、写入最多 10），且按全局请求间隔限速。数据库事务保证完整扫描切换的原子性。POST 发出前持久记录 `writing`，成功后回读确认才记 `migrated`。

服务端没有暴露可用的条件写入版本号，因此“回读当前值 → POST”之间仍存在很短的竞争窗口。迁移过程中请避免同时编辑同一条目的 NeoDB 收藏。程序不声称提供服务端原子比较并交换。

测试覆盖映射、隐私、合并跳转、限流、凭证失效、断点恢复、已提交但断连的请求、回读不一致、跨账号隔离和本地页面防跨站请求。无真实账号数据被用于这些测试。
