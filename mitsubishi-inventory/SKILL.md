---
name: mitsubishi-inventory
description: 查询三菱官网实时库存。当用户说「查三菱库存」「这个有没有货」「WNMG080408-MA库存」「三菱现货」「日本有没有货」或需要确认某三菱型号的实时库存/到货情况时使用。返回上海仓和日本仓库存。注意：需要三菱官网账号凭证（MMC_USERNAME/MMC_PASSWORD 或 config.ini），且依赖三菱官网接口稳定。不用于查报价（用 quotation-helper）或跨品牌替代（用 tool-selection）。
agent_created: true
---

# 三菱库存查询

查三菱官网实时库存（上海仓 + 日本仓）。这是**付费功能**，需要三菱官网账号，且强依赖官网接口稳定。

## ⚠️ 当前状态：可能不可用

2026-11-18 实测，三菱官网返回 **HTTP 401**（账号凭证失效或官网结构变更），引擎已 **fail-closed 保护**——宁可返回"查不到"，也绝不返回可能错误的库存数字。因为**询价场景里，错的库存比查不到更危险**。

跑查询前先知道：如果输出里 `ok: false` + `自检失败`，不是 skill 坏了，是上游三菱接口/凭证问题。

## 用法

```bash
py -3.13 "E:/Ingulf/my-skills/mitsubishi-inventory/scripts/stock.py" WNMG080408-MA UC5115
```

只查型号不查材质：

```bash
py -3.13 "E:/Ingulf/my-skills/mitsubishi-inventory/scripts/stock.py" WNMG080408-MA
```

## 输出字段

| 字段 | 含义 |
|------|------|
| `shanghai_stock` | 上海仓库存 |
| `japan_stock` | 日本仓库存 |
| `exists` | 该型号是否存在于三菱产品库 |
| `diagnostics.canary` | 联网自检是否通过（false = 上游问题，别信结果） |

## 修复思路（当查不到时）

1. **401 错误** → 三菱账号凭证失效。设置环境变量或编辑 `config.ini`：
   - `MMC_USERNAME` / `MMC_PASSWORD` / `MMC_COOKIE`
   - 或 `E:/Ingulf/智能询价` 下的 `config.ini` 的 `[account]` 段
2. **网络不通** → 本机需能访问三菱官网
3. **官网改版** → GWT-RPC 解析靠位置启发式，改版后需人工核对 `_extract_stock` 解析逻辑

## 技术细节

引擎 `mitsubishi_stock.py` 的 `QueryEngine`：
- **5 分钟缓存**：相同 (型号, 材质) 从缓存返回，降三菱调用频次
- **金丝雀自检**：每次进程首次查询（及每 6 小时）先用已知型号探测一次，失败即 fail-closed
- **配额管理**：按「行」计量，1 行 = 1 次三菱 RPC，单次最多 50 行

## 什么时候用它 vs 其他 skill

- **三菱官网实时库存**（有没有货/日本仓多少）→ 本 skill
- **报价/含税价** → `quotation-helper`
- **跨品牌替代方案** → `tool-selection`
