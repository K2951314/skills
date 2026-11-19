---
name: quote-document
description: 报价单/合同生成。当用户说「出报价单」「给这几个型号出报价单」「导出 Word/PDF/Excel 报价单」「打印报价单」「做合同」「盖章」「俄语/日语报价单」「报价单要发给客户」或问完价下一步要正式单据时使用。把型号批量询价后走智能询价后端导出路由层，生成 docx/pdf/png/xlsx（5 语言可选、可盖章、自动编号）。不用于只查价格（用 quotation-helper）、查替代（用 tool-selection）或查库存（用 mitsubishi-inventory）。
agent_created: true
---

# 报价单生成 — 询价到正式单据

把「问价」变成「能发给客户的单据」。与 `quotation-helper` 的分工：那边输出一行可复制的「型号 含税X」，这边出带单号、买方、条款、合计大写的正式文件。

## 用法

脚本在本技能目录的 `scripts/quote_document.py`（`$SKILL` = 本技能目录，即 my-skills 仓库下的 `quote-document`）：

```bash
# 单型号，Word 报价单
py -3.13 "$SKILL/scripts/quote_document.py" "D4*11*50*d4" --qty 3 \
    --buyer "青岛示例机械有限公司" --format docx --out ./out

# 多型号 + 已建档客户（自动编号 + 自动补买方字段）
py -3.13 "$SKILL/scripts/quote_document.py" "D4*11*50*d4" "WNMG080408-MA" \
    --customer cust-001 --format pdf --seal

# 环境与能力自检（PDF/PNG 是否可用、库在哪、依赖齐不齐）
py -3.13 "$SKILL/scripts/quote_document.py" --check
```

常用参数：`--format docx|pdf|png|xlsx`、`--kind quote|contract`、`--locale zh|en|ja|ru|uz`、`--qty`、`--number`（手工单号）、`--customer` / `--customer-code`（自动编号必需）、`--valid-until`、`--payment-method`、`--columns`、`--seal` / `--seam-seal`、`--png-mode pages|long`、`--json`。

## 项目定位（换机可移植）

**环境变量 `SQ_HOME`（设了就必须有效，设错直接报错）**；未设置时探测技能仓库同级目录 → 用户 home。找不到中文报错，不写盘符。

## 已在实现里固化的判断（别绕过去）

1. **走路由层，不直接调导出模块**：单号分配、客户编码临界区、脱敏列过滤、审计落库、RFC 5987 中文文件名、公式注入前缀全在 `routes_export.py`。脚本用进程内 TestClient 调 `/api/export/*`，与 GUI 同一条代码路径——所以同一批型号出的单据逐字段一致。
2. **默认在数据库副本上跑**：复制 `quotation.db` 到临时目录，退出即删。**不消耗真实编号序列**。要用生产序号必须显式 `--in-place`（会写编号序列与审计），跑前先确认这次真的要占号。
3. **价格直传界面显示价，服务端不重算**：脚本取 `engine.quote()` 返回的 `fields.quote_price`，与网页报价台同源。不要在这里另算一遍折扣/税。
4. **有效期与付款方式都是可选项**：不传就不显示，**绝不补默认天数**。付款方式只接受稳定 code（`prepaid`/`cod`/`deposit`），本地化在渲染层。
5. **PDF/PNG 依赖 LibreOffice**：缺依赖时后端返回 503 `export.libreoffice_missing`，脚本转成中文提示并建议改 docx/xlsx。工作目录必须是 ASCII 临时路径（项目内已处理）。
6. **售后/数据告警不能吞**：面价是「¥99」这类文本时引擎按 0 计算并带 `warnings`，脚本原样打到 stderr。**看到告警要跟用户说，不能报成功就完事**。
7. **`.env` 必须加载**：`SQ_LIBREOFFICE_PATH`、三菱凭证都只在 `.env` 里。脚本按 `override=False` 补加载，不覆盖调用方显式传的环境变量。
8. **本机 `SQ_DEV=1` 与 `DATABASE_URL`/`SQ_LICENSE` 共存会拒绝启动**（项目安全断言）。脚本在副本上操作，所以主动剥掉生产信号；这是**刻意的**，不是绕过安全检查——它保证不会连到 PG 生产库。

## 输出口径

- 成功：`单据已生成: <绝对路径>` + 单号 + 格式 + 行数 + 字节；`--json` 给结构化结果。
- 询价无命中：stderr 逐条列出，全部无命中退出码 1。
- 导出失败：HTTP 状态 + 后端稳定错误码，`libreoffice_missing` / `number_not_confirmed` 附中文修复建议。
- 退出码：0 成功 / 1 无命中 / 2 定位或依赖失败 / 3 导出失败 / 4 参数非法。

## 边界

- **不自己拼 docx**：版式、占位符、印章、骑缝章都在导出套件里。要改版式去改项目模板，不是改技能。
- **不做价格重算**：报价口径唯一权威是 `QuotationEngine` + 界面显示价。
- **不连 PostgreSQL 生产库**：看到 `DATABASE_URL` 指向 PG 直接拒绝；生产要走 HTTP 接口。
- **不替用户决定是否占号**：默认副本模式；`--in-place` 由用户显式要求。
- **合同与报价单走同一入口**：`--kind contract`；合同默认格式 docx。
