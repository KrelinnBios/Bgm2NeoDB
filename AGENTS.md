# AGENTS.md

本文件适用于整个仓库，作为 AI 编码代理修改本项目时的项目规范。除非用户另有要求，使用中文回复和说明。

## 项目状态

这是理解本仓库的最高优先级前提。

- Bgm2NeoDB 已进入稳定维护状态，功能和架构已基本定型。
- 本项目不再承担新功能开发、架构演进或依赖现代化任务。
- 后续维护限于缺陷修复、兼容性调整、API 适配、必要的文档修正和性能优化。
- 除非用户明确要求新增功能，否则默认把当前实现视为最终形态，不主动提出功能扩展、重构或"顺便优化"。

## 信息优先级

遇到描述冲突时按以下顺序判断：

1. 用户当前明确要求。
2. 当前仓库源码、测试和配置文件。
3. 本文件记录的实现约束和历史决定。
4. README 和文档等面向用户的说明文字。

## 项目概览

Bgm2NeoDB 是一个本地运行的 Python 网页工具，用于将 Bangumi 收藏迁移到 NeoDB 或兼容实例。项目采用 Flask 提供本地网页界面，使用 SQLite 保存迁移状态和映射关系。

**技术栈：**
- Python 3.11+（推荐 3.12）
- Flask（网页服务）
- aiohttp（异步 HTTP 客户端）
- SQLite（本地数据库）
- Playwright（浏览器界面测试）

**核心模块：**
- `main.py` - 程序入口，启动 Flask 服务
- `app/auth.py` - Bangumi 和 NeoDB 账号连接、凭证管理
- `app/bangumi.py` - Bangumi API 封装
- `app/neodb.py` - NeoDB API 封装
- `app/models.py` - 数据模型、映射和差异处理
- `app/database.py` - SQLite 数据持久化
- `app/migrator.py` - 迁移任务执行引擎
- `app/resolution.py` - 条目解析和匹配逻辑
- `app/routes.py` - Flask 路由和 API 端点
- `app/http.py` - HTTP 客户端和限流控制

## 开始任务前

修改代码前应先检查：

1. **依赖环境**：Python 3.11+ 是否已安装，虚拟环境是否已创建
2. **测试覆盖**：相关功能是否有自动化测试（`tests/` 目录）
3. **API 合约**：修改 Bangumi/NeoDB 交互前查看 `docs/API.md`
4. **已知设计**：查看下方"已确立的决定"章节，避免重复讨论已定的架构选择

## 修改原则

- **最小差异**：只修改必要的代码，不顺便重构或"清理"无关文件
- **优先标准库**：新增功能优先使用 Python 标准库，避免引入额外依赖
- **保持兼容性**：不破坏已有 API 端点和数据库结构，迁移记录向后兼容
- **测试先行**：修改核心逻辑前确认有相应测试覆盖，修改后运行测试验证
- **错误透明**：网络错误、API 失败和解析问题应保留具体原因，不用通用消息掩盖

## 验证

修改代码后运行以下检查：

```bash
# 代码质量检查
python -m ruff check app main.py tests
python -m ruff format --check app main.py tests

# 单元测试和集成测试
python -m pytest -q

# 浏览器界面测试（需要 Node.js 24 和 Playwright）
npm ci
npx playwright install chromium
npm run test:ui
```

所有测试使用模拟 HTTP 服务，不会访问真实 Bangumi 或 NeoDB API，也不会修改真实账号数据。

## 专项规范导航

本仓库内下列文件记录原来的项目特有约束；相关改动前必须读取对应专题。根规则适用于全仓库，目录级 `AGENTS.md` 仅补充局部入口。

- [凭证、服务运行、限流、数据库和迁移边界](docs/agent/operations.md)
- [本地部署、标题匹配与迁移的历史选择](docs/agent/decisions.md)

## 文档同步

修改以下内容时需要同步更新文档：

- **迁移规则**：更新 README.md "迁移规则详情"章节
- **API 交互**：更新 `docs/API.md`
- **解析逻辑**：更新 `docs/RESOLUTION.md`
- **数据库 schema**：在 `app/database.py` 注释中说明变更，提供迁移脚本

## 提交约定

- 提交消息使用中文，简明扼要说明改动内容
- 一次提交专注一个主题，不混合无关修改
- 修复 bug 时在消息中说明问题和修复方式
- 新增功能时说明功能目的，不只是列举改动文件
- 示例：
  - `修复 NeoDB 标签合并时的重复问题`
  - `支持从条目详情页按链接导入`
  - `优化解析超时处理逻辑`
