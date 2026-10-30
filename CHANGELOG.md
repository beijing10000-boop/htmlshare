# 更新记录

## 0.5.1

- 「接入 AI 工具」生成的说明加上 OpenAI Codex（`~/.codex/config.toml` 的 `url` + `http_headers`，命令行、IDE 插件、
  ChatGPT 桌面 App 里的 Codex 共用），并让 ChatGPT 网页版收到时直接告诉用户连不上内网、改用 Codex 或 Claude Code
  （ChatGPT 连接器从 OpenAI 云端访问，要求公网 HTTPS 和 OAuth，不支持固定 Bearer 凭证）。

## 0.5.0

- 首页新增「分享给我的页面」：列出别人发布、我能打开的页面（直接分享给我 / 全员可见 / 授权给我所在部门及其上级），
  显示发布者、能看到的原因、更新和过期时间。判断规则与打开页面时完全一致；没有按部门授权的页面时不查飞书，
  查部门失败时其余页面照常列出并提示。原「我分享的页面」改名「我发布的页面」。
- 远程 MCP 新增工具 `list_shared_with_me`；REST 新增 `GET /api/shared-pages`。
- 发布者名字：没在浏览器登录过的发布者（只用凭证），改用通讯录里的姓名显示（列表、403 提示页）。

升级：`git pull && docker compose up -d --build`；无需新增飞书权限，数据库结构不变。

## 0.4.0

- **远程 MCP（免安装）**：服务端在 `/mcp` 提供 Streamable HTTP MCP（无状态、JSON 响应），AI 工具直接连，
  同事电脑上不用装 Python 或任何软件。认证用 Bearer 凭证；没有有效凭证直接 401；Host/Origin 校验防 DNS 重绑定。
- **一次性上传地址**：`prepare_upload` 返回 30 分钟有效、只能用一次、绑定页面名的上传地址和现成的 curl 命令，
  AI 在终端 `curl -T 文件 地址` 上传（`PUT /api/uploads/<地址>`）；大文件不经过对话。不能执行命令时用 `publish_html` 直传内容。
- **网页「接入 AI 工具」**：一键生成专用凭证和一段可直接复制给 AI 的话（含 Claude Code / Cursor 配置方式）。
- **我的接入凭证**：首页列出全部凭证（名称、创建时间、最近使用），可逐个撤销。
- 内部：页面业务逻辑抽到 `service.py`，REST、远程 MCP、一次性上传共用。schema v4：新增 `upload_tickets` 表（自动创建）。
- 服务端新增依赖 `mcp==2.3.0`。客户端无功能变化。

升级：服务器上 `git pull && docker compose up -d --build`；飞书应用无需新增权限。

## 0.3.0

- 按姓名查人：可见人可以直接写姓名（中文名或英文名，忽略空格大小写），也可写 `name:张三`；
  重名时在 `unresolved` 里列出每个人的部门、邮箱、open_id，不会随便挑一个。
- 通讯录快照：后台线程按部门并发拉取成员（4 线程，失败重试；同步失败不覆盖旧快照），
  启动时快照过期就同步，之后每 6 小时一次；查不到人时提前同步（最多每 30 分钟一次）。
- 新接口 `GET /api/users/search?q=`，客户端新增 `htmlshare whois` 命令和 MCP `find_user` 工具。
- 用邮箱/open_id 加的可见人，若通讯录里有此人，显示成"张三 <zhangsan@x.com>"；移除可见人支持写姓名。
- 可见人列表里英文名可以带空格（`John Smith`）；"a@x.com b@x.com" 仍按空格拆开。
- schema v3：新增 `directory`、`meta` 表（自动创建）。

升级：飞书应用新增 `contact:user.base:readonly`（必需）和 `contact:user.email:readonly`（建议）并发布新版本；
服务器上 `git pull && docker compose up -d --build`，启动后约 1 分钟完成首次通讯录同步（看日志"通讯录同步完成"）。

## 0.2.0

- 按部门授权：可见人支持 `dept:部门名` / `部门:部门名`，包含所有子部门；同名部门提示候选路径，
  支持完整路径或路径后缀（`dept:技术中心/运维部`）和 open_department_id。
- 访问按部门授权的页面时才查询访问者所属部门（缓存 1 小时，重新登录立即刷新）；部门树缓存 10 分钟，
  飞书接口失败时沿用旧缓存，没有缓存则返回 503，不会误放行。
- 可见人列表里的部门名可以带空格（`dept:Sales Team`）。
- CLI 帮助、MCP 工具说明、分享站首页补充部门写法。
- 数据库 schema 升到 v2（新增 `user_depts` 表，启动时自动创建，无需手工迁移）。

升级：飞书应用需新增权限 `contact:contact.base:readonly`、`contact:department.base:readonly`、
`contact:department.organize:readonly`（缺它拿不到上级部门，服务端会明确报错而不是当成顶级部门）、
`contact:user.department:readonly` 并发布新版本；服务器上 `git pull && docker compose up -d --build`。
客户端新版：`pip install <分享站>/client/htmlshare-0.2.0-py3-none-any.whl`（0.1.0 客户端也能用，只是帮助文字里没有部门写法）。

## 0.1.0

- 首个版本：飞书网页登录、按人/全员授权、CLI + MCP 客户端、设备码登录、sandbox 隔离、Docker 部署。
