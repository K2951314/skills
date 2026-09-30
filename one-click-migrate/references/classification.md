# 必要数据分类规则

扫描时只读路径、大小、mtime、忽略规则命中情况。不打开密钥文件内容，不把值写进清单、对话或日志。

## 四类判定

| 类别 | 含义 | 默认动作 |
|---|---|---|
| `required` | 被排除，且运行、部署或签发依赖它，无法从 git 或包管理器重建 | 纳入清单，缺失则阻断导出 |
| `recommended` | 被排除，丢了能跑，但会丢业务数据、运维记录或历史快照 | 纳入清单，缺失只警告 |
| `rebuildable` | 被排除，但可从锁文件、镜像或首次运行重建 | 永不打包 |
| `refuse` | 被排除，且打包会扩大泄露面或没有恢复价值 | 永不打包，除非用户逐项点名并再次确认 |

判定顺序：先套 `refuse`，再套 `rebuildable`，再套项目清单里的显式条目，最后才用启发式。显式清单永远压过启发式。

## 通用 refuse

- 依赖与缓存：`node_modules/`、`.venv/`、`venv/`、`__pycache__/`、`.pytest_cache/`、`.mypy_cache/`、`.ruff_cache/`、`.tox/`、`dist/`、`build/`、`out/`、`coverage/`、`.nyc_output/`
- 编辑器与 AI 工具目录：`.vscode/`、`.idea/`、`.cursor/`、`.claude/`、`.codebuddy/`、`.workbuddy/`、`.workbuddy-ai/`、`.codex/`、`.continue/`、`.copilot/`
- 流水与临时：`logs/`、`*.log`、`tmp/`、`scratch/`、`sandbox/`、`*.tmp`、`*.cache`、`*.swp`
- 操作系统垃圾：`.DS_Store`、`Thumbs.db`、`Desktop.ini`
- 本技能产物目录（默认 `_migrate/`）以及任何 `*.enc`。打包进去会把上一份密钥包再包一层

## 通用 rebuildable

- 锁文件若已被 git 跟踪，不重复打包。若被 gitignore，且用户要锁死依赖版本，标 `recommended`，不要标 `required`。
- Docker 镜像层、`pip` / `npm` 可重建的环境。清单里写重建命令，不写进包。

## 通用 required 启发式

命中下面任一条，且当前被忽略，且文件真实存在，默认 `required`。不存在则记入「缺失」，导出前必须让用户看到。

- 环境文件：`.env`、`.env.*`。排除 `.env.example`、`.env.sample`、`.env.template`，这些应在 git 里。
- 密钥材料：`*.pem`、`*.key`、`*.p12`、`*.pfx`、`secrets.json`、`credentials.json`、`token.txt`、`keys/`
- 本地覆盖配置：`config.local.*`、`config.production.*`、`*.local.json`、`config.ini`、`admin-config.local.json`
- 应用数据库：`*.db`、`*.sqlite`、`*.sqlite3`，以及同名 `-wal` / `-shm` / `-journal`。三个伴随文件必须一起收，否则导入后可能是半截事务。

启发式命中后仍要人工过目。一个被忽略的 `*.csv` 可能是客户价格表（`recommended`），也可能是已放行的测试夹具，根本不该出现在未跟踪列表里。

## 通用 recommended 启发式

- `backups/`、`data/`、`uploads/`、`storage/`、`media/`
- 被忽略的业务表格：`*.xlsx`、`*.xls`、`*.ods`、`*.csv`（测试夹具除外）
- 被忽略的运维笔记：`_*.md`、`_LOCAL-GUIDE.md`、`_DEPLOYMENT-*.md`
- 体积异常大的历史快照：默认只收最新一份，更早的标 `archive`，不进包，除非用户点名

## 服务器侧启发式

只在用户确认「这是部署机」之后使用。不要把开发机上的同名文件当成服务器权威数据。

| 资产 | 常见位置 | 类别 |
|---|---|---|
| 进程环境 | `/etc/<app>.env`、systemd `EnvironmentFile=` 指向的文件 | required |
| 数据库转储 | `pg_dump -F c`、`mysqldump`、SQLite 文件本身 | required |
| 反向代理 | Caddyfile、nginx 站点配置 | recommended |
| 进程托管 | systemd unit | recommended |
| 版本锚点 | 数据库、运行时、代理的版本号，加拉取时间 | required（元数据，不含密钥值） |
| TLS 私钥 | `/etc/letsencrypt/live/` 或自签 `*.pem` | 默认 refuse。只有用户明确说新机器没有域名、必须搬现有证书，才纳入 |

服务器路径不写死。示例项目的具体路径见 `example-smart-quotation.md`。

## 清单字段

```yaml
id: env-local                 # 稳定 id，导入时做冲突键
path: .env                    # 相对项目根；服务器项用 remote 而不是 path
class: required               # required | recommended | archive
why: 本地开发启动依赖 JWT_SECRET 等
source: gitignore             # gitignore | heuristic | user | manifest
exists: true
bytes: 1204
mtime: 2026-09-20T08:11:02+08:00
sensitive: true               # true 时对话里只报键名和体积
on_missing: block             # block | warn | skip
```

服务器项额外字段：`remote`（当次确认的主机，不写入可提交文件）、`remote_path`、`capture`（`file` 或 `command`）。`capture: command` 的输出是生成物，不是把命令文本打进包。
