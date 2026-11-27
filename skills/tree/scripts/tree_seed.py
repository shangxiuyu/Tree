#!/usr/bin/env python3
"""
Tree Seed Engine — Local-First Context Migration & Chat History Distillation Layer.
Zero external dependencies (Python standard library only).

Design principles:
1. Read-only access to local agent data stores (never modifies source apps).
2. Runtime cache (`.tree/staging/`, `.tree/sources.json`) is gitignored so raw chats never pollute Git.
3. Strips code dumps, tool logs, system wrappers, and redacts secrets before staging.
4. Supports incremental cursors (`.tree/sources.json`) and official export imports (ChatGPT/Claude/Doubao/WorkBuddy/Markdown).
"""

from __future__ import annotations

import datetime
import hashlib
import importlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable


# Secret redaction patterns (best-effort safety net before staging)
SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"(?<![A-Za-z0-9_\-])sk-[A-Za-z0-9_\-]{16,}(?![A-Za-z0-9_\-])"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?<![A-Za-z0-9_\-])(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}(?![A-Za-z0-9_\-])"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"(?<![A-Za-z0-9_\-])github_pat_[A-Za-z0-9_]{22,}(?![A-Za-z0-9_\-])"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"(?<![A-Za-z0-9_\-])AIza[0-9A-Za-z_\-]{25,}(?![A-Za-z0-9_\-])"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"(?<![A-Za-z0-9_\-])xox[baprs]-[0-9A-Za-z\-]{10,}(?![A-Za-z0-9_\-])"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}(?![A-Za-z0-9_\-])"), "[REDACTED_JWT]"),
    (re.compile(r"(?<![A-Za-z0-9_\-])(?:LTAI|AKID)[A-Za-z0-9]{12,}(?![A-Za-z0-9_\-])"), "[REDACTED_CLOUD_AK]"),
    (re.compile(r"(?<![A-Za-z0-9_\-])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9_\-])"), "[REDACTED_CLOUD_AK]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]{20,}"), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"(?i)((?:cookie|set-cookie)\s*:\s*)[^\r\n]{12,}"), r"\1[REDACTED_COOKIE]"),
    (re.compile(r"(?i)((?:cookie|set-cookie|sessionid|sid)\s*=\s*['\"]?)[^\s'\"\n;&]{12,}"), r"\1[REDACTED_COOKIE]"),
    (re.compile(r"(?i)((?:api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['\"]?)[A-Za-z0-9_\-\./+=:@!#$%^*?~]{10,}"), r"\1[REDACTED]"),
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


def staging_dir(root: Path) -> Path:
    d = root / ".tree" / "staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sources_file(root: Path) -> Path:
    return root / ".tree" / "sources.json"


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


def redact_all(text: str) -> str:
    """Redact both secrets/tokens and PII (Chinese mobile phone numbers & 18-digit resident IDs)."""
    return redact_pii(redact_secrets(text))


def redact_lifestyle_text(text: str) -> str:
    """Redact phone numbers, ID cards, tokens, and keys while keeping preference semantics."""
    return redact_all(text)


def _ts_to_seconds(raw_ts: Any) -> float:
    """Normalize valid epoch seconds / ms / us / ns (2010..2100) into seconds, returning 0.0 on malformed values."""
    try:
        val = float(raw_ts or 0)
    except Exception:
        return 0.0
    if val <= 0:
        return 0.0
    min_sec, max_sec = 1262304000.0, 4102444800.0  # 2010-01-01 .. 2100-01-01
    for scale in (1.0, 1e3, 1e6, 1e9):
        candidate = val / scale
        if min_sec <= candidate <= max_sec:
            return candidate
    return 0.0


def _safe_fromtimestamp(raw_ts: Any, fmt: str = "%Y-%m-%d", fallback: str = "recent") -> str:
    """Format a Unix timestamp safely without ever crashing on malformed or out-of-range values."""
    sec = _ts_to_seconds(raw_ts)
    if sec <= 0:
        return fallback
    try:
        return datetime.datetime.fromtimestamp(sec).strftime(fmt)
    except (ValueError, OverflowError, OSError):
        return fallback


def _connect_sqlite_ro(db_path: str | Path, timeout: float = 1.0) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout)


def _has_substantive_memory_content(p: Path) -> bool:
    """Return True if a memory/rules file exists and still has content after stripping Tree's own managed block."""
    try:
        if not p.is_file() or p.stat().st_size == 0:
            return False
        raw = p.read_text(encoding="utf-8", errors="ignore")
        return bool(strip_tree_context_block(raw))
    except Exception:
        return False


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
    if _has_substantive_memory_content(base / "CLAUDE.md"):
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
    if _has_substantive_memory_content(base / "AGENTS.md"):
        memories.append(base / "AGENTS.md")
    mem_dir = base / "memories"
    if mem_dir.is_dir():
        memories.extend(sorted(mem_dir.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True))
    mem_db = base / "memories_1.sqlite"
    db_memory_count = 0
    if mem_db.is_file():
        try:
            conn = _connect_sqlite_ro(mem_db)
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
        if _has_substantive_memory_content(p):
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
            conn = _connect_sqlite_ro(db)
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
        if _has_substantive_memory_content(p):
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
            if (base / "accounts").is_dir():
                for p in sorted((base / "accounts").glob(f"*/shared/{name}")):
                    if _has_substantive_memory_content(p):
                        memories.append(p)
        db = base / "douchat.db"
        if db.is_file() and db.stat().st_size > 0:
            db_path = str(db)
            latest_date = file_mtime_iso(db)
            try:
                conn = _connect_sqlite_ro(db)
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
        "memory_files": [str(p) for p in memories],
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
                    conn = _connect_sqlite_ro(st)
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
                                    mtime = _ts_to_seconds(s.get("updatedAt") or s.get("createdAt") or 0)
                                    if 946684800 <= mtime <= 4102444800 and mtime > latest_mtime:
                                        latest_mtime = mtime
                except Exception:
                    pass

    if latest_mtime > 0:
        latest_date = _safe_fromtimestamp(latest_mtime, "%Y-%m-%d", fallback="")

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
    env_db = (os.environ.get("EDEN_DB_PATH") or "").strip()
    if env_db:
        p_env = Path(env_db).expanduser()
        if p_env.is_file() and p_env.stat().st_size > 0:
            return p_env
    home = Path.home()
    candidates = [
        home / "Library/Application Support/ahub-desktop/ahub.db",
        home / "Library/Application Support/eden/ahub.db",
        home / ".eden/ahub.db",
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
            conn = _connect_sqlite_ro(db)
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
        if _has_substantive_memory_content(p):
            memories.append(p)
    db = base / "state.db"
    db_path = ""
    session_count = 0
    latest_date = ""
    if db.is_file() and db.stat().st_size > 0:
        db_path = str(db)
        latest_date = file_mtime_iso(db)
        try:
            conn = _connect_sqlite_ro(db)
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
        if _has_substantive_memory_content(p):
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
# Built-in Zero-Dependency Context Sources: Google Chrome
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


def extract_chrome_context(
    days: int,
    max_sessions: int,
    skip_ids: set[str] | None = None,
) -> tuple[str, int, list[str]]:
    chrome_dir = _chrome_user_data_dir()
    if not chrome_dir:
        return "", 0, []
    profiles = _list_chrome_profiles(chrome_dir)
    if not profiles:
        return "", 0, []

    eff_days = days if days > 0 else 90
    limit = max(5, min(max_sessions, 30))
    lines = [
        "# Google Chrome 书签与高频访问线索（仅用于提炼常用工具、技术栈与关注领域，严禁流水入树）",
        f"> 提取时间: {now_iso()} | 回溯窗口: 近 {eff_days} 天 | 不读取 Cookie 与密码",
        "",
    ]

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
                conn = _connect_sqlite_ro(tmp_db)
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
    body_text = redact_all("\n".join(lines).strip() + "\n")
    fp = short_hash(f"chrome:{eff_days}:{total_items}:{body_text[120:600]}")
    if skip_ids and fp in skip_ids:
        return "", 0, []
    return body_text, total_items, [fp]


def scan_all_local_sources(_root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Discover all 12 built-in local sources (11 local AI clients + Chrome)."""
    return {
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
            preview = redact_all(s)[:80]
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
            conn = _connect_sqlite_ro(info["memory_db"])
            rows = conn.execute(
                "SELECT thread_id, rollout_slug, raw_memory, rollout_summary FROM stage1_outputs ORDER BY source_updated_at DESC LIMIT 30"
            ).fetchall()
            conn.close()
            for tid, slug, raw_mem, summary in rows:
                txt = redact_all((raw_mem or summary or "").strip())
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
                if not isinstance(obj, dict):
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
                if not isinstance(obj, dict):
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
                if not isinstance(obj, dict):
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
                if not isinstance(obj, dict):
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
                with dctx.stream_reader(io.BytesIO(raw_bytes)) as reader:
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
            if not isinstance(obj, dict):
                continue
            t = obj.get("type")
            if t == "session":
                if isinstance(obj.get("cwd"), str):
                    cwd = obj["cwd"]
                if isinstance(obj.get("createdAt"), (int, float)) and obj["createdAt"] > 1_000_000_000:
                    date_str = _safe_fromtimestamp(obj["createdAt"], "%Y-%m-%d", fallback=date_str)
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
    skip_ids: set[str] | None = None,
) -> tuple[str, int, list[str]]:
    if not db_path_str or not Path(db_path_str).is_file():
        return "", 0, []
    skip = skip_ids or set()
    lines: list[str] = [
        "# 豆包工作 (Douchat) 本地历史对话精华（人类发言与意图流）",
        f"> 生成时间: {now_iso()} | 范围: 最近 {days} 天，最多 {max_sessions} 场对话 | 只读安全提取。",
        "> **按需下钻双边上下文**：若某场人类发言有价值但缺 AI 回复看不清来龙去脉，用标题中的 `ref` 执行 `tree chat read \"<ref>\" --turns 30` 调阅人+AI完整对话。",
        "",
    ]
    cutoff_sec = (datetime.datetime.now().timestamp() - days * 86400) if days > 0 else 0.0
    sessions_out: list[tuple[float, str, str, list[str]]] = []

    try:
        conn = _connect_sqlite_ro(db_path_str)
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
            ts_sec = _ts_to_seconds(ts_val)
            fp = short_hash(f"douchat:{cid}:{ts_val}")
            if fp in skip or str(cid) in skip:
                continue
            if cutoff_sec and ts_sec > 946684800 and ts_sec < cutoff_sec:
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
                if not isinstance(mobj, dict):
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
                sessions_out.append((ts_sec, cid, cname, user_msgs))
        conn.close()
    except Exception as e:
        lines.append(f"> *(读取 Douchat 数据库时跳过: {e})*")
        return "\n".join(lines), 0, []

    sessions_out.sort(key=lambda x: x[0], reverse=True)
    total_chars = 0
    used = 0
    processed_ids: list[str] = []
    for ts_sec, cid, cname, user_msgs in sessions_out[:max_sessions]:
        date_str = _safe_fromtimestamp(ts_sec, "%Y-%m-%d", fallback="recent")
        ref = _format_chat_ref("douchat", str(cid))
        block_lines = [f"## [{date_str}] {redact_all(cname)} (`ref: {ref}` · {len(user_msgs)} 轮人类输入)"]
        for m in user_msgs[:15]:
            block_lines.append(f"- {m.replace(chr(10), ' / ')}")
        block_lines.append("")
        block = "\n".join(block_lines)
        if total_chars + len(block) > max_chars:
            break
        lines.append(block)
        total_chars += len(block)
        used += 1
        processed_ids.append(short_hash(f"douchat:{cid}:{int(ts_sec)}"))
    return "\n".join(lines), used, processed_ids


def extract_eden_chats(
    db_path_str: str,
    days: int,
    max_sessions: int,
    max_chars: int = 38000,
    skip_ids: set[str] | None = None,
) -> tuple[str, int, list[str]]:
    if not db_path_str or not Path(db_path_str).is_file():
        return "", 0, []
    skip = skip_ids or set()
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
        conn = _connect_sqlite_ro(db_path_str)
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
            if str(rid) in skip:
                continue
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
    for date_str, rid, rname, user_msgs in rooms_out:
        if used >= max_sessions:
            break
        fp = short_hash(f"eden:{rid}:{date_str}:{len(user_msgs)}")
        if fp in skip or str(rid) in skip:
            continue
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
        processed_ids.append(fp)
    return "\n".join(lines), used, processed_ids


def extract_hermes_chats(
    db_path_str: str,
    days: int,
    max_sessions: int,
    max_chars: int = 38000,
    skip_ids: set[str] | None = None,
) -> tuple[str, int, list[str]]:
    if not db_path_str or not Path(db_path_str).is_file():
        return "", 0, []
    skip = skip_ids or set()
    lines: list[str] = [
        "# Hermes Agent 本地历史对话精华（人类发言与意图流）",
        f"> 生成时间: {now_iso()} | 范围: 最近 {days} 天，最多 {max_sessions} 场会话 | 只读安全提取。",
        "> **按需下钻双边上下文**：若某场人类发言有价值但缺 AI 回复看不清来龙去脉，用标题中的 `ref` 执行 `tree chat read \"<ref>\" --turns 30` 调阅人+AI完整对话。",
        "",
    ]
    cutoff_sec = (datetime.datetime.now().timestamp() - days * 86400) if days > 0 else 0.0
    sessions_out: list[tuple[float, str, str, str, list[str]]] = []

    try:
        conn = _connect_sqlite_ro(db_path_str)
        norm_ts_expr = (
            "CASE WHEN COALESCE(last_activity_at, started_at, 0) > 1000000000000 "
            "THEN COALESCE(last_activity_at, started_at, 0) / 1000.0 "
            "ELSE COALESCE(last_activity_at, started_at, 0) END"
        )
        q = (
            f"SELECT id, COALESCE(title, id), ({norm_ts_expr}) AS ts "
            "FROM sessions WHERE COALESCE(message_count, 0) > 0 "
            + (f"AND ({norm_ts_expr}) >= ? " if cutoff_sec else "")
            + "ORDER BY ts DESC LIMIT ?"
        )
        params = (cutoff_sec, max(max_sessions * 2, 40)) if cutoff_sec else (max(max_sessions * 2, 40),)
        rows = conn.execute(q, params).fetchall()
        for sid, title, ts in rows:
            if str(sid) in skip:
                continue
            sec = _ts_to_seconds(ts)
            if cutoff_sec and sec > 946684800 and sec < cutoff_sec:
                continue
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
                date_str = _safe_fromtimestamp(sec, "%Y-%m-%d", fallback="recent")
                sessions_out.append((sec, date_str, str(sid), redact_all(str(title or sid))[:75], user_msgs))
        conn.close()
    except Exception as e:
        lines.append(f"> *(读取 Hermes 数据库时跳过: {e})*")
        return "\n".join(lines), 0, []

    sessions_out.sort(key=lambda x: x[0], reverse=True)
    total_chars = 0
    used = 0
    processed_ids: list[str] = []
    for _sec, date_str, sid, title, user_msgs in sessions_out:
        if used >= max_sessions:
            break
        fp = short_hash(f"hermes:{sid}:{date_str}:{len(user_msgs)}")
        if fp in skip or str(sid) in skip:
            continue
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
        processed_ids.append(fp)
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
    skip_ids: set[str] | None = None,
) -> tuple[str, int, list[str]]:
    if not base_dir_str or not Path(base_dir_str).is_dir():
        return "", 0, []
    skip = skip_ids or set()
    ws_dir = Path(base_dir_str) / "User/workspaceStorage"
    if not ws_dir.is_dir():
        return "", 0, []

    lines: list[str] = [
        "# Trae 本地历史对话精华（人类发言与意图流）",
        f"> 生成时间: {now_iso()} | 范围: 最近 {days} 天，最多 {max_sessions} 场对话 | 只读安全提取。",
        "> **按需下钻双边上下文**：若某场人类发言有价值但缺 AI 回复看不清来龙去脉，用标题中的 `ref` 执行 `tree chat read \"<ref>\" --turns 30` 调阅人+AI完整对话。",
        "",
    ]
    cutoff_sec = (datetime.datetime.now().timestamp() - days * 86400) if days > 0 else 0.0
    sessions_found: list[tuple[float, str, str, str, list[str]]] = []

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
            conn = _connect_sqlite_ro(st)
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
                    created_raw = s.get("updatedAt") or s.get("createdAt") or 0
                    created_sec = _ts_to_seconds(created_raw)
                    if cutoff_sec and created_sec > 946684800 and created_sec < cutoff_sec:
                        continue
                    sid = str(s.get("sessionId") or s.get("id") or "")
                    fp = short_hash(f"trae:{sid}:{int(created_sec)}")
                    if fp in skip or (sid and sid in skip):
                        continue
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
                        sessions_found.append((created_sec, sid, redact_all(full_label), str(st), user_msgs))
        except Exception:
            pass

    sessions_found.sort(key=lambda x: x[0], reverse=True)
    total_chars = 0
    used = 0
    processed_ids: list[str] = []

    for created_sec, sid, title, _st_path, user_msgs in sessions_found[:max_sessions]:
        date_str = _safe_fromtimestamp(created_sec, "%Y-%m-%d", fallback="recent")
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
        processed_ids.append(short_hash(f"trae:{sid}:{int(created_sec)}"))

    return "\n".join(lines), used, processed_ids


def extract_cursor_chats(
    db_path_str: str,
    days: int,
    max_sessions: int,
    max_chars: int = 35000,
    skip_ids: set[str] | None = None,
) -> tuple[str, int, list[str]]:
    if not db_path_str or not Path(db_path_str).is_file():
        return "", 0, []
    skip = skip_ids or set()
    lines: list[str] = [
        "# Cursor 本地历史对话精华（人类发言与意图流）",
        f"> 生成时间: {now_iso()} | 最多 {max_sessions} 场 Composer 对话 | 只读安全提取。",
        "> **按需下钻双边上下文**：若某场人类发言有价值但缺 AI 回复看不清来龙去脉，用标题中的 `ref` 执行 `tree chat read \"<ref>\" --turns 30` 调阅人+AI完整对话。",
        "",
    ]
    cutoff_sec = (datetime.datetime.now().timestamp() - days * 86400) if days > 0 else 0.0
    sessions_found: list[tuple[float, str, str, list[str]]] = []

    try:
        conn = _connect_sqlite_ro(db_path_str)
        # Order by rowid DESC so newest composers are always scanned first on large state.vscdb files
        cur = conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%' ORDER BY rowid DESC LIMIT ?",
            (max(max_sessions * 6, 400),),
        )
        for key, val in cur:
            if not val or str(key) in skip:
                continue
            try:
                obj = json.loads(val)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            updated_raw = obj.get("lastUpdatedAt") or obj.get("createdAt") or 0
            updated_sec = _ts_to_seconds(updated_raw)
            if cutoff_sec and updated_sec > 946684800 and updated_sec < cutoff_sec:
                continue
            fp = short_hash(f"cursor:{key}:{int(updated_sec)}")
            if fp in skip or str(key) in skip:
                continue
            title = redact_all(str(obj.get("name") or obj.get("text") or obj.get("composerId") or key))[:60].replace("\n", " ").strip()
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
                sessions_found.append((updated_sec, key, title, user_msgs))
        conn.close()
    except Exception as e:
        lines.append(f"> *(读取 Cursor 数据库时跳过: {e})*")
        return "\n".join(lines), 0, []

    sessions_found.sort(key=lambda x: x[0], reverse=True)
    total_chars = 0
    used = 0
    processed_ids: list[str] = []

    for updated_sec, key, title, user_msgs in sessions_found[:max_sessions]:
        date_str = _safe_fromtimestamp(updated_sec, "%Y-%m-%d", fallback="unknown-date")
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
        processed_ids.append(short_hash(f"cursor:{key}:{int(updated_sec)}"))

    return "\n".join(lines), used, processed_ids


# ---------------------------------------------------------------------------
# Generic Official Export Importer (ChatGPT / Claude Web / Doubao / WorkBuddy / MD)
# ---------------------------------------------------------------------------

def parse_generic_conversations_json(data: Any, max_sessions: int = 60) -> list[tuple[str, str, list[str]]]:
    """Parse ChatGPT export, Claude Web export, or generic [{role, content}] exports."""
    results: list[tuple[str, str, list[str]]] = []

    if isinstance(data, dict):
        # Single conversation object with metadata (`title`/`name` + `messages`) must keep its metadata
        if isinstance(data.get("messages"), list) and any(k in data for k in ("title", "name", "created_at", "create_time", "updated_at", "update_time")):
            data = [data]
        else:
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
            date_str = _safe_fromtimestamp(ts, "%Y-%m-%d", fallback="")
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
    """Import an official export file (ZIP/JSON/JSONL/MD/TXT) or directory into `.tree/staging/`."""
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

    def _is_binary_bytes(raw_b: bytes) -> bool:
        return b"\x00" in raw_b[:4096]

    def _import_jsonl_text(raw_text: str, title_name: str) -> None:
        msgs: list[str] = []
        for raw_line in raw_text.splitlines():
            if not raw_line.strip():
                continue
            try:
                obj = json.loads(raw_line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            role = str(obj.get("role") or obj.get("type") or "").lower()
            if role in ("user", "human", "user_input"):
                txt = clean_chat_text(extract_text_from_content(obj.get("content") or obj.get("message")), max_len=450)
                if txt:
                    msgs.append(txt)
        if msgs:
            blk = [f"## `{title_name}` ({len(msgs)} 条人类输入)"] + [f"- {m.replace(chr(10), ' / ')}" for m in msgs[:80]]
            append_block("\n".join(blk) + "\n")

    if src_path.is_file():
        suf = src_path.suffix.lower()
        if suf == ".zip":
            with zipfile.ZipFile(src_path, "r") as zf:
                for name in zf.namelist():
                    low = name.lower()
                    if low.endswith(".json") and ("conversation" in low or "chat" in low or "message" in low or len(zf.namelist()) <= 5):
                        handle_json_bytes(zf.read(name), name)
                    elif low.endswith((".md", ".txt")):
                        raw_b = zf.read(name)
                        if _is_binary_bytes(raw_b):
                            continue
                        txt = redact_all(raw_b.decode("utf-8", errors="ignore").strip())
                        if txt:
                            if not append_block(f"## `{name}`\n{txt[:2500]}\n"):
                                break
        elif suf == ".json":
            handle_json_bytes(src_path.read_bytes(), src_path.name)
        elif suf == ".jsonl":
            _import_jsonl_text(src_path.read_text(encoding="utf-8", errors="ignore"), src_path.name)
        else:
            raw_b = src_path.read_bytes()
            if _is_binary_bytes(raw_b):
                raise ValueError(f"不支持导入二进制文件: {src_path.name}")
            txt = redact_all(raw_b.decode("utf-8", errors="ignore").strip())
            if txt:
                prefix = f"## `{src_path.name}`\n"
                trunc_suffix = "\n…[已截断]"
                budget = max(200, max_chars - len(prefix) - len(trunc_suffix) - 2)
                snippet = (txt[:budget].rstrip() + trunc_suffix) if len(txt) > budget else txt
                append_block(f"{prefix}{snippet}\n")
    elif src_path.is_dir():
        files = sorted(
            [p for p in src_path.rglob("*") if p.is_file() and p.suffix.lower() in {".md", ".txt", ".json", ".jsonl"}],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for f in files[:60]:
            suf_f = f.suffix.lower()
            if suf_f == ".json":
                handle_json_bytes(f.read_bytes(), f.relative_to(src_path).as_posix())
            elif suf_f == ".jsonl":
                _import_jsonl_text(f.read_text(encoding="utf-8", errors="ignore"), f.relative_to(src_path).as_posix())
            else:
                raw_b = f.read_bytes()
                if _is_binary_bytes(raw_b):
                    continue
                txt = redact_all(raw_b.decode("utf-8", errors="ignore").strip())
                if txt:
                    rel = f.relative_to(src_path).as_posix()
                    if not append_block(f"## `{rel}` [{file_mtime_iso(f)}]\n{txt[:2000]}\n"):
                        break

    if item_count == 0:
        raise ValueError(f"未从 `{src_path.name}` 解析到有效文本素材（文件可能为空或格式不受支持）")

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
            "staging_file": out_file.relative_to(root).as_posix(),
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
        f"> 生成时间: {now_iso()} | 位置: `.tree/staging/`（已加入 `.gitignore`，不污染 Git 树干）",
        "",
        "## 一、已生成的清洗素材包",
    ]
    for item in files_meta:
        lines.append(f"- `{item['rel_path']}` — {item['desc']}（{item['chars']} 字符）")

    lines.extend(
        [
            "",
            "## 二、本次破土蒸馏流程",
            "1. **先读唯一内容契约**：写入前必先执行 `tree rules`（即根目录 `README.md`），所有分类归属、来源分级（`--origin`/`--source`）与写作红线以此为准。",
            "2. **轻扫描 → 按需下钻，不要盲目全吞**：",
            "   - **本地记忆**：已全量索引在 `memories-index.md`。先挑出全局画像、活跃项目与近期高价值记录，直接读原文件或用 `tree seed read \"<文件路径>\"`（自动脱敏）。",
            "   - **历史对话**：`chats-*.md` 默认只抽人类发言流用于初筛；遇到缺 AI 回复看不清上下文的场次，按标题里的 `` `ref: <source>:<id前缀>` `` 执行 `tree chat read \"<source>:<id前缀>\" --turns 30` 拉取双边对话。",
            "   - **当前工具独有的个人数据源**：若当前宿主连着其他 AI 看不到的外部数据（电商、外卖买菜、运动健康、笔记、播客等），一并调用提炼稳定的习惯、入口线索和后续别的 AI 也用得上的日常记录。",
            "3. **合并碎片，补齐全貌**：严禁把几段聊天碎片直接拼成一页；整理长期项目时结合 `--location` 目录已有文档还原来龙去脉；素材不足先问用户，不脑补。",
            "4. **确认后落盘并清理**：呈报破土草案经用户拍板后逐页 `tree write`，完成后执行 `tree seed clear` 清空 `.tree/staging/` 临时文件。",
        ]
    )
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def list_staging_files(root: Path) -> list[Path]:
    sd = root / ".tree" / "staging"
    if not sd.is_dir():
        return []
    return sorted(p for p in sd.glob("*.md") if p.is_file())


# ---------------------------------------------------------------------------
# CLI Command Handlers
# ---------------------------------------------------------------------------

def _pad_display(text: str, width: int) -> str:
    import unicodedata

    disp = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)
    return text + (" " * max(1, width - disp))


def cmd_seed_scan(root: Path, _args: Any, log_audit: Callable[..., None]) -> None:
    discovered = scan_all_local_sources(root)
    state = load_sources_state(root)
    state["detected"] = {
        k: {
            "name": v["name"],
            "available": v["available"],
            "memory_count": len(v.get("memory_files", [])) + int(v.get("memory_db_count", 0)),
            "chat_count": len(v.get("chat_files", [])) if "chat_files" in v else int(v.get("chat_count", 0)),
            "latest_date": v.get("latest_date", ""),
        }
        for k, v in discovered.items()
    }
    save_sources_state(root, state)
    log_audit(root, "seed:scan", ".tree/sources.json")

    print("🌱 Tree Seed 本机记忆与对话源探测结果（12 项内置本地只读源，未提取任何正文）：\n")
    available_ids: list[str] = []
    for sid, info in discovered.items():
        mem_n = len(info.get("memory_files", [])) + int(info.get("memory_db_count", 0))
        chat_n = len(info.get("chat_files", [])) if "chat_files" in info else int(info.get("chat_count", 0))
        if info["available"]:
            status = "✅ 已就绪"
            available_ids.append(sid)
        else:
            status = "—  未发现"
        latest = f" | 最近活跃: {info['latest_date']}" if info.get("latest_date") else ""
        unit = info.get("unit_label")
        count_col = f"记录: {chat_n:>4} {unit}" if unit else f"记忆/规则: {mem_n:>3} 份 | 历史对话: {chat_n:>4} 场"
        print(f"  [{sid:<11}] {status}  {_pad_display(info['name'], 24)} {count_col}{latest}")

    print("\n📦 其他本地导入方式：")
    print("  · 官方导出包或本地笔记目录（ChatGPT / Claude Web / Obsidian 等）：`tree seed import <文件或目录路径> --label <来源名>`")

    if available_ids:
        src_list = ",".join(available_ids)
        print("\n🚀 下一步（经你确认授权后执行本地清洗提取，暂存 `.tree/staging/`）：")
        print(f"  tree seed extract --sources {src_list} --days 60")


def cmd_seed_extract(root: Path, args: Any, log_audit: Callable[..., None]) -> None:
    discovered = scan_all_local_sources(root)
    if args.sources:
        selected = {s.strip().lower() for s in args.sources.split(",") if s.strip()}
    else:
        selected = {k for k, v in discovered.items() if v["available"]}

    valid_ids = set(discovered.keys())
    unknown = selected - valid_ids
    if unknown:
        print(
            f"❌ 未知来源标识: {', '.join(sorted(unknown))}（内置本机源: {', '.join(sorted(valid_ids))}）",
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
    full_reextract = bool(getattr(args, "full", False))

    def _skip_for(sid: str) -> set[str]:
        return set() if full_reextract else set(proc_map.get(sid) or [])

    had_prior_cursors = (not full_reextract) and any(proc_map.get(sid) for sid in selected)

    if not args.memories_only:
        if "claude" in selected and discovered["claude"]["chat_files"]:
            md, cnt, pids = extract_jsonl_source_chats(
                "claude",
                "Claude Code",
                discovered["claude"]["chat_files"],
                parse_claude_session,
                days=args.days,
                max_sessions=args.max_sessions,
                skip_ids=_skip_for("claude"),
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
                skip_ids=_skip_for("codex"),
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
                skip_ids=_skip_for("antigravity"),
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
                skip_ids=_skip_for("cursor"),
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
                skip_ids=_skip_for("trae"),
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
                skip_ids=_skip_for("workbuddy"),
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
                skip_ids=_skip_for("douchat"),
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
                skip_ids=_skip_for("hermes"),
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
                skip_ids=_skip_for("eden"),
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
                skip_ids=_skip_for("deepseek"),
            )
            if cnt > 0:
                out_p = sd / "chats-deepseek.md"
                out_p.write_text(md, encoding="utf-8")
                files_meta.append({"rel_path": str(out_p.relative_to(root)), "desc": f"DeepSeek Harness 对话精华（{cnt} 场）", "chars": len(md)})
                proc_map["deepseek"] = list(dict.fromkeys((proc_map.get("deepseek") or []) + pids))[-500:]

        if "chrome" in selected and discovered["chrome"]["available"]:
            md, cnt, pids = extract_chrome_context(
                days=args.days,
                max_sessions=args.max_sessions,
                skip_ids=_skip_for("chrome"),
            )
            if cnt > 0 and md:
                out_p = sd / "context-chrome.md"
                out_p.write_text(md, encoding="utf-8")
                files_meta.append({"rel_path": str(out_p.relative_to(root)), "desc": f"Google Chrome 书签与高频站点（{cnt} 条）", "chars": len(md)})
                proc_map["chrome"] = list(dict.fromkeys((proc_map.get("chrome") or []) + pids))[-500:]

    # Include any previously imported files in manifest
    for existing in list_staging_files(root):
        if existing.name.startswith("import-"):
            rel = existing.relative_to(root).as_posix()
            files_meta.append({"rel_path": rel, "desc": f"外部导入素材（{existing.name}）", "chars": len(existing.read_text(encoding="utf-8", errors="ignore"))})

    state["processed_sessions"] = proc_map
    save_sources_state(root, state)

    if not files_meta:
        hint = "（部分会话可能已被增量游标跳过，加 `--full` 可全量重新提取）" if had_prior_cursors else ""
        print(f"⚠️ 未在所选来源与时间窗口内提取到新的有效素材{hint}。也可扩大 `--days` 或使用 `tree seed import <路径>`。")
        return

    manifest = write_staging_manifest(root, files_meta)
    log_audit(root, "seed:extract", ",".join(sorted(selected)), detail=f"files={len(files_meta)}")

    print("✅ 已完成本地清洗与密钥脱敏，素材已暂存至 `.tree/staging/`（Git 已忽略，绝不污染树干）：\n")
    for item in files_meta:
        print(f"  📄 {item['rel_path']} — {item['desc']} ({item['chars']:,} 字符)")
    print(f"\n🗺️ 总览与蒸馏协议: `{manifest.relative_to(root)}`")
    print("👉 下一步：在任意宿主 Agent 中说「读一下 .tree/staging/，帮我起草 Tree 初始化破土方案」，确认后落盘。")


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
    cleaned = redact_all(strip_tree_context_block(raw))
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
        files_meta.append({"rel_path": str(p.relative_to(root)), "desc": p.stem, "chars": len(p.read_text(encoding="utf-8", errors="ignore"))})
    write_staging_manifest(root, files_meta)
    log_audit(root, "seed:import", str(args.path), detail=f"items={count}")
    print(f"✅ 已导入并清洗 {count} 条素材至 `{out_p.relative_to(root)}`（已自动脱敏，不进 Git 仓库）")
    print("👉 可在任意宿主 Agent 中读取 `.tree/staging/00-manifest.md` 并生成破土提案。")


def cmd_seed_status(root: Path, _args: Any, _log_audit: Callable[..., None]) -> None:
    state = load_sources_state(root)
    staged = list_staging_files(root)
    print("🌱 Tree Seed 本机迁移与暂存状态（`.tree/sources.json` & `.tree/staging/`）\n")
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
            print(f"  📄 .tree/staging/{p.name} ({p.stat().st_size:,} bytes)")
    else:
        print("  (空 — 已保持洁净)")

    v_seed_dir = root / "seed"
    if v_seed_dir.is_dir():
        legacy_files = sorted(
            p for p in v_seed_dir.rglob("*.md")
            if p.is_file() and p.name.lower() != "readme.md"
        )
        if legacy_files:
            print(f"\n⚠️ 检测到遗留 `seed/` 目录（{len(legacy_files)} 篇）：建议按主题归并至 `01~04` 并通过 `--origin external` 标注来源。")


def cmd_seed_clear(root: Path, _args: Any, log_audit: Callable[..., None]) -> None:
    sd = root / ".tree" / "staging"
    removed = 0
    if sd.is_dir():
        for p in list(sd.iterdir()):
            if p.is_file():
                p.unlink()
                removed += 1
            elif p.is_dir():
                shutil.rmtree(p)
                removed += 1
    log_audit(root, "seed:clear", ".tree/staging", detail=f"removed={removed}")
    print(f"🧹 已清空 `.tree/staging/`（移除 {removed} 个临时暂存文件，保留 `01~04` 知识库与 `.tree/sources.json` 增量游标）。")


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
    s = redact_all(s).strip()
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
    discovered = scan_all_local_sources(None)
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
            display = redact_all(f"{title} | {preview}" if title and title != preview else (preview or title))
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
                    conn = _connect_sqlite_ro(st)
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
                        for s in obj.get("list", []):
                            if not isinstance(s, dict):
                                continue
                            msgs = s.get("messages")
                            if not isinstance(msgs, list) or not msgs:
                                continue
                            sid = str(s.get("sessionId") or s.get("id") or "")
                            title = str(s.get("title") or s.get("name") or "").replace("\n", " ").strip()
                            mtime = _ts_to_seconds(s.get("updatedAt") or s.get("createdAt") or 0)
                            date_str = _safe_fromtimestamp(mtime, "%Y-%m-%d %H:%M", fallback="unknown")
                            first_user_msg = ""
                            for m in msgs:
                                if isinstance(m, dict) and str(m.get("role", "")).lower() == "user":
                                    first_user_msg = extract_text_from_content(m.get("content") or m.get("text")).strip()
                                    if first_user_msg:
                                        break
                            display_title = title or (first_user_msg[:50] if first_user_msg else f"Session {sid[:8]}")
                            if ws_label and ws_label not in display_title:
                                display_title = f"{display_title} ({ws_label})"
                            display_title = redact_all(display_title)
                            if q and (q not in display_title.lower() and q not in sid.lower()):
                                continue
                            items.append(
                                {
                                    "ref": _format_chat_ref("trae", sid),
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
            conn = _connect_sqlite_ro(db_p)
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
                mtime = _ts_to_seconds(obj.get("lastUpdatedAt") or obj.get("createdAt") or 0)
                date_str = _safe_fromtimestamp(mtime, "%Y-%m-%d %H:%M", fallback="unknown")
                title = redact_all(str(obj.get("name") or obj.get("text") or "").replace("\n", " ").strip())[:90]
                if not title:
                    title = f"Composer {cid[:8]}"
                if q and (q not in title.lower() and q not in cid.lower()):
                    continue
                items.append(
                    {
                        "ref": _format_chat_ref("cursor", cid),
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

    # 4. Douchat SQLite
    if "douchat" in wanted and discovered.get("douchat", {}).get("db_path"):
        db_p = discovered["douchat"]["db_path"]
        try:
            conn = _connect_sqlite_ro(db_p)
            rows = conn.execute("SELECT id, updatedAt, data FROM conversations ORDER BY updatedAt DESC LIMIT 30").fetchall()
            conn.close()
            for cid, updated_at, cdata in rows:
                try:
                    cobj = json.loads(cdata or "{}")
                except Exception:
                    cobj = {}
                if not isinstance(cobj, dict):
                    cobj = {}
                cname = redact_all(str(cobj.get("name") or cid))[:80]
                mtime = _ts_to_seconds(updated_at or 0)
                date_str = _safe_fromtimestamp(mtime, "%Y-%m-%d %H:%M", fallback="unknown")
                if q and (q not in cname.lower() and q not in cid.lower()):
                    continue
                items.append(
                    {
                        "ref": _format_chat_ref("douchat", str(cid)),
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

    # 5. Eden SQLite
    if "eden" in wanted and discovered.get("eden", {}).get("db_path"):
        db_p = discovered["eden"]["db_path"]
        try:
            conn = _connect_sqlite_ro(db_p)
            room_names = {r[0]: str(r[1] or r[0]) for r in conn.execute("SELECT id, name FROM rooms").fetchall()}
            rows = conn.execute(
                "SELECT room_id, MAX(timestamp) AS last_ts, COUNT(*) FROM messages WHERE sender_id = 'WebUser' AND type = 'chat' GROUP BY room_id ORDER BY last_ts DESC LIMIT 30"
            ).fetchall()
            conn.close()
            for rid, last_ts, cnt in rows:
                rname = redact_all(room_names.get(rid, rid))
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
                        "ref": _format_chat_ref("eden", str(rid)),
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

    # 6. Hermes SQLite
    if "hermes" in wanted and discovered.get("hermes", {}).get("db_path"):
        db_p = discovered["hermes"]["db_path"]
        try:
            conn = _connect_sqlite_ro(db_p)
            norm_ts_expr = (
                "CASE WHEN COALESCE(last_activity_at, started_at, 0) > 1000000000000 "
                "THEN COALESCE(last_activity_at, started_at, 0) / 1000.0 "
                "ELSE COALESCE(last_activity_at, started_at, 0) END"
            )
            rows = conn.execute(
                f"SELECT id, COALESCE(title, id), ({norm_ts_expr}) AS ts, COALESCE(message_count, 0) "
                "FROM sessions WHERE COALESCE(message_count, 0) > 0 ORDER BY ts DESC LIMIT 80"
            ).fetchall()
            conn.close()
            for sid, stitle, ts, mcnt in rows:
                stitle_str = str(stitle or "")
                low_title = stitle_str.strip().lower()
                if low_title.startswith(("[eden runtime context]", "这是 ahub 的私有 ai 主动检查")):
                    continue
                sec = _ts_to_seconds(ts)
                date_str = _safe_fromtimestamp(sec, "%Y-%m-%d %H:%M", fallback="unknown")
                title = f"{redact_all(stitle_str)[:70]} ({mcnt} 条消息)"
                if q and (q not in title.lower() and q not in str(sid).lower()):
                    continue
                items.append(
                    {
                        "ref": _format_chat_ref("hermes", str(sid)),
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
    discovered = scan_all_local_sources(None)
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
                if not isinstance(obj, dict):
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
                if not isinstance(obj, dict):
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
                if not isinstance(obj, dict):
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
                conn = _connect_sqlite_ro(st)
                rows = conn.execute(
                    "SELECT value FROM ItemTable WHERE key LIKE 'memento/icube-ai-%chat-storage-%'"
                ).fetchall()
                conn.close()
                for (v,) in rows:
                    obj = json.loads(v)
                    if not isinstance(obj, dict):
                        continue
                    for s in obj.get("list", []):
                        if not isinstance(s, dict):
                            continue
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

        title = redact_all(str(target_session.get("title") or target_session.get("name") or id_prefix))
        created = target_session.get("updatedAt") or target_session.get("createdAt") or 0
        date_str = _safe_fromtimestamp(created, "%Y-%m-%d", fallback="recent")

        for m in target_session.get("messages", []):
            if not isinstance(m, dict):
                continue
            role = str(m.get("role", "")).lower()
            if role not in ("user", "assistant"):
                continue
            author_label = "User" if role == "user" else "Trae AI"
            content_raw = m.get("content") or m.get("text")
            content = extract_text_from_content(content_raw)
            # If assistant content is empty in builder mode, fallback to proposal
            if not content and role == "assistant":
                task_content = m.get("agentTaskContent")
                if isinstance(task_content, dict):
                    content = extract_text_from_content(
                        task_content.get("proposal") or task_content.get("proposalReasoningContent")
                    )
            cleaned = _clean_handoff_text(content, keep_code=keep_code)
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
                if not isinstance(obj, dict):
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
            if not isinstance(obj, dict):
                continue
            t = obj.get("type")
            if t == "user/message":
                data = obj.get("data") or {}
                if not isinstance(data, dict):
                    continue
                src_info = data.get("source")
                if isinstance(src_info, dict) and str(src_info.get("kind") or "") in ("agent-instructions", "runtime-context"):
                    continue
                raw = extract_text_from_content(data.get("content"))
                cleaned = _clean_handoff_text(raw, keep_code=keep_code)
                if cleaned:
                    turns.append(("User", cleaned))
            elif t == "assistant/message":
                data = obj.get("data") or {}
                msg = data.get("message") if isinstance(data, dict) else None
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
        conn = _connect_sqlite_ro(db_p)
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
            title = redact_all(str(obj.get("name") or cid[:12]))
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
        conn = _connect_sqlite_ro(db_p)
        try:
            row = conn.execute("SELECT id, data FROM conversations WHERE id LIKE ? ESCAPE '\\' LIMIT 1", (f"{escaped_prefix}%",)).fetchone()
            if not row:
                raise FileNotFoundError(f"未找到豆包工作会话: {id_prefix}")
            cid, cdata = row
            try:
                cobj = json.loads(cdata or "{}")
            except Exception:
                cobj = {}
            if not isinstance(cobj, dict):
                cobj = {}
            cname = redact_all(str(cobj.get("name") or cid))
            owner_id = str(cobj.get("ownerId") or "")
            mrows = conn.execute("SELECT data FROM messages WHERE conversationId = ? ORDER BY createdAt ASC LIMIT 200", (cid,)).fetchall()
        finally:
            conn.close()
        for (mdata,) in mrows:
            try:
                mobj = json.loads(mdata or "{}")
            except Exception:
                continue
            if not isinstance(mobj, dict):
                continue
            aid = str(mobj.get("authorId") or "")
            author = str(mobj.get("authorName") or aid or "AI")
            is_human = (
                aid.lower() in {"user", "human"}
                or author.lower() in {"you", "user"}
                or (owner_id and aid == owner_id)
            )
            role = "User" if is_human else author
            cleaned = _clean_handoff_text(str(mobj.get("text") or ""), keep_code=keep_code)
            if cleaned:
                turns.append((role, cleaned))
        return f"豆包工作 `{cname}` ({cid[:16]})", turns

    if src == "eden":
        db_p = discovered.get("eden", {}).get("db_path")
        if not db_p:
            raise FileNotFoundError("本机未找到 Eden ahub.db")
        escaped_prefix = id_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conn = _connect_sqlite_ro(db_p)
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
        return f"Eden 房间 `{redact_all(str(rname))}` ({rid[:16]})", turns

    if src == "hermes":
        db_p = discovered.get("hermes", {}).get("db_path")
        if not db_p:
            raise FileNotFoundError("本机未找到 Hermes state.db")
        escaped_prefix = id_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conn = _connect_sqlite_ro(db_p)
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
        return f"Hermes `{redact_all(str(stitle))}` ({str(sid)[:16]})", turns

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
    id_prefix = id_prefix.strip()
    if not id_prefix:
        print("❌ 会话 ID 前缀不能为空，格式须为 `<source>:<id前缀>`（请先运行 `tree chat list` 查看）。", file=sys.stderr)
        sys.exit(1)
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
