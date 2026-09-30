# 包格式与校验（v1，已实现）

零第三方依赖：Python 标准库（`zipfile` + `hashlib`/`hmac`）。业务机只要有 Python 3.11+ 就能导能导能入——不依赖 openssl、tar、7-Zip。

威胁模型（诚实声明）：防「U 盘丢失 / 网盘同步泄露 / 误拷」。口令强度是唯一屏障，PBKDF2 120k 轮抗离线暴力，HMAC 抗篡改。它不替代本机磁盘加密，不提供前向保密，也不防拿到你机器的人。

## 容器布局

```text
blob = b"OCMIG1"(6) || salt(16) || mac(32) || cipher
cipher = payload_zip XOR keystream
keystream = sha256(enc_key || counter_le64) 分块拼接（计数器模式）
dk = PBKDF2-HMAC-SHA256(passphrase, salt, 120_000, dklen=64)
enc_key = dk[:32]；mac_key = dk[32:]
mac = HMAC-SHA256(mac_key, b"OCMIG1" || salt || cipher)
payload_zip = manifest.json + 逐文件 payload（正斜杠相对路径，ZIP_DEFLATED）
```

解密顺序（失败即停，目标零改动）：

1. 验 HMAC —— 口令错或密文被改，此处失败，退出码 4；
2. 解密得 zip；
3. 解 zip、逐成员预检（见下）；
4. 逐文件 sha256 与 manifest 清单对账，退出码 5。

随机 salt 保证同一口令两次导出的密文不同，无跨包密钥流复用。

## 文件名与落点

```text
<project>-<kind>-<YYYYmmdd-HHMMSS>.enc
```

`kind` 只有 `workspace` / `server`。落点 `<项目根>/<artifacts_dir>/`（manifest 里声明，智能询价沿用 `_换机`，默认 `.migrate`）。该目录与 `*.enc` 必须在项目忽略规则里；导出前检查，没有先补规则再打包。备份落在 `<artifacts_dir>/backup-<时间戳>/`，事务日志在 `<artifacts_dir>/journal/`。

## 包内 manifest.json

```json
{
  "format": 1,
  "kind": "workspace",
  "project": "smart-quotation",
  "engine": "1.0.0",
  "created_at": "2026-09-30T22:40:23+08:00",
  "hostname_hint": "dev-pc",
  "items": [
    {"id": "env-file", "path": ".env", "bytes": 1204, "sha256": "<hex>",
     "class": "required", "sensitive": true, "item_type": "file"}
  ],
  "rebuild": ["python -m venv .venv"],
  "verify": ["python -m pytest tests/ -q"]
}
```

`sha256` 针对**进包的字节**（sqlite 项是对快照字节算的，不是原库文件）。目录/glob 项展开为逐文件条目。不含任何密钥值。

## 导出流程

1. `plan` 解析 manifest → 逐条目展开、算 sha256；`required` 且 `on_missing=block` 缺失 → 中止（退出码 1）。
2. `sqlite` 项走在线 backup API 生成崩溃一致快照（`Connection.backup` + `serialize`），全程内存；**-wal/-shm/-journal 不发货**。
3. 内存组 zip → 加密 → 写 `.enc`。
4. **导出后回读**：解密 → 解 zip → 逐文件重算 sha256 对账。不一致就删掉这个 `.enc` 并报错——不留「看起来成功」的坏包。

## 导入流程

1. 验 HMAC → 解密 → 解 zip。
2. **成员预检（落盘前）**：拒绝 `..`、绝对路径、盘符、UNC、反斜杠形态、NUL、`.` 段、目录条目、符号链接成员、清单外成员、超限（成员 ≤10000、单文件 ≤1GiB、清单对账）。
3. 冲突决策（默认 `skip`）：`skip` 只写目标处不存在的；`keep-both` 写 `原名.incoming-<时间戳>`；`overwrite` 先备份再替换；`ask` 逐项交互。git 已跟踪文件一律跳过。不在 `merge.include` 白名单的路径一律跳过。
4. **SQLite 特殊规则**：恢复库前，把目标旁已存在的 `-wal`/`-shm`/`-journal` 先备份再删除——旧 WAL 帧重放进新库会直接损坏数据库（zk-ai 真实事故）。
5. 事务式写入：临时文件 → fsync → sha256 → `os.replace` 原子替换；JSONL journal 全程记录。任一步失败，**回滚本次导入已落盘的文件**（有备份从备份恢复，无备份删除），之后可 `import --recover <journal>` 重放恢复。
6. 收尾打印：写入/跳过/保留双方计数、备份目录、journal 路径、包内 `rebuild`/`verify` 命令。验证失败就停，不自动重导。

## 退出码（.cmd 启动器据此分支）

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 一般错误 |
| 2 | 用法/输入错误 |
| 3 | 目标冲突（已存在被跳过；.cmd 据此提供 `--on-conflict overwrite` 重试） |
| 4 | 口令错误或包被篡改（HMAC 失败） |
| 5 | 完整性失败（解密后哈希不符） |
| 6 | 平台依赖缺失 |
| 7 | 安全拒绝（server 包禁 merge 等） |
| 8 | 不支持（schema 版本过高等） |

## 旧格式包（legacy）

智能询价历史包是 `openssl aes-256-cbc -pbkdf2 -iter 200000` 的平铺 tar，文件头为 `Salted__`。legacy 导入路径（阶段 4 落地）只读兼容：

- 按内容标记判型（`sqdb.dump`/`etc-sq.env` → server；`.env`/`keys` → workspace；两类都有保守判 server）；
- 过同一套成员预检与事务写入；
- 报告里明示「旧包无 HMAC 认证」；
- 旧包不强制新口令长度规则。

## 汇报脱敏

只出现：id、类别、体积、sha256 前 8 位、存在性、命令结果。禁止出现：口令、env 值、PEM、token、连接串密码、`-pass pass:`。测试用假密钥值断言输出不含它们。
