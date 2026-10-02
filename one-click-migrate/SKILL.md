---
name: one-click-migrate
description: 在开发机或服务器之间迁移项目里被 git 排除、但运行又离不开的数据。当用户说「一键换机」「换电脑」「换服务器」「迁移项目」「打包必要数据」「导出被 gitignore 的配置」「导入换机包」「把服务器上的库和密钥打包下载」「换机恢复」时使用。先写/核对项目 .migrate/manifest.toml，再由技能内置引擎加密打包或校验后导入。不用于备份整个磁盘、同步 git 已跟踪的代码、或把 node_modules 与虚拟环境打进包里。
agent_created: true
---

# 一键换机

git 管代码，加密包管 git 不管、又无法重建的东西。两者合起来才是一个能跑的项目。

三层结构，各司其职：

```text
本技能（SKILL.md）     识别场景、高风险确认、调引擎、转译报告
引擎（src/migrate_engine/）  唯一副本，零第三方依赖（Python 3.11+ 标准库）
项目声明（<项目>/.migrate/manifest.toml）  只描述「该搬什么」，可提交、不含密钥值
```

不要替用户临场拼 openssl/tar/ssh 命令。一切执行走引擎 CLI。

## 启动引擎

```bash
# POSIX（Git Bash / Linux / macOS）
bash <技能目录>/scripts/migrate <子命令> --root <项目根>

# Windows
<技能目录>\scripts\migrate.cmd <子命令> --root <项目根>
```

技能目录 = 本 SKILL.md 的上级目录（用户级在 `~/.workbuddy-ai/skills/my-skills/one-click-migrate/`）。任何项目用 `--root` 指向它即可，不依赖当前目录。

## 先选场景

| 场景 | 做法 |
|---|---|
| 只换电脑 | 导出 workspace 包；新机 git clone + 建 venv + import。服务器不动 |
| 只换服务器 | `server export` 从旧机采集；新机 `server import` 上传暂存目录，系统路径人工确认后写入 |
| 两个一起 | 先 server 后 workspace。顺序反了，新电脑会连向已作废的旧地址 |

## 标准流程

### 1. 体检与清单

```bash
migrate doctor                      # 平台能力（python/git/sqlite/ssh）
migrate manifest validate           # 校验项目声明
migrate plan                        # 解析清单 + 忽略项差异建议，不打包
```

没有 manifest：`migrate manifest init --project <slug>` 生成草稿，逐项确认 class/item_type/on_missing 后再 plan。有 manifest：plan 会把「被忽略但未声明」的文件列为候选，确认后才加条目。

项目已有换机脚本（如智能询价的 `scripts/migrate.py`）：先读它，把它的资产清单翻译成 manifest 条目，不要两套并存跑。参考 `references/example-smart-quotation.md`。

### 1.5. SSH 免密配置（部署到服务器的项目，换机后必跑）

部署到服务器的项目（有 `scope = "server"` 条目）换机后，新机器没有 SSH 密钥，`server export/import` 用 `BatchMode=yes` 会直接失败。**换机后第一次跑 server 命令前，先配好免密**：

```bash
# 验证免密是否配好（不碰服务器，只跑 ssh target true）
migrate ssh-check --target ubuntu@<服务器IP>

# 一次配好：生成密钥（如无）→ 推公钥到服务器 → 验证
migrate ssh-setup --target ubuntu@<服务器IP>
```

`ssh-setup` 会交互要求输入服务器密码——**这是唯一一次**，之后公钥已进 `~/.ssh/authorized_keys`，免密生效。Windows 上没有 `ssh-copy-id` 时自动 fallback 到手动推送。

可选参数：`--port 22`（非默认端口）、`--force`（已有密钥时也在旁边新建一个）、`--strict-host`（不自动接受新主机指纹，要求先手动 `ssh target` 一次）。

`doctor` 也会检查 SSH 能力：有 ssh 可执行但无私钥时，提示跑 `ssh-setup`。`server export/import` 在 SSH 失败时同样给出 `ssh-setup` 引导。

### 2. 导出（旧机器）

```bash
migrate export                      # workspace 包；口令交互输入两次
migrate export --profile machine-only   # 不带数据库快照
migrate server export --target ubuntu@<旧服务器IP>   # 服务器包
```

口令：交互（不回显）/ 环境变量 `MIGRATE_PASSPHRASE` / `--passphrase-file`。口令不进 argv、不进包、不进对话。丢了包永远解不开，导出时必须把这句话说给用户。

### 3. 校验

```bash
migrate verify <包>                 # HMAC + 逐文件 sha256，不落盘
```

### 4. 导入（新机器）

```bash
migrate import <包>                            # 默认只写不存在的文件
migrate import <包> --on-conflict overwrite    # 覆盖（旧文件先进 backup-<时间戳>/）
migrate import <包> --dry-run                  # 只看计划
migrate server import <包> --target ubuntu@<新服务器IP>   # 上传暂存目录 + 人工命令清单
```

导入收尾必须打印包内 rebuild/verify 命令，并明确：验证失败就停，不自动重导。

## 批量换机（多项目一次跑完）

换机时往往好几个项目一起换，逐个跑 `migrate plan/export/import` 效率太低。批量换机用一份注册表（`~/.migrate-registry.toml`）列出所有项目，一次跑完。

### 1. 建注册表

```bash
migrate batch init --print          # 看模板
migrate batch init                  # 写到 ~/.migrate-registry.toml
```

注册表不进任何项目仓库——它跨项目，属于「这台机器」的配置。可以放同步盘（OneDrive 等）跨设备携带。改完逐项确认 `path` 存在：

```bash
migrate batch list                  # 列出注册表里所有项目，标记路径是否存在
```

注册表示例（路径写法跨设备兼容）：

```toml
schema_version = 1

[[projects]]
name = "smart-quotation"
path = "~/projects/smart-quotation"       # ~ 在 Windows/macOS/Linux 都展开
# profile = "machine-only"                # 可选：覆盖 manifest 的 profile

[[projects]]
name = "zk-ai"
path = "$HOME/AI/ZK-AI"                   # $VAR POSIX 风格

[[projects]]
name = "dianping"
path = "%USERPROFILE%/大众点评"            # %VAR% Windows 风格
```

**路径写法（跨设备兼容，关键）**：

| 写法 | 展开 | 适用场景 |
|---|---|---|
| `~/projects/foo` | 用户 home 目录 | 通用，任何 OS 都对 |
| `$HOME/projects/foo` | POSIX 环境变量 | 通用 |
| `%USERPROFILE%/projects/foo` | Windows 环境变量 | 注册表放同步盘跨 Windows 设备时最稳 |
| `D:/projects/foo` | 绝对路径 | 只在当前设备跑时可用，换设备就对不上 |

**注意**：TOML 基本字符串（双引号）里反斜杠是转义字符，Windows 绝对路径必须用正斜杠 `D:/foo` 或 TOML 字面量字符串（单引号 `'D:\foo'`）。

### 2. 批量计划（不打包，先看清单）

```bash
migrate batch plan                              # 逐项目列清单
migrate batch plan --only smart-quotation zk-ai # 只跑指定的
migrate batch plan --exclude dianping           # 排除指定的
```

`--only` / `--exclude` 按 `name` 过滤。拼错项目名会报错（不会静默跳过——批量执行最怕的就是「以为跑了其实没跑」）。

### 3. 批量导出（旧机器）

```bash
migrate batch export --passphrase-file pass.txt   # 所有项目共用一个口令
migrate batch export                              # 交互输入一次口令，所有项目共用
migrate batch export --per-project-passphrase     # 逐项目不同口令（逐个交互输入）
```

默认所有项目共用一个口令（交互输入一次，写临时文件传给每个单项目命令，结束后删）。逐项目不同口令用 `--per-project-passphrase`。

一个项目失败不中断其他项目。汇总报告列出每个项目的退出码和产物路径。批量退出码：全成功=0，否则=1。

### 4. 批量校验

```bash
migrate batch verify --passphrase-file pass.txt
```

逐项目找产物目录里最新的 `.enc` 包校验（不落盘）。

### 5. 批量导入（新机器）

```bash
migrate batch import --passphrase-file pass.txt           # 默认 skip（只写不存在的文件）
migrate batch import --on-conflict overwrite              # 覆盖
migrate batch import --dry-run                            # 只看计划
```

逐项目找产物目录里最新的 `.enc` 包导入。新机器上先把所有 `.enc` 包拷到各自项目的产物目录（`.migrate/` 或 manifest 里写的 `artifacts_dir`），再跑批量导入。

## 退出码

0 成功 / 1 一般错误 / 2 用法输入 / 3 目标冲突（.cmd 据此提供 overwrite 重试）/ 4 口令错或篡改 / 5 完整性失败 / 6 平台依赖缺失 / 7 安全拒绝（server 包禁 merge 等）/ 8 不支持（schema 版本过高等）。

## 必须停下来问用户确认

没有当次明确答复就停：

- 场景：换电脑 / 换服务器 / 两个一起
- manifest 终稿，尤其剔除 required 或纳入敏感目录
- 口令（导出两次；「丢了无法找回」要说到）
- 服务器地址（`user@host`；不进仓库、不进 manifest）
- 覆盖方式：skip / overwrite / keep-both / ask
- 是否改项目的忽略规则（产物目录与 `*.enc` 必须被排除）
- server 包写入系统路径或重启服务（引擎只给命令清单，不自行 sudo）

## 核心规则（引擎已实现，引用即可）

- 打包：内存 zip → OCMIG1 加密（PBKDF2 120k + 计数器密钥流 + HMAC 先验后解）；SQLite 走 backup API 快照，sidecar 不发货；导出后回读对账，坏包即删。
- 导入：成员预检（穿越/盘符/UNC/链接/清单外全拒绝）→ 冲突决策 → 事务写入（临时文件 + sha256 + 原子替换 + journal + 失败回滚）。
- 旧格式包（openssl `Salted__` 平铺 tar）：只读兼容，按内容标记判型，报告明示无 HMAC 认证。
- 脱敏：汇报只出 键名 / 体积 / sha256 前 8 位 / 存在性；禁出值、PEM、token、口令。

细节按需阅读：`references/classification.md`（分类与字段）、`references/package-format.md`（包格式与校验）、`references/conflicts.md`（冲突与确认）、`references/example-smart-quotation.md`（真实项目迁移）。

## 边界

- 不做整盘备份、不同步 git 已跟踪代码、不打依赖与缓存。
- 不自动合并两份数据库、不自动合并两份 env 键值。
- 不接受新 SSH 主机指纹（停下交人工），不自行 sudo 写系统文件。
- manifest 只进项目仓库，不进本技能仓库；技能内只放脱敏示例（`examples/`）。
