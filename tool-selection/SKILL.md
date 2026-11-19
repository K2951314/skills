---
name: tool-selection
description: 跨品牌数控刀具替代选型查询。当用户说「查替代」「找替代」「选型」「这个型号能替代什么」「WNMG080408的替代」「跨品牌选型」或给一个刀片/刀杆型号问替代方案时使用。输入一个型号，返回 ISO 解析、尺寸参数和三菱/泰珂洛/京瓷等跨品牌可替代的刀杆列表。不用于查报价（用 quotation-helper）、查实时库存（用 mitsubishi-inventory）；成套配刀/失效对策/新品牌专查/牌号适用矩阵请起网页面板（端口 5801），本技能只覆盖替代查询。
agent_created: true
---

# 智能选型 — 跨品牌刀具替代查询

一句话输入型号，拿到跨品牌替代清单。数据来自三菱/泰珂洛/京瓷/株洲等品牌样本解析。

## 用法

启动器在本技能目录的 `scripts/substitute.py`（`$SKILL` = 本技能目录，即 my-skills 仓库下的 `tool-selection`）。它负责定位项目、预检解释器版本与 pycbin，然后转发给项目的 `web/substitute.py`：

```bash
py -3.13 "$SKILL/scripts/substitute.py" WNMG080408-MA
```

**必须用 Python 3.13**。项目里有 pyc 版本绑定，用错版本会静默返回空（不报错），极易误判为"没有替代品"。启动器会先验版本，版本不对直接中文报错（退出码 2），不再静默。

批量查询（招标清单，多行型号）：

```bash
py -3.13 "$SKILL/scripts/substitute.py" "型号1
型号2
型号3"
```

## 项目定位（换机可移植）

启动器自动定位 smart-selection 项目根：**环境变量 `SS_HOME`（设了就必须有效，设错直接报错）**；未设置时按常见根探测：技能仓库的同级目录（my-skills 与项目同父目录）→ 用户 home 下。都找不到会中文报错并提示设置 `SS_HOME`。技能与脚本里不写任何盘符——写死盘符曾让本技能在换机后整条失效。

## 输出包含什么

- **ISO 解析**：形状 / 尺寸码 / 刀杆码 / 槽型
- **尺寸参数**：内切圆 IC、厚度 S、背孔 D1、刀尖圆角 RE（来源三菱样本名义值）
- **适用刀杆**：按三菱/泰珂洛/京瓷等品牌列出可替代型号

## 已知坑（务必记住）

1. **pycbin 不能丢**：项目内 `web/_vendor/substitute_orig.cpython-313.pycbin` 被 `.gitignore` 排除，是 `substitute.py` 的真实实现（原 .py 源码已误删，pyc 是唯一副本；`web/_pyc_recovered.py` 只是反汇编笔记，不是可执行源码）。换机必须用 one-click-migrate 从旧机搬回。启动器会预检它，缺失时报退出码 3 并给出找回指引。
2. **解释器必须 3.13**：pyc 只认 `substitute.cpython-313`，版本错会静默返回空。走启动器会被提前拦下；**绕过启动器直调 `web/substitute.py` 时仍要自己保证 3.13**。
3. **静默失败无提示**：没有报错但输出为空 = 大概率解释器不对，不是数据缺失。

## 启动完整面板（可选）

要看网页版（含成套配刀、失效对策、新品牌专查、标识释义）或批量报告，起 Flask 面板（端口 5801）：

```bash
py -3.13 "$SS_HOME/web/启动.py"
```

（`$SS_HOME` = smart-selection 项目根，定位方式见上节。）会自动开浏览器 `http://localhost:5801`。手机同 WiFi 可访问。

## 数据根目录

面板用到的样本 PDF 根目录在项目内 `mpgen/config/paths.local.toml` 的 `[sample] root`，换机后要按本机路径改（项目自带 `py mpgen/config/paths.py` 自检）。