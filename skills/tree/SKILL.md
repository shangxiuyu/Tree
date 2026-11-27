---
name: tree
description: 个人上下文操作系统（Tree Personal Context OS）。用于：(1) 按需查询用户的真实个人上下文（身份、思考、在推事项、做事方式与生活细节等）；(2) 获取并接力用户在本机其他 AI 客户端（Claude Code / Cursor / Codex / Trae / 豆包等）的聊天记录与历史记忆；(3) 当对话出现值得沉淀的个人事实、判断、偏好、生活记录、对 AI 的纠偏或目录调整时，通过 `tree` CLI 安全维护本地知识库。
---

# Tree CLI 操作指南

通过 `tree` CLI 操作用户本地知识库与跨端对话源（若未在 `PATH` 中，回退 `~/.local/bin/tree` 或本 Skill 目录 `scripts/tree`）。

> **职责边界**：本文件只管 CLI 命令怎么调；记什么、放哪一枝、写权分级与 Markdown 规范以知识库根目录 `README.md` 为唯一准绳（只读查询无需读它，**任何写、改、删前必须先执行 `tree rules`**）。

---

## 1. 查询与使用知识库（默认 `01~04`）
需要了解用户背景、项目来龙去脉、做事规矩或过往判断时，按“先看全景目录 → 再精读相关页”渐进加载，不要盲目遍历全库：
```bash
tree context [--no-always] [-q "<关键词>"] [--cwd "<目录>"] # 获取全景导航与情境对焦页（全局指令已注入常驻块时加 --no-always）
tree read "<相对路径或唯一短名>"                             # 按需精读 1~3 篇最相关专页
tree search "<关键词>"                                     # 全文与元数据组合检索（支持 --origin/--kind/--status/--tag/--always/--branch）
tree recent [--days 14]                                    # 查看近期更新与高频读写的活跃页面
```

## 2. 获取本机其他 AI 的聊天记录与旧记忆
- **日常跨 AI 实时接力**（用户提到“刚在另一个 AI 聊过 / 接着聊”或需盘点跨端工作与学习轨迹时）：
  ```bash
  tree chat list [--source cursor,claude --query "<关键词>" -n 15]
  tree chat read "<source>:<id前缀>" [--turns 24] [--raw-code] # 拉取去噪脱敏后的双边完整对话
  ```
- **新库破土与历史迁移**（`tree context` 提示 `幼苗未破土` 或用户要求初始化/导入旧记忆时）：
  1. 先只读探测本机 12 个内置源（11 款本地 AI 客户端 + Chrome）：`tree seed scan`，再给用户 **3 选 1**（不抛连环问卷）：
     - **A. 本地迁移（推荐）**：经用户授权后执行 `tree seed extract --days 60`（或导入导出包 `tree seed import "<路径>" --label "<来源>"`），读取 `.tree/staging/00-manifest.md`，配合 `tree seed read "<路径>"` 按需精读本地记忆，并一并调用当前宿主已挂载的其他个人数据源工具补齐上下文；用户确认草案后落盘，最后 `tree seed clear` 清空暂存区。
     - **B. 极速口述**：一次只问 3 个问题（① 基本信息与沟通忌讳；② 当前在推的 1~3 件事及路径；③ 现有做事规矩）。
     - **C. 边干活边种**：先处理手头任务，后续协作中自然沉淀。
  2. 查看采集游标与暂存状态：`tree seed status`。

## 3. 安全写回与结构维护
优先走 `tree` CLI 以自动完成写前打卡、防截断保护、双链同步与 Git 提交（仅当宿主无 Shell 工具或沙盒拦截 CLI 时，降级为先用文件工具完整读取 `README.md` 与目标原页，再直接编辑 `.md`）：
1. **写前必跑**：`tree rules`（读取最新 `README.md` 契约并完成 2 小时同身份打卡）+ `tree list [子目录]`（排查已有页面防重）。
2. **新增或更新页面**（自动从管道读取正文，全页覆写会自动合并保留未改动的旧 YAML 字段并刷新 `updated`）：
   ```bash
   cat <<'EOF' | tree write "<相对路径>.md" --reason "<变更原因>"
   # <标题>
   ...
   EOF
   ```
   - 常用元数据参数：`--always` / `--no-always`、`--origin self|external|mixed`、`--source "<出处>"`、`--kind project|idea`、`--location "<路径>"`、`--when "<特征>"`、`--status active|paused|archived|superseded`
   - 行为控制参数：
     - `--append`：仅用于在文末追加周期记录或独立新条目；**修改已有结论必须先 `tree read` 读全文再整页覆写**。
     - `--force`：长文整页覆写缩水超过 50% 会触发防截断拦截，确认有意大幅精简时附带。
3. **移动、删除、改主干与体检**：
   ```bash
   tree move "<原路径>" "<新路径>" --reason "<原因>"   # 重命名或升格专题目录，自动更新全库非保护页中的 [[双链]]
   tree delete "<相对路径>" --reason "<原因>"          # 删除前须先 tree search 排查反向 [[双链]]
   tree branch list|add|rename|remove                 # 查看或演化一级主干（保留角色语义与双链）
   tree lint                                          # 巡检断链、Wiki 孤儿页、路径过深、缺出处、敏感密钥等
   ```
   *(版本历史、回滚与远端 Git 备份命令：`tree log` / `tree diff` / `tree revert <commit>` / `tree remote` / `tree sync`，详见 `tree --help`。)*
