# my-skills

我的自建 AI 技能仓库（WorkBuddy AI / Claude Code / Codex / 豆包 通用）。

## 这个仓库是什么

- **唯一物理来源**：每个子目录 = 一个技能，核心是一份 `SKILL.md`
- **远程仓库**：<https://github.com/K2951314/skills.git>。本地工作副本路径由你决定（见下方示意），同时也是集成目录
- **开发在这里，生效靠链接**：本仓库**不在**任何工具的技能扫描路径内，通过 junction / 软链挂载到工具的技能目录
- **换电脑只需两步**：`git clone` + 跑一次 `scripts/link-skills.*`

```
<你选的路径>/my-skills/                    ← 权威副本（在这里开发、commit、push）
        │
        │  junction
        ▼
~/.workbuddy-ai/skills/my-skills          ← 生效位置（工具扫描这里）
```

## 目录结构

```
my-skills/
├── README.md
├── .gitignore
├── docs/
│   ├── skill-template.md      # SKILL.md 模板（故意不叫 SKILL.md，避免被扫描注册）
│   ├── repository-layout.md   # 命名、存放位置、新增规则、与远程仓库的对应关系
│   └── verification.md        # 验证与调试：判据口径、单点命令、已知假绿/假红
├── scripts/
│   ├── link-skills.ps1        # Windows：建 junction（.codex / .claude / .workbuddy-ai / .agents 四个都挂）
│   └── link-skills.sh         # macOS / Linux / Git Bash
├── tools/
│   └── verify_all.py          # 一键验收：所有技能跑正例+负例，给出退出码语义证据
└── <skill-name>/
    └── SKILL.md               # 每个技能一个目录
```

## 新增一个技能

完整约定见 `docs/repository-layout.md`。最短路径：

1. 在仓库根建目录，**目录名 = 技能名**（小写字母 / 数字 / 连字符，如 `weekly-report`）
2. 先看根目录是否已有同名技能。有则改原目录，不另建副本
3. 把 `docs/skill-template.md` 复制成 `<skill-name>/SKILL.md`
4. frontmatter 里 `name` **必须与目录名完全一致**
5. `description` 写清「做什么 + 什么时候用」，触发词前置
6. 在下面的已收录表加一行
7. **无需任何链接操作** —— 整仓一个 junction，新技能自动生效
8. **新开一个会话**再验证是否触发（`description` 变更不会热更新）

## 挂载（每台新电脑跑一次）

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\link-skills.ps1
```

Git Bash / macOS / Linux：

```bash
bash scripts/link-skills.sh
```

挂载结果：`<目标>/skills/<仓库名>` → 本仓库。默认一次挂四处 —— `~/.workbuddy-ai/skills`、`~/.codex/skills`、`~/.claude/skills`、`~/.agents/skills`（豆包）。新工具要加目标就传给脚本：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\link-skills.ps1 -TargetRoot "$env:USERPROFILE\<某个工具>\skills"
```

## 三条铁律

1. **仓库根不要放 `SKILL.md`** —— 否则整个仓库会被注册成一个叫 `my-skills` 的技能
2. **技能名必须等于它的目录名** —— 不一致会加载失败
3. **别把本仓库挪进任何 skills 扫描目录** —— 扫描是递归的，`.git` / `docs` / `scripts` 都会被纳入范围

## 换电脑流程

```bash
git clone https://github.com/K2951314/skills.git ~/my-skills
cd ~/my-skills && bash scripts/link-skills.sh      # Windows 用 scripts\link-skills.ps1
py -3.13 skill-inventory/scripts/portability_lint.py   # 换机后必跑：抓盘符硬编码 / 版本声明 / 漏挂载
py -3.13 tools/verify_all.py                           # 再跑一遍全量验收（13 项，含负例）
```

技能正文与脚本里**不要写死盘符路径**：项目定位一律走环境变量（如 `SQ_HOME` / `SS_HOME`，设了就必须有效）+ 常见根探测，找不到中文报错。写死盘符 = 换机即死（实测踩过）。

## 验证与调试

一条命令确认「这些技能现在能不能用」：`py -3.13 tools/verify_all.py`。

完整判据、单点调试命令、环境变量表、以及实测过的「假绿 / 假红」清单见
[`docs/verification.md`](docs/verification.md)。

## 已收录技能

| 技能 | 用途 |
|---|---|
| `skill-inventory` | 盘点本机技能目录：引用状态 / 重复 / 废弃 / 位置不规范；含换机可移植性 lint（盘符硬编码 / 解释器版本声明 / 挂载覆盖） |
| `one-click-migrate` | 识别并迁移被 git 排除、但运行必要的数据；换电脑或换服务器时加密打包与校验导入 |
| `zk-ai-gateway` | 调用 ZK-AI 网关上的模型：默认 `zk-auto` 由网关选模型/密钥/兜底，仅旗舰质量、长上下文批量、看图三种情况显式选别名。入口三选一（本机 8317 / 服务器回环 8318 / 公网 `https://120.53.28.29/zkai`），由 `ZKAI_BASE_URL` 或兜底默认值决定 |
| `pdf-sample-extraction` | 把 PDF 样本（工业样本/产品目录/规格书）里的表格抽成可信 CSV：先定分母（样本里印了什么）再抽，四层独立校验，过不了的整块隔离。含 5 个通用脚本（单页勘察 / 页清单 / 产出体检 / 回原文校验 / 规格表抽取）与 7 篇版式与踩坑参考 |
| `tool-selection` | 跨品牌数控刀具替代选型：输入型号返回 ISO 解析、尺寸参数与三菱/泰珂洛/京瓷等可替代刀杆。启动器预检 3.13 解释器与 pycbin，项目定位走 `SS_HOME` |
| `quotation-helper` | 本地报价查询：直调智能询价报价引擎（绕过 HTTP 鉴权），多公司租户，输出可复制报价文本。项目定位走 `SQ_HOME` |
| `mitsubishi-inventory` | 三菱刀具实时库存（上海/日本）：金丝雀自检 fail-closed，上游异常绝不返回可疑数字。项目定位走 `SQ_HOME` |
| `tooling-kit` | 成套配刀：输入加工形态 + 材料 + 粗/半精/精，输出刀杆+刀片+牌号+切削参数；五项互锁全对才出方案。切槽/螺纹明确报「专用刀片未收录」不造假配对。项目定位走 `SS_HOME` |
| `failure-diagnosis` | 失效对策：崩刃/积屑瘤/振动/寿命短等 14 类现象 → 各品牌样本**原文对策句**（带册/页码）。同义词归一，工序过滤自动带「综合」册。项目定位走 `SS_HOME` |
| `quote-document` | 报价单/合同生成：型号批量询价后出 docx/pdf/png/xlsx（5 语言、可盖章、自动编号）。走智能询价**导出路由层**（与 GUI 同一条代码路径），默认在库副本上跑、不消耗真实序列。项目定位走 `SQ_HOME` |

## 参考

- 技能格式规范：<https://agentskills.io/specification>
- 本机技能目录与自建规范：你本机技能仓库根下的 `skill-authoring-guide.md`（如有）
