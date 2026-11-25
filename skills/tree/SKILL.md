---
name: tree
description: 个人上下文操作系统（Tree Personal Context OS）。在对话中按需加载用户的基本情况（01-关于我）、独立思考（02-我的思考）、在推事项（03-我正在做的事）与工作方式（04-我如何工作），并在产生长期判断、项目决策或偏好更新时，先读知识库根目录 README.md 准则再写入本地 Tree 仓库。
---

# Tree Personal Context Skill

你已接入用户的 **Tree 个人上下文系统**（Git-backed Markdown Wiki），通过 `tree` CLI 读写（直接调用 `tree`；若未加入 `PATH`，回退使用 `~/.local/bin/tree` 或本 Skill 目录下的 `scripts/tree`）。

> **唯一规则事实源**：知识库根目录的 `README.md` 是人类与 AI 共同遵守的唯一分类地图与维护准则（含 `01~04` 推荐子分类、防串线铁律、`seed/` 外脑与精选收藏隔离区、以及人类笔记保护）。只读查询无需加载 `README.md`；**任何写入、移动或删除前必须先执行 `tree rules`（输出 2 KB 精简契约，等价于 `tree read README.md` 通过门禁；加 `--full` 可看全文）**。

---

## 标准工作流与命令

### 1. 只读消费（L0 → L1 渐进加载）
```bash
tree context [-q "<当前任务或话题关键词>"] # 获取 L0 全景导航包（自动根据当前工作目录、现场特征与话题跨 01~04 对焦，并暴露 seed/ 已挂载外脑源与精选收藏摘要）
tree read "<相对路径>"                 # 精准加载 1~3 篇最相关专页（支持 01~04 及 seed/ 下的外脑索引与精选收藏）
tree search "<关键词>"                 # 全文与文件名检索（覆盖 01~04 与 seed/，支持 --kind/--status/--tag/--always/--owner/--branch 筛选）
tree recent [--days 14]               # 按近期更新与读写热度查看活跃页面
```

### 2. Seed 外脑上下文源挂载、精选收藏与旧记忆迁移
全仓库**只有一个统一的 `seed/` 目录**：其中外脑注册表与精选收藏（`seed/*.md`）随 Git 同步；运行时的采集游标（`seed/sources.json`）、连接器代码（`seed/connectors/`）与临时脱敏抽取素材（`seed/staging/`）由 `.gitignore` 自动忽略：
1. **常驻外脑与精选收藏（`seed/*.md`，随 Git 同步，全权由 AI 替用户动态维护）**：
   - **`seed/外部上下文源.md`（千人千面、随增随减，严禁写死模板）**：由 AI 根据当前用户实际拥有的外部数据源动态创建与原地更新（`tree write "seed/外部上下文源.md" --no-always`）。数据源范围包括：`tree seed scan` 探测到的已就绪客户端与连接器、当前宿主已挂载的外部数据类 MCP/Skill（如播客、社区、笔记库、生鲜外卖、运动等）、以及用户在对话中接入的外部知识库（如得到大脑、抖音、Obsidian、飞书、Notion 等）。**只记当前用户真实已具备的源、覆盖范围与调用入口；每当用户新增、替换或停用/卸载某个外部数据源，或 AI 在破土、日常调用、园丁巡检中发现数据源增减时，AI 必须主动更新本页，无需用户手动编辑。**
   - **精选收藏（`seed/文章与链接.md`、`seed/工具与资源.md`、`seed/图片与审美/`）**：尚未内化成个人观点但值得留给 AI 参考的外部好文章、好工具与审美参考图（只记标题 + 链接 + 一句话为什么收藏，不塞原文全文；二进制图片自动 `.gitignore`）。
2. **首次破土初始化与旧记忆迁移（Onboarding & Extraction）**：
   当 `tree context` 返回 `[SEED_STATE: 幼苗未破土]` 或用户要求抽取其他 AI / 第三方系统的记忆时：
   - 只读探测本机来源：`tree seed scan`（已内置 Claude Code / Codex / Antigravity / Cursor / Trae / WorkBuddy / 豆包工作 Douchat / Hermes / OpenClaw / Eden / DeepSeek Harness + Chrome 书签/高频站点）。
   - 给用户 3 选 1 破土路径（不要连环问卷）：
     - **路径 A（本地记忆与历史迁移 — 推荐）**：
       - 用户授权后执行 `tree seed extract --days 60`（对本地 Markdown 记忆与规则生成**全量零截断索引** `seed/staging/memories-index.md`，对历史对话与 SQLite 记忆做脱敏解包），或对官方导出包执行 `tree seed import "<路径>" --label "<来源名>"`。
       - **索引驱动按需精读**：先通览 `memories-index.md` 全盘（不要盲目全吞），按客户端与项目分组挑出高价值入口（全局画像/规则、仍在推进项目的 `MEMORY.md`、近期关键记录；必要时向用户确认要迁哪些项目），直接读取原文件或用 `tree seed read "<文件路径>"` 只读读取（自动脱敏并剥离 Tree 自身同步块）。
       - **对话意图初筛 → 按需调阅人+AI 双边上下文**：`chats-*.md` 默认只抽人类发言流用于低成本初筛，每场会话标题自带 `` `ref: <source>:<id前缀>` ``。若某场人类发言有沉淀价值，但缺 AI 回复看不清上下文（如确认方案、否决提议、指代前文），**主动执行 `tree chat read "<source>:<id前缀>" --turns 30` 拉取该场对话的双边完整上下文**，看懂全貌后再提炼。
       - **兼容第三方外脑与记忆系统**：主动询问用户是否有其他外部大脑或个人知识库（如得到大脑、Mem0、Obsidian、OpenMemory、MCP 记忆服务等）。经用户授权后，可将长期核心判断蒸馏入 `01~04`，同时将该外脑的挂载方式与索引登记进 `seed/外部上下文源.md` 供日后所有 AI 持续感知调用；如需长期复用采集脚本再按 [references/seed-connectors.md](references/seed-connectors.md) 扩展只读插件。
     - **路径 B（极速口述破土）**：一次只问 3 个高杠杆问题（① 基本信息与最烦 AI 怎么说话；② 当下正在推进的 1~3 件事及路径；③ 是否有现成工作规矩）。
     - **路径 C（先干活，边聊边种）**：不打断当前任务，后续遇到高价值决策再顺手写入。
   - **蒸馏落盘**：先执行 `tree rules` 对齐推荐子分类与完整性要求，读取 `seed/staging/00-manifest.md` 与按需挑选的记忆/素材（写项目页时结合 `location` 下的现有文档/代码补全来龙去脉；`01/长期目标` 与 `01/价值观` 只写用户亲口表达过的梦想、3~5 年方向与人生信条，严禁拿 `03` 的短期/在推项目反推脑补，素材不足则留空不建或先问用户），呈报草案确认后逐页 `tree write`（外脑挂载与精选收藏写入 `seed/`），最后 `tree seed clear` 清理 `seed/staging/` 临时抽取缓存。
   - **外部大脑与生活社交平台（按需连接器 `water`）**：若用户希望接入**得到大脑（`getnote`）、flomo（`flomo`）、微信读书（`weread`）、即刻（`jike`）**（均为零依赖标准库，下载即用），或**微信、美团、叮咚买菜、闲鱼、Keep、淘宝/盒马、京东**等外部平台，经用户同意后通过 `tree seed add --list` / `tree seed add <平台ID>` 从官方连接器库（`https://github.com/shangxiuyu/water`）按需拉取至 `seed/connectors/` 并授权提取。外脑目录索引登记入 `seed/外部上下文源.md`，生活偏好页标 `--no-always`。

### 3. 跨 AI 实时接力
用户要求接着其他客户端的对话继续聊时，直接只读拉取（支持 10 个本地 AI 客户端，不含 `openclaw` 与 `chrome`，不污染树干）：
```bash
tree chat list [--source cursor --query "<关键词>"]
tree chat read "<source>:<id前缀>" --turns 30
```

### 4. 写入与维护（Update-First）
必须先执行 `tree rules` 与 `tree list` 检查已有页面，优先在已有专页原地更新（`NOOP > UPDATE > DELETE > ADD`）：
```bash
cat <<'EOF' | tree write "<相对路径>.md" --stdin [--always | --no-always] [--kind project] [--location "<路径>"] [--when "<特征>"] --reason "<变更原因>"
# <标题>
...
EOF
```
常用辅助参数与维护命令：
- `--append`（在长页面末尾安全追加）/ `--force`（大幅精简重写整页时跳过防截断拦截）/ `--status paused|archived|superseded`
- 重命名或迁移：`tree move "<原路径>" "<新路径>" --reason "<原因>"`（自动同步 `[[双链]]` 与 Git 历史）
- 删除过期页面：`tree delete "<相对路径>" --reason "<原因>"`（受保护人类笔记需加 `--human-approved`）
- 知识树体检：`tree lint`（检查空目录、碎片命名、记账腔、明文密钥与同名冲突）
- 同步全局指令受控块：`tree sync-global [--dry-run | --remove]`
- 可选云端同步：`tree remote <私有仓库URL>`、`tree sync`、`tree sync schedule --enable --time 23:00`
