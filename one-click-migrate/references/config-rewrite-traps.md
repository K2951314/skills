# 配置重写陷阱：服务活着、页面却坏了（2026-11-16 实战）

> 本文是 `one-click-migrate` 的第二份换服务器参考，只管**配置阶段**；搬迁阶段
> （数据、SSH 免密、安全组、证书签发、停旧机）见 `references/server-migration-pitfalls.md`。
>
> 第二次真实换机：`211.159.174.204`（Ubuntu）→ `<新机公网IP>`（TencentOS 4，root 直连，
> SSH 别名 `腾讯`）。应用、服务、RPM 环境全部正常起来，**连续踩了 5 个「服务活着、
> 页面/功能坏掉」的坑**。
>
> **共同根因**：新机的 Caddyfile / `.env` 是**重写**的，而不是从旧机逐块拷贝。
> 重写出来的版本看着更简洁，实际静默丢了配置。**迁移时务必逐块 diff 旧配置。**
>
> ⚠️ **时效声明**：坑 2 / 坑 5 里关于「自签证书 + 为 SDK 单开纯 HTTP 站点」的描述，
> 是 2026-11-16 当时的过渡配置。2026-11-20 起现役机已换成有效证书链 + 统一跳 HTTPS，
> **协议选择一律按下方核对命令实测，不要照搬历史结论**。

这一类故障有个共同特征，值得单独记：**它们全都不出现在服务端信号里**。
`systemctl` 全绿、日志零 error、`--failed` 为空，但用户打开页面就是坏的。
所以下面每条都给了「真机侧核对」，接口测试和服务 active **看不见**它们。

### 坑 0：SSH 别名与部署目标（最先修）

- `~/.ssh/config` 里 `Host` 那一行**必须以 `Host` 开头**。写成裸的 `腾讯` 会让整个
  config 语法错误，**所有** `ssh` 命令一起失效。
- `IdentityFile` 要指向具体密钥文件，不能指向目录 `~/.ssh/`。
- 登录用户用 VNC 进去 `whoami` 确认，不要猜（本次新机是 `root`，旧机是 `ubuntu`）。
  root 直连后，命令里的 `sudo` 可以省掉。
- 新机首次 root push 裸仓库（属 `www-data`）会报 `dubious ownership`：
  服务器侧 `git config --system --add safe.directory <repo>`（app 目录同样加）。
- 新机必须装 git（`dnf install -y git`），否则 `post-receive` 钩子的 checkout 必失败。
- `deploy.sh` 整体跑在沙箱里可能被 SIGTERM，分步执行 push + 核对即可。

### 坑 1：`/zkadmin/*` 路由丢失 → 管理接口 404

- 旧 Caddyfile 有 `@zkadmin path /zkadmin /zkadmin/*` 反代 8318，新机漏了。
- `console.js` 会把 `/admin/xxx` 改写成 `/zkadmin/xxx`，请求落到业务后端 → 404。
- **核对**：`curl -H 'X-Admin-Token: <token>' https://<新IP>/zkadmin/providers` 应 200。

### 坑 2：HTTPS 证书与 `default_sni` → WebCrypto 全废

- 报错形态：`管理员数据包失败：WebCrypto API is not available`。
- `crypto.subtle` 按 W3C 只在高安全上下文开放，**明文 http 必禁**。管理员加密包
  （AES-GCM + PBKDF2）依赖它。
- 浏览器访问**纯 IP 的 HTTPS 时不发 SNI**（RFC 规定 IP 不能作 SNI），Caddy 匹配不到
  证书就报 TLS `internal error`。旧 Caddyfile 顶部有 `default_sni <IP>`，新机漏了。
- 当时（tls internal 自签方案下）站点写法必须**唯一**：`https://<IP> { tls internal }`。
  不要写成 `http://IP, https://IP` ——双监听会让 internal issuer 失效。现役机已不用
  `tls internal`，这条只用于解释当时为什么会坏。
- 自签 CA 要导入客户端信任：服务器
  `/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt` →
  Windows `certutil -addstore -user Root` 或 `X509Store("Root","CurrentUser").Add()`。
  **每台要访问 admin 的电脑都要导**，否则每次弹警告。
- ⚠️ **以下为 2026-11-16 过渡态的旧做法，不代表现役配置**：当时用自签证书，
  Codex / Claude Code 不信任它，`/zkai/v1/*` 曾另开一个**不跳转的纯 HTTP 站点**。
  2026-11-20 起换成有效证书链后，现役入口是 `https://` + 308 统一跳转，
  **不要再按「必须开纯 HTTP 站点」配置**。客户端若报证书错误，先确认该客户端的
  CA 存储（curl 用系统 CA、Python requests 用 certifi、Node 用内置 CA）是否已信任
  证书链，再决定是否导入 CA。
- **核对（浏览器）**：`window.isSecureContext === true` 且 `typeof crypto.subtle === 'object'`。
- **核对（入口协议，2026-11-23 实测）**：
  `curl -s -m 8 -o /dev/null -w "%{http_code}\n" https://<当前公网IP>/zkai/health`
  应为 200；`/v1/models` 应返回 401（鉴权生效）。若 `http://` 返回 308，
  说明站点统一跳 HTTPS，SDK 的 base_url 必须写 `https://`。

### 坑 3：`/admin/*` 按 `Sec-Fetch-Mode` 误分流 → CSS/JS 全 404

- 新机 Caddyfile 曾用 `@zk_console_xhr2 header Sec-Fetch-Mode no-cors` 把请求转给
  网关 8318。但**浏览器加载 CSS/JS 静态资源恰好就是 `no-cors`**，于是所有
  `/admin/styles/*.css`、`/admin/lib/*.js` 全被错误转发 → 404（页面裸奔无样式）。
- 正解（对齐旧机）：网关用独立 `/zkadmin/*`，`/admin/*` 完整归属业务后端，
  **不要按 `Sec-Fetch-Mode` 分流**。
- **核对**：浏览器打开 admin，`document.styleSheets.length` 应等于引用数、零 404；
  同一路径在 8001 是 200、在 8318 是 404 就说明分流错了。

### 坑 4：明文 http 下 `navigator.clipboard` undefined → 复制按钮 TypeError

- 与坑 2 同源（非安全上下文），但独立成一个坑：直接 `navigator.clipboard.writeText(...)`
  会**同步**抛 `Cannot read properties of undefined`，`.catch` 接不住。
- 修法：先判 `clipboard && isSecureContext`，否则回退 `document.execCommand('copy')`。
  新增复制功能一律走这个 helper，不要直接调 clipboard API。

### 坑 5：`.env` 里的对外地址被一起带过去 → 面板显示旧 IP / SDK 报 308

- 网关 `.env` 的 `ZKAI_PUBLIC_BASE_URL` 迁移时留着旧 IP，「客户端接入」面板就一直
  显示旧地址。
- 改它**必须重启网关**：`deploy` 段不参与配置热加载，文件监听器也不看 `.env`。
- 公网 API 客户端一律配 `https://`（见坑 2 的现役口径）。若配了 `http://`
  而 Caddy 统一 308 跳转，OpenAI 兼容 SDK 会报 `API Error: 308 upstream_error`。
  控制台 `/ui` 等仍可强制跳 HTTPS。
- **核对**：`curl https://<新IP>/zkai/v1/models` 返回 401（不是 000 / 证书错误）；
  客户端接入面板显示新 IP。

### 通用：迁移配置核对四件套

1. **按块 diff Caddyfile**（不是 diff 应用代码）：站点块、`default_sni`、每个 `@` 命名
   匹配、每条 `handle` 的反代目标，逐条对照旧机。**本次 5 个坑里 3 个的根因都在这里。**
2. **按键 diff `.env`**：所有含 IP/域名/URL 的变量（`*_BASE_URL`、`APP_URL`、
   `ALLOW_ORIGINS`、`ZKAI_PUBLIC_*`），旧 IP 一律改成新 IP 或域名。
3. **真机浏览器验收**：`isSecureContext` / `crypto.subtle` / 样式表数量 / 零 404 /
   零 JS 错误。接口测试与服务 active **看不见**上面任何一类故障。
4. **备份留痕**：每次改 Caddyfile / `.env` 都 `cp` 带时间戳备份；改完 `validate`
   再 reload；失败能一键回滚。

### 地址会变：别把现役 IP 抄进文档

换机后确认的口径：**现役公网地址只写一次具体值**——在仓库的
`zk-ai-gateway/SKILL.md` 第零步（单独拷走本技能时，把这一处换成你自己维护的
网关地址配置）。本仓库其它文档一律用 `<当前公网IP>` 占位或 `ZKAI_BASE_URL`，
不重复抄；本文正文里的旧 IP 是历史记录，保留但标注已停用。

| 位置 | 内容 | 换机时改哪 |
|---|---|---|
| `~/.ssh/config` | `Host <别名>` / `HostName <IP>` / `User` / `IdentityFile` | 改这里，其它命令引用别名 |
| `scripts/deploy.conf`（属 SQ_HOME 项目，不在本仓库） | `SQ_DEPLOY_TARGET=<别名>` | 通常不用改（跟着别名走） |
| `zk-ai-gateway/SKILL.md` 第零步 | 唯一一处现役 IP | 改这一行；改完其它文档不用动 |
| 技能/SDK 配置 | `*_BASE_URL` 类环境变量 | 优先改环境变量，不改代码 |

历次地址：`120.53.28.29`（已停用）→ `211.159.174.204`（已停用）→
现役地址见 `zk-ai-gateway/SKILL.md`。前两个 `curl` 直接返回 `000`（连不上）
——这也是判断「是不是旧地址」最快的办法。