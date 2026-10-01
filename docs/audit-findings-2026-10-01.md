# 一键换机技能 · 审计确认清单（全部经本机实测复现）

审计时间：2026-10-01。来源：① 我自己逐行读码 + 本机复现；② workflow 8 路并行审计（93 条原始发现，10 critical / 28 high / 39 medium / 16 low）。

**筛选纪律**：workflow 的每条 critical/high 我都亲自复现或静态证实过才收录。已推翻 2 条（见文末「被推翻」）。多个 agent 独立命中同一缺陷（`cli.py:567` 被 3 个维度命中、`unpack.py:306` 被 4 个维度命中）是强交叉验证信号。

---

## A17. critical —— legacy 路径读不了真实的 server 历史包（`./` 成员名被当越界）

**[unpack.py:52-54](../../src/migrate_engine/unpack.py#L52-L54)** 的 `check_member_name` 无条件拒绝 `.` 段，而 [legacy.py:118](../../src/migrate_engine/legacy.py#L118) 把 tar 成员名原样交给它。

`pull_from_server.sh:105` 的打包命令是 `tar -czf - -C "$REPO/$STAGE" .`——**`.` 作为源参数，所有成员名都带 `./` 前缀**。

**实测**（用 `pull_from_server.sh` 的真实命令造包，口令已知，走 legacy 解密成功）：
```
$ tar -czf - -C STAGE . | openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000 -pass pass:xxx
$ migrate verify sq-server-test.enc --passphrase-file pw.txt
[错误] 旧包成员不安全：含 . / .. 段：./etc-sq.env
EXIT: 5
```

**受影响范围**：智能询价 `_换机/` 里的 `sq-server-20260930-205227.tar.gz.enc`（5.9MB，含 pg dump 与 `/etc/sq.env` 明文密钥）**读不了**。

**为什么这条最严重**：用户若照 SKILL.md「legacy 兜底」的说法删掉 `pull_from_server.sh` 与 `migrate.py`，就再也没有工具能打开这个包——而它是重建服务器的**唯一依据**（那 33 个生产键、Caddyfile、pg dump 都在里面）。报错信息还误导（说成员不安全/包损坏，真实原因只是 `./` 前缀）。

**注意与 workspace 包的区别**：`pack_workspace.sh:116` 用 `-C "$REPO" "${ITEMS[@]}"`，成员名**不带** `./`，所以 workspace 历史包不受影响。只有 server 包中招。我最初误判为两个都受影响，实测后修正。

**修法**：legacy 路径对成员名做归一化——`name.lstrip("./")`（逐段剥离前导 `.`）后再过预检。新包路径不做这个宽松化。

## A0. critical —— `migrate server import` 必然 NameError 崩溃

**[cli.py:567](../../src/migrate_engine/cli.py#L567)**：`cmd_server_import` 调用 `_server_hints(root)`，但函数体内从未定义 `root`，也不是模块全局名。

AST 静态证实（`ast.walk` 该函数体，`Name(Load)` 含 `root`、`Store` 集合不含）：
```
local names assigned: ['cmd','commands','passphrase','path','rep','target']
'root' assigned locally? False
'root' referenced?       True
```

**后果**：换服务器场景的第二步（上传到新机）100% 崩溃。这正打在用户「服务器也要覆盖」的核心诉求上。

**为什么 108 个测试全绿也抓不到**：`tests/integration/test_server.py` 只测底层的 `upload_stage()`，没有一条测试走 CLI 的 `cmd_server_import`。这条命令从未被任何测试调用过。

**修法**：`root = _root_of(args)`，或 `_server_hints(Path("."))`。同时补一条 CLI 级测试。

## A1. critical —— 导入的明文 `.env` 备份落进未被 gitignore 的目录

**[unpack.py:304-306](../../src/migrate_engine/unpack.py#L304-L306)** 硬编码 `DEFAULT_ARTIFACTS_DIR`，无视 manifest 的 `artifacts_dir`。`cmd_import` 侧 [cli.py:359](../../src/migrate_engine/cli.py#L359) 也硬编码 `.migrate`。

**实测**（智能询价）：
```
$ git check-ignore -v .migrate/fake-env-backup
（无输出）git check-ignore exit code: 1  ← NOT ignored
$ git status --short
?? .migrate/
```
该项目的 `.gitignore` 只有 `_换机/` 与 `*.enc`。覆盖导入时，旧 `.env`（明文 JWT_SECRET 等）按覆盖语义备份到 `.migrate/backup-<时间戳>/`，**一次 `git add -A` 就提交上去**。

这正是 `.gitignore` 注释里记载的那次历史事故的原样重演（「两个 .enc 躺在仓库根目录而 .gitignore 什么都没写」）。engine 自己的注释说 `*.enc` 是兜底，但它兜不住 `.migrate/backup-*/.env` 这种明文文件。

**修法**：`restore_package` / `apply_restore` 接收 `artifacts_dir` 参数，由 manifest 提供；并在导出前检查该目录确在忽略规则里（`git check-ignore`），不在就停下要求补规则——SKILL.md 已把这条列为「必须停下来问」的第 5 项，但引擎没实现。

## A2. critical —— 引擎会把 ZK-AI 的 48 个凭据键报成「死键，确认后可删」

**[env_audit.py:18-21](../../src/migrate_engine/env_audit.py#L18-L21)** 只扫 Python 源码里的 `os.environ` / `os.getenv`。

**实测**（ZK-AI，`code_dirs = ["app","scripts"]`）：
```
code_keys (Python 扫描): 10
env_keys  (.env):        54
dead (报成可删):         48
dead 键名: ADM_API_KEY, ANTHROPIC_BASE_URL, ANTHROPIC_KEY_01, GEMINI_KEY_01,
  MODELSCOPE_API_KEY, MOONSHOT_API_KEY, NVIDIA_API_KEY, NVIDIA_API_KEY_02,
  OPENAI_KEY_01, OPENAI_KEY_02, OPENROUTER_KEY_01, PW_TEST_PROV_KEY,
  SENSENOVA_API_KEY_02..10, STEPFUN_API_KEY_02, ZKAI_ADMIN_TOKEN,
  ZKAI_DATABASE_URL, ZKAI_HOST, ZKAI_REQUEST_TIMEOUT …（共 48 个）
```

这些键全部由 `config/providers.yaml` 的 `env:` 引用和 `config/burner.yaml` 的 `only:` 列表驱动，Python 静态扫描看不到。`migrate audit` 会输出「🗑 死键，确认后可删」——**用户照做，网关当场全瘫**，且不报错。

**修法**：manifest `[env_audit]` 增加声明式消费者扫描（YAML 的 `env:`/`env_var:`/`only:`、JSON 的 `$VAR`、shell 的 `${VAR}`）。这与 ZK-AI `scripts/migrate.py:173-218` 的 `_credential_env_refs` 是同一件事——那个实现正是为 2026-09-29 `SENSENOVA_API_KEY` 静默消失写的。

---

## A3. high —— `import --recover` 传 `.enc` 直接 traceback

**[cli.py:355-364](../../src/migrate_engine/cli.py#L355-L364)**：`--recover` 把必填的 `package`（文档教的是 `.enc` 包路径）当成 journal 文件传给 `recover()`。

**实测**：
```
main(['import', '<xxx>.enc', '--root', '.', '--recover'])
→ UnicodeDecodeError: 'utf-8' codec can't decode byte 0xc7 in position 7
  (完整 traceback，cli.py:360 → journal.py:87)
```

而 `--recover` 是 SKILL.md 与 references 里教的**崩溃恢复命令**——用户在导入中断后最需要它的那一刻，照接口用就崩。传错 journal 路径则 `recover()` 返回「journal 不存在」但 `finish(EXIT_OK)` → **假报成功**。

**修法**：`--recover` 改为独立的 `recover` 子命令，或接受 `--journal <path>`；失败不得 `EXIT_OK`。

## A4. high —— `export --with-server` / `--ask-server` 是谎言

**[cli.py:268-270](../../src/migrate_engine/cli.py#L268-L270)**：
```python
if args.with_server or args.ask_server:
    rep.warn("服务器资产采集（server 包）尚未启用——本次只打 workspace 包。"
             "换服务器场景请等引擎 server 子命令上线。")
```
而 `server export` / `server import` 已经在 [cli.py:505](../../src/migrate_engine/cli.py#L505) 实现了。用户按提示等一个已上线的功能。

**修法**：删掉这段 warn 与两个 flag，或让它们真的触发 `capture_server`。

## A5. medium —— 嵌套表键名拼错静默通过，`merge.includes` 拼错会**反向撤掉白名单**

实测 `validate_data`：
```
merge: {'includes': ['.env']}   → problems: []   merge_include: []   ← 静默失效
env_audit: {'code_dir': ['app']} → problems: []                       ← 静默失效
```

顶层字段拼错有告警（`known_top` 检查），嵌套表没有。而 `merge_include` 为空的后果比「没白名单」更糟：`cmd_import` 的 `whitelist = manifest.merge_include or None` → `None` 在 `restore_package` 里表示「无白名单限制」→ **放开一切路径**，只剩 `tracked` 兜底。

**修法**：对 `env_audit` / `merge` / `profiles.<name>` 的键名做白名单校验，未知键报 problem（和顶层一致）。`merge.include` 为空时报警而不是静默放开。

## A6. medium —— `review_status` 校验后无消费方

`grep review_status src/ tests/` 的结果只有**定义、字段名白名单、取值校验**三处，没有任何地方读它。`needs-review` / `skipped_by_user` 完全不生效——而 SKILL.md 把「manifest 终稿必须逐项确认」当确认机制教给用户。

**修法**：要么 `plan` / `export` 时对 `needs-review` 条目停下要求确认，要么从 schema 删掉这个字段。留一个不生效的确认字段比没有更危险。

---

## A7. critical —— 「server 包禁止合并进仓库」的防线在唯一真实路径上完全没生效

**[unpack.py:292-297](../../src/migrate_engine/unpack.py#L292-L297)** 的安全拒绝写在 `if package_manifest["kind"] == KIND_SERVER and merge:` 里，而 `merge` 默认 `False`，且 [cli.py](../../src/migrate_engine/cli.py) 的 `cmd_import` **从不传 `merge`**（`inspect.getsource` 确认调用处无该参数）。

**实测**（自造一个含 `sqdb.dump` + `etc-sq.env` 的 server 包，导入 git 项目根）：
```
导入迁移包
  这是服务器资产包：不合并进项目。换服务器步骤见 references/package-format.md。
  写入 2，保留双方 0，跳过 0。
EXIT CODE: 0
```
```
$ ls proj/
.migrate  .git  .gitignore  etc-sq.env  sqdb.dump      ← 明文 JWT_SECRET 落在项目根
$ git status --short
?? .migrate/
?? etc-sq.env
?? sqdb.dump                                  ← 未跟踪，git add -A 即提交
```

**后果三重叠加**：① 内含 PostgreSQL 转储与 `/etc/sq.env` 明文密钥的 server 包被直接写进 git 工作区；② 文件未跟踪，一次 `git add -A` 就进仓库；③ 退出码 0，用户以为一切按设计运行。

引擎宣称的两道防线——退出码 7 的安全拒绝、`merge.include` 白名单——在 CLI 路径上都没有触发（白名单为 `None` 因为裸目录无 manifest）。

**修法**：安全不变量改为按 `kind` 判，与 `merge` 无关——`cmd_import` 遇到 `kind == server` 直接拒绝并退出码 7，只指向 `server import`。

## A8. critical —— `recover()` 遇到含 `skip` 记录的 journal 就 KeyError 崩溃

**[journal.py:98-99](../../src/migrate_engine/journal.py#L98-L99)**：
```python
elif op == "skip":
    order.append(rec["dst"])     # 只加进 order，没加进 commits
...
rec = commits[dst]               # 下一轮循环直接 KeyError
```

**实测**（journal 里有一条 `skip`，即任何一次「不在白名单」或「git 已跟踪」的跳过）：
```
CRASH: KeyError '/tmp/some/.env'
```

**后果**：`--recover` 是 SKILL.md 与 references 教的**崩溃恢复命令**。而 `plan_restore` 在「不在白名单」「已被 git 跟踪」时必定写 `skip` 记录——所以只要导入过程中跳过过任何文件，恢复命令就 100% 不可用。用户在导入失败后最需要它的那一刻它崩溃。

**修法**：`skip` 记录不应进 `order`，或循环里用 `commits.get(dst)` 跳过缺失。

## A9. high —— skip 动作也删数据库 sidecar，正在写的库会丢数据

**[unpack.py:220](../../src/migrate_engine/unpack.py#L220)**：
```python
db_targets = [p.name for p in plan if p.action != "blocked" and p.item_type == "sqlite"]
```
条件只排除 `blocked`，不排除 **`skip`**。而 `skip` 是 `--on-conflict skip`（默认值）下的正常动作，语义是「目标已存在，不动它」。

**后果**：用户明确选择「不要碰我的数据库」，引擎却把 `data/zkai.db-wal` / `-shm` 删掉。SQLite 的 `-wal` 里可能有已提交但未 checkpoint 的帧，删它 = 丢那些提交。

附带：Windows 上数据库被进程占用时 `sidecar.unlink()` 抛 `PermissionError`（实测 `WinError 32 另一个程序正在使用此文件`），该异常在 `apply_restore` 的 `try` 块内 → **整个导入失败并回滚**。而「数据库正被占用时拒绝覆盖」正是 references/conflicts.md 承诺的行为，引擎没实现。

**修法**：`action != "blocked"` 改成 `action in {"write", "overwrite", "keep-both"}`；`unlink` 失败时暂停并要求用户先停进程，不要拖垮整个导入。

---

## A. critical —— 产物目录会被打进自己的包（scan.py）

**现象**：`artifacts_dir`（`.migrate` / `_换机`）不在 `scan._SKIP_DIRS` 里，而该集合是硬编码的，不知道 manifest 声明了什么。

**复现**（`/tmp/selftest`，manifest `artifacts_dir = "_换机"`，条目 `path = "*.md"`，`item_type = "glob"`）：
```
包内文件：
  _ops.md
  _换机/journal/leak.md      ← 引擎自己的事务日志被打进去了
```

**为什么严重**：智能询价的真实清单是 `artifacts_dir = "_换机"` + `_*.md` 形态 + `backups/` 目录条目。旧脚本 `pack_workspace.sh` 正是把 `sq-server-*.tar.gz.enc`、`sq-workspace-*.tar.gz.enc`、`server-keynames.txt` 写进 `_换机/`。所以第二次跑换机打包时，**上一个加密包会被整包塞进新包**——「包里有包」，体积翻倍且每次导出都比上次大一倍。用户完全看不出问题，因为 `plan` 的行里只显示文件数和总字节。

**引用的位置**
- [scan.py:23-24](../../src/migrate_engine/scan.py#L23-L24) `_SKIP_DIRS` 硬编码集合
- [scan.py:61-78](../../src/migrate_engine/scan.py#L61-L78) `_walk_files` 只按名跳过
- [scan.py:86-99](../../src/migrate_engine/scan.py#L86-L99) dir/glob 展开不知道 `artifacts_dir`

**修法**：`_walk_files` 增加 manifest 的 `artifacts_dir`（连同 `.migrate`、`*.enc`、`journal/`、`backup-*`）为必跳项；`resolve_item(root, item)` 签名要能拿到 manifest 或 artifacts_dir。glob 展开前先把产物目录从 `os.walk` 的 `dirnames` 里剔掉。

## B. high —— `.env` 的 `\r\r\n` 病害零检查，原样运到新机器

**现象**：引擎没有任何 env 健康检查。ZK-AI 的 `scripts/migrate.py::_check_env_health` 正是为这个病写的（导出+导入双侧检查），ungani 引擎没有等价物。

**复现**（`/tmp/crlf`，`.env` 内容 `JWT_SECRET=abc\r\r\nADMIN=x\r\r\n`）：
```
包内 1 个文件
导出后包内字节：doubled-CR in package: 2     ← 病害原样进包
```

**为什么严重**：值里的 `\r` 会跟着到新机器。pydantic-settings / python-dotenv 的 `str.splitlines()` 只吃掉终止符，行内容尾部的 CR 保留——ZK-AI 2026-09-24 真实事故表现为 `getaddrinfo failed` on `socket.bind`（一个 URL 末尾带了回车）。更难查的是：本机也带着病，只是本机的某些加载路径侥幸吞掉了它。

**引用的位置**
- [pack.py:70-97](../../src/migrate_engine/pack.py#L70-L97) `collect_entries` 只读字节不解释内容
- [unpack.py](../../src/migrate_engine/unpack.py) 导入侧无对应检查

**修法**：`pack.collect_entries` 与 `unpack.apply_restore` 双侧加 env 健康检查（ doubled-CR 计数 + UTF-16LE/BOM 检测）；命中时告警并要求确认，不静默发货。参照 `D:\zhangkun\AI\ZK-AI\scripts\migrate.py:117-160`。

**当前四个 env 文件的实测状态（2026-10-01，全部健康，所以 B 是「缺防护」不是「正发病」）**：

| 文件 | 编码 | CRLF | `\r\r` | 判定 |
|---|---|---|---|---|
| 智能询价 `.env` | utf-8（printable 1.000） | 95 | 0 | ok |
| 智能询价 `.env.server` | utf-8（printable 1.000） | 0（纯 LF，27 行） | 0 | ok |
| 大众点评 `.env` | utf-8（printable 1.000） | 56 | 0 | ok |
| ZK-AI `.env` | utf-8（printable 1.000） | 131 | 0 | ok |

> 中间我曾把 `.env.server` 和大众点评 `.env` 误判成 UTF-16LE，依据是「ASCII 解码失败」和 bytes/line 偏大。精确复核后推翻：四个文件都是 UTF-8，无 NUL 字节，无 BOM。Python 对 UTF-16 的宽容解码会把任意偶数长字节流都解成功，不能当判据。**判据是可打印比例 + NUL 计数**。
>
> 顺带一个真实差异：智能询价 `.env` 与 ZK-AI `.env` 是 CRLF，`.env.server` 与大众点评 `.env` 是纯 LF。CRLF 本身无害（`splitlines()` 吃终止符），有害的是 `\r\r\n`。

## C. high —— 导入后文件权限不保留，密钥变 globally readable

**现象**：`atomic_commit` 只 `write_bytes` + `os.replace`，不设 mode；包里也不记录 mode。

**复现**（`/tmp/perms`，`keys/test.pem` 源 0600 → 导出 → 导入到 `/tmp/perms_out`）：
```
.\keys\test.pem  0o666
```

**为什么严重**：技能宣称跨平台。Windows 上 0600 无实义所以看不出来，但换服务器场景（`migrate server import` 之后人工 `sudo cp`）落在 Linux 上，导入的 `keys/*.pem` 是全球可读的私钥。这不只是「不够好」，是把源机器上收紧的权限在目标机上放开了。

**引用的位置**
- [journal.py:63-75](../../src/migrate_engine/journal.py#L63-L75) `atomic_commit` 无 mode
- [pack.py:112-142](../../src/migrate_engine/pack.py#L112-L142) 包内 manifest 无 mode 字段

**修法**：包内 manifest.json 每项加 `mode`（`stat.S_IMODE`），导入时 `os.chmod`；敏感条目（`sensitive = true`）在 mode 缺失时默认收紧到 0600 而不是 0644。Windows 上 chmod 基本无效但要 try，不报错。

## D. high —— `env_audit` 在 `code_dirs = []` 时把一切已配键误报成死键

**现象**：`scan_code_keys` 在 `code_dirs` 为空列表时返回 `set()` → `result.scan_failed = True`；但 `dead` 的计算不理会 scan_failed，依然把 `env_keys - effective_code` 全部报成死键。

**复现**（`/tmp/crlf`，manifest `[env_audit] code_dirs = []`）：
```
[警告] 环境审计没扫到代码里的变量读取（code_dirs？），按 fail-closed 处理。
[警告] ADMIN 是死键（没代码读），确认后可删。
[警告] JWT_SECRET 是死键（没代码读），确认后可删。
```

**为什么严重**：这是诱导用户删配置的输出。项目里 `.env` 键很多、manifest 还没填 `code_dirs` 时，每次导出都刷一屏「建议删除」，其中可能有真正在用的键（智能询价的 `SQ_PYTHON` 由 `selfhost.ps1` 读，Python 静态扫不到——用户自己的注释里写着他差点误删）。

**引用的位置**
- [env_audit.py:43-61](../../src/migrate_engine/env_audit.py#L43-L61) `scan_code_keys` 空目录返回空
- [env_audit.py:92-113](../../src/migrate_engine/env_audit.py#L92-L113) `audit` 的 dead 计算
- [cli.py:287-296](../../src/migrate_engine/cli.py#L287-L296) 导出后的审计输出

**修法**：`scan_failed` 为真时 `dead` / `missing` 一律不上报（只报「没扫到」）。这与智能询价 `env_audit.py` 的 fail-closed 语义一致，它的 docstring 明说「一个在自己失效时静默放行的检查比没有检查更糟」。

## E. medium —— glob 语义是「匹配完整相对路径」，不是 basename

**现象**：`_walk_files` 里 `fnmatch.fnmatch(rel, pattern)`，`rel` 是相对 root 的正斜杠完整路径。

**复现**：
```
_LOCAL.md          fnmatch(_*.md) = True
deliverables/_x.md fnmatch(_*.md) = False     ← 只想递归收 _*.md 时收不到
_换机/notes.md     fnmatch(_*.md) = True      ← 与本清单 A 叠加，命中产物目录
```

**为什么重要**：`config/config.yaml` 用 `path = "*"` 会连带 `config/config.yaml.bak`、`data/zkai.db`、`.env` 全收。文档 [`classification.md`](../../references/classification.md) 写「glob 支持 * ?」，没写清作用在完整路径上，用户必然按 shell glob 的直觉理解。叠加 A 之后，`*.md` + `_换机/` 的组合等于必炸。

**修法**：在 references/classification.md 明确 glob 作用于**完整相对路径**；并给一个 `path = "config/*.yaml"` 显式排除 `*.bak` 的示例。是否要支持 `**` 另说，但语义必须先写清。

## F. medium —— 文档里的路径全部是旧机器 `E:\Ingulf`

**现象**：这些文档在新电脑上一行都用不了。
- [README.md:8,13,17,88](../../README.md)
- [docs/repository-layout.md:5,15,18](../../docs/repository-layout.md)
- [references/example-smart-quotation.md:7,41,45](../../references/example-smart-quotation.md)

真实路径是 `D:\zhangkun\my-skills`、`C:\Users\zhangkun\.workbuddy-ai\skills\my-skills`，项目在 `D:\zhangkun\智能询价`。

**为什么重要**：用户的目标就是换电脑。文档里写的换电脑流程指向一个不存在的盘符和用户名——这个技能自己就是干换机的，自己的文档换不了机。

**修法**：改成相对描述 + 一个「本机路径表」，不写死盘符。

## G. low —— `report.redact` 替换阈值 4 字符

[report.py:15-20](../../src/migrate_engine/report.py#L15-L20)：`len(secret) >= 4`。短密钥（`WX_PAY` 这类 4 字符占位、PIN）不被替换。测试用的假密钥都长，所以测不出来。

**修法**：脱敏不看长度，env 敏感项的值一律不进 report（现在靠的是没人打印，不是机制保证）。

## H. low —— journal 写绝对路径

[journal.py:71](../../src/migrate_engine/journal.py#L71) `record["ts"]` 之外每行都带 `dst` 的绝对路径，含 `C:\Users\zhangkun\`。产物目录被 git 排除所以不进仓库，但 `.migrate/journal/` 若被同步/备份就把用户名带出去了。

**修法**：journal 记相对 root 的路径。

---

## A10. high —— 技能自带的示例 manifest 无法通过自己的校验

[examples/smart-quotation/manifest.toml:83-84](../../examples/smart-quotation/manifest.toml#L83-L84) 的 `profiles.machine-only.exclude = ["backups-server"]` 引用了不存在的条目 id。

实测：
```
示例 manifest 校验 problems:
  - profiles.machine-only.exclude 引用了未知 id：'backups-server'
```

SKILL.md 与示例文件头都让用户「复制到项目 `.migrate/manifest.toml` 后逐项确认，再跑 plan」。第一步 `manifest validate` 就失败。示例是用户对 schema 的第一认知来源，它自己不合规。

## A11. high —— `merge.include` 逐字精确匹配，目录条目的文件一个都恢复不了

[unpack.py:174](../../src/migrate_engine/unpack.py#L174) 用 `name not in merge_whitelist` 全等比较。

实测 1（manifest 声明 `keys` 为 `item_type = "dir"`，白名单写 `["keys"]`）：

```
blocked    keys/test_license_private.pem    不在 manifest 的 merge.include 白名单
blocked    keys/test_license_public.pem     不在 manifest 的 merge.include 白名单
write      .env
```

实测 2（智能询价**真实** manifest + 真实展开，白名单 `[".env", ".env.server", "keys", "quotation.db", "deliverables"]`）：

```
解析出的文件总数:      46
会被 blocked 的数量:   43
能写入的数量:          3   ['.env', '.env.server', 'quotation.db']
blocked 样例: keys/test_license_private.pem / keys/test_license_public.pem /
  _HANDOFF-2026-09-20.md / _LOCAL-GUIDE.md / _MIGRATION-GUIDE.md /
  deliverables/2026-09-16-合并单元格与手机版图片位置.md …（共 43 个）
```

**46 个文件只能恢复 3 个。** `keys/` 里的 RSA 私钥（licenses 功能依赖）、9 个运维笔记、`deliverables/` 全部 32 个文件被静默拦下。用户照语义写 `"keys"` 得到的是静默失败。

**修法验证**：改成前缀匹配后，放行数从 3 提升到 37（`keys/` 2 + `deliverables/` 32 + 原来的 3）。剩下 9 个 `_*.md` 仍被挡住，是因为白名单里没写这个模式——那是 manifest 该写清的事（建议白名单支持 fnmatch），不是引擎缺陷。

**修法**：白名单匹配改为 `name == w or name.startswith(w.rstrip("/") + "/")`，并支持 fnmatch。

## A12. medium —— `_SKIP_DIRS` 静默丢弃项目自己的构建产物

`path = "."` 这类「搬整个目录」的用法下，[scan.py:23-24](../../src/migrate_engine/scan.py#L23-L24) 的 `dist/`、`build/`、`out/`、`coverage/` 被无条件跳过，且 `notes` 为空。

实测（`resolve_item(root, Item(path=".", item_type="dir"))`，目录含 `dist/ build/ out/ coverage/ mydata/`）：
```
解析出的文件: mydata\x.txt
notes: []
```

这四个名字既是「依赖与构建产物」也可能是项目自己要保留的内容。跳过本身合理，静默跳过不合理。

修法：把「因 `_SKIP_DIRS` 跳过的顶层目录」列进 `notes`，`plan` 时显示。

---

## A13. high —— `capture = "command"` 被 schema 允许，引擎却把它当文件路径 cat

[schemas/manifest.v1.schema.json](../../schemas/manifest.v1.schema.json) 的 `capture` 枚举含 `command`；`manifest.py:78` 的 `_CAPTURES` 也含。但 `capture_server` 里没有任何 `command` 分支（`inspect.getsource` 确认 `"capture == 'command'"` 不存在），会走 `else` 用 `remote_path = item.remote_path or item.path` 去 `cat`。

**后果**：用户按 schema 写自定义采集命令，得到的是「读不到文件」的报错，且报错指向一个他没写过的路径。

**修法**：要么实现 `command`（脚本体走 stdin，参数走位置参数，同 `_CAPTURE_FILE_SCRIPT`），要么从 schema 与 `_CAPTURES` 删掉它。**留一个能声明但不能执行的选项比没有更糟。**

## A14. high —— Linux/macOS 启动器用分号拼 PYTHONPATH，丢掉用户原有值

[scripts/migrate:35](one-click-migrate/scripts/migrate#L35)：
```sh
PYTHONPATH="$SRC_FOR_PY${PYTHONPATH:+;$PYTHONPATH}" exec "$PY" -m migrate_engine "$@"
```

实测推演：
```
SRC=/opt/skill/src  PYTHONPATH=/user/existing
拼接: /opt/skill/src;/user/existing     ← Linux 上这是单一路径项
正确: /opt/skill/src:/user/existing
```

**后果**：技能宣称支持 Linux/macOS（SKILL.md 的启动命令给了 `bash <技能目录>/scripts/migrate`）。在真正的 Linux/macOS 上，分号不是路径分隔符，用户的 `PYTHONPATH` 整体丢失，还多出一个含 `;` 的垃圾项。注释里只考虑了 Windows（`cygpath` 转换）却没考虑分隔符本身。

**修法**：按平台选分隔符——`case "$(uname -s)" in CYGWIN*|MINGW*|MSYS*) sep=';' ;; *) sep=':' ;; esac`。

## A15. high —— `git_tracked_files` 失败时静默返回 None，`tracked` 防线整体消失

`platform.git_tracked_files` 在「非 git 仓库」与「git 不可用」时都返回 `None`（实测两种情况均 `None`）。而 `restore_package` 里 `tracked=None` 的语义是**不设 tracked 保护**。

**后果**：references/conflicts.md 承诺「git 已跟踪文件一律跳过（以仓库为准）」。新机器上 git 不在 PATH、或用户在非 git 目录跑导入时，这条防线静默消失，**包内若有仓库源码路径会被直接覆盖**。

**修法**：区分「不是 git 仓库」（合法，返回 None + 在报告里说明 tracked 保护未启用）与「git 调用失败」（异常，停下问用户）。`cmd_import` 在 `tracked is None` 时明确打印「未启用 git 跟踪保护」。

---

## A16. high —— 全部条目被 git tracked blocked 时零写入，退出码仍 0

[cli.py:423](../../src/migrate_engine/cli.py#L423) 的冲突检测是：
```python
conflicts = [i for i in plan if i.action == "skip" and i.reason == "目标已存在"]
...
if conflicts and args.on_conflict == "skip": rep.finish(EXIT_CONFLICTS)
```
`blocked` 的 `reason` 是「已被 git 跟踪（以仓库为准）」或「不在 manifest 的 merge.include 白名单」，**不等于**「目标已存在」，所以不进 `conflicts`。

**实测**（`.env` 被 git 跟踪，`tracked={'.env'}`，`on_conflict='skip'`）：
```
written: []
skipped: ['.env（已被 git 跟踪（以仓库为准））']
→ 退出码 0
```

**后果**：导入零写入但报告成功。用户以为资产已恢复，实际一个文件都没落地。对大众点评尤其危险——若误把 tracked 的 `已探店.txt` / `我的风格.md` 写进 manifest，导入会静默空转。

**修法**：`written` 为空而 `skipped` 非空时给退出码 3（或单独一个「零写入」退出码），并把 blocked 原因分组显示。

---

## A18. high —— 上传到新机的暂存明文 world-readable

[server.py:263-266](../../src/migrate_engine/server.py#L263-L266) 的 `TarInfo` 未设 `mode`，tar 默认 **0644**；[server.py:269](../../src/migrate_engine/server.py#L269) 的 `mkdir -p` 不设 umask，而 `/tmp` 是 0777。

**后果**：`upload_stage` 落到新机暂存目录的 `/etc/sq.env`（含 `JWT_SECRET`、`ADMIN_API_KEY`）与 `sqdb.dump`（全库明文）是**同机任意用户可读**的。而清理命令 `rm -rf <stage_dir>` 只在用户「确认服务正常后」手动执行——ssh 中断或用户忘了，明文长期滞留 /tmp。

**修法**：`info.mode = 0o600`;远端命令改为 `mkdir -p -m 700 <stage>`；并在引擎侧提示「确认服务正常后立即执行 `rm -rf`」。

---

## 被 agent 报错、我实测推翻的（不要采信）

| agent 的说法 | 实测 |
|---|---|
| `env_audit` 的 `server_only` 交集方向写反，提醒永不触发 | 错。实测 `server_only=['SMTP_HOST']` + `.env` 里有 `SMTP_HOST` → 正确返回 `{'SMTP_HOST'}`。逻辑正常。 |
| `db_targets` 用 `p.name` 丢目录，嵌套 sqlite 的 sidecar 清理指向项目根 | 错。`p.name` 取的是**包内名**（`data/zkai.db`），`root / (name + suffix)` 路径正确。`/tmp/mtest2` 实测嵌套库的 stale `-wal` 被正确清除并报告 `data/zkai.db-wal`。（注意：**同一行代码另有一个真缺陷**——它不排除 `action == "skip"`，见 A9，两者不要混。） |
| `_ssh_binary` 的 `shlex.quote` 单引号在远端 shell 不可靠 | 错。实测 `'/tmp/my stage'`、`'/tmp/换机'` 都正确加引号，远端 bash 下单引号内空格与中文均安全。 |
| legacy 判型表达式 `has_server or (has_server and has_local)` 是 bug | 逻辑冗余但**结果正确**（恒等于 `has_server`，即「有 server 标记就判 server」），与注释里「两类都有保守判 server」的意图一致。只是写法难看。 |

> 补充说明：这 2 条被推翻的发现说明「多个 agent 报同一条」不总等于可信。`unpack.py:220` 被报了 2 次：一次说错（目录层级），一次说对（skip 动作）。我只认自己复现的那一半。

# 已排除的怀疑（实测推翻，不要再提）

| 怀疑 | 实测结论 |
|---|---|
| `unpack.py` sidecar 清理用 `p.name` 丢目录层级 | 否。`db_targets` 取的是包内名 `data/zkai.db`，`root / (name + suffix)` 正确。`/tmp/mtest2` 实测：嵌套 sqlite 的 stale `-wal` 被正确清除并在报告里显示 `data/zkai.db-wal`。 |
| 59MB 加密慢到不可用 | 慢但不堵：encrypt 27.1s / decrypt 25.2s（2.2 MB/s）。导出流程=读文件+zip+加密+解密回读+再加密校验，粗估 1 分钟级。可优化（见下方「待决策」），不是缺陷。 |

## 待决策：crypto 用 PBKDF2+计数器模式，无 GCM/ChaCha

PBKDF2 120k 在本机实测就是速率上限（2.2 MB/s 纯 Python XOR）。升级到 `hashlib.blake2b`/`hmac` 分块或换 `cryptography`（破坏零依赖）二选一。**先不动**——安全上够用（HMAC 先验后解、随机 salt 无跨包复用），只在大包时慢。若用户要搬 59MB 库且希望 <10s，再议。
