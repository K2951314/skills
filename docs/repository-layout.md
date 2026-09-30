# 仓库结构与新增规则

远程仓库：<https://github.com/K2951314/skills.git>

本地集成目录：`E:\Ingulf\my-skills`

二者是同一份内容。本地目录是工作副本，远程仓库是权威副本。技能在本地目录里新增和修改，确认后提交并推送。不要在 `~/.workbuddy-ai/skills/` 里另存一份技能正文。

## 对应关系

```text
https://github.com/K2951314/skills.git
        │  git clone / pull / push
        ▼
E:\Ingulf\my-skills                         工作副本，也是集成目录
        │  junction，整仓挂一次
        ▼
C:\Users\Ingulf\.workbuddy-ai\skills\my-skills
```

挂载由 `scripts/link-skills.ps1` 或 `scripts/link-skills.sh` 完成。新电脑上先克隆到固定路径，再跑一次挂载。之后新增技能不用重新挂载。

本仓库不在工具的扫描路径内。生效靠上面的 junction。不要把仓库本身挪进任何 skills 扫描目录，扫描是递归的，`.git`、`docs`、`scripts` 会被卷进去。

## 目录层级

只允许两层技能：仓库根下的一个目录就是一个技能。不要做 `skills/foo/SKILL.md` 这种再套一层，也不要在技能目录里再放另一个带 `SKILL.md` 的子目录。

```text
my-skills/
├── README.md
├── .gitignore
├── .gitattributes
├── docs/                      仓库说明。这里禁止放 SKILL.md
│   ├── skill-template.md
│   └── repository-layout.md
├── scripts/                   挂载脚本。不是技能
│   ├── link-skills.ps1
│   └── link-skills.sh
└── <skill-name>/              一个技能
    ├── SKILL.md               唯一入口
    ├── references/            按需阅读的规则，可选
    ├── scripts/               该技能专用的确定性脚本，可选
    └── assets/                输出用的模板，可选
```

仓库根禁止放 `SKILL.md`。否则整个仓库会被注册成一个叫 `my-skills` 的技能。

`docs/skill-template.md` 故意不叫 `SKILL.md`，避免被扫描注册。

## 命名

- 目录名就是技能名，也是 frontmatter 里的 `name`。三者必须一致。
- 只用小写字母、数字、连字符。不以连字符开头或结尾，不用下划线，不用中文，不用空格。
- 名称描述能力，不描述某一次任务。用 `one-click-migrate`，不用 `migrate-2026-09-30`。
- 新增前先看仓库根已有目录。同名目录已存在就改原技能，不另建 `one-click-migrate-2`。
- 一个目录只做一件事。功能重叠时合并，不并列两份入口。

## 新增一个技能

1. 在仓库根建目录，目录名等于技能名。
2. 复制 `docs/skill-template.md` 为 `<skill-name>/SKILL.md`，改掉模板占位。
3. `description` 写清做什么、什么时候用、不用于什么。触发说法放在前面。
4. 超过入口文件能舒适放下的规则，拆到该技能自己的 `references/`。不要把别的技能的参考文件拷进来。
5. 反复执行且要求结果稳定的步骤，放进该技能自己的 `scripts/`。仓库根的 `scripts/` 只放挂载脚本。
6. 在 `README.md` 的已收录表加一行。不要改其他技能的文件。
7. 提交前看 `git status`。密钥、压缩包、本机绝对路径里的私有数据不得出现。
8. 新开一个会话再验证触发。`description` 变更不会热更新。

## 避免覆盖

- 不在技能目录外写同名 `SKILL.md`。
- 不把一个技能的 `references/` 链到另一个技能。每个技能目录必须能单独拷走使用。
- 修改已有技能时只改该目录。仓库级约定变了，才改 `README.md`、`docs/`、`.gitignore`。
- 本地工具缓存、会话记忆、压缩分发包不进这个仓库。它们不是技能内容。
