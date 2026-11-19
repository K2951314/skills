# 验证与调试入口

一条命令回答「这些技能现在到底能不能用」。换机后先跑这个，别等用到时才发现死路径。

## 全量验收

```bash
py -3.13 tools/verify_all.py                # 13 项技能验收（含负例）
py -3.13 tools/verify_all.py --with-zkai    # 追加 ZK-AI 配置体检 + 巡检探针
py -3.13 tools/verify_all.py --json         # 机器可读
py -3.13 tools/verify_all.py --only quote-document,failure-diagnosis
```

输出每项一行：状态 / 耗时 / 首行结论。末尾给「通过 / 失败 / 跳过」三数。

**判据口径**（这份文件存在的意义就是让判据不被稀释）：

- **正例 + 负例成对**。只有成功案例证明不了技能会报错——每条负例都在验证「退出码
  语义」而不是报错文案（文案会改，语义是契约）。
- **SKIP 不是失败**。找不到项目就跳过并说明原因；换机后没装项目是常态，不能因此
  报红，否则红色会被训练成「忽略它」。
- **允许失败得可说**。三菱库存上游 401 时按设计返回退出码 3 + 结构化错误，
  `allow_exit=(0, 3)` 视为通过——判据是「没有静默给可疑数字」，不是「必须成功」。
- **只读**。报价单在库副本上跑；产物落 `.verify-out/` 并自动清理。

退出码：`0` 全通过 / `1` 有失败 / `2` 脚本自身错误（含解释器版本不对）。

## 单点调试

| 想查什么 | 命令 | 看什么 |
|---|---|---|
| 报价单环境与 PDF/PNG 能力 | `py -3.13 quote-document/scripts/quote_document.py --check` | `db_mode`、`export.pdf/png`、`quotation_db_exists` |
| LLM 走哪条路、通不通 | `py -3.13 智能询价/scripts/probe_llm.py` | `route`（gateway/direct）、`http_status`、建议 |
| LLM 只看配置不发请求 | `py -3.13 智能询价/scripts/probe_llm.py --dry-run` | 温度/超时/限流预算 |
| 三菱库存探针 | `SQ_HOME=... py -3.13 AI/ZK-AI/scripts/signal_probes.py` 无 CLI，用 `--with-zkai` | `ok/reason/detail` |
| 换机可移植性 | `py -3.13 skill-inventory/scripts/portability_lint.py` | 盘符硬编码 / 版本声明 / 挂载覆盖 |

## 环境变量

技能与探针都遵循同一套语义：**设了就必须有效，设错直接报错，绝不静默回退**。

| 变量 | 用途 | 不设时 |
|---|---|---|
| `SQ_HOME` | 智能询价项目根 | 探测技能仓库同级 → 用户 home |
| `SS_HOME` | smart-selection 项目根 | 同上 |
| `LLM_TIMEOUT_SEC` | LLM 单次超时 | 8 秒（经网关建议 20） |
| `LLM_PARAM_FALLBACK` | 上游拒温度时是否去掉重试 | 开（`0` 关闭） |
| `SQ_LIBREOFFICE_PATH` | PDF/PNG 依赖 | 从 PATH 探测 `soffice` |

## 已知的「假绿」与「假红」

这两类都实测踩过，写下来免得下次又当成真问题：

- **假绿**：`quote-document` 初版只给 `--seam-seal` 不给 `--seal` 时静默降级成
  「不盖章但报成功」。已改成退出码 4。**任何「报成功但做的事少了」都是缺陷。**
- **假绿**：`verify_all.py --only skill-inventory` 曾连带报一条「原文件未被覆盖」
  通过，而那条用例根本没跑（哨兵文件不存在 → 检查直接返回 PASS）。
  已改成「相关用例没跑就不做这项检查」。**没执行的检查不许报通过。**
- **静默无效**：`quote-document` 收了 `--locale` 却从没放进请求，等于参数形同虚设。
  实测 `--locale en` 出的仍是中文单据。已修，并加英文产物断言。
- **静默覆盖**：同名单据会覆盖已有文件（财务文件被替换是事故）。已改成改存 `-2`
  并提示；验收用「预置哨兵字节 + 跑完比对未变」来证明，而不是只看提示文案。
- **静默丢数据**：`llm.probe()` 拿不到上游错误原因——`HTTPError` 的 body 只能读
  一次，温度判定先读走了，诊断报告只剩 `HTTP Error 400: error`。已改成读一次即缓存。
- **假红**：巡检探针用错解释器会多报失败——smart-selection 的 `_health.py`
  用项目 venv 跑是 4 条闸门失败，系统 3.13 是 1 条。探针结论里带 `interpreter`
  字段就是为了让这类误报可归因。
- **假红**：`mitsubishi-inventory` 的 401 是上游凭证问题，不是技能故障。
  判据看 `diagnostics.canary`，不看退出码本身。

## 改动后必跑

```bash
py -3.13 tools/verify_all.py
cd 智能询价 && py -3.13 -m pytest tests/test_llm_gateway.py tests/test_export_api.py -q
cd AI/ZK-AI && .venv/Scripts/python.exe -m pytest -q && .venv/Scripts/python.exe -m ruff check scripts/
```
