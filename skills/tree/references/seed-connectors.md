# 按需连接器与自定义扩展协议（Tree Seed Connectors）

`tree seed` 核心只内置 **12 个零依赖本机基础来源**（11 款本地 AI 客户端 + Google Chrome 书签/高频站点），保持最小内核。

---

## 一、官方按需连接器库（GitHub `shangxiuyu/water`）

所有外部云端外脑与生活社交平台连接器（包括纯标准库零依赖、下载即用的 **得到大脑 `getnote`、flomo `flomo`、微信读书 `weread`、即刻 `jike`**，以及需要额外依赖 `playwright` / `requests` / `wechat-cli` / `ego-browser` 的 **美团、淘宝/盒马、叮咚买菜、京东、闲鱼、Keep、微信**）统一托管在官方仓库 `https://github.com/shangxiuyu/water`。新用户让 AI 按需下载即可开箱使用：

```bash
tree seed add --list                      # 查看官方按需连接器清单与前置依赖
tree seed add getnote,flomo,weread,jike   # 下载零依赖外脑/社交连接器至 seed/connectors/<id>/（无需 pip install）
tree seed login getnote                   # OAuth 扫码授权（flomo 走浏览器 OAuth 2.1；weread 传 --api-key；jike 扫码）
tree seed extract --sources getnote
# 各外脑包内均附带独立只读查询 CLI（如 getnote_client.py / flomo_client.py / weread_client.py），AI 可按需实时检索

# 若安装需要额外依赖的生活源（如美团）：
tree seed add meituan                     # 下载连接器包至 seed/connectors/meituan/
# 按提示安装所需依赖（如 pip install playwright && playwright install chromium）
tree seed login meituan                   # 扫码或短信登录（凭证仅保存在本机 .gitignore 隔离区）
tree seed extract --sources meituan
```

---

## 二、扩展未内置客户端（Agent 自进化 Seed 连接器协议）

当用户希望接入尚未内置的客户端（如 Windsurf、Cherry Studio、ChatWise、OpenCode、元宝桌面版等）时，按以下 4 步扩展 `seed/connectors/<name>.py`（或 `<name>/connector.py`）：

1. **经用户授权只读探查**
   只读查看该客户端在 `~/Library/Application Support/`、`~/.<app>/` 或用户指定路径的本地存储（标准明文 JSON / JSONL / Markdown / SQLite），或通过官方 OpenAPI / OAuth 接口读取，绝不修改原文件。
2. **编写只读插件**
   在知识库 `seed/connectors/<name>.py`（或包目录 `<name>/connector.py`）中实现标准接口（升级 `tree` CLI 不会覆盖用户自定义插件）：
   - 常量：`CONNECTOR_ID = "<name>"`、`CONNECTOR_NAME = "<显示名>"`
   - `discover() -> dict`：返回 `{"id", "name", "available", "needs_login"(可选), "memory_files", "chat_count", "latest_date", "detail"(可选)}`
   - `login_qr(timeout: int = 180) -> bool`（可选）：若来源需要扫码或交互授权，实现此函数即可通过 `tree seed login <name>` 调用
   - `extract(days: int, max_sessions: int, helpers: dict) -> tuple[str, int, list[str]]`：只提取高信噪比线索，**必须调用 `helpers["clean_chat_text"]` / `helpers["redact_lifestyle_text"]` 去噪脱敏**，返回 `(markdown_text, item_count, processed_ids)`。
3. **立即验证生效**
   运行 `tree seed scan` 与 `tree seed extract --sources <name>` 验证已自动加载并成功清洗。
