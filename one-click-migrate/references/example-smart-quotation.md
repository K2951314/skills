# 示例：智能询价的迁移

本文件记录一个真实项目怎么用本技能。换到别的项目时复用判定方式，不要照抄路径。

## 当前状态（2026-09-30）

- 项目声明已落地：`E:\Ingulf\智能询价\.migrate\manifest.toml`（12 条目：8 workspace + 4 server）。引擎 `plan`/`manifest validate` 已在真实项目验证通过。
- 旧模块仍在项目内：`scripts/migrate.py`（export/import/merge）、`scripts/pack_workspace.sh`、`scripts/pull_from_server.sh`、`scripts/env_audit.py`、`scripts/migrate.sh`、两个 `.cmd` 启动器。它们是历史包的唯一读取途径，删除前必须完成下面的切换清单。

## 声明怎么从旧脚本翻译过来

| 旧实现 | manifest 落点 |
|---|---|
| `pack_workspace.sh` 的 ITEMS | `items[]`：`.env`/`keys` required+block；`quotation.db` sqlite；`deliverables`/`_archive`/`_*.md`/`backups/server` recommended+warn |
| `pull_from_server.sh` 采集项 | `scope=server` 项：`etc-sq.env`（file+sudo cat）、`sqdb`（pg_dump）、`Caddyfile`、`sq.service` |
| `env_audit.py` 常量表 | `[env_audit]` 的 tooling_only / defaulted_or_optional / deprecated_aliases / non_python_consumers / prod_required / server_only |
| `migrate.py cmd_merge` 白名单 | `[merge] include` |
| `SQ_PACK_DIR=_换机` | `artifacts_dir = "_换机"`（`.gitignore` 的 `_换机/` 与 `*.enc` 保留不动） |
| `--no-backups` | `[profiles.machine-only] exclude = ["backups-server"]` |

约 15 个只存在于服务器 `/etc/sq.env` 的键（SMTP_*、WX_PAY、ZFB_PAY、ADMIN_WECHAT_*、ADMIN_CONTACT_*、APP_URL、SUBSCRIPTION_NOTIFY_EMAIL）写在 `[env_audit] server_only`：只换电脑可以不带；之后要重建服务器，必须另打 server 包，否则收款码、订阅通知、联系入口静默消失，且每一步都不报错。

## 两类包与判型

引擎按**包内 manifest.kind** 判型；旧包（openssl `Salted__` 平铺 tar）没有 manifest，按内容标记判：`sqdb.dump`/`etc-sq.env` → server，`.env`/`keys` → workspace，两类都有保守判 server（误合并是泄露，漏合并只是少恢复几个文件）。server 包禁止合并进仓库（退出码 7）。

server 包内文件与用法：`sqdb.dump` → 新库建好后 `pg_restore --no-owner --no-privileges`；`etc-sq.env` → `/etc/sq.env`（chmod 640）；`Caddyfile`/`sq.service` → 覆盖前先 diff；`ENV-METADATA.txt` → 新机照齐版本（记录过的锚点：PostgreSQL 16.15、Python 3.12.3、Caddy v2.11.4，以包内文件为准）。换机必改 `DATABASE_URL` 与 `ALLOW_ORIGINS`；客户链接嵌了 IP，换 IP 要重发。

## 该项目特有的坑（改引擎时不要回退）

1. Windows 上 PATH 的 `bash.exe` 可能是 WSL 存根（没有 openssl/tar）。引擎的 legacy 路径从 git 安装位置反推 Git Bash 并实跑探测。
2. `deliverables/` 全是中文文件名。调外部工具一律参数数组，不拼 shell 字符串。
3. 子进程输出按 bytes 收再按平台解码（Windows GBK / Linux UTF-8）。`text=True` 会把真错误盖成 UnicodeDecodeError。
4. Git Bash 里 `getpass` 会因 `/dev/tty` 不存在挂死。引擎口令输入：stdin 非 tty 走 readline，EOF 一律当中止。
5. `.cmd` 纯 ASCII + CRLF，中文提示全在 Python 侧。`.cmd` 块内 echo 的括号要转义，否则双击一闪什么都没发生。
6. 明文 tar 不落盘。legacy 解密用管道/临时文件即删。
7. `_换机/` 与 `*.enc` 必须留在 `.gitignore`（曾经两个 `.enc` 躺在仓库根目录，一次 `git add -A` 就会把加密的密钥+数据库提交上去）。

## 切换清单（删旧模块前必须全绿）

按 `E:\Ingulf\my-skills` 仓库里的实施方案执行，顺序如下：

1. 引擎全测试绿（`pytest`，含 security/integration/legacy）。
2. 用引擎对智能询价做一次真实导出一轮：`migrate export` → `migrate verify` → 导入临时目录 → 跑 `verify` 命令。
3. `scripts/migrate.py` 改薄包装：保留 `export` / `import` / `merge` 子命令签名与中文提示，转调引擎 CLI（引擎路径找 `OC_MIGRATE_ENGINE` 环境变量 → 默认 `E:\Ingulf\my-skills\one-click-migrate` → PATH 上的 `migrate`）。
4. 项目测试迁移：`tests/test_migrate_package_kind.py`、`test_migrate_cli_language.py`、`test_env_audit.py` 的断言改指向引擎行为（kind 判型在 `migrate_engine/legacy.py` 与包内 manifest；产物目录断言改查 `manifest.toml` 的 `artifacts_dir`）。
5. 删 `scripts/migrate.sh`（只打印命令的名不副实向导）→ 删 `pack_workspace.sh` / `pull_from_server.sh` / `env_audit.py` → 更新 `docs/换机恢复指南.md` 与 `docs/machine-migration.md` 入口（保留 openssl 手工三步作为 legacy 兜底）。
6. 旧 `.enc` 包回归：任选一个历史包 `migrate verify` + `import --dry-run` 通过。
