---
name: tool-selection
description: 跨品牌数控刀具替代选型查询。当用户说「查替代」「找替代」「选型」「这个型号能替代什么」「WNMG080408的替代」「跨品牌选型」或给一个刀片/刀杆型号问替代方案时使用。输入一个型号，返回 ISO 解析、尺寸参数和三菱/泰珂洛/京瓷等跨品牌可替代的刀杆列表。不用于查报价（用 quotation-helper）或查实时库存（用 mitsubishi-inventory）。
agent_created: true
---

# 智能选型 — 跨品牌刀具替代查询

一句话输入型号，拿到跨品牌替代清单。数据来自三菱/泰珂洛/京瓷/株洲等品牌样本解析。

## 用法

用户给一个型号（如 `WNMG080408-MA`），跑：

```bash
py -3.13 "E:/Ingulf/smart-selection/web/substitute.py" WNMG080408-MA
```

**必须用 Python 3.13**。项目里有 pyc 版本绑定，用错版本会静默返回空（不报错），极易误判为"没有替代品"。

批量查询（招标清单，多行型号）：

```bash
py -3.13 "E:/Ingulf/smart-selection/web/substitute.py" "型号1
型号2
型号3"
```

## 输出包含什么

- **ISO 解析**：形状 / 尺寸码 / 刀杆码 / 槽型
- **尺寸参数**：内切圆 IC、厚度 S、背孔 D1、刀尖圆角 RE（来源三菱样本名义值）
- **适用刀杆**：按三菱/泰珂洛/京瓷等品牌列出可替代型号

## 已知坑（务必记住）

1. **pycbin 不能丢**：`web/_vendor/substitute_orig.cpython-313.pycbin` 被 `.gitignore` 排除，是 `substitute.py` 的真实实现（原 .py 源码已误删，pyc 是唯一副本）。换机必须从旧机 `E:\Ingulf\智能选型\网页端\_vendor\` 找回。若查询突然报 `FileNotFoundError`，就是它丢了。
2. **解释器必须 3.13**：pyc 只认 `substitute.cpython-313`，版本错会静默返回空。
3. **静默失败无提示**：没有报错但输出为空 = 大概率解释器不对，不是数据缺失。

## 启动完整面板（可选）

要看网页版或批量报告，起 Flask 面板（端口 5801）：

```bash
py -3.13 "E:/Ingulf/smart-selection/web/启动.py"
```

会自动开浏览器 `http://localhost:5801`。手机同 WiFi 可访问。

## 数据根目录

面板用到的样本 PDF 根目录在 `mpgen/config/paths.local.toml` 的 `[sample] root`，换机后要按本机路径改。
