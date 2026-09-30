# 包格式与校验

## 文件名

```text
<项目短名>-<kind>-<时间戳>.tar.gz.enc
```

`kind` 只能是 `workspace` 或 `server`。时间戳格式 `YYYYMMDD-HHMMSS`，用打包机器的本地时间，并在 manifest 里带时区。示例：

```text
smart-quotation-workspace-20260930-131500.tar.gz.enc
smart-quotation-server-20260930-131500.tar.gz.enc
```

文件名只是提示。导入以包内 manifest 和内容标记为准，不信文件名。

## 落点

默认 `<项目根>/_migrate/`。项目已有等价目录时沿用，不要新建第二套。智能询价用 `_换机/`。

该目录和 `*.enc` 必须出现在忽略规则里。导出前检查；没有就先补规则再打包。加密包躺在仓库根目录时，一次 `git add -A` 就会把密钥和数据库提交上去。

## 包内布局

```text
manifest.json          # 不含密钥值
payload/...            # 与清单 path 对应的相对路径
```

`manifest.json` 字段：

```json
{
  "format": 1,
  "kind": "workspace",
  "project": "smart-quotation",
  "created_at": "2026-09-30T13:15:00+08:00",
  "hostname_hint": "dev-pc",
  "items": [
    {"id": "env-local", "path": ".env", "bytes": 1204, "sha256": "<hex>", "class": "required"}
  ],
  "rebuild": ["python -m venv .venv", "pip install -r requirements.txt"],
  "excluded_note": ["node_modules", ".venv", "logs"]
}
```

`sha256` 针对进包前的明文文件。数据库伴随文件各自一条。服务器转储的 `path` 用包内名（如 `sqdb.dump`）。manifest 在加密包内，可以保留当次确认过的主机别名，不要保留完整连接串。`remote_path` 不写入可被 git 提交的文件。

## 加密

- 算法：`openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000`
- 口令来自环境变量或 stdin。禁止 `-pass pass:...` 出现在进程参数里。
- 口令至少 12 位，且不能与项目名、主机名相同。导出时输入两次。
- 口令不进包、不进清单、不进对话。丢了无法找回，导出前必须说明。
- 明文 tar、明文 env、明文 dump 不落盘。打包用 `tar -czf - | openssl enc ...`，导入反过来。临时目录用 `trap` 或 `try/finally` 删除，中途失败也要删。

## 导出校验

不过就不出包。

1. 每个 `required` 且 `on_missing: block` 的项存在，体积大于 0。
2. 清单外没有把 `refuse` 类目录打进去。抽查包内路径前缀。
3. 加密完成后立刻解密到管道，重算每个文件的 sha256，与 manifest 一致。失败就删除 `.enc`。
4. 服务器 dump 非空，且文件头符合格式。PostgreSQL 自定义格式以 `PGDMP` 开头。空 dump 一律失败。
5. 对话里只报告项数、id、体积、是否敏感、总大小、输出路径。不报告文件内容和环境变量的值。

## 导入校验

不过就不覆盖。

1. 解密失败就停。不要重试超过 3 次。
2. 先核对项数与 sha256，再谈覆盖。校验失败的项不写入目标。
3. 包内路径必须是相对路径。拒绝 `..`、绝对路径、盘符、指向包外的符号链接。
4. `kind=server` 的包禁止合并进 git 工作区。
5. 导入后再算一次 sha256。不一致就从备份恢复该文件，并报告哪一项失败。

## 对照表

导出结束和导入结束都打这张表。列可以少，不能多报内容。

| id | 类别 | 体积 | sha256 前 8 位 | 结果 |
|---|---|---|---|---|
| env-local | required | 1.2 KB | a1b2c3d4 | 已写入 / 已跳过 / 失败已回滚 |

哈希前 8 位只供人眼对照是不是同一份。安全边界是包内完整哈希。
