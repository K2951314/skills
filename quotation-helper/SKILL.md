---
name: quotation-helper
description: 数控刀具智能报价查询。当用户说「查报价」「这个多少钱」「报个价」「含税价多少」「算利润率」「D4*11*50*d4报价」或给一个型号/规格问价格时使用。输入型号，返回面价、折扣率、报价和可直接复制的「型号 含税X」文本。基于本地报价数据库（多公司/多租户）。不用于查替代选型（用 tool-selection）或实时库存（用 mitsubishi-inventory）。
agent_created: true
---

# 智能报价 — 刀具报价查询

一句话查型号报价。数据来自本地报价库（3815+ 条真实数据，17 家公司多租户）。

## 用法

```bash
py -3.13 "E:/Ingulf/my-skills/quotation-helper/scripts/quote.py" "D4*11*50*d4"
```

指定公司（多租户）：

```bash
py -3.13 "E:/Ingulf/my-skills/quotation-helper/scripts/quote.py" "D4*11*50*d4" --company default
```

## 输出字段

| 字段 | 含义 |
|------|------|
| `item_key` | 型号/规格 |
| `name` | 产品名 |
| `face_price` | 面价（原始价） |
| `discount_percent` | 折扣率（默认 55，可被规则覆盖） |
| `quote_price` | 报价（面价 × 折扣 / 100，含税） |
| `copy_text` | 可直接发给客户的文本，如「D4*11*50*d4 含税18.1」 |
| `matched_rule` | 命中的报价规则 ID（`default` 表示默认折扣） |

## 什么时候用它 vs 其他 skill

- **查价格/报价** → 本 skill
- **查型号替代方案**（A 能不能替代 B）→ `tool-selection`
- **查三菱官网实时库存**（有没有货）→ `mitsubishi-inventory`

## 技术细节

报价直接调 `QuotationEngine`，绕过 FastAPI 的认证/限流层，本地快查无需登录。数据库在 `E:/Ingulf/智能询价/quotation.db`。

报价公式（可在后台配置中心改）：`face_price * discount_percent / 100`，默认 55 折。非数值面价（如「¥99」文本）会标 `warnings`，报价按 0 算——**看到 warnings 要留意**。

## 多公司说明

`--company` 对应后台 GUI 配置中心里按公司隔离的价格/折扣/品牌规则。`default` 是默认租户。换公司前先确认该 `company_id` 存在（可查 `quotation.db` 的 `companies` 表）。
