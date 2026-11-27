<div align="center">

# 🌱 Tree

**属于你自己的本地个人上下文系统 · Personal Context OS**

*模型和算力属于大厂，但上下文属于你自己。*

<p>
  <img src="https://img.shields.io/badge/Python-%E2%89%A5%203.9-24292f?style=flat" alt="Python >= 3.9" />
  &nbsp;
  <img src="https://img.shields.io/badge/Dependencies-Zero-24292f?style=flat" alt="Zero Dependencies" />
  &nbsp;
  <img src="https://img.shields.io/badge/Storage-Local%20Markdown%20%2B%20Git-24292f?style=flat" alt="Local Markdown + Git" />
</p>

[快速上手](#30-秒快速上手) · [工作原理](#工作原理) · [核心设计](#为什么是-tree) · [支持生态](#支持客户端与数据源) · [命令速查](#cli-命令速查) · [目录结构](#目录结构)

</div>

---

日常在 Claude Code、Cursor、Codex、Antigravity、Trae 或豆包之间切换时，每个工具都只能看到你的局部切片——写代码的只见代码，聊方案的不知过往拍板，换个客户端又要从头自我介绍。

**Tree** 是一个极简、零依赖的 **Agent Skill + CLI**。它用一套完全留在你本机的 Git-backed Markdown 知识库，把散落在各处的「你是谁、怎么想、在做什么、怎么做事与生活」拼合起来，让所有接入的 AI 共享同一份随时间复利的个人上下文。

---

## 30 秒快速上手

环境要求：**Python ≥ 3.9** 与 **Git**（100% Python 标准库实现，零第三方依赖，macOS / Linux 开箱即用）。以下两种方式任选其一，点击代码块右上角即可一键复制：

### 方式一：终端一行命令安装

打开终端粘贴回车，一步完成克隆、初始化与客户端挂载（装完向任意 AI 发送 `帮我扫描本机记忆并初始化 Tree` 即可破土）：

```bash
git clone https://github.com/shangxiuyu/Tree.git ~/Tree && ~/Tree/bin/tree init && ~/Tree/bin/tree install-skill
```

### 方式二：复制提示词，交给 AI 全自动搞定

不想自己敲命令？复制下面这段话，直接发给 Claude Code、Cursor、Codex、Antigravity、Trae、Windsurf 或任意支持本地命令的 AI 助手：

```text
请帮我安装并配置 Tree 个人上下文系统（https://github.com/shangxiuyu/Tree.git）：
1. 将仓库克隆到 ~/Tree，依次执行 ~/Tree/bin/tree init 和 ~/Tree/bin/tree install-skill 完成挂载；
2. 执行 ~/Tree/bin/tree seed scan 只读扫描我本机已有的 AI 记忆与对话源，把扫描结果和初始化方案发我确认后再提炼入树。
```

日常可直接用 **Obsidian / VS Code** 打开 `~/Tree/vault` 浏览或手改 Markdown，所有变更由 Git 完整留痕（终端随时可跑 `tree lint` 体检、`tree log` 查历史）。

<details>
<summary><b>自定义知识库位置与 PATH 说明</b></summary>

- **自定义存储路径**：若希望将个人知识库放在指定位置（如现有 Obsidian 库或同步盘），把安装命令中的后两步加上 `TREE_HOME=/your/path` 即可（例如 `TREE_HOME=~/Obsidian/Tree ~/Tree/bin/tree init`）。
- **路径解析优先级**：`TREE_HOME` 环境变量 → 当前工作目录向上查找 `.tree/config.json` → `~/.config/tree/home`（`init` / `install-skill` 时自动记录，此后裸跑 `tree` 自动命中）→ 仓库默认 `vault/`。
- **与系统 `tree` 命令共存**：若本机通过 Homebrew 安装过目录树工具 `/opt/homebrew/bin/tree`，请确保 `~/.local/bin` 在 `PATH` 中排在前面。

</details>

---

## 工作原理

```text
    本机 12 源只读破土              本地个人知识树 (vault/)               全客户端 AI 按需共享与接力
 ┌────────────────────────┐       ┌──────────────────────────┐       ┌──────────────────────────────┐
 │ 11款本地 AI 历史与记忆 │ ────> │ 01-关于我    02-我的思考 │ <───> │ 第 0 秒：常驻底线静态注入    │
 │ Chrome 书签 / 导出包   │ seed  │ 03-在做的事  04-如何工作 │ rules │ 第 1 秒：工作区与话题对焦    │
 └────────────────────────┘       └──────────────────────────┘ write │ 按需层：单页精读 & chat 接力 │
       自动脱敏 · 零污染                 Markdown + Git              └──────────────────────────────┘
```

Tree 不把全量历史记录硬塞进每次对话，而是通过**三层渐进式上下文**兼顾“懂你”与“高信噪比”：

1. **第 0 秒 · 常驻底线（`always: true`）**：少数全场景通用的核心身份与做事原则，通过 `<!-- TREE-CONTEXT -->` 受控块静态同步进各客户端全局指令文件，开局即生效、零重复读取。
2. **第 1 秒 · 情境自动对焦（`tree context`）**：进入具体项目或话题时，根据当前工作目录（`location`）、触发词与 **14 天半衰期读写热度**自动点亮最相关的按需专页，淘汰容易过期的手动置顶标签。
3. **按需精读与跨端接力（`tree read` / `tree chat`）**：需要深挖时按目录路标精读 1~3 篇专页；刚在 Claude 聊完准备去 Cursor 动手时，一句“接着刚才在 Claude 聊的做”，直接通过 `tree chat` 拉取去噪后的跨端对话实时接力。

---

## 为什么是 Tree

| 设计原则 | 具体机制 |
| :--- | :--- |
| **本地优先，零锁定** | 纯本地 Markdown 文件夹 + Git 历史 + Python 标准库脚本。无需注册云账号、无需搭数据库或常驻后台服务，随时可用任意文本编辑器接管。 |
| **严分「人的上下文」与「代码文档」** | Tree 只沉淀模型不知道的个人真实信息（身份、判断、取舍拍板、否决过的老路、做事规矩与生活偏好）；函数名、端口号等工程细节留在代码仓本地 `CLAUDE.md` / `docs/`。 |
| **人机共读的唯一宪法** | 不维护“给人看”与“给 AI 看”两套说明。`vault/README.md` 是唯一内容契约（支持自由增删改一级主干与家规），AI 执行任何写、改、删前必须先跑 `tree rules` 完成写前门禁打卡。 |
| **防幻觉截断与双链自愈** | 整页覆写缩水超过 50% 自动拦截防误删；重命名或移动页面自动同步全库 `[[双链]]`；每次写入自动生成独立 Git Commit 并记入 `.tree/audit.jsonl`，随时一键 `tree revert`。 |
| **自带消费闭环防死库** | 初始化自动在知识库内预置个人工作流模板 `tree-daily`（工作 · 学习 · 生活三维日报），衍生快变量隔离在 `outputs/`，让沉淀下来的上下文每天真正被消费和校准。 |

---

## 支持客户端与数据源

`tree install-skill` 严格遵循**最小足迹原则**：只向你本机**已经安装存在**的工具目录写入软链或受控块，绝不凭空创建用不到的配置文件夹。

| 能力维度 | 原生覆盖范围 |
| :--- | :--- |
| **Skill + 全局受控块挂载** | Claude Code · Codex · Gemini CLI / Antigravity · Windsurf · Hermes Agent · OpenCode · WorkBuddy · OpenClaw · Universal Agents（`~/.agents`），并支持同步豆包工作全局指令受控块 |
| **Skill 入口挂载** | Cursor · Trae（全局规则由客户端内部管理，自动挂载 Skill 入口） |
| **本机冷启动与跨端对话接力** | 内置 **12 个纯本地只读源**：Claude Code · Codex · Antigravity · Cursor · Trae · WorkBuddy · 豆包 · Hermes · OpenClaw · Eden · DeepSeek + Google Chrome（书签与高频站点） |
| **离线导出包导入** | 支持 `tree seed import` 导入 ChatGPT、Claude、豆包、WorkBuddy 官方导出包及本地 Markdown 目录 |

---

## CLI 命令速查

| 场景 | 命令 | 说明 |
| :--- | :--- | :--- |
| **初始化与挂载** | `tree init` | 初始化四维起步骨架、唯一维护宪法 `README.md` 与独立 Git 仓库 |
| | `tree install-skill` | 一键挂载 `tree` CLI、Skill 与全局受控块到本机已安装的客户端 |
| | `tree sync-global` | 手动刷新或移除（`--remove`）各客户端全局指令文件中的受控块 |
| **读取与接力** | `tree context [-q "<话题>"]` | 生成 L0 全景导航包并按当前目录、话题与热度自动对焦 |
| | `tree read "<路径或短名>"` | 按需精读指定上下文专页 |
| | `tree search "<关键词>"` | 全文与元数据组合检索（支持 `--origin` / `--kind` / `--status` / `--branch`） |
| | `tree recent [--days 14]` | 按更新时间与近期读写热度查看活跃焦点 |
| | `tree chat list` / `read` | 跨客户端检索并拉取你在本机其他 AI 工具中的近期对话记录 |
| | `tree seed scan` / `extract` | 只读探测并脱敏提取本机 12 个内置数据源，用于新库破土或增量盘点 |
| **安全写回与演进** | `tree rules` | 读取最新 `vault/README.md` 契约并完成 2 小时同身份写前打卡 |
| | `tree write "<路径>.md"` | 新增或更新页面（自动合并保留未改动的 YAML 元数据并提交 Git） |
| | `tree move` / `delete` | 移动重命名（自动更新全库 `[[双链]]`）或安全删除过期页面 |
| | `tree branch list\|add\|rename` | 查看或自由演化知识树一级主干目录 |
| **体检与版本** | `tree lint` | 巡检断链、孤儿页、目录过深、缺出处、常驻超长与敏感密钥泄漏 |
| | `tree log` / `diff` / `revert` | 查看演进历史、对比变更差异或安全回滚任意一次提交 |
| | `tree remote` / `sync` | 绑定私有 Git 远端仓库并双向同步（rebase 拉取 + 推送备份） |

---

## 目录结构

工具代码与你的个人知识库物理分离（`vault/` 默认 `.gitignore`，拥有自己独立的私有 Git 历史）：

```text
Tree/
├── bin/tree                     # CLI 便捷入口（指向 skills/tree/scripts/tree）
├── skills/tree/                 # 自包含的核心 Agent Skill
│   ├── SKILL.md                 # 给 AI 的触发入口与 CLI 操作指南
│   ├── scripts/                 # tree / tree_focus.py / tree_seed.py（纯 Python 标准库）
│   └── assets/                  # TREE_README.md 宪法模板与预置 tree-daily 工作流
├── tests/                       # 标准库回归测试集（python3 -m unittest discover -s tests）
└── vault/                       # 你的本地个人知识库实例（独立 Git 仓库，可放任意路径）
    ├── README.md                # 人机共读的唯一维护宪法（树主人可自由演化）
    ├── 01-关于我/               # 默认主干：身份底色、长期目标、品味偏好、设备与外脑入口
    ├── 02-我的思考/             # 默认主干：个人判断、灵感反思与同主题专题 Wiki
    ├── 03-我正在做的事/         # 默认主干：工作、学习、生活推进中事项
    ├── 04-我如何工作/           # 默认主干：做事原则、表达要求、流程标准与个人定制 Skill
    ├── outputs/                 # 快变量衍生输出隔离区（如每日简报，默认 .gitignore）
    └── .tree/                   # 运行配置、审计日志与脱敏暂存缓存
```
