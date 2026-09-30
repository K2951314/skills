# my-skills

我的自建 AI 技能仓库（WorkBuddy AI / Claude Code / Codex 通用）。

## 这个仓库是什么

- **唯一物理来源**：每个子目录 = 一个技能，核心是一份 `SKILL.md`
- **开发在这里，生效靠链接**：本仓库**不在**任何工具的技能扫描路径内，通过 junction / 软链挂载到工具的技能目录
- **换电脑只需两步**：`git clone` + 跑一次 `scripts/link-skills.*`

```
E:\Ingulf\my-skills\                    ← 权威副本（在这里开发、commit、push）
        │
        │  junction
        ▼
C:\Users\Ingulf\.workbuddy-ai\skills\my-skills   ← 生效位置（工具扫描这里）
```

## 目录结构

```
my-skills/
├── README.md
├── .gitignore
├── docs/
│   └── skill-template.md      # SKILL.md 模板（故意不叫 SKILL.md，避免被扫描注册）
├── scripts/
│   ├── link-skills.ps1        # Windows：建 junction
│   └── link-skills.sh         # macOS / Linux / Git Bash
└── <skill-name>/
    └── SKILL.md               # 每个技能一个目录
```

## 新增一个技能

1. 在仓库根建目录，**目录名 = 技能名**（小写字母 / 数字 / 连字符，如 `weekly-report`）
2. 把 `docs/skill-template.md` 复制成 `<skill-name>/SKILL.md`
3. frontmatter 里 `name` **必须与目录名完全一致**
4. `description` 写清「做什么 + 什么时候用」，触发词前置
5. **无需任何链接操作** —— 整仓一个 junction，新技能自动生效
6. **新开一个会话**再验证是否触发（`description` 变更不会热更新）

## 挂载（每台新电脑跑一次）

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\link-skills.ps1
```

Git Bash / macOS / Linux：

```bash
bash scripts/link-skills.sh
```

挂载结果：`~/.workbuddy-ai/skills/<仓库名>` → 本仓库。

## 三条铁律

1. **仓库根不要放 `SKILL.md`** —— 否则整个仓库会被注册成一个叫 `my-skills` 的技能
2. **技能名必须等于它的目录名** —— 不一致会加载失败
3. **别把本仓库挪进任何 skills 扫描目录** —— 扫描是递归的，`.git` / `docs` / `scripts` 都会被纳入范围

## 换电脑流程

```bash
git clone git@github.com:<你的用户名>/my-skills.git ~/my-skills
cd ~/my-skills && bash scripts/link-skills.sh      # Windows 用 scripts\link-skills.ps1
```

## 已收录技能

| 技能 | 用途 |
|---|---|
| `skill-inventory` | 盘点本机技能目录：引用状态 / 重复 / 废弃 / 位置不规范 |

## 参考

- 技能格式规范：<https://agentskills.io/specification>
- 本机技能目录与自建规范：`E:\Ingulf\skills\skill-authoring-guide.md`
