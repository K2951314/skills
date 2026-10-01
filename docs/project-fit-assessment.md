# 三个项目的适用性评估（独立调研 + workflow 审计结论，待用户决策）

结论先行：**这个技能现在不够完善，不能直接替换三个项目的换机模块**。引擎骨架是对的（manifest 驱动 + HMAC 先验后解 + 成员预检 + 事务导入，比三个项目的旧实现都扎实），但经实测有 **18 条缺陷**（6 critical / 9 high / 3 medium），其中 4 条会直接导致密钥泄露、2 条会逼用户删掉正在用的配置。缺口不补就换，会丢东西。

> 缺陷明细见 `audit-findings-2026-10-01.md`（每条有文件行号 + 本机复现输出）。本文聚焦「三个项目的适用性」与「能力缺口」。

---

## 一、智能询价 —— 覆盖面最高，但有 2 个真缺口 + 3 个 bug 会咬人

### 它自己的换机模块要搬什么

| 旧实现 | 文件 | 引擎现状 |
|---|---|---|
| 打包本机资产 | `scripts/pack_workspace.sh` | ✅ 覆盖（`migrate export`） |
| 拉服务器资产 | `scripts/pull_from_server.sh` | ✅ 覆盖（`migrate server export`） |
| 加密容器 | openssl AES-256-CBC + tar.gz | ✅ 覆盖且更强（HMAC 先验后解） |
| 加密包判型 | `migrate.py:detect_package_kind` | ✅ 覆盖且更可靠（manifest.kind，不靠文件名猜） |
| 解包+合并进仓库 | `migrate.py:cmd_merge` | ⚠️ 语义不同，见缺口 1 |
| 环境变量键名审计 | `scripts/env_audit.py` | ⚠️ 部分覆盖，见缺口 2 |
| 统一场景入口 | `scripts/migrate.sh`（--machine/--server/--both） | ⚠️ 无等价物，但 SKILL.md 用表格替代，可接受 |
| Windows bash/openssl 探测 | `migrate.py:find_bash` / `find_openssl` | ✅ 不需要了（引擎零外部依赖） |

### 缺口 1：`cmd_merge` 的「白名单外也搬」语义

旧 `cmd_merge` 的 `RELATIVE_TOP` 是 `.env / .env.server / keys / quotation.db / deliverables / _archive / backups / backups-latest / restore_workspace` 加所有 `*.md`。

引擎的 `plan_restore` 相反：**不在 `merge.include` 白名单里的一律 `blocked`**（[unpack.py:174-177](../../src/migrate_engine/unpack.py#L174-L177)）。白名单漏一个路径，那个文件静默不恢复，而且 `plan` 阶段只显示 `blocked / 不在 manifest 的 merge.include 白名单`。

`examples/smart-quotation/manifest.toml` 的白名单是 `[".env", "keys", "quotation.db", "deliverables"]`——比旧脚本少了 `.env.server`、`_archive`、`backups`、所有根目录 `_*.md`。照这个示例导入，`_HANDOFF-*.md`、`_MIGRATION-GUIDE.md`、`_LOCAL-GUIDE.md` 全丢。这些文件是用户特意写「丢了要重新踩坑」的运维记录。

**建议**：白名单改成显式列出 + 一个「白名单外文件」的汇总行（列名字，不静默），导入时必须让用户看见「这 7 个文件被拦了」。

### 缺口 2：`env_audit` 只读单个 `.env`，旧实现读两源

引擎 `audit()` 的签名是 `env_files: list[str] | None = None` 但 `cli.cmd_export` 从不传（[cli.py:288](../../src/migrate_engine/cli.py#L288)），实际只读 `.env`。

智能询价的真实状态是 `.env`（本地）+ `.env.server`（部署配置，24 键）+ 服务器 `/etc/sq.env`（33 键）。**24 键 vs 33 键的差集就是「重建服务器时会丢什么」**。旧 `_audit_env_after_export` 会在导了 server 包之后核对 `_换机/server-keynames.txt`，明确说「这些键只在服务器上，只换电脑不影响，重建服务器必须再导 server 包」。

引擎没有这个核对。后果正是 `scripts/env_audit.py` docstring 里写的静默丢失链：
```
换电脑 → workspace 包（24 键，缺 14）→ 新机器 make_server_env.py 重建
→ 生成的 .env.server 仍缺这 14 个 → 收款码消失、订阅通知静默失效
→ 「联系管理员」入口隐藏。每一步都不报错。
```

**建议**：`env_audit` 支持 `env_files = [".env", ".env.server"]`；`server export` 之后核对 server 包里 env 文件的键名集合（引擎已经拿到了，`capture_server` 有原文，只需导出键名）。

### 会咬人的 bug（详见 `audit-findings-2026-10-01.md`）

- **A（critical）**：`artifacts_dir = "_换机"` 不在跳过集合，`_换机/` 里的 `sq-server-*.enc`、`sq-workspace-*.enc` 会被打进新包 → 包里有包。
- **B（high）**：`.env.server` 若带 `\r\r\n` 病害，原样运到新机器。
- **C（high）**：`keys/*.pem` 导入后 0o666。部署机上 = 公开私钥。

### 真实数据规模

`_换机/` 已有 8.5MB 的两个 `.enc`。包内会有：`quotation.db`（1.6MB sqlite）、`keys/`、`deliverables/`（2.7MB）、`_archive/`、`backups/`（58MB，旧脚本只取最新一份 27MB）。引擎无「只取最新一份」的机制，manifest 只能写 `path = "backups/legacy-export-20260920-094939.json"` 逐一点名。这是可用的，但没人会想到要这么写——**文档该给这个模式**。

---

## 二、ZK-AI —— 缺口最大，现在换过去一定丢功能

ZK-AI 的 `scripts/migrate.py`（39.7KB）比引擎的整个 `pack.py` 关心的事情多得多。逐条对：

| ZK-AI 能力 | 引擎 | 不补的后果 |
|---|---|---|
| `.env` 行尾 `\r\r\n` 病害检查（导出+导入双侧，`_check_env_health`） | ❌ 无 | 新机 `socket.bind` 报 `getaddrinfo failed`（2026-09-24 真实事故） |
| 凭据环境变量交叉核对（`_check_credential_envs`） | ❌ 无 | 见下 |
| 导入后手术式写入 `~/.codex/config.toml`（`_apply_chatgpt_client`） | ❌ 无 | ChatGPT 桌面版打开连不到网关 |
| 导入后写用户级环境变量 `HKCU\Environment`（`_provision_chatgpt_env`） | ❌ 无 | explorer 启动的桌面版报 `Missing environment variable` |
| 数据库 + WAL sidecar 一致性处理 | ✅ 有，实现正确 | — |
| sidecar 不发货、导入前清 | ✅ 有 | — |
| 冲突先备份再覆盖 | ✅ 有 | — |
| 加密 + 完整性校验 | ✅ 有且更强 | — |
| `ZKAI-MIGRATE` 旧包导入 | ❌ 无（legacy 只认 `Salted__`） | 已有的 `zkaie-machine-*.zip` 解不开 |

### 「凭据环境变量交叉核对」是这个项目最关键的能力

`.env` 只带 36 个键，但凭据名来自 `config/providers.yaml` 的 `env:` 引用和 `config/burner.yaml` 的 `only:` 列表。名字只存在于 OS 环境变量（`HKCU\Environment` 或 Machine）时：

> 源机器正常，新机器那个凭据 DISABLED，**且没有任何地方说为什么以前能用**。

2026-09-29 真实踩到：`SENSENOVA_API_KEY`（`sensenova-01`，priority 100，account-A 的 key）设在用户级、不在 `.env` 里，而 `.env` 只有 `SENSENOVA_API_KEY_02` 到 `_10`。换一次机，网关最重要的凭据静默消失。

引擎的 `env_audit` 完全扫不到这类消费者（它只扫 Python 源码里的 `os.environ.get`）。

**建议**：新增 `[env_audit] consumers` 声明式扫描器（YAML 的 `env:` 引用、`only:` 列表、JSON 的 `$VAR`、sh 脚本的 `${VAR}`），把「代码/YAML 引用了但 `.env` 和 env_files 都没配」报出来。

### 包导入后要动项目外的东西

`~/.codex/config.toml` 和 `HKCU\Environment` 都在项目目录外。引擎的整个设计假设「导入 = 往项目里写文件」，没有 post-import 钩子，也没有「写项目外路径」的安全模型。

**建议**：manifest 增加 `[hooks] post_import`，声明式列出可执行动作（如 `write_user_env_var`、`patch_client_config`），引擎提供有限的几个内置动作而不是任意命令。每个动作必须可 dry-run、可备份、可跳过，且汇报只出键名不出值。

### 数据规模

`data/zkai.db` 59MB + `zkai.db-wal` 6.6MB。实测加密 27s / 解密 25s。导出流程 = 读 + zip + 加密 + 解密回读 + 逐文件 sha256 对账，端到端约 1 分钟。能用，但每次导出都 1 分钟起。`data/*.log` 合计 176MB 必须排除——它被 `.gitignore` 的 `*.log` 拦着，不会自动进包，但 manifest 若写 `path = "data"`（dir 型）就会全收。**文档必须写清「data/ 要逐文件点名，不要用 dir 型」**。

---

## 三、大众点评 —— 引擎覆盖不了它的核心诉求

这个项目没有自带换机模块，但有 `tandian-deploy/` 部署模块。用户的诉求是「换电脑后和现在一样」，对这个项目意味着两件事：

### 3.1 换电脑：引擎能覆盖 80%

要搬的：`.env`（36 键，含 `AMAP_KEY`、4 家模型 API key）、`tandian-deploy/deploy.conf`（含服务器 IP）、`输出/`（gitignored 运行产物）、`已探店.txt` / `我的风格.md`（**git 已跟踪**）。

**两个坑**：

1. **`已探店.txt`、`我的风格.md`、`我的评价样本.txt` 被 git 跟踪**（实测：58 个 tracked 文件里包含它们，且 `git status` clean、无未 push 提交）。引擎 `plan_restore` 对 tracked 文件一律 `blocked / 已被 git 跟踪（以仓库为准）`（[unpack.py:179-182](../../src/migrate_engine/unpack.py#L179-L182)）。

   `git clone` 之后这三个文件天然存在，所以 clone + `skip/blocked` **结果上是对的**——但这只在这个项目当前「工作树干净、无未 push 提交」的前提下成立。

   **真实风险**：如果换机当天本地有未提交的 `已探店.txt` 改动（`visited` 标记），或者有 commit 了但没 push，那么：
   - 未提交的改动只在旧机器文件系统里，导入包若声明了它会 `blocked`（git tracked），用户以为恢复到了最新，实际是仓库版本；
   - 未 push 的 commit 在新机器 clone 不到。

   引擎不会提示这两种差异。「随仓库同步，多机不分叉」的项目语义要求**导入前明确报告 `git diff` 与未 push 提交**。

   **建议**：`import` 时若有 manifest，额外输出两组信息：① `git status --short` 的已修改 tracked 文件（导入不会动它们）；② `git log origin/HEAD..HEAD` 的未 push 提交数。都不静默。

2. **`输出/` 是报告产物**，`CLAUDE.md` 说按 `OUTPUT_KEEP_DAYS` 自动清理。它是二手的（可重新生成），不该占包体积。manifest 里根本不该声明它，文档该说明「运行产物不进包」。

### 3.2 换服务器：引擎完全覆盖不了

`Switch-Server.ps1` 做的是 5 步编排：连通性 → 新机安装（venv + `/etc/tandian.env` + systemd + Caddy 改写）→ tar 同步 → **公网 curl 验收** → 改 `deploy.conf`。旧机全程不动留回退，稳定后 `Stop-Remote.ps1` 收尾（停 tandian + 把 Caddy 还原给智能询价独占）。

引擎的 `server export/import` 是「打包文件 → 上传暂存 → 给命令清单」，既不能幂等安装、不能 Caddy 改写、不能公网验收、不能改本地 `deploy.conf`、不能管理回退窗口。

**这不是引擎的缺陷，是scope 不同。** 引擎的定位是「搬数据」，`tandian-deploy` 的定位是「搬部署」。用户说「部署在服务器的也要覆盖，方便迁移服务器」，如果指望这个技能替代 `tandian-deploy`，那是给技能加了它不该承担的复杂度（要多懂 venv、systemd、Caddy、公网探测、deploy.conf 协议）。

**建议（需要用户决策）**：
- **方案 A（推荐）**：技能保持「搬数据」，明确写出「部署编排不在本技能范围」。`tandian-deploy/` 保留。技能产出的 server 包可以作为 `Switch-Server.ps1` 的输入之一（把 `/etc/tandian.env` 这类资产也纳入换服务器流程），但编排仍归 PowerShell。
- **方案 B**：给技能加 `deploy` 编排层。工作量大到值得单独一个技能，且会引入「引擎自行 sudo」的红线问题。

---

## 四、三个项目的共同缺口

1. **产物目录自包含**（A）——三个项目全中，`_换机/`、`.migrate/`、`exports/`、`imports_backup/` 都会被 glob/dir 条目卷进去。
2. **`.env` 健康检查**（B）——ZK-AI 真实发生过，另两个项目的 `.env` 没有检查过，同样可能带病。
3. **权限不保留**（C）——三个项目都有密钥文件（`keys/*.pem`、`.env`）。
4. **SQLite 大库性能**——智能询价 1.6MB 无感，ZK-AI 59MB 要 1 分钟。可优化（zlib compresslevel=1 已有，但瓶颈在 PBKDF2 之后的 Python XOR 循环）。
5. **旧包不兼容**——智能询价的 openssl `Salted__` 包能读（legacy 路径），ZK-AI 的 `ZKAI-MIGRATE` 包不能读。要删 ZK-AI 的 `migrate.py` 就得先能读它的旧包，或者明确「旧包用旧模块读一次，之后作废」。

---

## 五、给用户的决策点

在动手改之前，有四件事需要你定：

1. **大众点评的 `tandian-deploy/` 怎么办？** 保留（方案 A）还是让技能尝试覆盖部署编排（方案 B）？
2. **ZK-AI 的 `~/.codex` + `HKCU\Environment` 写入，要不要进技能？** 这是「换机后桌面版直接能用」的最后一环，但它要求引擎写项目外的路径。
3. **性能底线**：59MB 包 1 分钟能接受，还是要优化到 10 秒内？
4. **删除时机**：旧模块是「新技能验证通过后再删」还是「先删再用新的重打」？智能询价的 `_换机/` 里已有 8.5MB 历史包，那些是 openssl 格式，legacy 路径能读——删模块前应该至少跑通一次「旧包 verify + dry-run」。
