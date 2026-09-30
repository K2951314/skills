# 冲突、覆盖与确认

默认不覆盖已有文件。用户没点头之前，导入只写目标处不存在的路径。

## 冲突模式（--on-conflict）

| 模式 | 行为 |
|---|---|
| `skip`（默认） | 已存在的不动；只写不存在的。有跳过时退出码 3，提示可加 overwrite 重试 |
| `ask` | 逐项给体积/mtime/sha256 前 8 位，问 overwrite / skip / keep-both |
| `keep-both` | 写 `原名.incoming-<时间戳>`；env 文件**不自动合并键值**，只报告键名差异 |
| `overwrite` | 仍先备份，再原子替换 |

无论哪档：

- 覆盖前旧文件复制到 `<artifacts_dir>/backup-<时间戳>/`，保留相对路径。这份备份不进新包。
- 写入走临时文件 → fsync → sha256 → `os.replace`；替换失败从备份拷回。
- git 已跟踪文件一律跳过（以仓库为准）。包里出现已跟踪路径 = 清单可能写错，标为异常并跳过。
- 不在 `merge.include` 白名单的路径一律不写。
- SQLite 类条目：恢复库前，把目标旁已存在的 `-wal`/`-shm`/`-journal` 先备份再删除——旧 WAL 帧重放进新库 = `database disk image is malformed`。
- 数据库正被占用（用户说服务在跑）：拒绝覆盖，先停进程。

## 事务与崩溃恢复

- 全程 JSONL journal（`<artifacts_dir>/journal/import-<时间戳>.jsonl`）：backup / write / commit / rollback 各一行。
- 任一步失败：回滚本次导入已落盘的文件（有备份从备份恢复，无备份删除），然后报错停下。
- 进程被杀等未回滚场景：`migrate import <包> --recover` 不传包时按提示重放 journal；半成品临时文件删除，已提交但损坏的从备份恢复。

## 必须停下来问的步骤

1. **场景**：换电脑 / 换服务器 / 两个一起（先 server 后 workspace）。
2. **manifest 终稿**：剔除 `required` 必须逐项确认；纳入敏感目录（keys/、含服务器细节的笔记）要明说。
3. **口令**：导出输入两次；「丢了包永远解不开」必须说到。口令不进 argv/包/对话。
4. **服务器地址**：`user@host`；不进仓库、不进 manifest。SSH 强制 `BatchMode=yes` + `StrictHostKeyChecking=yes`：要密码或遇新指纹立即停下交人工，绝不自动接受 known_hosts。
5. **覆盖方式**：全跳过 / 逐项 / 保留双方 / 全覆盖。
6. **改忽略规则**：产物目录与 `*.enc` 不在忽略规则里时，先提议补规则，同意后再改。不为了打包去 `git add`。
7. **server 包**：禁止合并进 git 工作区（退出码 7）。上传只进新机暂存目录；`/etc` 与 systemd 由人工执行命令清单，引擎不自行 sudo。
8. **删除残留**：发现上次留下的明文 tar/env/dump，先报告路径，确认后才删。工具自己创建的临时文件由工具负责清理，不等确认。

## 导入后（不在包里，必须说清楚）

- 跑 manifest 的 `rebuild`（venv/依赖）与 `verify`（测试/起服务）。
- 换服务器必改：数据库连接串、对外访问来源（ALLOW_ORIGINS 之类）；换了 IP/域名要重发已发链接（token 在库里，不用重建账号）。
- 验证失败：停。不自动重导、不自动覆盖。报出失败命令和退出码。

## 对话里禁止出现

口令、env 值、连接串密码、私钥 PEM、token、`-pass pass:`、服务器 IP/邮箱写进可提交文件。

可以出现：键名、文件体积、项数、sha256 前 8 位、是否缺失、命令是否成功。
