# 更新记录

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
