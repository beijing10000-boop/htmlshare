# htmlshare — AI 生成的 HTML 一键发布到内网，飞书登录按人授权

AI 工具（Claude Code、Cursor、Codex 等）生成 HTML 后，通过分享站提供的**远程 MCP**（免安装）或本地命令行把文件传到内网分享站，
得到 `https://<分享站>/<名字>.html` 链接。发布时指定谁能看；访问者打开链接先走飞书登录，在名单里才能看到内容。

```
AI 工具 ──远程 MCP（/mcp，Bearer token）──▶ 分享服务 ──▶ prepare_upload 返回一次性上传地址
   └──── 终端里 curl -T 本地 HTML 文件 ─────────────▶ /api/uploads/<一次性地址>
                                                    ├─ 存文件 /data/pages/<名字>.html
                                                    ├─ 存可见名单（open_id / 部门）
                                                    └─ 调飞书：邮箱/手机号/姓名/部门 → open_id

浏览者打开 /<名字>.html → 未登录跳飞书授权 → 回调拿到 open_id → 在名单里就返回页面，否则 403
```

## 目录

| 路径 | 说明 |
| --- | --- |
| `server/` | 服务端（FastAPI + SQLite）：飞书登录、存储、权限、REST API、远程 MCP |
| `client/` | 可选的本地客户端 Python 包 `htmlshare`：命令行 + 本地 MCP（给不支持远程 MCP 的工具用） |
| `Dockerfile` / `docker-compose.yml` | 部署；镜像里顺带打好客户端 wheel，供同事从分享站直接 `pip install` |

## 一、配置飞书应用（一次性）

1. 打开 [飞书开发者后台](https://open.feishu.cn/app) → **创建企业自建应用**，记下 App ID / App Secret。
2. **添加应用能力 → 网页应用**。
3. **安全设置 → 重定向 URL**：添加 `<HTMLSHARE_PUBLIC_URL>/auth/callback`（必须与配置完全一致）。
4. **权限管理** 开通：
   - `contact:user.id:readonly`：通过手机号或邮箱获取用户 ID（发布时把邮箱换成 open_id，必需）
   - `contact:contact.base:readonly`：获取通讯录基本信息（按部门授权用，调用部门/用户接口的前提）
   - `contact:department.base:readonly`：获取部门基础信息（按部门名授权）
   - `contact:department.organize:readonly`：获取部门组织架构信息（上级部门，用于"含子部门"和完整路径）
   - `contact:user.department:readonly`：获取用户组织架构信息（判断访问者属于哪些部门）
   - `contact:user.base:readonly`：获取用户基本信息（按姓名查人：同步通讯录时拿姓名、英文名、在职状态）
   - `contact:user.email:readonly`：获取用户邮箱（建议开：重名时靠邮箱区分；也用于页面上显示）
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
- 远程 MCP 只接受 Host 为 `HTMLSHARE_PUBLIC_URL` 主机名（或本机）的请求（DNS 重绑定防护），换域名时同步改这个配置。

数据都在 docker 卷 `htmlshare-data`（`/data`）：`htmlshare.db` 存元数据，`pages/` 存 HTML 文件。备份这个卷即可。

## 三、接入 AI 工具（免安装，推荐）

1. 浏览器打开分享站，用飞书登录，点 **「接入 AI 工具」**。
2. 页面会生成一段话（含分享站地址和一个专用凭证），**复制发给你的 AI 工具**（Claude Code、Cursor、Codex 等），它会自己完成配置并调用 `whoami` 确认。
3. 按 AI 的提示重启工具或新开会话，之后直接说「把这个报告发布出去，给张三和技术中心看」即可。

各工具的配置方式（那段话里已经写好，AI 会照做）：

```bash
# Claude Code
claude mcp add --transport http --scope user htmlshare http://<分享站>/mcp --header "Authorization: Bearer <凭证>"
```

```json
// Cursor：~/.cursor/mcp.json
{ "mcpServers": { "htmlshare": { "url": "http://<分享站>/mcp", "headers": { "Authorization": "Bearer <凭证>" } } } }
```

```toml
# OpenAI Codex：~/.codex/config.toml（命令行、IDE 插件、ChatGPT 桌面 App 里的 Codex 共用）
[mcp_servers.htmlshare]
url = "http://<分享站>/mcp"
http_headers = { "Authorization" = "Bearer <凭证>" }
```

> **ChatGPT 网页版 / 聊天里的连接器**连不上：它从 OpenAI 云端访问 MCP 服务，要求公网 HTTPS，认证只支持 OAuth 或无认证，
> 不支持固定 Bearer 凭证。要接入只能走 OpenAI 的 Secure MCP Tunnel 并改成 OAuth，目前没做；请用 Codex。

**发布本地文件怎么传上去**：服务端读不到你电脑上的文件，所以分两步——AI 先调 `prepare_upload` 拿到一个
**30 分钟有效、只能用一次、只能发布到指定页面名**的上传地址，再在终端执行 `curl -T 文件 地址`（Windows PowerShell 里是 `curl.exe`，
Windows 10 以上和 macOS 自带）。这和 S3 / R2 预签名 URL 是同一个思路：大文件不经过对话、不费 token，命令里也不出现长期凭证。
不能执行命令的工具才用 `publish_html` 直接传 HTML 内容。

远程 MCP 提供的工具：

| 工具 | 作用 |
| --- | --- |
| `prepare_upload` | 发布本地文件第一步：拿一次性上传地址（`name`、`viewers`、`expires_days`），返回现成的 curl 命令 |
| `publish_html` | 直接传 `html_content` 发布（不能执行命令时用） |
| `update_viewers` | 修改可见人：`add` / `remove` / `set_to`，也可改 `expires_days` |
| `find_user` | 按姓名/英文名/邮箱查同事（部门、邮箱、open_id），处理重名 |
| `list_my_pages` | 列出我发布的页面 |
| `list_shared_with_me` | 列出别人分享给我、我能打开的页面 |
| `delete_page` | 删除页面 |
| `whoami` | 当前凭证对应的身份 |

分享站首页也会同时列出「我发布的页面」和「分享给我的页面」（标明是直接分享、全员可见还是通过哪个部门）。
「我发布的页面」每行有「设置」：在网页上查看/增删可见人（输入时联想同事和部门，重名直接给候选按钮）、
一键全员可见或仅自己可见、改过期时间、删除页面。

凭证在分享站首页「我的接入凭证」里可以看到最近使用时间、随时撤销。

> **Claude Desktop** 的远程连接要经过 Anthropic 云端，访问不到内网，请用下面的命令行客户端（本地 MCP）。

## 四、命令行客户端（可选）

适合 Claude Desktop 等不支持远程 MCP 的工具，或者习惯命令行的人。需要 Python 3.10+：

```bash
pip install http://<分享站>/client/htmlshare-<版本>-py3-none-any.whl
htmlshare login --server http://<分享站>       # 浏览器飞书登录 → 核对代码 → 确认授权
claude mcp add --scope user htmlshare -- htmlshare mcp   # 本地 MCP（stdio）
```

```json
// Claude Desktop / Cursor 的本地 MCP 配置
{ "mcpServers": { "htmlshare": { "command": "htmlshare", "args": ["mcp"] } } }
```

没法开浏览器的环境（远程服务器、CI）：在分享站首页「命令行客户端」里生成凭证，然后设置环境变量
`HTMLSHARE_SERVER`、`HTMLSHARE_TOKEN`（优先级高于配置文件）。

只能执行命令、不支持 MCP 的工具，把下面这段放进项目的 `CLAUDE.md` / `AGENTS.md`：

```markdown
## 分享 HTML
生成 HTML 报告或页面后，如果用户想分享给同事或要一个链接，用 htmlshare 发布：
    htmlshare publish <文件绝对路径> --name <英文短名> --to <姓名,飞书邮箱,dept:部门名,... | all>
- 可见人可以直接写姓名，也可以用飞书邮箱、手机号或 dept:部门名（含子部门），all 表示全员；用户没说给谁看就先问，不要擅自用 all。
- 提示"有 N 个人叫 xx"时，从列出的部门/邮箱里判断是谁并改用邮箱；判断不了就问用户。查人：htmlshare whois <姓名>
- 提示"同名部门"时，用提示里的完整路径重试（如 dept:技术中心/运维部）。
- 输出里有"注意：以下可见人没有加上"时，要把原因告诉用户。
- 修改可见人：htmlshare share <名字> --add a@x.com --remove b@x.com；查看我的页面：htmlshare ls
```

常用命令：

```bash
htmlshare publish report.html --name q3-report --to a@corp.com,13800000000 --expires 30
htmlshare publish report.html --name ops-weekly --to "dept:技术中心/运维部,dept:研发部"
htmlshare publish report.html --name review --to "张三,Li Si"     # 按姓名（中文名或英文名）
htmlshare whois 张三                                               # 重名时查部门和邮箱
htmlshare publish report.html --name q3-report          # 覆盖更新内容，可见人不变
htmlshare share q3-report --add c@corp.com --remove a@corp.com
htmlshare share q3-report --set all                      # 改成全员可见
htmlshare share q3-report --set ""                       # 改成仅自己可见
htmlshare ls
htmlshare rm q3-report
htmlshare whoami / logout
```

## 权限规则

- 可见人：姓名、飞书邮箱、手机号、`dept:部门名`、`all`（任何本企业登录用户），也可以直接填 open_id（`ou_...`）。
- 姓名：中文名或英文名完全匹配（忽略空格和大小写，`Zhang San` = `zhangsan`），也可以写成 `name:张三`。
  重名时不会随便挑一个，而是在 `unresolved` 里列出每个人的部门、邮箱和 open_id，改用邮箱或 open_id 即可。
  姓名来自后台同步的通讯录快照：服务启动时（快照过期的话）同步一次，之后每 6 小时一次，全量约几十秒；
  查不到人时会提前触发一次同步（最多每 30 分钟一次），所以新入职的同事稍等一会儿或直接用邮箱。
  离职、未加入的人不会出现在通讯录里。
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
- API 和远程 MCP 只认 Bearer token（数据库只存 sha256），不认 Cookie；`/mcp` 没有有效凭证直接 401，不进 MCP 会话；
  另有 Host/Origin 校验防 DNS 重绑定。凭证可在首页逐个撤销，撤销后立即失效。
- 一次性上传地址：随机 192 位、只存哈希、30 分钟过期、只能用一次、绑定发布者和页面名；内容校验不通过时不作废地址，可以修好文件重传。
- CLI 登录采用设备码流程，需要用户在浏览器里核对代码并确认。
- 访问记录写在 `access_log` 表（谁、什么时候、是否放行）。
- 通讯录搜索（`/api/users/search`、`find_user`、`whois`）需要凭证，只返回在职员工的姓名、英文名、部门、邮箱、open_id，
  不返回手机号——和员工在飞书里本来就能看到的范围一致。

## 本地开发

```bash
cd server && pip install -e .[dev] && pytest     # 远程 MCP 测试会起真实 uvicorn + MCP HTTP 客户端
cd client && pip install -e .[dev] && pytest     # 客户端测试会在进程内拉起服务端

# 不连飞书跑起来（登录页随便填邮箱；邮箱前缀 ops-/rd-/sales- 会被分到 技术中心/运维部、技术中心/研发部、销售部）：
HTMLSHARE_PUBLIC_URL=http://localhost:8780 HTMLSHARE_DEV_FAKE_LOGIN=1 HTMLSHARE_DATA_DIR=./data \
  uvicorn --factory htmlshare_server.app:create_app --port 8780 --app-dir server
```

## 后续计划

- ~~按部门授权~~（0.2.0）、~~按姓名查人~~（0.3.0）、~~远程 MCP 免安装接入、凭证列表与撤销~~（0.4.0）、
  ~~分享给我的页面~~（0.5.0）、~~网页上配置访问范围~~（0.6.0）
- 按飞书群授权
- 发布后用飞书机器人通知可见人
- 多文件页面（zip：HTML + 图片/JS，沿用一次性上传地址 `curl -T site.zip`）
