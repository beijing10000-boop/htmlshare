# 更新记录

## 0.2.0

- 按部门授权：可见人支持 `dept:部门名` / `部门:部门名`，包含所有子部门；同名部门提示候选路径，
  支持完整路径或路径后缀（`dept:技术中心/运维部`）和 open_department_id。
- 访问按部门授权的页面时才查询访问者所属部门（缓存 1 小时，重新登录立即刷新）；部门树缓存 10 分钟，
  飞书接口失败时沿用旧缓存，没有缓存则返回 503，不会误放行。
- 可见人列表里的部门名可以带空格（`dept:Sales Team`）。
- CLI 帮助、MCP 工具说明、分享站首页补充部门写法。
- 数据库 schema 升到 v2（新增 `user_depts` 表，启动时自动创建，无需手工迁移）。

升级：飞书应用需新增权限 `contact:contact.base:readonly`、`contact:department.base:readonly`、
`contact:user.department:readonly` 并发布新版本；服务器上 `git pull && docker compose up -d --build`。
客户端新版：`pip install <分享站>/client/htmlshare-0.2.0-py3-none-any.whl`（0.1.0 客户端也能用，只是帮助文字里没有部门写法）。

## 0.1.0

- 首个版本：飞书网页登录、按人/全员授权、CLI + MCP 客户端、设备码登录、sandbox 隔离、Docker 部署。
