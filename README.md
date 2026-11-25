# Tree —— 个人上下文系统（Personal Context OS）

> **模型和算力属于大厂，但上下文属于你自己。**
> Tree 是一个极简、零依赖的 **Skill + CLI** 个人上下文操作系统：由一个本地 Git-backed Markdown 知识库、一个通用 `SKILL.md` 和一个纯 Python 标准库编写的 `tree` CLI 组成。

日常使用时，人类与 AI 只需共同阅读个人知识库内的唯一说明书（`vault/README.md`，新用户执行 `tree init` 自动生成）。

---

## 仓库结构（工具与个人知识库分离）

```text
Tree/
├── README.md                  # 本文件（GitHub 工具安装引导）
├── bin/
│   └── tree                   # 指向 skills/tree/scripts/tree 的便捷入口软链
├── skills/
│   └── tree/                  # 完全自包含的核心 Agent Skill
│       ├── SKILL.md           # 给 AI 的命令调用入口
│       ├── scripts/           # tree CLI 与 tree_seed.py / tree_focus.py（零依赖 Python 标准库）
│       ├── assets/            # TREE_README.md（初始化模板）与 default-skills/
│       └── references/        # seed-connectors.md（扩展客户端连接器协议）
├── tests/                     # 标准库回归测试（python3 -m unittest discover -s tests）
└── vault/                     # 本地个人知识库实例（Git 忽略，独立 Git 仓库）
    ├── README.md              # 人机共读的唯一分类与维护说明书（AI 写入前必读）
    ├── 01-关于我/
    ├── 02-我的思考/
    ├── 03-我正在做的事/
    ├── 04-我如何工作/
    ├── seed/                  # 外脑数据源注册表与精选收藏（进 Git） + 连接器与脱敏暂存缓存（staging/ & connectors/ 自动 .gitignore）
    ├── outputs/               # 可选：AI 生成的日报等快变量输出区（默认 .gitignore）
    └── .tree/                 # config.json / changelog.jsonl / audit.jsonl
```

---

## 快速安装与初始化

```bash
# 0. 克隆工具仓库到本地
git clone https://github.com/shangxiuyu/Tree.git && cd Tree

# 1. 初始化个人知识库（默认指向 vault/，也可通过 TREE_HOME 指定任意路径）
./bin/tree init

# 2. 将 CLI、Skill 与全局受控块挂载到本机已安装的 AI Agent（Claude / Codex / Gemini / Cursor 等）
./bin/tree install-skill

# 3. 查看 L0 全景导航包与健康度检查
./bin/tree context
./bin/tree lint
```

> **知识库路径解析优先级**：`TREE_HOME` 环境变量 → 当前工作目录向上查找 `.tree/config.json` → 仓库默认 `vault/`（若使用自定义 `TREE_HOME`，`install-skill` 会自动在宿主全局受控块中注入对应 `TREE_HOME` 路径，确保 GUI 客户端也能准确命中）。先执行 `install-skill` 再执行 `init` 同样兼容，`init` 会自动将已挂载的 `tree-daily` 软链升级指向你的个人知识库副本。
