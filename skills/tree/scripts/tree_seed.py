#!/usr/bin/env python3
"""
Tree Seed Engine — Local-First Context Migration & Chat History Distillation Layer.
Zero external dependencies (Python standard library only).

Design principles:
1. Read-only access to local agent data stores (never modifies source apps).
2. Unified under `seed/` — persistent external brain registry & curated collections (`seed/*.md`) are tracked by Git, while runtime cache (`seed/staging/`, `seed/connectors/`, `seed/sources.json`) is gitignored so raw chats never pollute the Git tree.
3. Strips code dumps, tool logs, system wrappers, and redacts secrets before staging.
4. Supports incremental cursors (`seed/sources.json`) and official export imports (ChatGPT/Claude/Doubao/WorkBuddy/Markdown).
"""

from __future__ import annotations

import datetime
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable

DEFAULT_WATER_REPO = "https://raw.githubusercontent.com/shangxiuyu/water/main"
KNOWN_WATER_CONNECTORS = ("getnote", "flomo", "weread", "jike", "meituan", "taobao", "dingdong", "jd", "xianyu", "keep", "wechat")
SOURCE_ALIASES = {"dedao": "getnote", "biji": "getnote", "wxread": "weread", "fumo": "flomo"}


# Secret redaction patterns (best-effort safety net before staging)
SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]+ PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{25,}\b"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"), "[REDACTED_JWT]"),
    (re.compile(r"\b(?:LTAI|AKID)[A-Za-z0-9]{12,}\b"), "[REDACTED_CLOUD_AK]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]{20,}"), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"(?i)((?:cookie|set-cookie|sessionid|sid)\s*[:=]\s*['\"]?)[^\s'\"\n;]{12,}"), r"\1[REDACTED_COOKIE]"),
    (re.compile(r"(?i)((?:api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['\"]?)[^\s'\",;]{10,}"), r"\1[REDACTED]"),
]

# System wrapper tags to strip from user/assistant messages
TAG_BLOCK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"<ADDITIONAL_METADATA>[\s\S]*?</ADDITIONAL_METADATA>", re.IGNORECASE),
    re.compile(r"<SYSTEM_MESSAGE>[\s\S]*?</SYSTEM_MESSAGE>", re.IGNORECASE),
    re.compile(r"<USER_SETTINGS_CHANGE>[\s\S]*?</USER_SETTINGS_CHANGE>", re.IGNORECASE),
    re.compile(r"<system-reminder\b[^>]*>[\s\S]*?</system-reminder>", re.IGNORECASE),
    re.compile(r"<local-command-stdout>[\s\S]*?</local-command-stdout>", re.IGNORECASE),
    re.compile(r"<command-message>[\s\S]*?</command-message>", re.IGNORECASE),
    re.compile(r"<command-name>[\s\S]*?</command-name>", re.IGNORECASE),
    re.compile(r"<command-args>[\s\S]*?</command-args>", re.IGNORECASE),
    re.compile(r"<environment_context>[\s\S]*?</environment_context>", re.IGNORECASE),
    re.compile(r"<permissions instructions>[\s\S]*?</permissions instructions>", re.IGNORECASE),
    re.compile(r"<collaboration_mode>[\s\S]*?</collaboration_mode>", re.IGNORECASE),
]

CODE_FENCE_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def seed_dir(root: Path) -> Path:
    d = root / "seed"
    legacy = root / ".tree" / "seed"
    if legacy.is_dir():
        d.mkdir(parents=True, exist_ok=True)
        for item in list(legacy.iterdir()):
            dst = d / item.name
            if not dst.exists():
                shutil.move(str(item), str(dst))
        try:
            shutil.rmtree(legacy)
        except Exception:
            pass
    d.mkdir(parents=True, exist_ok=True)
    return d


def staging_dir(root: Path) -> Path:
    d = seed_dir(root) / "staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


def user_connectors_dir(root: Path) -> Path:
    return seed_dir(root) / "connectors"


def sources_file(root: Path) -> Path:
    return seed_dir(root) / "sources.json"


def load_sources_state(root: Path) -> dict[str, Any]:
    sf = sources_file(root)
    if sf.is_file():
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {
        "updated_at": "",
        "detected": {},
        "processed_sessions": {},
        "imports": [],
    }


def save_sources_state(root: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    sf = sources_file(root)
    sf.parent.mkdir(parents=True, exist_ok=True)
    tmp = sf.parent / f".{sf.name}.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(sf)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        raise


def redact_secrets(text: str) -> str:
    out = text
    for pat, repl in SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


TREE_CONTEXT_BLOCK_RE = re.compile(
    r"<!--\s*TREE-CONTEXT:START\s*-->[\s\S]*?<!--\s*TREE-CONTEXT:END\s*-->",
    re.IGNORECASE,
)


def strip_tree_context_block(text: str) -> str:
    """Remove Tree's own injected managed block so migrations never re-import Tree into itself."""
    if not text:
        return ""
    cleaned = TREE_CONTEXT_BLOCK_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def redact_pii(text: str) -> str:
    """Redact Chinese mobile phone numbers and 18-digit resident ID numbers."""
    out = re.sub(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)", r"\1****\2", text)
    out = re.sub(
        r"(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)",
        "[REDACTED_ID]",
        out,
    )
    return out


def redact_lifestyle_text(text: str) -> str:
    """Redact phone numbers, ID cards, tokens, and keys while keeping preference semantics."""
    return redact_pii(redact_secrets(text))


def clean_chat_text(text: str, max_len: int = 500) -> str:
    if not text:
        return ""
    s = text.replace("\r\n", "\n")
    # Unwrap <USER_REQUEST>...</USER_REQUEST> if present
    m = re.search(r"<USER_REQUEST>([\s\S]*?)</USER_REQUEST>", s, re.IGNORECASE)
    if m:
        s = m.group(1)
    for pat in TAG_BLOCK_PATTERNS:
        s = pat.sub("", s)
    # Collapse long fenced code blocks so chats stay focused on human intent & decisions
    def _shorten_fence(match: re.Match[str]) -> str:
        blk = match.group(0)
        if len(blk) <= 180:
            return blk
        return "[代码块已省略]"

    s = CODE_FENCE_RE.sub(_shorten_fence, s)
    s = redact_pii(redact_secrets(s))
    # Filter obvious boilerplate system prompts injected as user role in some agents
    stripped = s.strip()
    if not stripped:
        return ""
    lower_head = stripped[:120].lower()
    noise_prefixes = (
        "# agents.md instructions",
        "<instructions>",
        "this session is being continued from a previous conversation",
        "[request interrupted by user]",
        "caveat: the messages below were generated by the user while running local commands",
        "这是 ahub 的私有 ai 主动检查",
        "[eden runtime context]",
        "你正在浏览用户的 eden 朋友圈",
        "--- [群聊协同协议",
        "your response above was stopped by a safety classifier",
    )
    if any(lower_head.startswith(p) for p in noise_prefixes):
        return ""
    # Normalize excessive blank lines
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    if len(stripped) > max_len:
        stripped = stripped[:max_len].rstrip() + "…"
    return stripped


def extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                itype = item.get("type", "")
                if itype in ("text", "input_text", "output_text"):
                    txt = item.get("text")
                    if isinstance(txt, str):
                        parts.append(txt)
        return "\n".join(parts)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if isinstance(content.get("parts"), list):
            return "\n".join(str(p) for p in content["parts"] if isinstance(p, str))
    return ""


def file_mtime_iso(p: Path) -> str:
    try:
        dt = datetime.datetime.fromtimestamp(p.stat().st_mtime).astimezone()
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def within_days(p: Path, days: int) -> bool:
    if days <= 0:
        return True
    try:
        cutoff = datetime.datetime.now().timestamp() - days * 86400
        return p.stat().st_mtime >= cutoff
    except Exception:
        return True


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Local Source Discovery (Read-Only)
# ---------------------------------------------------------------------------

def get_cursor_state_db() -> Path | None:
    home = Path.home()
    candidates = [
        home / "Library/Application Support/Cursor/User/globalStorage/state.vscdb",
        home / ".config/Cursor/User/globalStorage/state.vscdb",
        home / "AppData/Roaming/Cursor/User/globalStorage/state.vscdb",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def discover_claude_sources() -> dict[str, Any]:
    home = Path.home()
    base = home / ".claude"
    memories: list[Path] = []
    if (base / "CLAUDE.md").is_file():
        memories.append(base / "CLAUDE.md")
    proj_dir = base / "projects"
    if proj_dir.is_dir():
        memories.extend(sorted(proj_dir.glob("*/memory/*.md"), key=lambda p: p.stat().st_mtime, reverse=True))
    chats: list[Path] = []
    if proj_dir.is_dir():
        chats = sorted(proj_dir.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "id": "claude",
        "name": "Claude Code",
        "available": bool(memories or chats),
        "memory_files": [str(p) for p in memories],
        "chat_files": [str(p) for p in chats],
        "latest_date": file_mtime_iso(chats[0]) if chats else (file_mtime_iso(memories[0]) if memories else ""),
    }


def discover_codex_sources() -> dict[str, Any]:
    home = Path.home()
    base = home / ".codex"
    memories: list[Path] = []
    if (base / "AGENTS.md").is_file() and (base / "AGENTS.md").stat().st_size > 0:
        memories.append(base / "AGENTS.md")
    mem_dir = base / "memories"
    if mem_dir.is_dir():
        memories.extend(sorted(mem_dir.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True))
    mem_db = base / "memories_1.sqlite"
    db_memory_count = 0
    if mem_db.is_file():
        try:
            conn = sqlite3.connect(f"file:{mem_db}?mode=ro", uri=True)
            row = conn.execute("SELECT COUNT(*) FROM stage1_outputs").fetchone()
            db_memory_count = int(row[0]) if row else 0
            conn.close()
        except Exception:
            pass
    sess_dir = base / "sessions"
    chats = sorted(sess_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if sess_dir.is_dir() else []
    return {
        "id": "codex",
        "name": "Codex",
        "available": bool(memories or chats or db_memory_count > 0),
        "memory_files": [str(p) for p in memories],
        "memory_db": str(mem_db) if db_memory_count > 0 else "",
        "memory_db_count": db_memory_count,
        "chat_files": [str(p) for p in chats],
        "latest_date": file_mtime_iso(chats[0]) if chats else (file_mtime_iso(memories[0]) if memories else ""),
    }


def discover_antigravity_sources() -> dict[str, Any]:
    home = Path.home()
    gem = home / ".gemini"
    memories: list[Path] = []
    for name in ("GEMINI.md", "MEMORY.md"):
        p = gem / name
        if p.is_file() and p.stat().st_size > 0:
            memories.append(p)
    brain = gem / "antigravity/brain"
    chats = (
        sorted(brain.glob("*/.system_generated/logs/transcript.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if brain.is_dir()
        else []
    )
    return {
        "id": "antigravity",
        "name": "Antigravity / Gemini",
        "available": bool(memories or chats),
        "memory_files": [str(p) for p in memories],
        "chat_files": [str(p) for p in chats],
        "latest_date": file_mtime_iso(chats[0]) if chats else (file_mtime_iso(memories[0]) if memories else ""),
    }


def discover_cursor_sources() -> dict[str, Any]:
    home = Path.home()
    memories: list[Path] = []
    cur_rules = home / ".cursor/rules"
    if cur_rules.is_dir():
        for ext in ("*.md", "*.mdc"):
            memories.extend(cur_rules.rglob(ext))
    if (home / ".cursorrules").is_file():
        memories.append(home / ".cursorrules")
    db = get_cursor_state_db()
    composer_count = 0
    if db:
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = conn.execute(
                "SELECT COUNT(*) FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
            ).fetchone()
            composer_count = int(row[0]) if row else 0
            conn.close()
        except Exception:
            pass
    return {
        "id": "cursor",
        "name": "Cursor",
        "available": bool(memories or composer_count > 0),
        "memory_files": [str(p) for p in sorted(memories)],
        "db_path": str(db) if db else "",
        "chat_count": composer_count,
        "latest_date": file_mtime_iso(db) if db else (file_mtime_iso(memories[0]) if memories else ""),
    }


def discover_workbuddy_sources() -> dict[str, Any]:
    home = Path.home()
    base = home / ".workbuddy"
    memories: list[Path] = []
    for name in ("MEMORY.md", "USER.md", "IDENTITY.md", "SOUL.md"):
        p = base / name
        if p.is_file() and p.stat().st_size > 0:
            memories.append(p)
    for sub in ("memory", "memery"):
        mdir = base / sub
        if mdir.is_dir():
            memories.extend(sorted(mdir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True))
    # Deduplicate by resolved path
    seen_mem: set[str] = set()
    uniq_mem: list[Path] = []
    for p in memories:
        key = str(p.resolve())
        if key not in seen_mem:
            seen_mem.add(key)
            uniq_mem.append(p)

    proj_dir = base / "projects"
    chats = sorted(proj_dir.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if proj_dir.is_dir() else []
    return {
        "id": "workbuddy",
        "name": "WorkBuddy",
        "available": bool(uniq_mem or chats),
        "memory_files": [str(p) for p in uniq_mem],
        "chat_files": [str(p) for p in chats],
        "latest_date": file_mtime_iso(chats[0]) if chats else (file_mtime_iso(uniq_mem[0]) if uniq_mem else ""),
    }


def get_douchat_base_dir() -> Path | None:
    home = Path.home()
    candidates = [
        home / "Library/Application Support/douchat",
        home / ".config/douchat",
        home / "AppData/Roaming/douchat",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None


def discover_douchat_sources() -> dict[str, Any]:
    base = get_douchat_base_dir()
    memories: list[Path] = []
    db_path = ""
    conv_count = 0
    latest_date = ""
    if base:
        for name in ("MEMORY.md", "USER.md"):
            memories.extend(sorted((base / "accounts").glob(f"*/shared/{name}")) if (base / "accounts").is_dir() else [])
        db = base / "douchat.db"
        if db.is_file() and db.stat().st_size > 0:
            db_path = str(db)
            latest_date = file_mtime_iso(db)
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                row = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
                conv_count = int(row[0]) if row else 0
                conn.close()
            except Exception:
                pass
    if not latest_date and memories:
        latest_date = file_mtime_iso(memories[0])
    return {
        "id": "douchat",
        "name": "豆包工作 (Douchat)",
        "available": bool(memories or conv_count > 0),
        "memory_files": [str(p) for p in memories if p.is_file() and p.stat().st_size > 0],
        "db_path": db_path,
        "chat_count": conv_count,
        "latest_date": latest_date,
    }


def get_trae_base_dir() -> Path | None:
    home = Path.home()
    candidates = [
        home / "Library/Application Support/Trae",
        home / ".config/Trae",
        home / "AppData/Roaming/Trae",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None


def discover_trae_sources() -> dict[str, Any]:
    home = Path.home()
    base = get_trae_base_dir()
    memories: list[Path] = []
    for cand_p in (home / ".traerules", home / ".trae/rules"):
        if cand_p.is_file() and cand_p.stat().st_size > 0:
            memories.append(cand_p)
        elif cand_p.is_dir():
            memories.extend(sorted(p for p in cand_p.rglob("*") if p.is_file() and p.suffix.lower() in {".md", ".mdc"}))

    session_count = 0
    latest_date = ""
    latest_mtime = 0.0

    if base:
        ws_dir = base / "User/workspaceStorage"
        if ws_dir.is_dir():
            for ws in ws_dir.iterdir():
                st = ws / "state.vscdb"
                if not st.is_file():
                    continue
                try:
                    conn = sqlite3.connect(f"file:{st}?mode=ro", uri=True)
                    rows = conn.execute(
                        "SELECT value FROM ItemTable WHERE key LIKE 'memento/icube-ai-%chat-storage-%'"
                    ).fetchall()
                    conn.close()
                    for (v,) in rows:
                        try:
                            obj = json.loads(v)
                        except Exception:
                            continue
                        if not isinstance(obj, dict):
                            continue
                        slist = obj.get("list")
                        if isinstance(slist, list):
                            for s in slist:
                                if isinstance(s, dict) and s.get("messages"):
                                    session_count += 1
                                    created = int(s.get("updatedAt") or s.get("createdAt") or 0)
                                    mtime = created / 1000.0 if created > 1_000_000_000_000 else float(created)
                                    if mtime > latest_mtime:
                                        latest_mtime = mtime
                except Exception:
                    pass

    if latest_mtime > 1_000_000_000:
        latest_date = datetime.datetime.fromtimestamp(latest_mtime).strftime("%Y-%m-%d")

    return {
        "id": "trae",
        "name": "Trae IDE",
        "available": bool(memories or session_count > 0),
        "memory_files": [str(p) for p in memories],
        "base_path": str(base) if base else "",
        "chat_count": session_count,
        "latest_date": latest_date,
    }


def get_eden_db_path() -> Path | None:
    home = Path.home()
    candidates = [
        home / "Library/Application Support/ahub-desktop/ahub.db",
        home / "Library/Application Support/eden/ahub.db",
        home / ".eden/ahub.db",
        home / "Desktop/Eden-AI-main/ahub.db",
    ]
    valid = [p for p in candidates if p.is_file() and p.stat().st_size > 0]
    if not valid:
        return None
    valid.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return valid[0]


def discover_eden_sources() -> dict[str, Any]:
    db = get_eden_db_path()
    room_count = 0
    latest_date = ""
    if db:
        latest_date = file_mtime_iso(db)
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = conn.execute(
                "SELECT COUNT(DISTINCT room_id) FROM messages WHERE sender_id = 'WebUser' AND type = 'chat'"
            ).fetchone()
            room_count = int(row[0]) if row else 0
            conn.close()
        except Exception:
            pass
    return {
        "id": "eden",
        "name": "Eden 工作台",
        "available": room_count > 0,
        "memory_files": [],
        "db_path": str(db) if db else "",
        "chat_count": room_count,
        "latest_date": latest_date,
    }


def discover_deepseek_sources() -> dict[str, Any]:
    home = Path.home()
    dsh_dir = home / ".dsh"
    sessions_dir = dsh_dir / "sessions"
    chats: list[Path] = []
    if sessions_dir.is_dir():
        candidates_order = (
            "session.v4.jsonl.zstd",
            "session.jsonl.zstd",
            "session.v4.jsonl",
            "session.jsonl",
        )
        for sess_folder in sessions_dir.glob("*/*"):
            if not sess_folder.is_dir():
                continue
            for fname in candidates_order:
                p = sess_folder / fname
                if p.is_file() and p.stat().st_size > 0:
                    chats.append(p)
                    break
        chats.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "id": "deepseek",
        "name": "DeepSeek Harness",
        "available": bool(chats),
        "memory_files": [],
        "chat_files": [str(p) for p in chats],
        "latest_date": file_mtime_iso(chats[0]) if chats else "",
    }


def discover_hermes_sources() -> dict[str, Any]:
    home = Path.home()
    base = home / ".hermes"
    memories: list[Path] = []
    for rel in ("SOUL.md", "memories/MEMORY.md", "memories/USER.md"):
        p = base / rel
        if p.is_file() and p.stat().st_size > 0:
            memories.append(p)
    db = base / "state.db"
    db_path = ""
    session_count = 0
    latest_date = ""
    if db.is_file() and db.stat().st_size > 0:
        db_path = str(db)
        latest_date = file_mtime_iso(db)
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE COALESCE(message_count, 0) > 0"
            ).fetchone()
            session_count = int(row[0]) if row else 0
            conn.close()
        except Exception:
            pass
    if not latest_date and memories:
        latest_date = file_mtime_iso(memories[0])
    return {
        "id": "hermes",
        "name": "Hermes Agent",
        "available": bool(memories or session_count > 0),
        "memory_files": [str(p) for p in memories],
        "db_path": db_path,
        "chat_count": session_count,
        "latest_date": latest_date,
    }


def discover_openclaw_sources() -> dict[str, Any]:
    home = Path.home()
    base = home / ".openclaw"
    memories: list[Path] = []
    for name in ("AGENTS.md", "SOUL.md", "USER.md", "IDENTITY.md", "MEMORY.md"):
        p = base / name
        if p.is_file() and p.stat().st_size > 0:
            memories.append(p)
    for sub in ("memory", "memories"):
        mdir = base / sub
        if mdir.is_dir():
            memories.extend(sorted(mdir.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True))
    seen_mem: set[str] = set()
    uniq_mem: list[Path] = []
    for p in memories:
        key = str(p.resolve())
        if key not in seen_mem:
            seen_mem.add(key)
            uniq_mem.append(p)
    return {
        "id": "openclaw",
        "name": "OpenClaw",
        "available": bool(uniq_mem),
        "memory_files": [str(p) for p in uniq_mem],
        "chat_count": 0,
        "latest_date": file_mtime_iso(uniq_mem[0]) if uniq_mem else "",
    }


# ---------------------------------------------------------------------------
# Built-in Zero-Dependency Context Sources: Google Chrome & Jike (即刻)
# ---------------------------------------------------------------------------

CHROME_EPOCH_OFFSET_US = 11644473600 * 1_000_000
CHROME_SKIP_HOSTS = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "accounts.google.com",
    "login.microsoftonline.com",
}
CHROME_SKIP_HOST_KEYWORDS = ("oauth", "sso.", "auth.", "login.")


def _chrome_user_data_dir() -> Path | None:
    home = Path.home()
    if sys.platform == "darwin":
        p = home / "Library/Application Support/Google/Chrome"
    elif sys.platform.startswith("linux"):
        p = home / ".config/google-chrome"
    else:
        p = home / "AppData/Local/Google/Chrome/User Data"
    return p if p.is_dir() else None


def _list_chrome_profiles(chrome_dir: Path) -> list[dict[str, Any]]:
    info_cache: dict[str, Any] = {}
    ls_path = chrome_dir / "Local State"
    if ls_path.is_file():
        try:
            raw = json.loads(ls_path.read_text(encoding="utf-8"))
            ic = ((raw.get("profile") or {}).get("info_cache")) if isinstance(raw, dict) else None
            if isinstance(ic, dict):
                info_cache = ic
        except Exception:
            pass

    cand_ids = set(info_cache.keys())
    try:
        for entry in chrome_dir.iterdir():
            if entry.is_dir() and (entry.name == "Default" or entry.name.startswith("Profile ")):
                cand_ids.add(entry.name)
    except Exception:
        pass

    profiles: list[dict[str, Any]] = []
    for pid in cand_ids:
        pdir = chrome_dir / pid
        hist = pdir / "History"
        bmk = pdir / "Bookmarks"
        if not hist.is_file() and not bmk.is_file():
            continue
        meta = info_cache.get(pid) if isinstance(info_cache.get(pid), dict) else {}
        email = str(meta.get("user_name") or "").strip()
        name = str(meta.get("name") or pid).strip()
        mtime_src = hist if hist.is_file() else bmk
        profiles.append(
            {
                "id": pid,
                "name": name,
                "email": email,
                "dir": pdir,
                "latest_date": file_mtime_iso(mtime_src),
            }
        )

    profiles.sort(key=lambda x: (0 if x["email"] else 1, 0 if x["id"] == "Default" else 1, x["id"]))
    return profiles


def _clean_chrome_url(url: str) -> tuple[str, str] | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    host = parsed.hostname or ""
    if not host or host in CHROME_SKIP_HOSTS or any(k in host for k in CHROME_SKIP_HOST_KEYWORDS):
        return None
    if (host.endswith("google.com") or host == "www.baidu.com" or host == "cn.bing.com") and parsed.path in {"/search", "/s", "/aclk"}:
        return None
    clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
    return host, clean


def discover_chrome_sources() -> dict[str, Any]:
    chrome_dir = _chrome_user_data_dir()
    profiles = _list_chrome_profiles(chrome_dir) if chrome_dir else []
    latest = max((p["latest_date"] for p in profiles if p["latest_date"]), default="")
    return {
        "id": "chrome",
        "name": "Google Chrome (书签/常逛)",
        "available": bool(profiles),
        "memory_files": [],
        "chat_count": len(profiles),
        "unit_label": "个配置",
        "latest_date": latest,
    }


def extract_chrome_context(days: int, max_sessions: int) -> tuple[str, int, list[str]]:
    chrome_dir = _chrome_user_data_dir()
    if not chrome_dir:
        return "", 0, []
    profiles = _list_chrome_profiles(chrome_dir)
    if not profiles:
        return "", 0, []

    limit = max(5, min(max_sessions, 30))
    lines = [
        "# Google Chrome 书签与高频访问线索（仅用于提炼常用工具、技术栈与关注领域，严禁流水入树）",
        f"> 提取时间: {now_iso()} | 回溯窗口: 近 {days or 60} 天 | 不读取 Cookie 与密码",
        "",
    ]

    eff_days = days if days > 0 else 90
    cutoff_us = int((time.time() - eff_days * 86400) * 1_000_000) + CHROME_EPOCH_OFFSET_US
    min_visits = 1 if eff_days <= 7 else 3
    order_by = "last_visit_time DESC" if eff_days <= 7 else "visit_count DESC, last_visit_time DESC"
    total_items = 0

    for p in profiles[:2]:
        label = f"{p['name']} ({p['email']})" if p["email"] else p["name"]
        hist_path = p["dir"] / "History"
        bmk_path = p["dir"] / "Bookmarks"
        host_counts: Counter[str] = Counter()
        pages: list[str] = []
        bookmarks: list[str] = []

        if hist_path.is_file():
            tmp_dir = Path(tempfile.mkdtemp(prefix="tree_chrome_"))
            tmp_db = tmp_dir / "History"
            seen_urls: set[str] = set()
            try:
                shutil.copy2(hist_path, tmp_db)
                conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
                cur = conn.execute(
                    f"""
                    SELECT url, title, visit_count,
                           date(last_visit_time/1000000 - 11644473600, 'unixepoch', 'localtime') AS d
                    FROM urls
                    WHERE last_visit_time >= ? AND visit_count >= ? AND title != ''
                    ORDER BY {order_by}
                    LIMIT 250
                    """,
                    (cutoff_us, min_visits),
                )
                for url, title, vcount, d_str in cur.fetchall():
                    cleaned = _clean_chrome_url(str(url or ""))
                    if not cleaned:
                        continue
                    host, clean_u = cleaned
                    host_counts[host] += int(vcount or 1)
                    if clean_u not in seen_urls and len(pages) < limit:
                        seen_urls.add(clean_u)
                        t = str(title or "").strip().replace("\n", " ")[:90]
                        pages.append(f"- [{d_str or ''}] ({vcount}次) {t} — {clean_u}")
                conn.close()
            except Exception:
                pass
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        if bmk_path.is_file() and eff_days > 7:
            try:
                bdata = json.loads(bmk_path.read_text(encoding="utf-8"))

                def _walk_bmk(node: Any, folder: str = "") -> None:
                    if len(bookmarks) >= limit or not isinstance(node, dict):
                        return
                    ntype = node.get("type")
                    if ntype == "url":
                        title = str(node.get("name") or "").strip()
                        url = str(node.get("url") or "").strip()
                        cleaned = _clean_chrome_url(url)
                        if title and cleaned:
                            _, clean_u = cleaned
                            prefix = f"[{folder}] " if folder else ""
                            bookmarks.append(f"- {prefix}{title} ({clean_u})")
                    elif ntype == "folder":
                        fname = str(node.get("name") or "").strip()
                        next_folder = f"{folder}/{fname}" if (folder and fname) else (fname or folder)
                        for child in node.get("children") or []:
                            _walk_bmk(child, next_folder)

                roots = bdata.get("roots") or {}
                if isinstance(roots, dict):
                    for rnode in roots.values():
                        _walk_bmk(rnode)
            except Exception:
                pass

        top_hosts = host_counts.most_common(12)
        if not top_hosts and not pages and not bookmarks:
            continue

        lines.append(f"## Profile: {label}")
        if top_hosts:
            lines.append("### 高频访问域名（工具链与注意力分布）")
            lines.append(", ".join(f"`{h}`({c})" for h, c in top_hosts))
            lines.append("")
        if pages:
            lines.append("### 高频回访页面")
            lines.extend(pages)
            lines.append("")
            total_items += len(pages)
        if bookmarks:
            lines.append("### 书签精选")
            lines.extend(bookmarks)
            lines.append("")
            total_items += len(bookmarks)

    if total_items == 0:
        return "", 0, []
    return redact_lifestyle_text("\n".join(lines).strip() + "\n"), total_items, [short_hash(f"chrome:{total_items}")]


def get_connector_helpers() -> dict[str, Any]:
    return {
        "clean_chat_text": clean_chat_text,
        "redact_secrets": redact_secrets,
        "redact_lifestyle_text": redact_lifestyle_text,
        "extract_text_from_content": extract_text_from_content,
        "now_iso": now_iso,
        "file_mtime_iso": file_mtime_iso,
        "within_days": within_days,
        "short_hash": short_hash,
    }


def _load_connector_module(entry_file: Path, mod_tag: str, extra_sys_dir: Path | None = None) -> Any | None:
    inserted = False
    dir_str = str(extra_sys_dir) if extra_sys_dir else ""
    if dir_str and dir_str not in sys.path:
        sys.path.insert(0, dir_str)
        inserted = True
    try:
        spec = importlib.util.spec_from_file_location(mod_tag, str(entry_file))
        if not spec or not spec.loader:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if inserted and dir_str in sys.path:
            # Keep package dir at the end so lazy imports inside extract()/login_qr() also resolve
            sys.path.remove(dir_str)
            sys.path.append(dir_str)


def load_plugin_connectors(root: Path) -> dict[str, Any]:
    """Dynamically load read-only connectors from `seed/connectors/<name>.py` or `<name>/connector.py`."""
    plugins: dict[str, Any] = {}
    search_dirs = [
        Path(__file__).resolve().parent / "seed_connectors",
        user_connectors_dir(root),
    ]
    for d in search_dirs:
        if not d.is_dir():
            continue
        # 1. Single-file connectors: *.py
        for py_file in sorted(d.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                mod = _load_connector_module(py_file, f"tree_seed_plugin_{py_file.stem}", d)
                if not mod:
                    continue
                cid = str(getattr(mod, "CONNECTOR_ID", py_file.stem)).strip().lower()
                if cid and callable(getattr(mod, "discover", None)):
                    plugins[cid] = mod
            except Exception as e:
                print(f"⚠️ 加载自定义连接器 `{py_file.name}` 失败（已跳过）: {e}", file=sys.stderr)
        # 2. Self-contained package connectors: <name>/connector.py (installed via `tree seed add`)
        for sub_dir in sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
            entry = sub_dir / "connector.py"
            if not entry.is_file():
                entry = sub_dir / "__init__.py"
            if not entry.is_file():
                continue
            try:
                mod = _load_connector_module(entry, f"tree_seed_pkg_{sub_dir.name}", sub_dir)
                if not mod:
                    continue
                cid = str(getattr(mod, "CONNECTOR_ID", sub_dir.name)).strip().lower()
                if cid and callable(getattr(mod, "discover", None)):
                    plugins[cid] = mod
            except Exception as e:
                print(f"⚠️ 加载连接器包 `{sub_dir.name}` 失败（已跳过）: {e}", file=sys.stderr)
    return plugins


def scan_all_local_sources(root: Path | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    discovered: dict[str, dict[str, Any]] = {
        "claude": discover_claude_sources(),
        "codex": discover_codex_sources(),
        "antigravity": discover_antigravity_sources(),
        "cursor": discover_cursor_sources(),
        "trae": discover_trae_sources(),
        "workbuddy": discover_workbuddy_sources(),
        "douchat": discover_douchat_sources(),
        "hermes": discover_hermes_sources(),
        "openclaw": discover_openclaw_sources(),
        "eden": discover_eden_sources(),
        "deepseek": discover_deepseek_sources(),
        "chrome": discover_chrome_sources(),
    }
    plugins = load_plugin_connectors(root) if root else {}
    for cid, mod in plugins.items():
        try:
            info = mod.discover()
            if isinstance(info, dict):
                info.setdefault("id", cid)
                info.setdefault("name", getattr(mod, "CONNECTOR_NAME", cid))
                info["is_plugin"] = True
                discovered[cid] = info
        except Exception as e:
            print(f"⚠️ 自定义连接器 `{cid}` 探测失败（已跳过）: {e}", file=sys.stderr)
    return discovered, plugins


# ---------------------------------------------------------------------------
# Extraction Pipelines (Noise Stripping + Redaction)
# ---------------------------------------------------------------------------

def _summarize_memory_file(p: Path) -> tuple[int, str, bool]:
    """Return (effective_chars, first_line_preview, only_tree_block)."""
    try:
        raw = p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return 0, "", False
    had_tree_block = bool(TREE_CONTEXT_BLOCK_RE.search(raw))
    stripped = strip_tree_context_block(raw)
    if not stripped:
        return 0, "", had_tree_block
    # Strip YAML frontmatter if present for preview
    body = stripped
    if body.startswith("---\n"):
        end_idx = body.find("\n---", 4)
        if end_idx >= 0:
            body = body[end_idx + 4 :].strip()
    preview = ""
    for line in body.splitlines():
        s = line.strip().lstrip("#>-* ").strip()
        if s and not s.startswith("<!--"):
            preview = redact_secrets(s)[:80]
            break
    return len(stripped), preview, False


def build_memories_index(
    discovered: dict[str, dict[str, Any]],
    selected: set[str],
) -> tuple[str, int]:
    """Build a zero-truncation index of all local memory/rule files so the host Agent can inspect and read on demand."""
    sections: list[str] = [
        "# 本地 AI 记忆与规则文件全量索引（按需精读，零截断）",
        f"> 生成时间: {now_iso()} | 仅作只读导航索引，不修改原文件。",
        "> **用法**：请先通览全盘，挑出高价值入口（全局画像/规则、仍在推进项目的 `MEMORY.md` 或近期核心记录），直接读取原文件或执行 `tree seed read \"<文件路径>\"`（自动脱敏并剥离 Tree 自身同步块）。",
        "> **第三方记忆系统**：若用户本机还装有 Mem0、Obsidian、OpenMemory、MCP 记忆服务或其他自定义记忆目录，请主动询问并在获得授权后一并只读查询梳理。",
        "",
    ]
    total_files = 0

    for sid, info in discovered.items():
        if sid not in selected:
            continue
        mem_paths = [Path(p) for p in info.get("memory_files", []) if Path(p).is_file()]
        if not mem_paths:
            continue

        sections.append(f"## {info.get('name', sid)} (`{sid}` · {len(mem_paths)} 份)")
        if sid == "claude":
            # Group Claude Code project memories by project slug for fast scanning
            global_files: list[Path] = []
            proj_groups: dict[str, list[Path]] = {}
            for p in mem_paths:
                parts = p.parts
                if "projects" in parts and "memory" in parts:
                    idx = parts.index("projects")
                    proj_slug = parts[idx + 1] if idx + 1 < len(parts) else "unknown"
                    proj_groups.setdefault(proj_slug, []).append(p)
                else:
                    global_files.append(p)
            for p in global_files:
                chars, preview, only_tree = _summarize_memory_file(p)
                tag = "[仅含 Tree 同步块，可跳过]" if only_tree else f"{chars} 字符"
                hint = f" — {preview}" if preview else ""
                sections.append(f"- `{p}` [{file_mtime_iso(p)} · {tag}]{hint}")
                total_files += 1
            for proj_slug, plist in proj_groups.items():
                # Put MEMORY.md first within each project group
                plist_sorted = sorted(
                    plist,
                    key=lambda x: (0 if x.name.upper() == "MEMORY.MD" else 1, -x.stat().st_mtime),
                )
                latest_d = file_mtime_iso(max(plist, key=lambda x: x.stat().st_mtime))
                sections.append(f"### 项目 `{proj_slug}`（{len(plist)} 份 · 最近 {latest_d}）")
                for p in plist_sorted:
                    chars, preview, only_tree = _summarize_memory_file(p)
                    tag = "[仅含 Tree 同步块，可跳过]" if only_tree else f"{chars} 字符"
                    hint = f" — {preview}" if preview else ""
                    sections.append(f"- `{p}` [{file_mtime_iso(p)} · {tag}]{hint}")
                    total_files += 1
        else:
            for p in mem_paths:
                chars, preview, only_tree = _summarize_memory_file(p)
                tag = "[仅含 Tree 同步块，可跳过]" if only_tree else f"{chars} 字符"
                hint = f" — {preview}" if preview else ""
                sections.append(f"- `{p}` [{file_mtime_iso(p)} · {tag}]{hint}")
                total_files += 1
        sections.append("")

    return "\n".join(sections).rstrip() + "\n", total_files


def extract_sqlite_memories(
    discovered: dict[str, dict[str, Any]],
    selected: set[str],
    max_chars: int = 35000,
) -> tuple[str, int]:
    """Unpack non-Markdown SQLite memory stores (e.g. Codex memories_1.sqlite) into readable Markdown."""
    sections: list[str] = [
        "# 本地数据库记忆解包（SQLite 只读导出）",
        f"> 生成时间: {now_iso()} | 仅针对无法直接用文本工具打开的数据库记忆。",
        "",
    ]
    total_chars = 0
    row_count = 0
    for sid, info in discovered.items():
        if sid not in selected or not info.get("memory_db"):
            continue
        sections.append(f"## 来源：{info.get('name', sid)} (`{info['memory_db']}`)")
        try:
            conn = sqlite3.connect(f"file:{info['memory_db']}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT thread_id, rollout_slug, raw_memory, rollout_summary FROM stage1_outputs ORDER BY source_updated_at DESC LIMIT 30"
            ).fetchall()
            conn.close()
            for tid, slug, raw_mem, summary in rows:
                txt = redact_secrets((raw_mem or summary or "").strip())
                if not txt:
                    continue
                block = f"### `{slug or tid}`\n{txt[:1500]}\n"
                if total_chars + len(block) > max_chars:
                    sections.append(f"\n> *(已达单次导出上限 {max_chars} 字符)*\n")
                    break
                sections.append(block)
                total_chars += len(block)
                row_count += 1
        except Exception:
            pass
    return "\n".join(sections).rstrip() + "\n", row_count


def extract_memories_bundle(
    discovered: dict[str, dict[str, Any]],
    selected: set[str],
    max_chars: int = 45000,
) -> tuple[str, int]:
    """Deprecated compatibility alias; kept for external callers."""
    return build_memories_index(discovered, selected)


def parse_claude_session(path: Path) -> tuple[str, str, list[str]]:
    """Return (date_str, cwd_or_title, user_utterances)."""
    user_msgs: list[str] = []
    cwd = ""
    title = ""
    date_str = file_mtime_iso(path)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type")
                if not cwd and isinstance(obj.get("cwd"), str):
                    cwd = obj["cwd"]
                if t == "ai-title" and isinstance(obj.get("aiTitle"), str):
                    title = obj["aiTitle"].strip()
                if t == "user" and not obj.get("isSidechain"):
                    msg = obj.get("message", {})
                    raw = extract_text_from_content(msg.get("content") if isinstance(msg, dict) else msg)
                    cleaned = clean_chat_text(raw, max_len=450)
                    if cleaned and len(cleaned) >= 4:
                        user_msgs.append(cleaned)
    except Exception:
        pass
    label = title or (Path(cwd).name if cwd else path.stem[:12])
    return date_str, f"{label} ({cwd})" if cwd and title else (cwd or label), user_msgs


def parse_codex_session(path: Path) -> tuple[str, str, list[str]]:
    user_msgs: list[str] = []
    cwd = ""
    date_str = file_mtime_iso(path)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type")
                payload = obj.get("payload")
                if t == "session_meta" and isinstance(payload, dict):
                    cwd = str(payload.get("cwd") or cwd)
                elif t == "response_item" and isinstance(payload, dict):
                    if payload.get("type") == "message" and payload.get("role") == "user":
                        raw = extract_text_from_content(payload.get("content"))
                        cleaned = clean_chat_text(raw, max_len=450)
                        if cleaned and len(cleaned) >= 4:
                            user_msgs.append(cleaned)
    except Exception:
        pass
    return date_str, cwd or path.stem[:20], user_msgs


def parse_antigravity_session(path: Path) -> tuple[str, str, list[str]]:
    user_msgs: list[str] = []
    date_str = file_mtime_iso(path)
    conv_id = path.parent.parent.parent.name if len(path.parents) >= 3 else path.stem
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") == "USER_INPUT":
                    raw = extract_text_from_content(obj.get("content"))
                    cleaned = clean_chat_text(raw, max_len=450)
                    if cleaned and len(cleaned) >= 4:
                        user_msgs.append(cleaned)
    except Exception:
        pass
    return date_str, f"conversation:{conv_id[:12]}", user_msgs


def parse_workbuddy_session(path: Path) -> tuple[str, str, list[str]]:
    user_msgs: list[str] = []
    cwd = ""
    title = ""
    date_str = file_mtime_iso(path)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type")
                if not cwd and isinstance(obj.get("cwd"), str):
                    cwd = obj["cwd"]
                if t == "ai-title" and isinstance(obj.get("aiTitle") or obj.get("title"), str):
                    title = str(obj.get("aiTitle") or obj.get("title")).strip()
                if t == "message" and str(obj.get("role", "")).lower() == "user":
                    raw = extract_text_from_content(obj.get("content"))
                    cleaned = clean_chat_text(raw, max_len=450)
                    if cleaned and len(cleaned) >= 4:
                        user_msgs.append(cleaned)
    except Exception:
        pass
    label = title or (Path(cwd).name if cwd else path.stem[:12])
    return date_str, f"{label} ({cwd})" if cwd and title else (cwd or label), user_msgs


def _find_zstd_bin() -> str | None:
    found = shutil.which("zstd")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/zstd", "/usr/local/bin/zstd"):
        if Path(candidate).is_file():
            return candidate
    return None


def iter_deepseek_session_lines(path: Path) -> list[str]:
    """Read lines from DeepSeek Harness session.v4.jsonl.zstd / session.jsonl(.zstd) without external Python deps."""
    if path.suffix != ".zstd":
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()
    raw_bytes = path.read_bytes()
    # Try Python 3.14+ stdlib compression.zstd or optional zstandard module first
    for mod_name in ("compression.zstd", "zstandard"):
        try:
            mod = importlib.import_module(mod_name)
            if hasattr(mod, "decompress"):
                return mod.decompress(raw_bytes).decode("utf-8", errors="ignore").splitlines()
            if hasattr(mod, "ZstdDecompressor"):
                dctx = mod.ZstdDecompressor()
                with dctx.stream_reader(raw_bytes) as reader:
                    return reader.read().decode("utf-8", errors="ignore").splitlines()
        except Exception:
            pass
    zstd_bin = _find_zstd_bin()
    if zstd_bin:
        try:
            res = subprocess.run(
                [zstd_bin, "-dc", str(path)],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if res.returncode == 0 and res.stdout:
                return res.stdout.decode("utf-8", errors="ignore").splitlines()
        except Exception:
            pass
    return []


def parse_deepseek_session(path: Path) -> tuple[str, str, list[str]]:
    user_msgs: list[str] = []
    cwd = ""
    title = ""
    date_str = file_mtime_iso(path)
    try:
        for line in iter_deepseek_session_lines(path):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            t = obj.get("type")
            if t == "session":
                if isinstance(obj.get("cwd"), str):
                    cwd = obj["cwd"]
                if isinstance(obj.get("createdAt"), (int, float)) and obj["createdAt"] > 1_000_000_000:
                    sec = obj["createdAt"] / 1000 if obj["createdAt"] > 1_000_000_000_000 else obj["createdAt"]
                    date_str = datetime.datetime.fromtimestamp(sec).strftime("%Y-%m-%d")
            elif t == "session/title":
                tdata = obj.get("data")
                if isinstance(tdata, dict) and isinstance(tdata.get("title"), str):
                    cand_title = tdata["title"].strip()
                    if cand_title and not cand_title.lower().startswith("[eden runtime context]"):
                        title = cand_title
            elif t == "user/message":
                data = obj.get("data")
                if not isinstance(data, dict):
                    continue
                src_info = data.get("source")
                if isinstance(src_info, dict):
                    skind = str(src_info.get("kind") or "")
                    if skind in ("agent-instructions", "runtime-context"):
                        continue
                raw = extract_text_from_content(data.get("content"))
                cleaned = clean_chat_text(raw, max_len=450)
                if cleaned and len(cleaned) >= 4:
                    user_msgs.append(cleaned)
    except Exception:
        pass
    sess_id = path.parent.name
    label = title or (Path(cwd).name if cwd else sess_id[:12])
    return date_str, f"{label} ({cwd})" if cwd and title else (cwd or label), user_msgs


def _format_chat_ref(source_id: str, raw_id: str) -> str:
    """Build a collision-free `<source>:<id>` ref accepted by `tree chat read`."""
    rid = str(raw_id or "").strip()
    if source_id == "codex" and rid.startswith("rollout-"):
        # Format: rollout-YYYY-MM-DDTHH-MM-SS-<uuid>; use the UUID portion to avoid prefix collisions
        m = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$", rid)
        if m:
            return f"codex:{m.group(1)[:13]}"
    # Keep full ID when short/structured (e.g. hermes timestamps, douchat direct-*, deepseek session-*)
    if len(rid) <= 40:
        return f"{source_id}:{rid}"
    return f"{source_id}:{rid[:24]}"


def extract_douchat_chats(
    db_path_str: str,
    days: int,
    max_sessions: int,
    max_chars: int = 35000,
) -> tuple[str, int, list[str]]:
    if not db_path_str or not Path(db_path_str).is_file():
        return "", 0, []
    lines: list[str] = [
        "# 豆包工作 (Douchat) 本地历史对话精华（人类发言与意图流）",
        f"> 生成时间: {now_iso()} | 范围: 最近 {days} 天，最多 {max_sessions} 场对话 | 只读安全提取。",
        "> **按需下钻双边上下文**：若某场人类发言有价值但缺 AI 回复看不清来龙去脉，用标题中的 `ref` 执行 `tree chat read \"<ref>\" --turns 30` 调阅人+AI完整对话。",
        "",
    ]
    cutoff_ms = (datetime.datetime.now().timestamp() - days * 86400) * 1000 if days > 0 else 0
    sessions_out: list[tuple[int, str, str, list[str]]] = []

    try:
        conn = sqlite3.connect(f"file:{db_path_str}?mode=ro", uri=True)
        agent_ids = {r[0] for r in conn.execute("SELECT id FROM agents").fetchall() if r and r[0]}
        conv_rows = conn.execute(
            "SELECT id, updatedAt, data FROM conversations ORDER BY updatedAt DESC LIMIT ?",
            (max(max_sessions * 3, 60),),
        ).fetchall()
        for cid, updated_at, cdata_str in conv_rows:
            ts_val = 0
            try:
                ts_val = int(updated_at or 0)
            except Exception:
                ts_val = 0
            if cutoff_ms and ts_val > 1_000_000_000_000 and ts_val < cutoff_ms:
                continue
            cname = cid
            owner_id = ""
            try:
                cobj = json.loads(cdata_str or "{}")
                cname = str(cobj.get("name") or cid)[:60]
                owner_id = str(cobj.get("ownerId") or "")
            except Exception:
                pass
            mrows = conn.execute(
                "SELECT data FROM messages WHERE conversationId = ? ORDER BY createdAt ASC LIMIT 200",
                (cid,),
            ).fetchall()
            user_msgs: list[str] = []
            for (mdata_str,) in mrows:
                try:
                    mobj = json.loads(mdata_str or "{}")
                except Exception:
                    continue
                aid = str(mobj.get("authorId") or "")
                aname = str(mobj.get("authorName") or "").lower()
                if aid in agent_ids:
                    continue
                is_human = (aid.lower() in {"user", "human"}) or (aname in {"you", "user"}) or (owner_id and aid == owner_id)
                if not is_human:
                    continue
                txt = clean_chat_text(str(mobj.get("text") or ""), max_len=450)
                if txt and len(txt) >= 2:
                    user_msgs.append(txt)
            if user_msgs:
                sessions_out.append((ts_val, cid, cname, user_msgs))
        conn.close()
    except Exception as e:
        lines.append(f"> *(读取 Douchat 数据库时跳过: {e})*")
        return "\n".join(lines), 0, []

    total_chars = 0
    used = 0
    processed_ids: list[str] = []
    for ts_val, cid, cname, user_msgs in sessions_out[:max_sessions]:
        date_str = (
            datetime.datetime.fromtimestamp(ts_val / 1000).strftime("%Y-%m-%d")
            if ts_val > 1_000_000_000_000
            else (datetime.datetime.fromtimestamp(ts_val).strftime("%Y-%m-%d") if ts_val > 1_000_000_000 else "recent")
        )
        ref = _format_chat_ref("douchat", str(cid))
        block_lines = [f"## [{date_str}] {cname} (`ref: {ref}` · {len(user_msgs)} 轮人类输入)"]
        for m in user_msgs[:15]:
            block_lines.append(f"- {m.replace(chr(10), ' / ')}")
        block_lines.append("")
        block = "\n".join(block_lines)
        if total_chars + len(block) > max_chars:
            break
        lines.append(block)
        total_chars += len(block)
        used += 1
        processed_ids.append(short_hash(f"douchat:{cid}:{ts_val}"))
    return "\n".join(lines), used, processed_ids


def extract_eden_chats(
    db_path_str: str,
    days: int,
    max_sessions: int,
    max_chars: int = 38000,
) -> tuple[str, int, list[str]]:
    if not db_path_str or not Path(db_path_str).is_file():
        return "", 0, []
    lines: list[str] = [
        "# Eden 工作台本地历史对话精华（人类发言与意图流）",
        f"> 生成时间: {now_iso()} | 范围: 最近 {days} 天，最多 {max_sessions} 个活跃房间 | 只读安全提取。",
        "> **按需下钻双边上下文**：若某场人类发言有价值但缺 AI 回复看不清来龙去脉，用标题中的 `ref` 执行 `tree chat read \"<ref>\" --turns 30` 调阅人+AI完整对话。",
        "",
    ]
    cutoff_iso = (
        (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
        if days > 0
        else ""
    )
    rooms_out: list[tuple[str, str, str, list[str]]] = []

    try:
        conn = sqlite3.connect(f"file:{db_path_str}?mode=ro", uri=True)
        room_names = {
            r[0]: str(r[1] or r[0])
            for r in conn.execute("SELECT id, name FROM rooms").fetchall()
        }
        q = (
            "SELECT room_id, MAX(timestamp) AS last_ts FROM messages "
            "WHERE sender_id = 'WebUser' AND type = 'chat' "
            + ("AND timestamp >= ? " if cutoff_iso else "")
            + "GROUP BY room_id ORDER BY last_ts DESC LIMIT ?"
        )
        params = (cutoff_iso, max(max_sessions * 2, 40)) if cutoff_iso else (max(max_sessions * 2, 40),)
        active_rooms = conn.execute(q, params).fetchall()
        for rid, last_ts in active_rooms:
            mrows = conn.execute(
                "SELECT content FROM messages WHERE room_id = ? AND sender_id = 'WebUser' AND type = 'chat' ORDER BY timestamp ASC LIMIT 200",
                (rid,),
            ).fetchall()
            user_msgs: list[str] = []
            for (content,) in mrows:
                txt = clean_chat_text(str(content or ""), max_len=450)
                if txt and len(txt) >= 4:
                    user_msgs.append(txt)
            if user_msgs:
                date_str = str(last_ts or "")[:10] or "recent"
                rooms_out.append((date_str, rid, room_names.get(rid, rid), user_msgs))
        conn.close()
    except Exception as e:
        lines.append(f"> *(读取 Eden 数据库时跳过: {e})*")
        return "\n".join(lines), 0, []

    total_chars = 0
    used = 0
    processed_ids: list[str] = []
    for date_str, rid, rname, user_msgs in rooms_out[:max_sessions]:
        selected = user_msgs[:6] + ["…[中间轮次省略]…"] + user_msgs[-8:] if len(user_msgs) > 15 else user_msgs
        ref = _format_chat_ref("eden", str(rid))
        block_lines = [f"## [{date_str}] {rname} (`ref: {ref}` · {len(user_msgs)} 轮人类输入)"]
        for m in selected:
            block_lines.append(f"- {m.replace(chr(10), ' / ')}")
        block_lines.append("")
        block = "\n".join(block_lines)
        if total_chars + len(block) > max_chars:
            break
        lines.append(block)
        total_chars += len(block)
        used += 1
        processed_ids.append(short_hash(f"eden:{rid}:{date_str}:{len(user_msgs)}"))
    return "\n".join(lines), used, processed_ids


def extract_hermes_chats(
    db_path_str: str,
    days: int,
    max_sessions: int,
    max_chars: int = 38000,
) -> tuple[str, int, list[str]]:
    if not db_path_str or not Path(db_path_str).is_file():
        return "", 0, []
    lines: list[str] = [
        "# Hermes Agent 本地历史对话精华（人类发言与意图流）",
        f"> 生成时间: {now_iso()} | 范围: 最近 {days} 天，最多 {max_sessions} 场会话 | 只读安全提取。",
        "> **按需下钻双边上下文**：若某场人类发言有价值但缺 AI 回复看不清来龙去脉，用标题中的 `ref` 执行 `tree chat read \"<ref>\" --turns 30` 调阅人+AI完整对话。",
        "",
    ]
    cutoff_sec = (datetime.datetime.now().timestamp() - days * 86400) if days > 0 else 0.0
    sessions_out: list[tuple[str, str, str, list[str]]] = []

    try:
        conn = sqlite3.connect(f"file:{db_path_str}?mode=ro", uri=True)
        q = (
            "SELECT id, COALESCE(title, id), COALESCE(last_activity_at, started_at, 0) AS ts "
            "FROM sessions WHERE COALESCE(message_count, 0) > 0 "
            + ("AND COALESCE(last_activity_at, started_at, 0) >= ? " if cutoff_sec else "")
            + "ORDER BY ts DESC LIMIT ?"
        )
        params = (cutoff_sec, max(max_sessions * 2, 40)) if cutoff_sec else (max(max_sessions * 2, 40),)
        rows = conn.execute(q, params).fetchall()
        for sid, title, ts in rows:
            mrows = conn.execute(
                "SELECT content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id ASC LIMIT 120",
                (sid,),
            ).fetchall()
            user_msgs: list[str] = []
            for (content,) in mrows:
                txt = clean_chat_text(str(content or ""), max_len=450)
                if txt and len(txt) >= 4:
                    user_msgs.append(txt)
            if user_msgs:
                sec = float(ts or 0)
                if sec > 1_000_000_000_000:
                    sec /= 1000.0
                date_str = datetime.datetime.fromtimestamp(sec).strftime("%Y-%m-%d") if sec > 1_000_000_000 else "recent"
                sessions_out.append((date_str, str(sid), str(title or sid)[:75], user_msgs))
        conn.close()
    except Exception as e:
        lines.append(f"> *(读取 Hermes 数据库时跳过: {e})*")
        return "\n".join(lines), 0, []

    total_chars = 0
    used = 0
    processed_ids: list[str] = []
    for date_str, sid, title, user_msgs in sessions_out[:max_sessions]:
        selected = user_msgs[:6] + ["…[中间轮次省略]…"] + user_msgs[-8:] if len(user_msgs) > 15 else user_msgs
        ref = _format_chat_ref("hermes", str(sid))
        block_lines = [f"## [{date_str}] {title} (`ref: {ref}` · {len(user_msgs)} 轮人类输入)"]
        for m in selected:
            block_lines.append(f"- {m.replace(chr(10), ' / ')}")
        block_lines.append("")
        block = "\n".join(block_lines)
        if total_chars + len(block) > max_chars:
            break
        lines.append(block)
        total_chars += len(block)
        used += 1
        processed_ids.append(short_hash(f"hermes:{sid}:{date_str}:{len(user_msgs)}"))
    return "\n".join(lines), used, processed_ids


def extract_jsonl_source_chats(
    source_id: str,
    source_name: str,
    chat_files: list[str],
    parser_fn: Callable[[Path], tuple[str, str, list[str]]],
    days: int,
    max_sessions: int,
    max_chars: int = 38000,
    skip_ids: set[str] | None = None,
) -> tuple[str, int, list[str]]:
    lines: list[str] = [
        f"# {source_name} 本地历史对话精华（人类发言与意图流）",
        f"> 生成时间: {now_iso()} | 范围: 最近 {days} 天，最多 {max_sessions} 场对话 | 已剥离代码日志与敏感密钥。",
        "> **按需下钻双边上下文**：若某场人类发言有价值但缺 AI 回复看不清来龙去脉，用标题中的 `ref` 执行 `tree chat read \"<ref>\" --turns 30` 调阅人+AI完整对话。",
        "",
    ]
    total_chars = 0
    used_sessions = 0
    processed_ids: list[str] = []

    for fp_str in chat_files:
        if used_sessions >= max_sessions or total_chars >= max_chars:
            break
        p = Path(fp_str)
        if not p.is_file() or not within_days(p, days):
            continue
        sess_hash = short_hash(f"{fp_str}:{p.stat().st_mtime_ns}")
        if skip_ids and sess_hash in skip_ids:
            continue
        date_str, label, user_msgs = parser_fn(p)
        if not user_msgs:
            continue
        # Keep up to 15 highest-signal turns per session (first + sampled/recent)
        if len(user_msgs) > 15:
            selected_msgs = user_msgs[:6] + ["…[中间轮次省略]…"] + user_msgs[-8:]
        else:
            selected_msgs = user_msgs

        if source_id == "antigravity":
            raw_id = p.parent.parent.parent.name if len(p.parents) >= 3 else p.stem
        elif source_id == "deepseek":
            raw_id = p.parent.name
        else:
            raw_id = p.stem
        ref = _format_chat_ref(source_id, raw_id)

        block_lines = [f"## [{date_str}] {label} (`ref: {ref}` · {len(user_msgs)} 轮人类输入)"]
        for idx, m in enumerate(selected_msgs, 1):
            single = m.replace("\n", " / ")
            block_lines.append(f"- {single}")
        block_lines.append("")
        block = "\n".join(block_lines)
        if total_chars + len(block) > max_chars:
            break
        lines.append(block)
        total_chars += len(block)
        used_sessions += 1
        processed_ids.append(short_hash(f"{fp_str}:{p.stat().st_mtime_ns}"))

    return "\n".join(lines), used_sessions, processed_ids


def extract_trae_chats(
    base_dir_str: str,
    days: int,
    max_sessions: int,
    max_chars: int = 35000,
) -> tuple[str, int, list[str]]:
    if not base_dir_str or not Path(base_dir_str).is_dir():
        return "", 0, []
    ws_dir = Path(base_dir_str) / "User/workspaceStorage"
    if not ws_dir.is_dir():
        return "", 0, []

    lines: list[str] = [
        "# Trae 本地历史对话精华（人类发言与意图流）",
        f"> 生成时间: {now_iso()} | 范围: 最近 {days} 天，最多 {max_sessions} 场对话 | 只读安全提取。",
        "> **按需下钻双边上下文**：若某场人类发言有价值但缺 AI 回复看不清来龙去脉，用标题中的 `ref` 执行 `tree chat read \"<ref>\" --turns 30` 调阅人+AI完整对话。",
        "",
    ]
    cutoff_ms = (datetime.datetime.now().timestamp() - days * 86400) * 1000 if days > 0 else 0
    sessions_found: list[tuple[int, str, str, str, list[str]]] = []

    for ws in ws_dir.iterdir():
        st = ws / "state.vscdb"
        if not st.is_file():
            continue
        try:
            ws_json = ws / "workspace.json"
            ws_label = ""
            if ws_json.is_file():
                try:
                    fld = json.loads(ws_json.read_text(encoding="utf-8")).get("folder", "")
                    if fld:
                        ws_label = urllib.parse.unquote(fld.split("/")[-1])
                except Exception:
                    pass
            conn = sqlite3.connect(f"file:{st}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT value FROM ItemTable WHERE key LIKE 'memento/icube-ai-%chat-storage-%'"
            ).fetchall()
            conn.close()
            for (v,) in rows:
                try:
                    obj = json.loads(v)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                slist = obj.get("list")
                if not isinstance(slist, list):
                    continue
                for s in slist:
                    if not isinstance(s, dict):
                        continue
                    msgs = s.get("messages")
                    if not isinstance(msgs, list) or not msgs:
                        continue
                    created = int(s.get("updatedAt") or s.get("createdAt") or 0)
                    if cutoff_ms and created > 1_000_000_000_000 and created < cutoff_ms:
                        continue
                    sid = str(s.get("sessionId") or s.get("id") or "")
                    title = str(s.get("title") or s.get("name") or "").replace("\n", " ").strip()
                    user_msgs: list[str] = []
                    for m in msgs:
                        if not isinstance(m, dict):
                            continue
                        if str(m.get("role", "")).lower() == "user":
                            raw = extract_text_from_content(m.get("content") or m.get("text"))
                            txt = clean_chat_text(raw, max_len=450)
                            if txt and len(txt) >= 2:
                                user_msgs.append(txt)
                    if user_msgs:
                        tag = title or (user_msgs[0][:40] if user_msgs else sid[:8])
                        full_label = f"{tag} ({ws_label})" if ws_label and ws_label not in tag else tag
                        sessions_found.append((created, sid, full_label, str(st), user_msgs))
        except Exception:
            pass

    sessions_found.sort(key=lambda x: x[0], reverse=True)
    total_chars = 0
    used = 0
    processed_ids: list[str] = []

    for created, sid, title, _st_path, user_msgs in sessions_found[:max_sessions]:
        date_str = (
            datetime.datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d")
            if created > 1_000_000_000_000
            else (datetime.datetime.fromtimestamp(created).strftime("%Y-%m-%d") if created > 1_000_000_000 else "recent")
        )
        ref = _format_chat_ref("trae", str(sid))
        block_lines = [f"## [{date_str}] {title} (`ref: {ref}` · {len(user_msgs)} 轮人类输入)"]
        for m in user_msgs[:15]:
            block_lines.append(f"- {m.replace(chr(10), ' / ')}")
        block_lines.append("")
        block = "\n".join(block_lines)
        if total_chars + len(block) > max_chars:
            break
        lines.append(block)
        total_chars += len(block)
        used += 1
        processed_ids.append(short_hash(f"trae:{sid}:{created}"))

    return "\n".join(lines), used, processed_ids


def extract_cursor_chats(
    db_path_str: str,
    days: int,
    max_sessions: int,
    max_chars: int = 35000,
) -> tuple[str, int, list[str]]:
    if not db_path_str or not Path(db_path_str).is_file():
        return "", 0, []
    lines: list[str] = [
        "# Cursor 本地历史对话精华（人类发言与意图流）",
        f"> 生成时间: {now_iso()} | 最多 {max_sessions} 场 Composer 对话 | 只读安全提取。",
        "> **按需下钻双边上下文**：若某场人类发言有价值但缺 AI 回复看不清来龙去脉，用标题中的 `ref` 执行 `tree chat read \"<ref>\" --turns 30` 调阅人+AI完整对话。",
        "",
    ]
    cutoff_ms = (datetime.datetime.now().timestamp() - days * 86400) * 1000 if days > 0 else 0
    sessions_found: list[tuple[int, str, str, list[str]]] = []

    try:
        conn = sqlite3.connect(f"file:{db_path_str}?mode=ro", uri=True)
        # Bound scan rows to protect against huge multi-GB state.vscdb files
        cur = conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%' LIMIT ?",
            (max(max_sessions * 6, 200),),
        )
        for key, val in cur:
            if not val:
                continue
            try:
                obj = json.loads(val)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            created = int(obj.get("createdAt") or obj.get("lastUpdatedAt") or 0)
            if cutoff_ms and created and created < cutoff_ms:
                continue
            title = str(obj.get("name") or obj.get("text") or obj.get("composerId") or key)[:60].replace("\n", " ").strip()
            user_msgs: list[str] = []

            # Old schema: inline conversation array
            conv = obj.get("conversation")
            if isinstance(conv, list):
                for item in conv:
                    if isinstance(item, dict) and item.get("type") == 1:
                        txt = clean_chat_text(str(item.get("text") or item.get("richText") or ""), max_len=450)
                        if txt and len(txt) >= 4:
                            user_msgs.append(txt)

            # New schema: fullConversationHeadersOnly -> lookup user bubbles (type == 1)
            if not user_msgs and isinstance(obj.get("fullConversationHeadersOnly"), list):
                cid = obj.get("composerId") or key.split(":", 1)[-1]
                user_bubble_ids = [
                    h.get("bubbleId")
                    for h in obj["fullConversationHeadersOnly"]
                    if isinstance(h, dict) and h.get("type") == 1 and h.get("bubbleId")
                ][:12]
                for bid in user_bubble_ids:
                    brow = conn.execute(
                        "SELECT value FROM cursorDiskKV WHERE key = ?",
                        (f"bubbleId:{cid}:{bid}",),
                    ).fetchone()
                    if brow and brow[0]:
                        try:
                            bobj = json.loads(brow[0])
                            txt = clean_chat_text(str(bobj.get("text") or bobj.get("rawText") or ""), max_len=450)
                            if txt and len(txt) >= 4:
                                user_msgs.append(txt)
                        except Exception:
                            pass

            if not user_msgs and isinstance(obj.get("text"), str):
                txt = clean_chat_text(obj["text"], max_len=450)
                if txt and len(txt) >= 4:
                    user_msgs.append(txt)

            if user_msgs:
                sessions_found.append((created, key, title, user_msgs))
        conn.close()
    except Exception as e:
        lines.append(f"> *(读取 Cursor 数据库时跳过: {e})*")
        return "\n".join(lines), 0, []

    sessions_found.sort(key=lambda x: x[0], reverse=True)
    total_chars = 0
    used = 0
    processed_ids: list[str] = []

    for created, key, title, user_msgs in sessions_found[:max_sessions]:
        date_str = (
            datetime.datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d")
            if created > 1_000_000_000_000
            else "unknown-date"
        )
        cid = key.split(":", 1)[-1]
        ref = _format_chat_ref("cursor", cid)
        block_lines = [f"## [{date_str}] {title or key} (`ref: {ref}` · {len(user_msgs)} 轮人类输入)"]
        for m in user_msgs[:12]:
            block_lines.append(f"- {m.replace(chr(10), ' / ')}")
        block_lines.append("")
        block = "\n".join(block_lines)
        if total_chars + len(block) > max_chars:
            break
        lines.append(block)
        total_chars += len(block)
        used += 1
        processed_ids.append(short_hash(f"cursor:{key}:{created}"))

    return "\n".join(lines), used, processed_ids


# ---------------------------------------------------------------------------
# Generic Official Export Importer (ChatGPT / Claude Web / Doubao / WorkBuddy / MD)
# ---------------------------------------------------------------------------

def parse_generic_conversations_json(data: Any, max_sessions: int = 60) -> list[tuple[str, str, list[str]]]:
    """Parse ChatGPT export, Claude Web export, or generic [{role, content}] exports."""
    results: list[tuple[str, str, list[str]]] = []

    if isinstance(data, dict):
        # Single conversation or wrapper dict
        for key in ("conversations", "chats", "sessions", "data", "messages"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]

    if not isinstance(data, list):
        return results

    # Case A: flat message list [{"role": "user", "content": "..."}]
    if data and isinstance(data[0], dict) and ("role" in data[0] or "sender" in data[0]):
        msgs: list[str] = []
        for m in data:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or m.get("sender") or "").lower()
            if role in ("user", "human"):
                txt = clean_chat_text(extract_text_from_content(m.get("content") or m.get("text")), max_len=450)
                if txt:
                    msgs.append(txt)
        if msgs:
            results.append(("", "导入对话记录", msgs))
        return results

    # Case B: list of conversation objects (ChatGPT mapping / Claude chat_messages / generic messages)
    for conv in data:
        if not isinstance(conv, dict):
            continue
        title = str(conv.get("title") or conv.get("name") or conv.get("uuid") or "未命名对话")[:80]
        ts = conv.get("update_time") or conv.get("create_time") or conv.get("updated_at") or conv.get("created_at")
        date_str = ""
        if isinstance(ts, (int, float)) and ts > 1_000_000_000:
            sec = ts / 1000 if ts > 1_000_000_000_000 else ts
            date_str = datetime.datetime.fromtimestamp(sec).strftime("%Y-%m-%d")
        elif isinstance(ts, str) and len(ts) >= 10:
            date_str = ts[:10]

        user_msgs: list[str] = []

        # ChatGPT export format: mapping dict
        if isinstance(conv.get("mapping"), dict):
            for node in conv["mapping"].values():
                if not isinstance(node, dict):
                    continue
                msg = node.get("message")
                if not isinstance(msg, dict):
                    continue
                author = msg.get("author") or {}
                if isinstance(author, dict) and author.get("role") == "user":
                    raw = extract_text_from_content(msg.get("content"))
                    txt = clean_chat_text(raw, max_len=450)
                    if txt and len(txt) >= 4:
                        user_msgs.append(txt)

        # Claude Web export format: chat_messages list
        elif isinstance(conv.get("chat_messages"), list):
            for m in conv["chat_messages"]:
                if isinstance(m, dict) and str(m.get("sender", "")).lower() in ("human", "user"):
                    raw = extract_text_from_content(m.get("text") or m.get("content"))
                    txt = clean_chat_text(raw, max_len=450)
                    if txt and len(txt) >= 4:
                        user_msgs.append(txt)

        # Generic messages list inside conversation object
        elif isinstance(conv.get("messages"), list):
            for m in conv["messages"]:
                if isinstance(m, dict) and str(m.get("role") or m.get("sender") or "").lower() in ("human", "user"):
                    raw = extract_text_from_content(m.get("content") or m.get("text"))
                    txt = clean_chat_text(raw, max_len=450)
                    if txt and len(txt) >= 4:
                        user_msgs.append(txt)

        if user_msgs:
            results.append((date_str, title, user_msgs))

    results.sort(key=lambda x: x[0], reverse=True)
    return results[:max_sessions]


def import_external_path(root: Path, src_path: Path, label: str = "", max_chars: int = 45000) -> tuple[Path, int]:
    """Import an official export file (ZIP/JSON/JSONL/MD/TXT) or directory into `seed/staging/`."""
    src_path = src_path.expanduser().resolve()
    if not src_path.exists():
        raise FileNotFoundError(f"导入路径不存在: {src_path}")

    tag = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_\-]", "_", (label or src_path.stem))[:32] or "imported"
    out_file = staging_dir(root) / f"import-{tag}.md"
    lines: list[str] = [
        f"# 外部导入素材：{label or src_path.name}",
        f"> 来源路径: `{src_path}` | 导入时间: {now_iso()} | 已完成清洗与密钥脱敏。",
        "",
    ]
    total_chars = 0
    item_count = 0

    def append_block(blk: str) -> bool:
        nonlocal total_chars, item_count
        if total_chars + len(blk) > max_chars:
            return False
        lines.append(blk)
        total_chars += len(blk)
        item_count += 1
        return True

    def handle_json_bytes(raw_bytes: bytes, source_hint: str) -> None:
        try:
            data = json.loads(raw_bytes.decode("utf-8", errors="ignore"))
        except Exception:
            return
        convs = parse_generic_conversations_json(data)
        for date_str, title, msgs in convs:
            blk = [f"## [{date_str or 'imported'}] {title} ({len(msgs)} 轮人类输入)"]
            for m in msgs[:12]:
                blk.append(f"- {m.replace(chr(10), ' / ')}")
            blk.append("")
            if not append_block("\n".join(blk)):
                break

    if src_path.is_file():
        suf = src_path.suffix.lower()
        if suf == ".zip":
            with zipfile.ZipFile(src_path, "r") as zf:
                for name in zf.namelist():
                    low = name.lower()
                    if low.endswith(".json") and ("conversation" in low or "chat" in low or "message" in low or len(zf.namelist()) <= 5):
                        handle_json_bytes(zf.read(name), name)
                    elif low.endswith((".md", ".txt")):
                        txt = redact_secrets(zf.read(name).decode("utf-8", errors="ignore").strip())
                        if txt:
                            if not append_block(f"## `{name}`\n{txt[:2500]}\n"):
                                break
        elif suf == ".json":
            handle_json_bytes(src_path.read_bytes(), src_path.name)
        elif suf == ".jsonl":
            msgs: list[str] = []
            for raw_line in src_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not raw_line.strip():
                    continue
                try:
                    obj = json.loads(raw_line)
                except Exception:
                    continue
                role = str(obj.get("role") or obj.get("type") or "").lower()
                if role in ("user", "human", "user_input"):
                    txt = clean_chat_text(extract_text_from_content(obj.get("content") or obj.get("message")), max_len=450)
                    if txt:
                        msgs.append(txt)
            if msgs:
                blk = [f"## `{src_path.name}` ({len(msgs)} 条人类输入)"] + [f"- {m.replace(chr(10), ' / ')}" for m in msgs[:80]]
                append_block("\n".join(blk) + "\n")
        else:
            txt = redact_secrets(src_path.read_text(encoding="utf-8", errors="ignore").strip())
            if txt:
                append_block(f"## `{src_path.name}`\n{txt[:max_chars]}\n")
    elif src_path.is_dir():
        files = sorted(
            [p for p in src_path.rglob("*") if p.is_file() and p.suffix.lower() in {".md", ".txt", ".json"}],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for f in files[:60]:
            if f.suffix.lower() == ".json":
                handle_json_bytes(f.read_bytes(), str(f.relative_to(src_path)))
            else:
                txt = redact_secrets(f.read_text(encoding="utf-8", errors="ignore").strip())
                if txt:
                    rel = f.relative_to(src_path)
                    if not append_block(f"## `{rel}` [{file_mtime_iso(f)}]\n{txt[:2000]}\n"):
                        break

    out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    state = load_sources_state(root)
    imports = state.get("imports")
    if not isinstance(imports, list):
        imports = []
    imports.append(
        {
            "timestamp": now_iso(),
            "source": str(src_path),
            "label": label or src_path.name,
            "staging_file": str(out_file.relative_to(root)),
            "items": item_count,
        }
    )
    state["imports"] = imports[-30:]
    save_sources_state(root, state)
    return out_file, item_count


def write_staging_manifest(root: Path, files_meta: list[dict[str, Any]]) -> Path:
    manifest = staging_dir(root) / "00-manifest.md"
    lines = [
        "# Tree Seed 破土素材总览（待宿主 Agent 蒸馏）",
        f"> 生成时间: {now_iso()} | 位置: `seed/staging/`（已加入 `.gitignore`，不污染 Git 树干）",
        "",
        "## 一、已生成的清洗素材包",
    ]
    for item in files_meta:
        lines.append(f"- `{item['rel_path']}` — {item['desc']}（{item['chars']} 字符）")

    lines.extend(
        [
            "",
            "## 二、给宿主 Agent 的破土蒸馏协议",
            "1. **先读根契约**：写入前必先执行 `tree read README.md`。",
            "2. **记忆文件与对话精华均走「轻扫描 → 按需精读/下钻」，不要盲目全吞**：",
            "   - **本地记忆**：已全量索引在 `memories-index.md`（零截断，含各客户端与项目分组）。请先通览索引，挑出全局画像/规则、仍在推进项目的 `MEMORY.md` 或近期高价值记录，直接读取原文件或用 `tree seed read \"<文件路径>\"` 只读读取（自动脱敏并剥离 Tree 自身同步块）。",
            "   - **历史对话（按需调阅人+AI 双边上下文）**：`chats-*.md` 默认只抽人类发言流用于低成本初筛，每场对话标题均带有可直达句柄 `` `ref: <source>:<id前缀>` ``。当你发现某场对话里人类发言很有价值，但离开 AI 回复看不清前因后果（如「就按第 2 个方案」「这个不行」「按刚才说的定」）时，**主动执行 `tree chat read \"<source>:<id前缀>\" --turns 30` 拉取该场对话的人+AI 双边上下文**，看清全貌后再提炼，严禁只凭半截人类发言瞎猜。",
            "   - **第三方外脑与记忆系统**：用户本机或云端可能还装有其他外部大脑或个人知识库（如得到大脑、Mem0、Obsidian、OpenMemory、MCP 记忆服务或自定义记忆目录）。请主动向用户确认，经授权后将已消化的长期核心判断梳理入 `01~04`，并将该外脑的范围、检索方式（MCP/Skill/CLI/本地路径）与核心目录索引登记进 `seed/外部上下文源.md`。",
            "3. **先求完整清晰，拒绝无头碎片（适用于 01~04 与 seed/ 所有页面）**：",
            "   - 聊天记录天然是碎片化的（临时排错、局部指令），**严禁把几段聊天碎片直接拼成一页**。无论写 `01` 个人情况、`02` 思考判断、`03` 项目、`04` 工作规范还是 `seed/` 外脑与精选收藏，都要保证**一个没看过历史聊天的人/AI 读完这页，能独立看懂完整上下文和来龙去脉**。",
            "   - **主动补全信息源**：写 `03` 项目或工具时，先去读 `--location` 目录下的现有文档/代码入口，结合聊天记录还原完整面貌；写 `02` 或 `04` 时，把结论背后的原因、适用场景和边界讲清楚，不写没头没尾的孤立条目。",
            "   - **参考落点（严格对齐 `README.md` 推荐子分类，宁缺毋滥）**：",
            "     - `01-关于我/`：客观事实放 `基本信息.md`、`习惯偏好.md`、`设备与资产.md`；**`价值观.md` 与 `长期目标.md` 只记录用户亲口表达过的人生信条、梦想与 3~5 年职业/人生大方向**——严禁把近期高频推进的具体项目反推拔高成“长期目标”，素材里没有就留空不建，不要强行填满常驻位。",
            "     - `03-我正在做的事/`：已动手推进的事项归入 `工作/`、`学习/`、`生活/`（长期项目建 `工作/<项目名>/项目总览.md` 并标 `--kind project --location \"<路径或链接>\"`）。**严守 Tree 个人上下文定位（只记 User Context，不记 Repo Context）**：站在项目主人的视角写清「① 项目背景与核心价值 → ② 已经做成了什么（产品成果与核心机制） → ③ 核心产品拍板与否决取舍 → ④ 接下来做什么与演进规划 → ⑤ 从项目中沉淀的核心教训 + 外部入口索引」；**严禁把代码仓里的函数名、类名、DOM 选择器、单测补丁等代码实现细节搬进 Tree，更严禁按技术模块拆出 `Agent接入.md`、`测试与发版.md` 这类代码说明书**（代码细节一律留给 `--location` 仓库本地的 `CLAUDE.md`/`docs/`）。",
            "     - `02-我的思考/` 与 `04-我如何工作/`：跨项目独立思考与随笔放 `02`；跨任务可复用的协作底线（`--always`）与工作规范放 `04`。",
            "     - `seed/`（外脑与精选收藏隔离区，标 `--no-always`，由 AI 替用户全权动态维护）：根据用户当前真实可用的外部数据源（`tree seed scan` 已就绪源、本机已挂载的外部数据类 MCP/Skill、以及用户接入的得到大脑/抖音/Obsidian 等外脑）主动创建或原地更新 `seed/外部上下文源.md`（千人千面、随增随减，只记真实存在的源范围、检索入口与目录索引，不搬原文）；尚未内化成个人观点但值得留给 AI 参考的好文章、好工具记入 `seed/文章与链接.md`、`seed/工具与资源.md`（只记标题 + 链接 + 一句话收藏理由）。",
            "4. **信息不足就先问人，严禁硬编或反推脑补**：只要素材拼不出完整清晰的页面（尤其是 `01/长期目标`、`01/价值观` 这类高维底色页），要么留空不建，要么先向用户提出具体问题补齐，再呈报「初始 Tree 破土草案」。",
            "5. **经人类确认后再落盘**：草案经用户删改拍板后，逐页调用 `tree write` 写入。",
            "6. **落盘后清理土壤**：写入完成后可执行 `tree seed clear` 清理临时暂存文件（保留游标索引）。",
        ]
    )
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def list_staging_files(root: Path) -> list[Path]:
    sd = seed_dir(root) / "staging"
    if not sd.is_dir():
        return []
    return sorted(p for p in sd.glob("*.md") if p.is_file())


# ---------------------------------------------------------------------------
# CLI Command Handlers
# ---------------------------------------------------------------------------

def cmd_seed_scan(root: Path, _args: Any, log_audit: Callable[..., None]) -> None:
    discovered, plugins = scan_all_local_sources(root)
    state = load_sources_state(root)
    state["detected"] = {
        k: {
            "name": v["name"],
            "available": v["available"],
            "memory_count": len(v.get("memory_files", [])) + int(v.get("memory_db_count", 0)),
            "chat_count": len(v.get("chat_files", [])) if "chat_files" in v else int(v.get("chat_count", 0)),
            "latest_date": v.get("latest_date", ""),
            "is_plugin": bool(v.get("is_plugin", False)),
        }
        for k, v in discovered.items()
    }
    save_sources_state(root, state)
    log_audit(root, "seed:scan", "seed/sources.json")

    print("🌱 Tree Seed 认知与上下文来源探测结果（只读扫描，未提取任何正文）：\n")
    available_ids: list[str] = []
    for sid, info in discovered.items():
        mem_n = len(info.get("memory_files", [])) + int(info.get("memory_db_count", 0))
        chat_n = len(info.get("chat_files", [])) if "chat_files" in info else int(info.get("chat_count", 0))
        if info["available"]:
            status = "✅ 已就绪"
            available_ids.append(sid)
        elif info.get("needs_login"):
            status = "🔑 需登录"
        else:
            status = "—  未发现"
        latest = f" | 最近活跃: {info['latest_date']}" if info.get("latest_date") else ""
        detail_hint = f" | {info['detail']}" if (info.get("is_plugin") and info.get("detail") and not info["available"]) else ""
        ptag = " [插件]" if info.get("is_plugin") else ""
        unit = info.get("unit_label")
        if unit in ("已登录", "可扫码"):
            count_col = f"状态: {unit:<10}"
        elif unit:
            count_col = f"记录: {chat_n:>4} {unit}"
        else:
            count_col = f"记忆/规则: {mem_n:>3} 份 | 历史对话: {chat_n:>4} 场"
        print(f"  [{sid:<11}] {status}  {info['name']:<24}{ptag} {count_col}{latest}{detail_hint}")

    print("\n📦 按需扩展外部连接器与导出包（托管于 GitHub `shangxiuyu/water`）：")
    missing_zero_dep = [cid for cid in ("getnote", "flomo", "weread", "jike") if cid not in discovered]
    if missing_zero_dep:
        print(f"  ① 零依赖外部外脑/社区（下载即用）：`tree seed add {','.join(missing_zero_dep)}`")
    print("  ② 查看/安装全部外部外脑与生活连接器（得到大脑/flomo/微信读书/即刻/美团/淘宝盒马/叮咚/京东/闲鱼/Keep/微信）：")
    print("     运行 `tree seed add --list` 查看清单，或运行 `tree seed add <平台ID>` 按需下载到 `seed/connectors/`。")
    print("  ③ 网页端官方导出包（ChatGPT / Claude Web / Obsidian 等）：`tree seed import <文件或目录路径> --label <来源名>`")
    print("  ④ 自定义本地连接器：在 `seed/connectors/<name>.py` 实现 `discover()` 与 `extract()` 即可自动挂载。")

    if available_ids:
        src_list = ",".join(available_ids)
        print("\n🚀 下一步（经你确认授权后执行清洗提取，并由 AI 自动为你创建/更新 `seed/外部上下文源.md`）：")
        print(f"  tree seed extract --sources {src_list} --days 60")


def cmd_seed_extract(root: Path, args: Any, log_audit: Callable[..., None]) -> None:
    discovered, plugins = scan_all_local_sources(root)
    if args.sources:
        selected = {SOURCE_ALIASES.get(s.strip().lower(), s.strip().lower()) for s in args.sources.split(",") if s.strip()}
    else:
        selected = {k for k, v in discovered.items() if v["available"]}

    valid_ids = set(discovered.keys())
    unknown = selected - valid_ids
    if unknown:
        water_hits = sorted(unknown & set(KNOWN_WATER_CONNECTORS))
        if water_hits:
            print(
                f"❌ 连接器 `{', '.join(water_hits)}` 尚未安装到本机。可先运行 `tree seed add {','.join(water_hits)}` 从 GitHub `water` 仓库按需拉取安装。",
                file=sys.stderr,
            )
        else:
            print(
                f"❌ 未知来源标识: {', '.join(sorted(unknown))}（当前可用: {', '.join(sorted(valid_ids))}）",
                file=sys.stderr,
            )
        sys.exit(1)

    sd = staging_dir(root)
    files_meta: list[dict[str, Any]] = []
    state = load_sources_state(root)
    proc_map = state.get("processed_sessions")
    if not isinstance(proc_map, dict):
        proc_map = {}

    # 1. Memories & rules: full zero-truncation index + optional SQLite unpack
    if not args.chats_only:
        idx_md, mem_count = build_memories_index(discovered, selected)
        if mem_count > 0:
            out_p = sd / "memories-index.md"
            out_p.write_text(idx_md, encoding="utf-8")
            files_meta.append(
                {
                    "rel_path": str(out_p.relative_to(root)),
                    "desc": f"本地记忆与规则全量索引（{mem_count} 份，零截断；按需直读或用 `tree seed read` 读取）",
                    "chars": len(idx_md),
                }
            )
        db_md, db_count = extract_sqlite_memories(discovered, selected)
        if db_count > 0:
            out_db = sd / "memories-db.md"
            out_db.write_text(db_md, encoding="utf-8")
            files_meta.append(
                {
                    "rel_path": str(out_db.relative_to(root)),
                    "desc": f"本地数据库记忆解包（{db_count} 条）",
                    "chars": len(db_md),
                }
            )

    # 2. Chat histories per selected source
    if not args.memories_only:
        if "claude" in selected and discovered["claude"]["chat_files"]:
            md, cnt, pids = extract_jsonl_source_chats(
                "claude",
                "Claude Code",
                discovered["claude"]["chat_files"],
                parse_claude_session,
                days=args.days,
                max_sessions=args.max_sessions,
                skip_ids=set(proc_map.get("claude") or []),
            )
            if cnt > 0:
                out_p = sd / "chats-claude.md"
                out_p.write_text(md, encoding="utf-8")
                files_meta.append({"rel_path": str(out_p.relative_to(root)), "desc": f"Claude Code 对话精华（{cnt} 场）", "chars": len(md)})
                proc_map["claude"] = list(dict.fromkeys((proc_map.get("claude") or []) + pids))[-500:]

        if "codex" in selected and discovered["codex"]["chat_files"]:
            md, cnt, pids = extract_jsonl_source_chats(
                "codex",
                "Codex",
                discovered["codex"]["chat_files"],
                parse_codex_session,
                days=args.days,
                max_sessions=args.max_sessions,
                skip_ids=set(proc_map.get("codex") or []),
            )
            if cnt > 0:
                out_p = sd / "chats-codex.md"
                out_p.write_text(md, encoding="utf-8")
                files_meta.append({"rel_path": str(out_p.relative_to(root)), "desc": f"Codex 对话精华（{cnt} 场）", "chars": len(md)})
                proc_map["codex"] = list(dict.fromkeys((proc_map.get("codex") or []) + pids))[-500:]

        if "antigravity" in selected and discovered["antigravity"]["chat_files"]:
            md, cnt, pids = extract_jsonl_source_chats(
                "antigravity",
                "Antigravity",
                discovered["antigravity"]["chat_files"],
                parse_antigravity_session,
                days=args.days,
                max_sessions=args.max_sessions,
                skip_ids=set(proc_map.get("antigravity") or []),
            )
            if cnt > 0:
                out_p = sd / "chats-antigravity.md"
                out_p.write_text(md, encoding="utf-8")
                files_meta.append({"rel_path": str(out_p.relative_to(root)), "desc": f"Antigravity 对话精华（{cnt} 场）", "chars": len(md)})
                proc_map["antigravity"] = list(dict.fromkeys((proc_map.get("antigravity") or []) + pids))[-500:]

        if "cursor" in selected and discovered["cursor"].get("db_path"):
            md, cnt, pids = extract_cursor_chats(
                discovered["cursor"]["db_path"],
                days=args.days,
                max_sessions=args.max_sessions,
            )
            if cnt > 0:
                out_p = sd / "chats-cursor.md"
                out_p.write_text(md, encoding="utf-8")
                files_meta.append({"rel_path": str(out_p.relative_to(root)), "desc": f"Cursor 对话精华（{cnt} 场）", "chars": len(md)})
                proc_map["cursor"] = list(dict.fromkeys((proc_map.get("cursor") or []) + pids))[-500:]

        if "trae" in selected and discovered["trae"].get("base_path"):
            md, cnt, pids = extract_trae_chats(
                discovered["trae"]["base_path"],
                days=args.days,
                max_sessions=args.max_sessions,
            )
            if cnt > 0:
                out_p = sd / "chats-trae.md"
                out_p.write_text(md, encoding="utf-8")
                files_meta.append({"rel_path": str(out_p.relative_to(root)), "desc": f"Trae IDE 对话精华（{cnt} 场）", "chars": len(md)})
                proc_map["trae"] = list(dict.fromkeys((proc_map.get("trae") or []) + pids))[-500:]

        if "workbuddy" in selected and discovered["workbuddy"]["chat_files"]:
            md, cnt, pids = extract_jsonl_source_chats(
                "workbuddy",
                "WorkBuddy",
                discovered["workbuddy"]["chat_files"],
                parse_workbuddy_session,
                days=args.days,
                max_sessions=args.max_sessions,
            )
            if cnt > 0:
                out_p = sd / "chats-workbuddy.md"
                out_p.write_text(md, encoding="utf-8")
                files_meta.append({"rel_path": str(out_p.relative_to(root)), "desc": f"WorkBuddy 对话精华（{cnt} 场）", "chars": len(md)})
                proc_map["workbuddy"] = list(dict.fromkeys((proc_map.get("workbuddy") or []) + pids))[-500:]

        if "douchat" in selected and discovered["douchat"].get("db_path"):
            md, cnt, pids = extract_douchat_chats(
                discovered["douchat"]["db_path"],
                days=args.days,
                max_sessions=args.max_sessions,
            )
            if cnt > 0:
                out_p = sd / "chats-douchat.md"
                out_p.write_text(md, encoding="utf-8")
                files_meta.append({"rel_path": str(out_p.relative_to(root)), "desc": f"豆包工作 (Douchat) 对话精华（{cnt} 场）", "chars": len(md)})
                proc_map["douchat"] = list(dict.fromkeys((proc_map.get("douchat") or []) + pids))[-500:]

        if "hermes" in selected and discovered["hermes"].get("db_path"):
            md, cnt, pids = extract_hermes_chats(
                discovered["hermes"]["db_path"],
                days=args.days,
                max_sessions=args.max_sessions,
            )
            if cnt > 0:
                out_p = sd / "chats-hermes.md"
                out_p.write_text(md, encoding="utf-8")
                files_meta.append({"rel_path": str(out_p.relative_to(root)), "desc": f"Hermes Agent 对话精华（{cnt} 场）", "chars": len(md)})
                proc_map["hermes"] = list(dict.fromkeys((proc_map.get("hermes") or []) + pids))[-500:]

        if "eden" in selected and discovered["eden"].get("db_path"):
            md, cnt, pids = extract_eden_chats(
                discovered["eden"]["db_path"],
                days=args.days,
                max_sessions=args.max_sessions,
            )
            if cnt > 0:
                out_p = sd / "chats-eden.md"
                out_p.write_text(md, encoding="utf-8")
                files_meta.append({"rel_path": str(out_p.relative_to(root)), "desc": f"Eden 工作台对话精华（{cnt} 个房间）", "chars": len(md)})
                proc_map["eden"] = list(dict.fromkeys((proc_map.get("eden") or []) + pids))[-500:]

        if "deepseek" in selected and discovered["deepseek"]["chat_files"]:
            md, cnt, pids = extract_jsonl_source_chats(
                "deepseek",
                "DeepSeek Harness",
                discovered["deepseek"]["chat_files"],
                parse_deepseek_session,
                days=args.days,
                max_sessions=args.max_sessions,
            )
            if cnt > 0:
                out_p = sd / "chats-deepseek.md"
                out_p.write_text(md, encoding="utf-8")
                files_meta.append({"rel_path": str(out_p.relative_to(root)), "desc": f"DeepSeek Harness 对话精华（{cnt} 场）", "chars": len(md)})
                proc_map["deepseek"] = list(dict.fromkeys((proc_map.get("deepseek") or []) + pids))[-500:]

        if "chrome" in selected and discovered["chrome"]["available"]:
            md, cnt, pids = extract_chrome_context(days=args.days, max_sessions=args.max_sessions)
            if cnt > 0 and md:
                out_p = sd / "context-chrome.md"
                out_p.write_text(md, encoding="utf-8")
                files_meta.append({"rel_path": str(out_p.relative_to(root)), "desc": f"Google Chrome 书签与高频站点（{cnt} 条）", "chars": len(md)})
                proc_map["chrome"] = list(dict.fromkeys((proc_map.get("chrome") or []) + pids))[-500:]

        # Execute any selected plugin connectors from `seed/connectors/`
        helpers = get_connector_helpers()
        for cid, mod in plugins.items():
            if cid not in selected or not callable(getattr(mod, "extract", None)):
                continue
            try:
                md, cnt, pids = mod.extract(args.days, args.max_sessions, helpers)
                if cnt > 0 and md:
                    out_p = sd / f"context-{cid}.md"
                    out_p.write_text(md, encoding="utf-8")
                    cname = discovered[cid].get("name", cid)
                    files_meta.append({"rel_path": str(out_p.relative_to(root)), "desc": f"{cname} 提炼线索（{cnt} 条）", "chars": len(md)})
                    proc_map[cid] = list(dict.fromkeys((proc_map.get(cid) or []) + (pids or [])))[-500:]
            except Exception as e:
                print(f"⚠️ 连接器 `{cid}` 提取失败（已跳过）: {e}", file=sys.stderr)

    # Include any previously imported files in manifest
    for existing in list_staging_files(root):
        if existing.name.startswith("import-"):
            rel = str(existing.relative_to(root))
            files_meta.append({"rel_path": rel, "desc": f"外部导入素材（{existing.name}）", "chars": existing.stat().st_size})

    state["processed_sessions"] = proc_map
    save_sources_state(root, state)

    if not files_meta:
        print("⚠️ 未在所选来源与时间窗口内提取到有效素材。可尝试扩大 --days 或使用 `tree seed import <路径>`。")
        return

    manifest = write_staging_manifest(root, files_meta)
    log_audit(root, "seed:extract", ",".join(sorted(selected)), detail=f"files={len(files_meta)}")

    print("✅ 已完成本地清洗与密钥脱敏，素材已暂存至 `seed/staging/`（Git 已忽略，绝不污染树干）：\n")
    for item in files_meta:
        print(f"  📄 {item['rel_path']} — {item['desc']} ({item['chars']:,} 字符)")
    print(f"\n🗺️ 总览与蒸馏协议: `{manifest.relative_to(root)}`")
    print("👉 下一步：在任意宿主 Agent 中说「读一下 seed/staging/，帮我起草 Tree 初始化破土方案」，确认后落盘。")


def cmd_seed_read(root: Path, args: Any, log_audit: Callable[..., None]) -> None:
    """Read a local memory/rule file read-only with secret redaction and Tree managed-block stripping."""
    raw_path = (getattr(args, "path", "") or "").strip()
    if not raw_path:
        print("❌ 请提供要只读读取的记忆文件路径。", file=sys.stderr)
        sys.exit(1)
    p = Path(raw_path).expanduser().resolve()
    if not p.is_file():
        print(f"❌ 文件不存在: {p}", file=sys.stderr)
        sys.exit(1)
    try:
        raw = p.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"❌ 读取失败: {e}", file=sys.stderr)
        sys.exit(1)

    had_tree_block = bool(TREE_CONTEXT_BLOCK_RE.search(raw))
    cleaned = redact_secrets(strip_tree_context_block(raw))
    max_chars = int(getattr(args, "max_chars", 12000) or 0)
    truncated = False
    if max_chars > 0 and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + f"\n\n…[已截断，总长 {len(cleaned)} 字符，可传 --max-chars 0 读取全文]"
        truncated = True

    log_audit(root, "seed:read", str(p))
    print(f"# [Tree Seed 只读记忆] `{p}`")
    meta_parts = [f"修改日期: {file_mtime_iso(p)}", f"有效字数: {len(cleaned)}"]
    if had_tree_block:
        meta_parts.append("已自动剥离 TREE-CONTEXT 同步块")
    if truncated:
        meta_parts.append("已截断")
    print(f"> {' | '.join(meta_parts)}\n")
    if not cleaned:
        print("*(该文件去除 Tree 自身同步块后无额外内容，可跳过)*")
    else:
        print(cleaned)


def cmd_seed_import(root: Path, args: Any, log_audit: Callable[..., None]) -> None:
    try:
        out_p, count = import_external_path(root, Path(args.path), label=args.label or "")
    except Exception as e:
        print(f"❌ 导入失败: {e}", file=sys.stderr)
        sys.exit(1)

    # Refresh manifest with all staging files
    files_meta = []
    for p in list_staging_files(root):
        if p.name == "00-manifest.md":
            continue
        files_meta.append({"rel_path": str(p.relative_to(root)), "desc": p.stem, "chars": p.stat().st_size})
    write_staging_manifest(root, files_meta)
    log_audit(root, "seed:import", str(args.path), detail=f"items={count}")
    print(f"✅ 已导入并清洗 {count} 条素材至 `{out_p.relative_to(root)}`（已自动脱敏，不进 Git 仓库）")
    print("👉 可在任意宿主 Agent 中读取 `seed/staging/00-manifest.md` 并生成破土提案。")


def cmd_seed_status(root: Path, _args: Any, _log_audit: Callable[..., None]) -> None:
    state = load_sources_state(root)
    staged = list_staging_files(root)
    v_seed_dir = root / "seed"
    persisted_seed_files = (
        sorted(
            p
            for p in v_seed_dir.rglob("*.md")
            if p.is_file()
            and p.name.lower() != "readme.md"
            and "staging" not in p.relative_to(v_seed_dir).parts
            and "connectors" not in p.relative_to(v_seed_dir).parts
        )
        if v_seed_dir.is_dir()
        else []
    )
    print("🌱 Tree Seed 统一种子区状态 (seed/)\n")
    print(f"一、外脑与精选收藏持久页（`seed/`，进 Git，共 {len(persisted_seed_files)} 篇）：")
    if persisted_seed_files:
        for pf in persisted_seed_files:
            print(f"  📄 {pf.relative_to(root)} ({pf.stat().st_size:,} bytes)")
    else:
        print("  (尚未建立外脑源或精选收藏页，可按需写入 `seed/外部上下文源.md`、`seed/文章与链接.md` 等)")

    print("\n二、本机采集源与临时抽取缓存（`seed/sources.json` & `seed/staging/`，Git 忽略）：")
    if state.get("updated_at"):
        print(f"最近扫描: {state['updated_at']}")
    detected = state.get("detected") or {}
    if detected:
        print("\n已探测本机来源:")
        for sid, info in detected.items():
            mark = "✅" if info.get("available") else "—"
            print(f"  {mark} {sid:<12} 记忆: {info.get('memory_count', 0):>3} | 对话: {info.get('chat_count', 0):>4} | 最近: {info.get('latest_date') or '-'}")
    else:
        print("尚未运行过扫描，可执行: tree seed scan")

    print(f"\n待蒸馏暂存素材 ({len(staged)} 个文件):")
    if staged:
        for p in staged:
            print(f"  📄 seed/staging/{p.name} ({p.stat().st_size:,} bytes)")
    else:
        print("  (空 — 已保持洁净)")


def cmd_seed_clear(root: Path, _args: Any, log_audit: Callable[..., None]) -> None:
    sd = seed_dir(root) / "staging"
    removed = 0
    if sd.is_dir():
        for p in list(sd.iterdir()):
            if p.is_file():
                p.unlink()
                removed += 1
            elif p.is_dir():
                shutil.rmtree(p)
                removed += 1
    log_audit(root, "seed:clear", "seed/staging", detail=f"removed={removed}")
    print(f"🧹 已清空 `seed/staging/`（移除 {removed} 个临时暂存文件，保留 seed/*.md 持久页与 sources.json 增量游标）。")


def _safe_rel_subpath(raw_rel: str, strip_prefix: str = "") -> Path:
    """Validate and normalize a relative path from an external manifest (forbids traversal and absolute paths)."""
    cleaned = str(raw_rel or "").replace("\\", "/").strip()
    if not cleaned or cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned) or "\x00" in cleaned:
        raise ValueError(f"非法相对路径: {raw_rel!r}")
    if strip_prefix:
        norm_pref = strip_prefix.strip("/") + "/"
        if cleaned.startswith(norm_pref):
            cleaned = cleaned[len(norm_pref):]
    cleaned = cleaned.strip("/")
    if not cleaned or cleaned == ".":
        raise ValueError(f"空相对路径或目录占位: {raw_rel!r}")
    raw_parts = cleaned.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise ValueError(f"路径禁止包含穿越或空段: {raw_rel!r}")
    return Path(*raw_parts)


def _read_water_resource(rel_path: str, source_override: str = "") -> bytes:
    """Fetch a file from local water directory (if specified) or GitHub `shangxiuyu/water`."""
    safe_rel = _safe_rel_subpath(rel_path)
    src = (source_override or os.environ.get("TREE_WATER_SOURCE") or "").strip()
    if src:
        local_dir = Path(src).expanduser().resolve()
        if local_dir.is_dir():
            target = (local_dir / safe_rel).resolve()
            if not target.is_relative_to(local_dir):
                raise ValueError(f"越界路径被拦截: {rel_path}")
            if not target.is_file():
                raise FileNotFoundError(f"本地源目录中不存在文件: {target}")
            return target.read_bytes()
        base_url = src.rstrip("/")
    else:
        base_url = DEFAULT_WATER_REPO.rstrip("/")

    url = f"{base_url}/{safe_rel.as_posix().lstrip('/')}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Tree-Seed-CLI/2.0 (https://github.com/shangxiuyu/Tree)"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def cmd_seed_login(root: Path, args: Any, log_audit: Callable[..., None]) -> None:
    raw_cid = str(args.source or "").strip().lower()
    cid = SOURCE_ALIASES.get(raw_cid, raw_cid)
    timeout = int(getattr(args, "timeout", 180) or 180)

    plugins = load_plugin_connectors(root)
    mod = plugins.get(cid)
    if not mod:
        if cid in KNOWN_WATER_CONNECTORS:
            print(
                f"❌ 连接器 `{cid}` 尚未安装。请先运行 `tree seed add {cid}` 从官方 `water` 仓库下载后再登录。",
                file=sys.stderr,
            )
        else:
            print(
                f"❌ 未找到连接器 `{cid}`。外部平台连接器请先运行 `tree seed add --list` 查看并安装。",
                file=sys.stderr,
            )
        sys.exit(1)

    login_fn = getattr(mod, "login_qr", None)
    if not callable(login_fn):
        print(f"ℹ️ 连接器 `{cid}` 无需交互登录，可直接运行 `tree seed extract --sources {cid}`。")
        return

    import inspect

    kwargs: dict[str, Any] = {"timeout": timeout}
    try:
        sig_params = inspect.signature(login_fn).parameters
        api_key = str(getattr(args, "api_key", "") or "")
        client_id = str(getattr(args, "client_id", "") or "")
        if "api_key" in sig_params:
            kwargs["api_key"] = api_key
        if "client_id" in sig_params:
            kwargs["client_id"] = client_id
    except Exception:
        pass

    ok = bool(login_fn(**kwargs))
    log_audit(root, "seed:login", cid, detail=f"ok={ok}")
    if not ok:
        sys.exit(1)
    print(f"👉 登录完成，可运行：`tree seed extract --sources {cid}` 提取脱敏线索。")


def cmd_seed_add(root: Path, args: Any, log_audit: Callable[..., None]) -> None:
    src_override = getattr(args, "from_source", "") or ""
    try:
        reg_raw = _read_water_resource("registry.json", source_override=src_override)
        registry = json.loads(reg_raw.decode("utf-8"))
    except Exception as e:
        print(f"❌ 读取官方 `water` 连接器清单失败: {e}", file=sys.stderr)
        print("   可检查网络后重试，或通过 `--from <本地water目录>` 指定本地仓库路径。", file=sys.stderr)
        sys.exit(1)

    conn_list = registry.get("connectors") or []
    conn_map = {str(c.get("id", "")).lower(): c for c in conn_list if isinstance(c, dict) and c.get("id")}

    if getattr(args, "list_only", False) or not getattr(args, "sources", ""):
        installed = set(load_plugin_connectors(root).keys())
        print("📦 Tree Seed 官方按需连接器库（GitHub: `shangxiuyu/water`）\n")
        print("   说明：Tree 核心仅内置本机 AI 客户端与 Chrome；外部外脑与生活社交连接器托管在 `water`，按需下载即可：\n")
        for cid, item in conn_map.items():
            mark = "✅ 已安装" if cid in installed else "⬇ 可安装"
            reqs = ", ".join(item.get("requires") or []) or "无（Python 标准库零依赖，下载即用）"
            print(f"  [{cid:<10}] {mark}  {item.get('name', cid)}")
            print(f"               提取范围: {', '.join(item.get('reads') or [])}")
            print(f"               前置依赖: {reqs}\n")
        zero_dep_avail = [k for k in ("getnote", "flomo", "weread", "jike") if k in conn_map]
        sample_arg = ",".join(zero_dep_avail) if zero_dep_avail else ",".join(list(conn_map.keys())[:3])
        if sample_arg:
            print(f"👉 安装示例：`tree seed add {sample_arg}`")
        return

    requested = [SOURCE_ALIASES.get(s.strip().lower(), s.strip().lower()) for s in str(args.sources).split(",") if s.strip()]
    builtin_hits = [s for s in requested if s in {"chrome", "claude", "codex", "antigravity", "cursor", "trae", "workbuddy", "douchat", "hermes", "openclaw", "eden", "deepseek"}]
    if builtin_hits:
        print(f"ℹ️ `{', '.join(builtin_hits)}` 已内置在 Tree 核心中，无需额外下载（直接用 `tree seed scan` 即可）。")
        requested = [s for s in requested if s not in builtin_hits]
        if not requested:
            return

    unknown = [s for s in requested if s not in conn_map]
    if unknown:
        print(f"❌ 官方 `water` 库中未找到连接器: {', '.join(unknown)}（可选: {', '.join(sorted(conn_map.keys()))}）", file=sys.stderr)
        sys.exit(1)

    dest_base = user_connectors_dir(root).resolve()
    dest_base.mkdir(parents=True, exist_ok=True)

    for cid in requested:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", cid):
            print(f"❌ 非法连接器标识: `{cid}`", file=sys.stderr)
            sys.exit(1)
        meta = conn_map[cid]
        files = meta.get("files") or ([meta["file"]] if meta.get("file") else [])
        if not files:
            print(f"⚠️ 连接器 `{cid}` 未声明可下载文件，已跳过。", file=sys.stderr)
            continue

        pkg_dir = (dest_base / cid).resolve()
        if not pkg_dir.is_relative_to(dest_base):
            print(f"❌ 非法安装路径: `{cid}`", file=sys.stderr)
            sys.exit(1)

        if pkg_dir.exists() and not getattr(args, "force", False):
            print(f"ℹ️ 连接器 `{cid}` 已存在于 `{pkg_dir.relative_to(root.resolve())}`（传 `--force` 可覆盖更新）。")
        else:
            expected_hashes = meta.get("sha256") if isinstance(meta.get("sha256"), dict) else {}
            stage_dir = (dest_base / f".{cid}.{os.getpid()}.tmp").resolve()
            backup_dir = (dest_base / f".{cid}.{os.getpid()}.bak").resolve()
            for tmp_p in (stage_dir, backup_dir):
                if tmp_p.is_symlink() or tmp_p.is_file():
                    tmp_p.unlink()
                elif tmp_p.exists():
                    shutil.rmtree(tmp_p, ignore_errors=True)
            stage_dir.mkdir(parents=True, exist_ok=True)
            moved_backup = False
            try:
                for rel_f in files:
                    raw_bytes = _read_water_resource(str(rel_f), source_override=src_override)
                    exp_sha = str(expected_hashes.get(str(rel_f)) or "").strip().lower()
                    if exp_sha:
                        actual_sha = hashlib.sha256(raw_bytes).hexdigest().lower()
                        if actual_sha != exp_sha:
                            raise ValueError(
                                f"校验失败：`{rel_f}` SHA256 不匹配 (期望 {exp_sha[:12]}，实际 {actual_sha[:12]})"
                            )
                    rel_sub = _safe_rel_subpath(str(rel_f), strip_prefix=f"connectors/{cid}")
                    out_file = (stage_dir / rel_sub).resolve()
                    if not out_file.is_relative_to(stage_dir):
                        raise ValueError(f"越界文件写入被拦截: {rel_f}")
                    out_file.parent.mkdir(parents=True, exist_ok=True)
                    out_file.write_bytes(raw_bytes)
                if pkg_dir.exists() or pkg_dir.is_symlink():
                    pkg_dir.replace(backup_dir)
                    moved_backup = True
                stage_dir.replace(pkg_dir)
                if moved_backup:
                    if backup_dir.is_symlink() or backup_dir.is_file():
                        backup_dir.unlink()
                    elif backup_dir.exists():
                        shutil.rmtree(backup_dir, ignore_errors=True)
            except Exception as e:
                if moved_backup and not pkg_dir.exists() and not pkg_dir.is_symlink() and (backup_dir.exists() or backup_dir.is_symlink()):
                    try:
                        backup_dir.replace(pkg_dir)
                    except Exception:
                        pass
                shutil.rmtree(stage_dir, ignore_errors=True)
                print(f"❌ 安装连接器 `{cid}` 失败（已自动回滚清理）: {e}", file=sys.stderr)
                sys.exit(1)
            log_audit(root, "seed:add", cid, detail=f"files={len(files)},src={src_override or 'official'}")
            print(f"✅ 已安装连接器 `{cid}` ({meta.get('name', cid)}) -> `{pkg_dir.relative_to(root.resolve())}/`")

        install_cmd = str(meta.get("install_cmd") or "").strip()
        reqs = meta.get("requires") or []
        if install_cmd:
            print(f"   📦 请确认已安装依赖：`{install_cmd}`")
        elif reqs:
            print(f"   📦 前置依赖：{'; '.join(reqs)}")
        print(f"   🚀 下一步：`tree seed login {cid}` -> `tree seed extract --sources {cid}`\n")


def cmd_seed_remove(root: Path, args: Any, log_audit: Callable[..., None]) -> None:
    raw = str(getattr(args, "sources", "") or "").strip()
    if not raw:
        print("❌ 请指定要卸载的连接器 ID，例如 `tree seed remove meituan`", file=sys.stderr)
        sys.exit(1)
    dest_base = user_connectors_dir(root).resolve()
    targets = [SOURCE_ALIASES.get(s.strip().lower(), s.strip().lower()) for s in raw.split(",") if s.strip()]
    for cid in targets:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", cid):
            print(f"❌ 非法连接器标识: `{cid}`", file=sys.stderr)
            sys.exit(1)
        pkg_dir = (dest_base / cid).resolve()
        pkg_file = (dest_base / f"{cid}.py").resolve()
        removed_any = False
        if pkg_dir.is_dir() and pkg_dir.is_relative_to(dest_base):
            shutil.rmtree(pkg_dir)
            removed_any = True
        if pkg_file.is_file() and pkg_file.is_relative_to(dest_base):
            pkg_file.unlink()
            removed_any = True
        if removed_any:
            log_audit(root, "seed:remove", cid)
            print(f"🗑️ 已卸载连接器 `{cid}`")
        else:
            print(f"ℹ️ 连接器 `{cid}` 未安装（跳过）")


# ---------------------------------------------------------------------------
# Cross-Agent Live Chat Handoff (`tree chat list` / `tree chat read`)
# ---------------------------------------------------------------------------

def _clean_handoff_text(text: str, keep_code: bool = False, max_len: int = 2400) -> str:
    if not text:
        return ""
    s = text.replace("\r\n", "\n")
    m = re.search(r"<USER_REQUEST>([\s\S]*?)</USER_REQUEST>", s, re.IGNORECASE)
    if m:
        s = m.group(1)
    for pat in TAG_BLOCK_PATTERNS:
        s = pat.sub("", s)
    if not keep_code:
        def _trim_fence(match: re.Match[str]) -> str:
            blk = match.group(0)
            if len(blk) <= 600:
                return blk
            return blk[:400].rstrip() + "\n... [长代码块已折叠，传 --raw-code 可查看全文]\n```"
        s = CODE_FENCE_RE.sub(_trim_fence, s)
    s = redact_secrets(s).strip()
    if not s:
        return ""
    low = s[:120].lower()
    skip_heads = (
        "# agents.md instructions",
        "<instructions>",
        "this session is being continued from a previous conversation",
        "这是 ahub 的私有 ai 主动检查",
        "[eden runtime context]",
        "你正在浏览用户的 eden 朋友圈",
        "--- [群聊协同协议",
        "your response above was stopped by a safety classifier",
    )
    if any(low.startswith(p) for p in skip_heads):
        return ""
    s = re.sub(r"\n{3,}", "\n\n", s)
    if len(s) > max_len:
        s = s[:max_len].rstrip() + "…[已截断]"
    return s


def _list_recent_chats(source_filter: str = "", query: str = "", limit: int = 20) -> list[dict[str, Any]]:
    discovered, _ = scan_all_local_sources(None)
    wanted = {s.strip().lower() for s in source_filter.split(",") if s.strip()} if source_filter else set(discovered.keys())
    q = query.strip().lower()
    items: list[dict[str, Any]] = []

    # 1. File-based JSONL sources: claude, codex, antigravity, workbuddy, deepseek
    for sid in ("claude", "codex", "antigravity", "workbuddy", "deepseek"):
        if sid not in wanted or sid not in discovered:
            continue
        for fp_str in discovered[sid].get("chat_files", [])[:40]:
            p = Path(fp_str)
            if not p.is_file():
                continue
            mtime = p.stat().st_mtime
            date_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            if sid == "antigravity":
                raw_id = p.parent.parent.parent.name if len(p.parents) >= 3 else p.stem
            elif sid == "deepseek":
                raw_id = p.parent.name
            else:
                raw_id = p.stem
            short_id = raw_id[:12]
            # Fast title/preview extraction from first user line
            title = ""
            preview = ""
            if sid == "claude":
                _, title, umsgs = parse_claude_session(p)
                preview = umsgs[0][:60] if umsgs else ""
            elif sid == "codex":
                _, title, umsgs = parse_codex_session(p)
                preview = umsgs[0][:60] if umsgs else ""
            elif sid == "antigravity":
                _, title, umsgs = parse_antigravity_session(p)
                preview = umsgs[0][:60] if umsgs else ""
            elif sid == "workbuddy":
                _, title, umsgs = parse_workbuddy_session(p)
                preview = umsgs[0][:60] if umsgs else ""
            elif sid == "deepseek":
                _, title, umsgs = parse_deepseek_session(p)
                preview = umsgs[0][:60] if umsgs else ""
            if not preview:
                continue
            display = f"{title} | {preview}" if title and title != preview else (preview or title)
            if q and (q not in display.lower() and q not in raw_id.lower()):
                continue
            items.append(
                {
                    "ref": _format_chat_ref(sid, raw_id),
                    "source": sid,
                    "raw_id": raw_id,
                    "path": str(p),
                    "mtime": mtime,
                    "date": date_str,
                    "title": display[:95],
                }
            )

    # 2. Trae workspaceStorage
    if "trae" in wanted and discovered.get("trae", {}).get("base_path"):
        ws_dir = Path(discovered["trae"]["base_path"]) / "User/workspaceStorage"
        if ws_dir.is_dir():
            for ws in ws_dir.iterdir():
                st = ws / "state.vscdb"
                if not st.is_file():
                    continue
                try:
                    ws_json = ws / "workspace.json"
                    ws_label = ""
                    if ws_json.is_file():
                        try:
                            fld = json.loads(ws_json.read_text(encoding="utf-8")).get("folder", "")
                            if fld:
                                ws_label = urllib.parse.unquote(fld.split("/")[-1])
                        except Exception:
                            pass
                    conn = sqlite3.connect(f"file:{st}?mode=ro", uri=True)
                    rows = conn.execute(
                        "SELECT value FROM ItemTable WHERE key LIKE 'memento/icube-ai-%chat-storage-%'"
                    ).fetchall()
                    conn.close()
                    for (v,) in rows:
                        try:
                            obj = json.loads(v)
                        except Exception:
                            continue
                        for s in obj.get("list", []):
                            if not isinstance(s, dict):
                                continue
                            msgs = s.get("messages")
                            if not isinstance(msgs, list) or not msgs:
                                continue
                            sid = str(s.get("sessionId") or s.get("id") or "")
                            title = str(s.get("title") or s.get("name") or "").replace("\n", " ").strip()
                            created = int(s.get("updatedAt") or s.get("createdAt") or 0)
                            mtime = created / 1000.0 if created > 1_000_000_000_000 else float(created)
                            date_str = (
                                datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                                if mtime > 1_000_000_000
                                else "unknown"
                            )
                            first_user_msg = ""
                            for m in msgs:
                                if isinstance(m, dict) and str(m.get("role", "")).lower() == "user":
                                    first_user_msg = str(m.get("content") or m.get("text") or "").strip()
                                    if first_user_msg:
                                        break
                            display_title = title or (first_user_msg[:50] if first_user_msg else f"Session {sid[:8]}")
                            if ws_label and ws_label not in display_title:
                                display_title = f"{display_title} ({ws_label})"
                            if q and (q not in display_title.lower() and q not in sid.lower()):
                                continue
                            items.append(
                                {
                                    "ref": f"trae:{sid[:12]}",
                                    "source": "trae",
                                    "raw_id": sid,
                                    "path": str(st),
                                    "mtime": mtime,
                                    "date": date_str,
                                    "title": display_title[:95],
                                }
                            )
                except Exception:
                    pass

    # 3. Cursor SQLite (scan recent rows by rowid DESC so newest composers surface first)
    if "cursor" in wanted and discovered.get("cursor", {}).get("db_path"):
        db_p = discovered["cursor"]["db_path"]
        try:
            conn = sqlite3.connect(f"file:{db_p}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%' ORDER BY rowid DESC LIMIT 400"
            ).fetchall()
            conn.close()
            for key, val in rows:
                try:
                    obj = json.loads(val or "{}")
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                # Skip empty composer shells with no messages
                has_conv = isinstance(obj.get("conversation"), list) and len(obj["conversation"]) > 0
                has_headers = isinstance(obj.get("fullConversationHeadersOnly"), list) and len(obj["fullConversationHeadersOnly"]) > 0
                if not (has_conv or has_headers):
                    continue
                cid = str(obj.get("composerId") or key.split(":", 1)[-1])
                created = int(obj.get("lastUpdatedAt") or obj.get("createdAt") or 0)
                mtime = created / 1000.0 if created > 1_000_000_000_000 else float(created)
                date_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime > 1_000_000_000 else "unknown"
                title = str(obj.get("name") or obj.get("text") or "").replace("\n", " ").strip()[:90]
                if not title:
                    title = f"Composer {cid[:8]}"
                if q and (q not in title.lower() and q not in cid.lower()):
                    continue
                items.append(
                    {
                        "ref": f"cursor:{cid[:12]}",
                        "source": "cursor",
                        "raw_id": cid,
                        "path": db_p,
                        "mtime": mtime,
                        "date": date_str,
                        "title": title,
                    }
                )
        except Exception:
            pass

    # 3. Douchat SQLite
    if "douchat" in wanted and discovered.get("douchat", {}).get("db_path"):
        db_p = discovered["douchat"]["db_path"]
        try:
            conn = sqlite3.connect(f"file:{db_p}?mode=ro", uri=True)
            rows = conn.execute("SELECT id, updatedAt, data FROM conversations ORDER BY updatedAt DESC LIMIT 30").fetchall()
            conn.close()
            for cid, updated_at, cdata in rows:
                cobj = json.loads(cdata or "{}")
                cname = str(cobj.get("name") or cid)[:80]
                ts = int(updated_at or 0)
                mtime = ts / 1000.0 if ts > 1_000_000_000_000 else float(ts)
                date_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime > 1_000_000_000 else "unknown"
                if q and (q not in cname.lower() and q not in cid.lower()):
                    continue
                items.append(
                    {
                        "ref": f"douchat:{cid[:16]}",
                        "source": "douchat",
                        "raw_id": cid,
                        "path": db_p,
                        "mtime": mtime,
                        "date": date_str,
                        "title": cname,
                    }
                )
        except Exception:
            pass

    # 4. Eden SQLite
    if "eden" in wanted and discovered.get("eden", {}).get("db_path"):
        db_p = discovered["eden"]["db_path"]
        try:
            conn = sqlite3.connect(f"file:{db_p}?mode=ro", uri=True)
            room_names = {r[0]: str(r[1] or r[0]) for r in conn.execute("SELECT id, name FROM rooms").fetchall()}
            rows = conn.execute(
                "SELECT room_id, MAX(timestamp) AS last_ts, COUNT(*) FROM messages WHERE sender_id = 'WebUser' AND type = 'chat' GROUP BY room_id ORDER BY last_ts DESC LIMIT 30"
            ).fetchall()
            conn.close()
            for rid, last_ts, cnt in rows:
                rname = room_names.get(rid, rid)
                ts_str = str(last_ts or "")[:16].replace("T", " ")
                try:
                    mtime = datetime.datetime.fromisoformat(str(last_ts).replace("Z", "+00:00")).timestamp()
                except Exception:
                    mtime = 0.0
                title = f"{rname} ({cnt} 条人类消息)"
                if q and (q not in title.lower() and q not in rid.lower()):
                    continue
                items.append(
                    {
                        "ref": f"eden:{rid[:16]}",
                        "source": "eden",
                        "raw_id": rid,
                        "path": db_p,
                        "mtime": mtime,
                        "date": ts_str,
                        "title": title,
                    }
                )
        except Exception:
            pass

    # 5. Hermes SQLite
    if "hermes" in wanted and discovered.get("hermes", {}).get("db_path"):
        db_p = discovered["hermes"]["db_path"]
        try:
            conn = sqlite3.connect(f"file:{db_p}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT id, COALESCE(title, id), COALESCE(last_activity_at, started_at, 0) AS ts, COALESCE(message_count, 0) "
                "FROM sessions WHERE COALESCE(message_count, 0) > 0 ORDER BY ts DESC LIMIT 80"
            ).fetchall()
            conn.close()
            for sid, stitle, ts, mcnt in rows:
                stitle_str = str(stitle or "")
                low_title = stitle_str.strip().lower()
                if low_title.startswith(("[eden runtime context]", "这是 ahub 的私有 ai 主动检查")):
                    continue
                sec = float(ts or 0)
                if sec > 1_000_000_000_000:
                    sec /= 1000.0
                date_str = datetime.datetime.fromtimestamp(sec).strftime("%Y-%m-%d %H:%M") if sec > 1_000_000_000 else "unknown"
                title = f"{stitle_str[:70]} ({mcnt} 条消息)"
                if q and (q not in title.lower() and q not in str(sid).lower()):
                    continue
                items.append(
                    {
                        "ref": f"hermes:{str(sid)[:16]}",
                        "source": "hermes",
                        "raw_id": str(sid),
                        "path": db_p,
                        "mtime": sec,
                        "date": date_str,
                        "title": title,
                    }
                )
        except Exception:
            pass

    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:limit]


def _read_session_turns(source: str, id_prefix: str, keep_code: bool = False) -> tuple[str, list[tuple[str, str]]]:
    """Return (session_header, [(role_label, cleaned_text), ...]) for a specific cross-agent session."""
    discovered, _ = scan_all_local_sources(None)
    src = source.lower().strip()
    turns: list[tuple[str, str]] = []

    if src == "claude":
        target = next((Path(p) for p in discovered["claude"]["chat_files"] if Path(p).stem.startswith(id_prefix)), None)
        if not target:
            raise FileNotFoundError(f"未找到 Claude Code 会话: {id_prefix}")
        with open(target, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type")
                if t in ("user", "assistant") and not obj.get("isSidechain"):
                    msg = obj.get("message", {})
                    raw = extract_text_from_content(msg.get("content") if isinstance(msg, dict) else msg)
                    cleaned = _clean_handoff_text(raw, keep_code=keep_code)
                    if cleaned:
                        turns.append(("User" if t == "user" else "Claude", cleaned))
        return f"Claude Code `{target.stem}` ({file_mtime_iso(target)})", turns

    if src == "codex":
        target = next(
            (Path(p) for p in discovered["codex"]["chat_files"] if Path(p).stem.startswith(id_prefix) or id_prefix in Path(p).stem),
            None,
        )
        if not target:
            raise FileNotFoundError(f"未找到 Codex 会话: {id_prefix}")
        with open(target, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                payload = obj.get("payload")
                if obj.get("type") == "response_item" and isinstance(payload, dict) and payload.get("type") == "message":
                    role = str(payload.get("role", "")).lower()
                    if role in ("user", "assistant"):
                        raw = extract_text_from_content(payload.get("content"))
                        cleaned = _clean_handoff_text(raw, keep_code=keep_code)
                        if cleaned:
                            turns.append(("User" if role == "user" else "Codex", cleaned))
        return f"Codex `{target.stem}` ({file_mtime_iso(target)})", turns

    if src == "antigravity":
        target = next(
            (p for p in (Path(x) for x in discovered["antigravity"]["chat_files"]) if (p.parent.parent.parent.name if len(p.parents) >= 3 else p.stem).startswith(id_prefix)),
            None,
        )
        if not target:
            raise FileNotFoundError(f"未找到 Antigravity 会话: {id_prefix}")
        conv_id = target.parent.parent.parent.name if len(target.parents) >= 3 else target.stem
        with open(target, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type")
                if t in ("USER_INPUT", "PLANNER_RESPONSE"):
                    raw = extract_text_from_content(obj.get("content"))
                    cleaned = _clean_handoff_text(raw, keep_code=keep_code)
                    if cleaned:
                        turns.append(("User" if t == "USER_INPUT" else "Antigravity", cleaned))
        return f"Antigravity `{conv_id}` ({file_mtime_iso(target)})", turns

    if src == "trae":
        base_dir = discovered.get("trae", {}).get("base_path")
        if not base_dir:
            raise FileNotFoundError("本机未找到 Trae 安装目录")
        ws_dir = Path(base_dir) / "User/workspaceStorage"
        target_session = None
        target_ws_label = ""
        for ws in ws_dir.iterdir():
            st = ws / "state.vscdb"
            if not st.is_file():
                continue
            try:
                conn = sqlite3.connect(f"file:{st}?mode=ro", uri=True)
                rows = conn.execute(
                    "SELECT value FROM ItemTable WHERE key LIKE 'memento/icube-ai-%chat-storage-%'"
                ).fetchall()
                conn.close()
                for (v,) in rows:
                    obj = json.loads(v)
                    for s in obj.get("list", []):
                        sid = str(s.get("sessionId") or s.get("id") or "")
                        if sid.startswith(id_prefix):
                            target_session = s
                            ws_json = ws / "workspace.json"
                            if ws_json.is_file():
                                try:
                                    fld = json.loads(ws_json.read_text(encoding="utf-8")).get("folder", "")
                                    if fld:
                                        target_ws_label = urllib.parse.unquote(fld.split("/")[-1])
                                except Exception:
                                    pass
                            break
                    if target_session:
                        break
            except Exception:
                pass
            if target_session:
                break

        if not target_session:
            raise FileNotFoundError(f"未找到 Trae 会话: {id_prefix}")

        title = str(target_session.get("title") or target_session.get("name") or id_prefix)
        created = int(target_session.get("updatedAt") or target_session.get("createdAt") or 0)
        date_str = (
            datetime.datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d")
            if created > 1_000_000_000_000
            else (datetime.datetime.fromtimestamp(created).strftime("%Y-%m-%d") if created > 1_000_000_000 else "recent")
        )

        for m in target_session.get("messages", []):
            if not isinstance(m, dict):
                continue
            role = str(m.get("role", "")).lower()
            author_label = "User" if role == "user" else "Trae AI"
            content = m.get("content") or m.get("text")
            # If assistant content is empty in builder mode, fallback to proposal
            if not content and role == "assistant":
                task_content = m.get("agentTaskContent")
                if isinstance(task_content, dict):
                    content = task_content.get("proposal") or task_content.get("proposalReasoningContent")
            cleaned = _clean_handoff_text(str(content or ""), keep_code=keep_code)
            if cleaned:
                turns.append((author_label, cleaned))

        ws_suffix = f" ({target_ws_label})" if target_ws_label else ""
        return f"Trae `{title}`{ws_suffix} ({date_str})", turns

    if src == "workbuddy":
        target = next((Path(p) for p in discovered["workbuddy"]["chat_files"] if Path(p).stem.startswith(id_prefix)), None)
        if not target:
            raise FileNotFoundError(f"未找到 WorkBuddy 会话: {id_prefix}")
        with open(target, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") == "message":
                    role = str(obj.get("role", "")).lower()
                    if role in ("user", "assistant"):
                        raw = extract_text_from_content(obj.get("content"))
                        cleaned = _clean_handoff_text(raw, keep_code=keep_code)
                        if cleaned:
                            turns.append(("User" if role == "user" else "WorkBuddy", cleaned))
        return f"WorkBuddy `{target.stem}` ({file_mtime_iso(target)})", turns

    if src == "deepseek":
        target = next((Path(p) for p in discovered["deepseek"]["chat_files"] if Path(p).parent.name.startswith(id_prefix)), None)
        if not target:
            raise FileNotFoundError(f"未找到 DeepSeek Harness 会话: {id_prefix}")
        for line in iter_deepseek_session_lines(target):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            t = obj.get("type")
            if t == "user/message":
                data = obj.get("data") or {}
                src_info = data.get("source")
                if isinstance(src_info, dict) and str(src_info.get("kind") or "") in ("agent-instructions", "runtime-context"):
                    continue
                raw = extract_text_from_content(data.get("content"))
                cleaned = _clean_handoff_text(raw, keep_code=keep_code)
                if cleaned:
                    turns.append(("User", cleaned))
            elif t == "assistant/message":
                msg = (obj.get("data") or {}).get("message")
                if isinstance(msg, dict):
                    raw = extract_text_from_content(msg.get("content"))
                    cleaned = _clean_handoff_text(raw, keep_code=keep_code)
                    if cleaned:
                        turns.append(("DeepSeek", cleaned))
        return f"DeepSeek Harness `{target.parent.name}` ({file_mtime_iso(target)})", turns

    if src == "cursor":
        db_p = discovered.get("cursor", {}).get("db_path")
        if not db_p:
            raise FileNotFoundError("本机未找到 Cursor state.vscdb")
        escaped_prefix = id_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conn = sqlite3.connect(f"file:{db_p}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE ? ESCAPE '\\' LIMIT 1",
                (f"composerData:{escaped_prefix}%",),
            ).fetchone()
            if not row:
                raise FileNotFoundError(f"未找到 Cursor Composer 会话: {id_prefix}")
            key, val = row
            obj = json.loads(val or "{}")
            cid = str(obj.get("composerId") or key.split(":", 1)[-1])
            title = str(obj.get("name") or cid[:12])
            conv = obj.get("conversation")
            if isinstance(conv, list) and conv:
                for item in conv:
                    if not isinstance(item, dict):
                        continue
                    itype = item.get("type")
                    if itype in (1, 2):
                        cleaned = _clean_handoff_text(str(item.get("text") or item.get("richText") or ""), keep_code=keep_code)
                        if cleaned:
                            turns.append(("User" if itype == 1 else "Cursor AI", cleaned))
            elif isinstance(obj.get("fullConversationHeadersOnly"), list):
                for h in obj["fullConversationHeadersOnly"][:80]:
                    if not isinstance(h, dict):
                        continue
                    itype = h.get("type")
                    bid = h.get("bubbleId")
                    if itype in (1, 2) and bid:
                        brow = conn.execute(
                            "SELECT value FROM cursorDiskKV WHERE key = ?",
                            (f"bubbleId:{cid}:{bid}",),
                        ).fetchone()
                        if brow and brow[0]:
                            try:
                                bobj = json.loads(brow[0])
                                cleaned = _clean_handoff_text(str(bobj.get("text") or bobj.get("rawText") or ""), keep_code=keep_code)
                                if cleaned:
                                    turns.append(("User" if itype == 1 else "Cursor AI", cleaned))
                            except Exception:
                                pass
        finally:
            conn.close()
        return f"Cursor `{title}` ({cid[:12]})", turns

    if src == "douchat":
        db_p = discovered.get("douchat", {}).get("db_path")
        if not db_p:
            raise FileNotFoundError("本机未找到豆包工作 douchat.db")
        escaped_prefix = id_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conn = sqlite3.connect(f"file:{db_p}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT id, data FROM conversations WHERE id LIKE ? ESCAPE '\\' LIMIT 1", (f"{escaped_prefix}%",)).fetchone()
            if not row:
                raise FileNotFoundError(f"未找到豆包工作会话: {id_prefix}")
            cid, cdata = row
            cname = str(json.loads(cdata or "{}").get("name") or cid)
            mrows = conn.execute("SELECT data FROM messages WHERE conversationId = ? ORDER BY createdAt ASC LIMIT 200", (cid,)).fetchall()
        finally:
            conn.close()
        for (mdata,) in mrows:
            try:
                mobj = json.loads(mdata or "{}")
            except Exception:
                continue
            author = str(mobj.get("authorName") or mobj.get("authorId") or "AI")
            role = "User" if str(mobj.get("authorId", "")).lower() == "user" or author.lower() == "you" else author
            cleaned = _clean_handoff_text(str(mobj.get("text") or ""), keep_code=keep_code)
            if cleaned:
                turns.append((role, cleaned))
        return f"豆包工作 `{cname}` ({cid[:16]})", turns

    if src == "eden":
        db_p = discovered.get("eden", {}).get("db_path")
        if not db_p:
            raise FileNotFoundError("本机未找到 Eden ahub.db")
        escaped_prefix = id_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conn = sqlite3.connect(f"file:{db_p}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT id, name FROM rooms WHERE id LIKE ? ESCAPE '\\' LIMIT 1", (f"{escaped_prefix}%",)).fetchone()
            if not row:
                raise FileNotFoundError(f"未找到 Eden 房间: {id_prefix}")
            rid, rname = row
            mrows = conn.execute(
                "SELECT sender_id, sender_name, content FROM messages WHERE room_id = ? AND type = 'chat' ORDER BY timestamp ASC LIMIT 200",
                (rid,),
            ).fetchall()
        finally:
            conn.close()
        for sid, sname, content in mrows:
            role = "User" if sid == "WebUser" else str(sname or sid)
            cleaned = _clean_handoff_text(str(content or ""), keep_code=keep_code)
            if cleaned:
                turns.append((role, cleaned))
        return f"Eden 房间 `{rname}` ({rid[:16]})", turns

    if src == "hermes":
        db_p = discovered.get("hermes", {}).get("db_path")
        if not db_p:
            raise FileNotFoundError("本机未找到 Hermes state.db")
        escaped_prefix = id_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conn = sqlite3.connect(f"file:{db_p}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT id, COALESCE(title, id) FROM sessions WHERE id LIKE ? ESCAPE '\\' LIMIT 1", (f"{escaped_prefix}%",)).fetchone()
            if not row:
                raise FileNotFoundError(f"未找到 Hermes 会话: {id_prefix}")
            sid, stitle = row
            mrows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? AND role IN ('user', 'assistant') ORDER BY id ASC LIMIT 200",
                (sid,),
            ).fetchall()
        finally:
            conn.close()
        for role, content in mrows:
            rlabel = "User" if str(role).lower() == "user" else "Hermes"
            cleaned = _clean_handoff_text(str(content or ""), keep_code=keep_code)
            if cleaned:
                turns.append((rlabel, cleaned))
        return f"Hermes `{stitle}` ({str(sid)[:16]})", turns

    raise ValueError(f"不支持的会话来源: {source}（支持: claude, codex, antigravity, cursor, trae, workbuddy, douchat, hermes, eden, deepseek）")


def cmd_chat_list(root: Path, args: Any, log_audit: Callable[..., None]) -> None:
    items = _list_recent_chats(source_filter=args.source or "", query=args.query or "", limit=args.limit)
    log_audit(root, "chat:list", args.source or "all", detail=f"query={args.query or ''},count={len(items)}")
    if not items:
        print("未找到匹配的本地 AI 聊天会话。可尝试不带 --query 或先运行 `tree seed scan` 检查可用来源。")
        return
    print(f"💬 跨 AI 近期对话清单（共 {len(items)} 场，使用 `tree chat read <ref>` 拉取详细上下文）：\n")
    ref_width = min(max((len(it["ref"]) for it in items), default=25), 52)
    for it in items:
        print(f"  [{it['ref']:<{ref_width}}] ({it['date']}) {it['title']}")


def cmd_chat_read(root: Path, args: Any, log_audit: Callable[..., None]) -> None:
    ref = args.ref.strip()
    if ":" not in ref:
        print("❌ 会话标识格式须为 `<source>:<id前缀>`，例如 `cursor:000f4926` 或 `claude:1a2b3c4d`。请先运行 `tree chat list` 查看。", file=sys.stderr)
        sys.exit(1)
    source, id_prefix = ref.split(":", 1)
    try:
        header, turns = _read_session_turns(source, id_prefix, keep_code=bool(args.raw_code))
    except Exception as e:
        print(f"❌ 读取跨 AI 会话失败: {e}", file=sys.stderr)
        sys.exit(1)

    log_audit(root, "chat:read", ref, detail=f"turns={len(turns)}")
    if not turns:
        print(f"⚠️ 会话 {header} 中未解析到有效双边文本。")
        return

    max_turns = args.turns if args.turns and args.turns > 0 else len(turns)
    shown = turns[-max_turns:] if len(turns) > max_turns else turns
    print(f"# [跨 AI 实时接力上下文] {header}")
    print(f"> 共 {len(turns)} 轮有效消息（当前展示最近 {len(shown)} 轮 | 已自动过滤系统噪音并脱敏密钥）\n")
    for idx, (role, txt) in enumerate(shown, len(turns) - len(shown) + 1):
        print(f"### [{idx}] {role}")
        print(txt)
        print()
