# htmlshare — AI 生成的 HTML 一键发布到内网，飞书登录按人授权

AI 工具（Claude Code、Cursor、Codex 等）生成 HTML 后，调用 `htmlshare` 的 CLI 或 MCP 把文件传到内网分享站，
得到 `https://<分享站>/<名字>.html` 链接。发布时指定谁能看；访问者打开链接先走飞书登录，在名单里才能看到内容。

```
AI 工具 ──(CLI / MCP)──▶ htmlshare 客户端 ──HTTPS + 个人 Token──▶ 分享服务
                                                              ├─ 存文件 /data/pages/<名字>.html
                                                              ├─ 存可见名单（open_id）
                                                              └─ 调飞书：邮箱/手机号 → open_id

浏览者打开 /<名字>.html → 未登录跳飞书授权 → 回调拿到 open_id → 在名单里就返回页面，否则 403
```

## 目录

| 路径 | 说明 |
| --- | --- |
| `server/` | 服务端（FastAPI + SQLite），飞书登录、存储、权限、API |
| `client/` | 客户端 Python 包 `htmlshare`：命令行 + MCP server（同一个包） |
| `Dockerfile` / `docker-compose.yml` | 部署；镜像里顺带打好客户端 wheel，供同事从分享站直接 `pip install` |

## 一、配置飞书应用（一次性）

1. 打开 [飞书开发者后台](https://open.feishu.cn/app) → **创建企业自建应用**，记下 App ID / App Secret。
2. **添加应用能力 → 网页应用**。
3. **安全设置 → 重定向 URL**：添加 `<HTMLSHARE_PUBLIC_URL>/auth/callback`（必须与配置完全一致）。
4. **权限管理** 开通：
   - `contact:user.id:readonly`：通过手机号或邮箱获取用户 ID（发布时把邮箱换成 open_id，必需）
   - `contact:contact.base:readonly`：获取通讯录基本信息（按部门授权用，调用部门/用户接口的前提）
   - `contact:department.base:readonly`：获取部门基础信息（按部门名授权）
   - `contact:user.department:readonly`：获取用户组织架构信息（判断访问者属于哪些部门）
   - `contact:user.email:readonly`：获取用户邮箱（可选，只用于页面上显示）
   - 具体名称以后台显示为准；缺哪个权限，接口报错里会直接给出申请链接
5. **通讯录权限范围**（数据权限）设为「全部成员」，否则范围外的人查不到。
6. **版本管理与发布**：可用范围选全员（或需要的部门），提交发布，等管理员审批。

> 上传和登录必须用**同一个应用**：飞书的 open_id 是按应用区分的，两边一致才能比对。

## 二、部署（内网）

```bash
cp .env.example .env    # 填 HTMLSHARE_PUBLIC_URL、HTMLSHARE_SECRET_KEY、FEISHU_APP_ID、FEISHU_APP_SECRET
docker compose up -d --build
docker compose logs -f   # 看到 Uvicorn running 即可
curl http://<服务器IP>:8080/healthz
```

网络要求：

- **服务器** 能访问 `open.feishu.cn:443`（用授权码换 token、查用户）。
- **用户浏览器** 能访问分享站域名和 `accounts.feishu.cn`。内网域名没问题，OAuth 跳转在浏览器里完成，飞书不需要能访问你的服务器。
- 在手机飞书里打开链接，需要手机连着内网或 VPN。
- 建议在前面挂 Nginx/Caddy 做 HTTPS（用内网 CA 或公网证书都行）。如果用纯 http，Cookie 不会带 Secure 标记。
  如果飞书后台不接受 http 的重定向 URL，就必须上 HTTPS。

数据都在 docker 卷 `htmlshare-data`（`/data`）：`htmlshare.db` 存元数据，`pages/` 存 HTML 文件。备份这个卷即可。

## 三、客户端安装与登录（每个用户一次）

```bash
pip install https://share.example.com/client/htmlshare-0.1.0-py3-none-any.whl
htmlshare login --server https://share.example.com
```

`login` 会打开浏览器：飞书登录 → 核对页面上的代码与终端一致 → 点「确认授权」。token 保存在 `~/.htmlshare/config.json`。

没法开浏览器的环境（远程服务器、CI）：在分享站首页点「生成 API Token」，然后设置环境变量
`HTMLSHARE_SERVER`、`HTMLSHARE_TOKEN`（优先级高于配置文件）。

## 四、接入 AI 工具

**能执行命令的工具（Claude Code、Codex、Gemini CLI……）** 直接用命令行就行。把下面这段放进项目的 `CLAUDE.md` / `AGENTS.md`：

```markdown
## 分享 HTML
生成 HTML 报告或页面后，如果用户想分享给同事或要一个链接，用 htmlshare 发布：
    htmlshare publish <文件绝对路径> --name <英文短名> --to <飞书邮箱,dept:部门名,... | all>
- 可见人用飞书邮箱、手机号或 dept:部门名（含子部门），all 表示全员；用户没说给谁看就先问，不要擅自用 all。
- 提示"同名部门"时，用提示里的完整路径重试（如 dept:技术中心/运维部）。
- 输出里有"注意：以下可见人没有加上"时，要把原因告诉用户。
- 修改可见人：htmlshare share <名字> --add a@x.com --remove b@x.com；查看我的页面：htmlshare ls
```

**MCP**（Claude Code、Claude Desktop、Cursor 等）：

```bash
claude mcp add htmlshare -- htmlshare mcp
```

```json
// Cursor / Claude Desktop 的 mcp 配置
{ "mcpServers": { "htmlshare": { "command": "htmlshare", "args": ["mcp"] } } }
```

MCP 提供的工具：

| 工具 | 作用 |
| --- | --- |
| `publish_html` | 发布：`file_path`（绝对路径）或 `html_content` 二选一，`name`、`viewers`、`expires_days` |
| `update_viewers` | 修改可见人：`add` / `remove` / `set_to`，也可改 `expires_days` |
| `list_my_pages` | 列出我发布的页面 |
| `delete_page` | 删除页面 |
| `whoami` | 当前登录身份 |

## 五、命令行

```bash
htmlshare publish report.html --name q3-report --to a@corp.com,13800000000 --expires 30
htmlshare publish report.html --name ops-weekly --to "dept:技术中心/运维部,dept:研发部"
htmlshare publish report.html --name q3-report          # 覆盖更新内容，可见人不变
htmlshare share q3-report --add c@corp.com --remove a@corp.com
htmlshare share q3-report --set all                      # 改成全员可见
htmlshare share q3-report --set ""                       # 改成仅自己可见
htmlshare ls
htmlshare rm q3-report
htmlshare whoami / logout
```

## 权限规则

- 可见人：飞书邮箱、手机号、`dept:部门名`、`all`（任何本企业登录用户），也可以直接填 open_id（`ou_...`）。
- 部门：`dept:运维部` 或 `部门:运维部`，**包含所有子部门**。有同名部门时会提示候选路径，
  改写成完整路径或路径后缀即可（`dept:技术中心/运维部`）；也可以直接填 `dept:od-xxxx`（open_department_id）。
  访问者的所属部门缓存 1 小时，部门树缓存 10 分钟；调岗后重新登录立即生效。
  只有按部门授权的页面才会去查访问者的部门，飞书接口出错时返回 503「暂时无法确认权限」，不会误放行。
- 发布者本人总能看。新页面不填可见人 = 仅自己可见；覆盖发布不填可见人 = 保留原名单。
- 查不到的人会在返回结果的 `unresolved` 里列出原因，页面照常发布（只是少了这些人）。
- 页面名全局唯一：同一个人再次发布同名 = 覆盖更新；名字被别人占用会返回 409。
- 默认 30 天过期（`expires_days=0` 永不过期），过期页面自动清理。

## 安全设计

- 上传的 HTML 可以包含任意脚本，所以返回页面时带 `Content-Security-Policy: sandbox allow-scripts ...`（不含 `allow-same-origin`）。
  页面运行在隔离的 opaque origin 里：脚本能正常跑，但读不到登录 Cookie，也调不了分享站的 API、读不了别的页面。
- sandbox 下 `localStorage` / `sessionStorage` / `document.cookie` 会抛异常。服务端会注入一个小垫片，把它们换成内存版，
  避免 AI 生成的页面因为没写 try/catch 而整段脚本报错（数据只在当前页面生命周期内有效）。
- 登录 Cookie：HttpOnly、SameSite=Lax、https 下加 Secure；OAuth 带 state 校验；`next` 参数只允许站内路径。
- API 只认 Bearer token（数据库只存 sha256），不认 Cookie；CLI 登录采用设备码流程，需要用户在浏览器里核对代码并确认。
- 访问记录写在 `access_log` 表（谁、什么时候、是否放行）。

## 本地开发

```bash
cd server && pip install -e .[dev] && pytest
cd client && pip install -e .[dev] && pytest      # 客户端测试会在进程内拉起服务端

# 不连飞书跑起来（登录页随便填邮箱；邮箱前缀 ops-/rd-/sales- 会被分到 技术中心/运维部、技术中心/研发部、销售部）：
HTMLSHARE_PUBLIC_URL=http://localhost:8780 HTMLSHARE_DEV_FAKE_LOGIN=1 HTMLSHARE_DATA_DIR=./data \
  uvicorn --factory htmlshare_server.app:create_app --port 8780 --app-dir server
```

## 后续计划

- ~~按部门授权~~（0.2.0 已完成）
- 按飞书群授权、按姓名查人（AI 往往只知道名字）
- 发布后用飞书机器人通知可见人
- 网页端管理（改可见人、删页面、Token 列表与撤销）
- 多文件页面（zip：HTML + 图片/JS）
