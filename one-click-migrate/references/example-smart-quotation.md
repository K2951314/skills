# 示例：智能询价

本文件是分类规则在一个真实项目上的落点。换到别的项目时不要照抄路径，只复用判定方式。

项目原则：git 管代码，加密包管 git 不管、又无法重建的东西。

本机开发目录是 `E:\Ingulf\智能询价`。技能正文和本仓库都不记录该项目的服务器地址、账号和密钥值。

## 已有实现

调用这些入口，不要重写。

| 入口 | 作用 |
|---|---|
| `scripts/migrate.py export` | 本机资产打包；可加 `--with-server` |
| `scripts/migrate.py import <enc> <outdir>` | 解密并按内容分流 |
| `scripts/pack_workspace.sh` | 本机包 |
| `scripts/pull_from_server.sh <user@host>` | 服务器包 |
| `scripts/migrate.sh --machine\|--server\|--both` | 场景选择 |
| `scripts/env_audit.py` | 只审计键名，导出后报缺失和死键 |
| `scripts/migrate_machine.cmd` | 双击启动器。纯 ASCII + CRLF，不能写中文 |

产物目录是 `_换机/`。`.gitignore` 已排除该目录，另有 `*.enc` 兜底。

## 本机包

`pack_workspace.sh` 当前清单：

| 路径 | 类别 | 缺失时 |
|---|---|---|
| `.env` | required | 阻断。本地起不来 |
| `.env.server` | required（若本机存在） | 阻断本机包，但它不是服务器权威配置 |
| `keys/` | required | 阻断。RSA 对不上则已发 license 失效 |
| `quotation.db` | recommended | 警告。本地开发数据；生产库在服务器 PostgreSQL |
| `deliverables/` | recommended | 警告 |
| `_archive/` | recommended | 警告 |
| `_*.md` | recommended | 警告。含部署记录，敏感 |
| `backups/` 最新一份 json，加 `backups/server` | recommended | 警告。更早的大快照不进包 |

导出后必须跑 `env_audit.py`。约 15 个键只在服务器 `/etc/sq.env`，不在本机 `.env.server`：

```text
APP_URL
SUBSCRIPTION_NOTIFY_EMAIL
SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD SMTP_USE_TLS SMTP_FROM SMTP_FROM_NAME
WX_PAY ZFB_PAY
ADMIN_WECHAT_QR ADMIN_WECHAT_ID ADMIN_CONTACT_PHONE ADMIN_CONTACT_EMAIL
```

只换电脑可以不带这些键。之后还要重建服务器，就必须另打 server 包。否则收款码、订阅通知和联系入口会静默消失，且每一步都不报错。

## 服务器包

不要合并进仓库。识别依据是内容里有 `sqdb.dump` 或 `etc-sq.env`，不看文件名。

| 包内文件 | 来源 | 新机器用法 |
|---|---|---|
| `sqdb.dump` | `pg_dump -F c -d sqdb` | `pg_restore --no-owner --no-privileges` |
| `etc-sq.env` | `/etc/sq.env` | `/etc/sq.env`，权限 `640`，属主 `root:www-data` |
| `Caddyfile` | `/etc/caddy/Caddyfile` | 覆盖前先 diff |
| `sq.service` | `/etc/systemd/system/sq.service` | daemon-reload 前先 diff |
| `ENV-METADATA.txt` | 版本与拉取时间 | 新机对齐版本，不含密钥值 |
| `server-keynames.txt` | 只含键名，落在 `_换机/`，不进加密包 | 给导出后审计用 |

版本以包内 `ENV-METADATA.txt` 为准，不要凭记忆。记录过的对照值是 PostgreSQL 16.15、Python 3.12.3、Caddy v2.11.4。

新服务器上必须改的值：`DATABASE_URL`、`ALLOW_ORIGINS`。客户链接里嵌了地址，换地址后要重发链接。token 在库里，不用重建公司。

## 不打包

`.venv/`、`__pycache__/`、`logs/`、`.workbuddy/`、测试夹具、`_换机/` 自身、全部 `*.enc`。

`backups/` 只带最新一份。更早的快照需要时从服务器备份脚本重拉。

## 分流

| 内容标记 | 判定 | 下一步 |
|---|---|---|
| 有 `.env` 或 `keys/`，且无 `sqdb.dump` / `etc-sq.env` | workspace | 问一句后合并，git 已跟踪的跳过 |
| 有 `sqdb.dump` 或 `etc-sq.env` | server | 禁止合并，只给出上新服务器的步骤 |
| 两类标记同时出现 | server | 误合并是泄露，漏合并只是少恢复几个文件 |

## 不要回退的坑

1. Windows 上 PATH 的 `bash.exe` 经常是 WSL 存根，没有 tar 和 openssl。从 git 安装位置反推 Git Bash，并真跑一次 `command -v tar` 和 `command -v openssl`。
2. 解包走 Git Bash 的 GNU tar。另一份 tar 处理中文路径会报 `Invalid empty pathname`。
3. 子进程输出按 bytes 收，再按 GBK 解码并替换无法解码的字节。文本模式会把 tar 的真错误盖掉。
4. Git Bash 里 `getpass` 会因 `/dev/tty` 不存在而挂死。管道输入走 `readline()`。EOF 必须当中止，不能当成空口令再问。
5. `.cmd` 块内 `echo` 的括号要转义，否则双击窗口一闪，报错来不及显示。
6. 明文 tar 不落盘。口令只走环境变量或 stdin，不进 argv。
7. 调用 bash 用参数数组，不要拼 shell 字符串。目标路径可能含中文或空格。
