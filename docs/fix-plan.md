# 修复方案（**待你确认后再动手**，未改任何引擎代码）

结论：**这个技能现在不能替换三个项目的换机模块。** 经 workflow 8 路审计 + 我逐条实测，确认 **19 条缺陷**（7 critical / 9 high / 3 medium），其中 5 条会直接导致密钥泄露或功能瘫痪。

- 明细与复现输出：`audit-findings-2026-10-01.md`
- 三项目适用性：`project-fit-assessment.md`

## 修复顺序的原则

先修**会泄露密钥的**和**会让流程断的**，再修会丢数据的，最后修文档。每一批都要配测试，跑绿了才进下一批。

---

# 第一批：密钥泄露与流程断裂（7 条 critical）

### 修 1 · A17 legacy 读不了真实 server 历史包

`pull_from_server.sh` 用 `tar -C STAGE .` 打包 → 成员名带 `./` → `check_member_name` 拒绝 `.` 段。

**修法**：legacy 路径对成员名做归一化后再过预检。

```python
# legacy.py，仅 legacy 路径，新包不宽松化
def _normalize_member(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name
```
接在 `for info in members` 循环内，`check_member_name(_normalize_member(info.name))`，并用归一化后的名字作包内名。

**测试**：用 `tar -C stage . | openssl enc ...` 真实命令造一个包，断言 `verify` 与 `import --dry-run` 通过。这条必须有集成测试，因为它是「删旧模块」的硬前提。

### 修 2 · A7 server 包能经 `import` 明文写进项目根

`unpack.py:292` 的拒绝条件是 `kind == SERVER and merge`，而 `cmd_import` 从不传 `merge`。

**修法**：按 kind 判，与 `merge` 无关。
```python
if package_manifest["kind"] == KIND_SERVER:
    raise PackageError(
        "这是服务器资产包……请用 `migrate server import`。", exit_code=EXIT_REFUSED)
```
`merge` 参数随之失去存在意义，可一并去掉。`cmd_import` 那条只打印提示的分支也删掉（改成拒绝）。

**测试**：构造 server 包 → `migrate import` → 断言退出码 7 且项目根零写入。

### 修 3 · A0 `migrate server import` NameError

`cli.py:567` 用未定义的 `root`。

**修法**：`root = _root_of(args)`，与 `cmd_server_export:506` 一致。

**测试**：补一条 CLI 级测试走 `cmd_server_import`。**这是 108 个测试全绿却漏掉它的原因**——`test_server.py` 只测底层 `upload_stage()`。

### 修 4 · A8 `recover()` 遇 `skip` 记录 KeyError

`journal.py:98-99` 把 `skip` 加进 `order` 却不加进 `commits`，下一轮 `commits[dst]` 崩。

**修法**：`skip` 不进 `order`；循环改 `rec = commits.get(dst)`，`None` 则跳过。同时给 `main()` 加 catch-all，未知异常转 `EXIT_ERROR` 报告而不是裸 traceback（这条同时覆盖 A 系列里所有「traceback 甩脸」）。

**测试**：journal 含 `skip` + `commit` 混合记录 → `recover()` 不崩。

### 修 5 · A1 导入备份落进未被 gitignore 的 `.migrate/`

`unpack.py:306` 硬编码 `DEFAULT_ARTIFACTS_DIR`，`cli.py:359` 硬编码 `.migrate`。

**修法**：`restore_package` / `cmd_import` 接收 manifest 的 `artifacts_dir`。另加 SKILL.md 已承诺但引擎没实现的检查：**导出前 `git check-ignore` 验证产物目录确被排除，不在就停下要求补规则**。

**测试**：`artifacts_dir = "_换机"` → 导入 → 断言备份落在 `_换机/backup-*` 而非 `.migrate/`。

### 修 6 · A2 `env_audit` 把 ZK-AI 48 个凭据报成死键

`env_audit.py` 只扫 Python `os.environ`。

**修法**：manifest `[env_audit]` 加声明式消费者：
```toml
[[env_audit.consumers]]
glob = "config/providers.yaml"
pattern = '''(?m)^\s*-?\s*env(?:_var)?\s*:\s*(?:\$\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))\s*$'''
label_from = "id"

[[env_audit.consumers]]
glob = "config/burner.yaml"
pattern = '''(?m)^\s*-\s*([A-Z][A-Z0-9_]{2,})\s*$'''
section = "only"
```
正则与 `_credential_env_refs`（ZK-AI `migrate.py:162-218`）对齐——那是为 2026-09-29 `SENSENOVA_API_KEY` 静默消失写的实现。

**测试**：拿 ZK-AI 真实 `config/` 跑，断言 `SENSENOVA_API_KEY_02..10`、`NVIDIA_API_KEY` 不在 `dead` 里。

### 修 7 · A 产物目录被打进自己的包

`scan._SKIP_DIRS` 硬编码，不知道 `artifacts_dir`。

**修法**：`resolve_item(root, item, skip_dirs)` / `_walk_files(..., skip)` 增加参数；调用方传 `manifest.artifacts_dir`，并**永远**追加 `.migrate`、`*.enc` 名、`backup-*`、`journal`。在 `os.walk` 的 `dirnames` 阶段剔除（省 IO），并把剔除的顶层目录记进 `notes`。

**测试**：`artifacts_dir = "_换机"` + `path="_*.md"` glob + `path="."` dir，断言 `.enc` 与 journal 都不在包内。

---

# 第二批：丢数据与静默失败（9 条 high）

| # | 缺陷 | 修法要点 |
|---|---|---|
| A11 | `merge.include` 全等匹配，46 文件只恢复 3 个 | 前缀匹配 `name == w or name.startswith(w.rstrip("/")+"/")`，实测放行 3→37；白名单支持 fnmatch（剩下 9 个 `_*.md` 靠这个） |
| A9 | `--on-conflict skip` 仍删数据库 sidecar | `action != "blocked"` → `action in {"write","overwrite","keep-both"}`；`unlink` 失败要停并要求用户先停进程，别拖垮整个导入 |
| A3 | `--recover` 传 `.enc` 直接 traceback / 传错假报 exit 0 | 拆成独立 `recover` 子命令或 `--journal <path>`；失败不得 `EXIT_OK` |
| A18 | 上传暂存明文 world-readable | `info.mode = 0o600`；远端 `mkdir -p -m 700` |
| A16 | 全部 blocked 时零写入但 exit 0 | `written` 空而 `skipped` 非空 → 退出码 3，并按原因分组显示 |
| A13 | `capture="command"` schema 允许但静默当文件路径 cat | 实现它（脚本体走 stdin）或从 schema 删掉。留不能执行的选项比没有更糟 |
| A14 | Linux 启动器分号拼 PYTHONPATH | 按平台选分隔符 |
| A15 | `git_tracked_files` 失败静默 None → tracked 防线消失 | 区分「非 git 仓库」（合法，报告说明）与「git 调用失败」（停下问） |
| B | `.env` 的 `\r\r\n` 病害零检查 | 新增 `env_health.py`，导出+导入双侧检查 4 项（`\r\r` 计数、UTF-16/BOM、空文件、NUL） |
| C | 导入后 `keys/*.pem` 0600→0o666 | 包内 manifest 每项加 `mode`；导入侧 `os.chmod`；缺失时 `sensitive` 项默认 0600 |
| D | `code_dirs` 扫不到时仍误报死键 | `scan_failed` 为真时 `missing`/`dead` 一律返回空集 |
| A10 | 自带示例 manifest 过不了自己校验 | 修 [examples/smart-quotation/manifest.toml:83](../../examples/smart-quotation/manifest.toml#L83) 的 `backups-server` → 真实 id |
| A5 | `merge.includes` 拼错**反向撤掉**白名单 | 嵌套表键名白名单校验；`merge.include` 为空时报警而非静默放开 |
| A6 | `review_status` 校验后无消费方 | 要么 `plan`/`export` 对 `needs-review` 停下确认，要么删字段 |

（这表里 15 条是我把第二批该做的都列全了，含 3 条 medium。你说先修哪些。）

---

# 第三批：三个项目的能力缺口

### 智能询价（缺口 2 个）

1. **`env_files` 多源**：`audit()` 签名已支持 `env_files`，但 `cli.cmd_export:288` 从不传。要传 `[".env", ".env.server"]`，并在 `server export` 后核对 server 包里 env 文件的键名集合（对应旧 `_audit_env_after_export` 读 `server-keynames.txt` 的能力）。这条关系到「重建服务器时丢 14 个键」的静默丢失链。
2. **`backups/` 只取最新一份**：旧 `pack_workspace.sh` 有 `ls -1t backups/*.json | head -1` 逻辑。引擎只能逐文件点名。建议加 `item_type = "latest-glob"` 或文档明确这个模式。

### ZK-AI（缺口 4 个，最关键）

| 缺口 | 旧实现 | 不补的后果 |
|---|---|---|
| `.env` 病害检查 | `_check_env_health` | 第二批的 B 覆盖 |
| 凭据交叉核对 | `_check_credential_envs` | 第二批的 A2 覆盖 |
| 写 `~/.codex/config.toml` | `_apply_chatgpt_client` | 见下 |
| 写 `HKCU\Environment` | `_provision_chatgpt_env` | 见下 |
| 读 `ZKAI-MIGRATE` 旧包 | — | 见下 |

**post-import 钩子**（要单独设计安全模型）：manifest 声明式 + **引擎提供有限几个内置动作**，不执行任意命令。
```toml
[[hooks.post_import]]
action = "write_user_env_var"
name_from = "config/chatgpt.yaml:env_key"
value_from_env = "ZKAI_API_TOKEN"
backup = true

[[hooks.post_import]]
action = "patch_client_config"
config = "config/chatgpt.yaml"
target = "~/.codex/config.toml"
```
每个动作可 dry-run、可备份、可跳过，汇报只出键名不出值。这两个目标都在项目目录外，是整个引擎第一个「写项目外路径」的能力，安全模型必须单独评审。

**`ZKAI-MIGRATE` 只读兼容**（删 ZK-AI 旧模块的硬前提）：它的格式是 `magic + salt(16) + sha256(plain)(32) + cipher`，加密算法与引擎同构（PBKDF2 120k + 计数器密钥流）。约 30 行：`is_zkai_blob()` 判型 + 复用 `_derive_keys`/`_keystream` + 内层是 zip（含 `manifest.json`）。**建议按新引擎格式重新导出一个包，确认无误后再删旧模块**，兼容只作兜底。

### 大众点评

- **换电脑**：A11/A16 修完即可用。注意 `已探店.txt`、`我的风格.md`、`我的评价样本.txt` 被 git 跟踪（实测 58 个 tracked 文件含它们），导入前要报告 `git diff` 与未 push 提交。
- **换服务器**：**引擎覆盖不了**。`Switch-Server.ps1` 是「连通性 → 新机安装(venv/systemd/Caddy 改写) → tar 同步 → 公网 curl 验收 → 改 deploy.conf」的 5 步编排，引擎只搬文件。建议保留 PowerShell 做编排，技能的 server 包作为它的输入。

---

# 第四批：文档

1. **旧机器路径**：`README.md`、`docs/repository-layout.md`、`references/example-smart-quotation.md` 全是 `E:\Ingulf`。改成「本机路径表」放 `docs/`，其余用相对描述。这个技能自己就是干换机的，自己的文档换不了机。
2. **glob 语义**：明写作用于**完整相对路径**（不是 basename），`config/*.yaml` 会连带 `.bak`。配 ZK-AI `data/` 的警告：**不要用 dir 型**（会收 176MB 日志）。
3. **`_SKIP_DIRS` 提示**：A12——被跳过的顶层目录要显示出来，别静默。
4. **`.venv/` 不该进候选区**：实测 `plan` 输出 `[候选] .venv/Lib/site-packages/certifi/cacert.pem → required（密钥材料）`。候选区走 `git_ignored_files`，没过 `_SKIP_DIRS`。
5. **`exit` 码表对齐**：`package-format.md` 与 `plan` 实际值不符；`EXIT_UNSUPPORTED`(8) 在 `.cmd` 侧无处理，而 SKILL.md 承诺「.cmd 据此分支」。

---

# 验证门槛（每批必过）

```bash
cd one-click-migrate && py -m pytest -q          # 当前 108 条全绿，改完必须仍全绿 + 新增用例
```

三个项目各跑一轮真实闭环（**这是删旧模块的唯一依据**）：

```bash
migrate export --root <项目>          # 旧机
migrate verify <包>                    # 不落盘
migrate import <包> --out <临时目录>    # 新机模拟
# 然后跑该项目 manifest 里的 verify 命令
```

外加回归：智能询价 `_换机/` 的两个 openssl 历史包（workspace + server）`verify` + `import --dry-run` 通过——**这条由第一批修 1 保证**。

---

# 需要你决策的 4 件事

1. **范围**：先修第一批 7 条（约 2~3 小时，每条配测试），还是连第二批 15 条一起？
2. **ZK-AI 的 post-import 钩子**（写 `~/.codex` + `HKCU\Environment`）要不要进技能？我建议进，但它是引擎第一个「写项目外路径」的能力，要单独过安全设计。
3. **大众点评部署编排**要不要进？我建议**不要**——保留 `Switch-Server.ps1`，技能只搬数据。给技能加部署层 = 一个新技能，且会撞「引擎自行 sudo」的红线。
4. **`ZKAI-MIGRATE` 兼容**：加只读支持（约 30 行），还是「用旧模块导出一次、换成新格式后作废」？

我建议的顺序：**第一批 7 条 → 跑通三项目闭环 → 再决定第二、三批**。每一步之前我停下来跟你确认，不连着做。
