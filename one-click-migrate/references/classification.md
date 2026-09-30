# 必要数据分类规则

扫描只读路径、大小、mtime、忽略规则命中。不打开密钥文件内容，不把值写进清单、对话或日志。

## 四类判定

| 类别 | 含义 | 去向 |
|---|---|---|
| `required` | 被排除，且运行/部署/签发依赖它，无法从 git 或包管理器重建 | 必须进 manifest；缺失默认阻断导出 |
| `recommended` | 被排除，丢了能跑，但会丢业务数据、运维记录或历史快照 | 进 manifest；缺失只警告 |
| `rebuildable` | 被排除，但可从锁文件、镜像或首次运行重建 | **只属启发式，不得写进 manifest**；清单里写重建命令（rebuild） |
| `refuse` | 被排除，且打包会扩大泄露面或没有恢复价值 | **只属启发式，不得写进 manifest**；除非用户逐项点名并再次确认 |

判定顺序：先套 `refuse`，再套 `rebuildable`，再套 manifest 里的显式条目，最后才用启发式。显式条目永远压过启发式。

## 通用 refuse（不进任何包）

依赖与缓存：`node_modules/`、`.venv/`、`venv/`、`__pycache__/`、`.pytest_cache/`、`.mypy_cache/`、`.ruff_cache/`、`.tox/`、`dist/`、`build/`、`out/`、`coverage/`、`.nyc_output/`

编辑器与 AI 工具目录：`.vscode/`、`.idea/`、`.cursor/`、`.claude/`、`.codebuddy/`、`.workbuddy/`、`.workbuddy-ai/`、`.codex/`、`.continue/`、`.copilot/`

流水与临时：`logs/`、`*.log`、`tmp/`、`scratch/`、`sandbox/`、`*.tmp`、`*.cache`、`*.swp`

系统垃圾：`.DS_Store`、`Thumbs.db`、`Desktop.ini`

引擎自身产物：`<artifacts_dir>/`（默认 `.migrate`，智能询价是 `_换机/`）、任何 `*.enc`、`backup-*/`、`journal/`

TLS 私钥：`/etc/letsencrypt/live/`、自签 `*.pem`——默认 refuse；证书可重新签发，私钥进包等于多一份泄露面。只有用户明确说「新机器没有域名、必须搬现有证书」才纳入。

## 通用 required 启发式（plan 会列为候选）

- 环境文件：`.env`、`.env.*`（排除 `.env.example`/`.sample`/`.template`，这些应在 git 里）
- 密钥材料：`*.pem`、`*.key`、`*.p12`、`*.pfx`、`secrets.json`、`credentials.json`、`token.txt`、`keys/`
- 本地覆盖配置：`config.local.*`、`config.production.*`、`*.local.json`、`config.ini`
- 应用数据库：`*.db`、`*.sqlite*`（建议 `item_type = "sqlite"`）

## 通用 recommended 启发式

`backups/`、`data/`、`uploads/`、`storage/`、被忽略的业务表格（`*.xlsx`/`*.csv`/`*.ods`，测试夹具除外）、运维笔记（`_*.md`）。体积异常大的历史快照：默认只收一份，更早的注明「按需单独拷贝」。

## 服务器侧

只在用户确认「这是部署机」后使用。不要把开发机上的同名文件当成服务器权威数据。

| 资产 | item_type / capture | 说明 |
|---|---|---|
| 进程环境文件（`/etc/<app>.env`） | `file` + `capture = "file"` | sudo cat 采集；空文件/读不到按 on_missing 处理 |
| PostgreSQL | `pg` + `capture = "pg_dump"` | 需 `pg_db`；校验 `PGDMP` 头；空 dump 失败 |
| 反向代理 / systemd unit | `file` | 覆盖前先 diff |
| 版本锚点 | 引擎自动生成 `ENV-METADATA.txt` | 新机照齐版本，不含密钥值 |

## manifest 字段（`.migrate/manifest.toml`，TOML）

```toml
schema_version = 1
project = "smart-quotation"        # slug，包名前缀
artifacts_dir = "_换机"             # 产物目录；必须在忽略规则里

[[items]]
id = "env-file"                    # 稳定 id，导入冲突键
path = ".env"                      # 项目内相对路径；glob 支持 * ?
class = "required"                 # required | recommended | archive
item_type = "file"                 # file | dir | glob | sqlite | pg
scope = "workspace"                # workspace | server | both
on_missing = "block"               # block | warn | skip
sensitive = true                   # 汇报只出键名与体积
review_status = "confirmed"        # confirmed | needs-review | skipped_by_user
note = "…"
# server 条目附加：
remote_path = "/etc/sq.env"        # 服务器路径（主机地址不进文件）
capture = "file"                   # file | pg_dump
pg_user = "postgres"               # capture=pg_dump 时
pg_db = "sqdb"                     # capture=pg_dump 时必填

[profiles.machine-only]            # --profile 选择
exclude = ["backups-server"]

[merge]                            # 导入合并白名单；不在名单的路径一律不写
include = [".env", "keys"]

rebuild = ["python -m venv .venv"] # 记录进包内清单，引擎不自动执行
verify = ["python -m pytest tests/ -q"]

[env_audit]                        # 键名审计策略；值永不入库
code_dirs = ["backend", "scripts"]
tooling_only = ["SQ_DEV"]
defaulted_or_optional = ["LLM_MODEL"]
deprecated_aliases = { EMAIL_FROM = "SMTP_FROM" }
non_python_consumers = ["SQ_PYTHON"]
prod_required = ["JWT_SECRET"]
server_only = ["SMTP_HOST"]
```

陷阱提醒：TOML 里 `[[items]]` 之后的顶层键会被收进最后一个条目。`rebuild`/`verify` 必须放在第一个 `[[items]]` 之前。条目级未知字段引擎会报错（不静默忽略），正是为了拦住这个错。

## 声明与生成的分离

- manifest（人写，可提交）：该搬什么。
- 包内 manifest.json（引擎生成）：这一包装了什么——逐文件 bytes/sha256/item_type/class。
- 运行快照（exists/bytes/mtime）绝不写回项目 manifest，避免每次运行制造 diff。
