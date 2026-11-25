#!/usr/bin/env python3
"""
Tree Focus Engine — Situational Auto-Focus & Cross-Branch Resonance Layer.
Zero external dependencies (Python standard library only).

Architecture & Design Principles:
1. Decoupled from CLI glue: pure domain logic for probing environment signals,
   scoring cross-branch pages (01~04), and expanding 1-hop graph resonances.
2. Zero mandatory configuration: works out-of-the-box with existing Markdown
   content (frontmatter `location`, `when`, `tags`, `> 适用时机：` trigger line,
   `[[wikilinks]]`, and body path references), while supporting explicit `--cwd`
   and `--query` / `-q` focus lenses.
3. Three-Layer Situational Pipeline:
   - Layer 1 (Spatial & Scene Probe): inspects caller CWD (even outside Tree),
     Git remote/repo identity, and workspace file fingerprints (Python, Node/Electron,
     Web/Slides, Writing/Docs, Ops/Deploy).
   - Layer 2 (Direct Signal Scoring across 01~04): matches pages by explicit
     `location`, `when` rules, body path mentions, scene-to-trigger line (`适用时机`),
     and optional `--query` intent tokens.
   - Layer 3 (1-Hop Cross-Branch Resonance): once anchor pages are locked (e.g. a
     project in `03`), follows `[[wikilinks]]`, shared `#tags`, and shared workspace
     path mentions to pull in companion pages from `01` (assets/people),
     `02` (thinking scales), and `04` (working norms/SOPs).
"""

from __future__ import annotations

import dataclasses
import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path


WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
INLINE_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9_\-\u4e00-\u9fff]+)(?=\s|$|[，。；、,.!?:;])")
TRIGGER_LINE_RE = re.compile(r"^>\s*(?:适用时机|何时读|触发条件)[：:]\s*(.+)$", re.MULTILINE)
CJK_SEQ_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
ASCII_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_\-\.]{1,}", re.IGNORECASE)


# Lightweight scene taxonomy inferred from workspace files & query tokens.
# Maps scene_id -> (human_label, file_markers, extensions, trigger_keywords)
SCENE_TAXONOMY: dict[str, tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "python": (
        "Python 工程",
        ("pyproject.toml", "requirements.txt", "setup.py", "uv.lock", "Pipfile"),
        (".py",),
        ("python", "pip", "pytest", "cli", "标准库", "脚本"),
    ),
    "node_frontend": (
        "Node / 前端 / 桌面端工程",
        ("package.json", "tsconfig.json", "pnpm-lock.yaml", "bun.lock", "vite.config.ts", "next.config.ts"),
        (".ts", ".tsx", ".js", ".jsx", ".vue"),
        ("node", "electron", "react", "typescript", "前端", "桌面端", "npm", "打包", "发版"),
    ),
    "slides_canvas": (
        "演示文稿 / 画布 / Web Deck",
        ("slides.html", "deck.html"),
        (".pptx", ".key", ".excalidraw"),
        ("ppt", "演示", "演讲", "逐字稿", "讲稿", "画布", "excalidraw", "deck", "slide", "幻灯片"),
    ),
    "ops_deploy": (
        "部署运维 / 服务器环境",
        ("Dockerfile", "docker-compose.yml", "nginx.conf", "Caddyfile", "ecosystem.config.js"),
        (".sh", ".service"),
        ("部署", "运维", "服务器", "vps", "ecs", "阿里云", "nginx", "ssh", "端口", "上线", "release"),
    ),
    "writing_docs": (
        "写作 / 方案 / 内容创作",
        (),
        (".md", ".mdx", ".txt"),
        ("写作", "文章", "公众号", "稿件", "专栏", "排版", "文案", "复盘", "大纲"),
    ),
}


@dataclasses.dataclass(frozen=True)
class WorkspaceSignal:
    """Snapshot of the caller's live working environment and intent query."""

    cwd: Path
    is_inside_tree: bool
    git_root: Path | None
    git_remote: str
    repo_name: str
    top_files: tuple[str, ...]
    top_extensions: tuple[str, ...]
    detected_scenes: tuple[str, ...]
    query: str
    query_tokens: tuple[str, ...]


@dataclasses.dataclass
class PageNode:
    """In-memory representation of a Markdown context page in 01~04."""

    path: Path
    rel_path: str
    branch: str
    stem: str
    meta: dict[str, str]
    body: str
    updated_iso: str
    status: str
    age_days: int
    is_always: bool
    heat_score: float
    summary: str
    trigger_hint: str
    locations: tuple[str, ...]
    when_rules: tuple[str, ...]
    tags: frozenset[str]
    wikilinks: tuple[str, ...]


@dataclasses.dataclass
class FocusedHit:
    """A page selected by the Situational Auto-Focus engine."""

    node: PageNode
    score: float
    match_kind: str  # "anchor" | "scene" | "query" | "resonance"
    reasons: list[str]
    inline_preview: bool = False


@dataclasses.dataclass
class FocusBundle:
    """Complete situational focus result ready for L0 rendering."""

    signal: WorkspaceSignal
    hits: list[FocusedHit]
    matched_known_location: bool
    untracked_workspace_hint: str = ""

    @property
    def has_focus(self) -> bool:
        return bool(self.hits or self.untracked_workspace_hint)


def _normalize_path_str(raw: str) -> str:
    s = raw.strip().strip("`'\"")
    if not s:
        return ""
    if s.startswith("~"):
        s = os.path.expanduser(s)
    if s == "/":
        return "/"
    return s.rstrip("/")


def _split_multi_values(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    parts = [p.strip() for p in re.split(r"[,，;；\n]+", raw) if p.strip()]
    return tuple(parts)


def _extract_tags(meta: dict[str, str], body: str) -> frozenset[str]:
    tags: set[str] = set()
    raw_meta = meta.get("tags", "").strip()
    if raw_meta:
        for t in re.split(r"[,，\s]+", raw_meta):
            cleaned = t.strip(" []'\"#").lower()
            if cleaned and cleaned != "focus":
                tags.add(cleaned)
    for m in INLINE_TAG_RE.finditer(body):
        tok = m.group(1).strip().lower()
        if tok and tok not in {"focus", "note"}:
            tags.add(tok)
    return frozenset(tags)


def _extract_wikilinks(body: str) -> tuple[str, ...]:
    links: list[str] = []
    for m in WIKILINK_RE.finditer(body):
        target = m.group(1).strip()
        if target and target not in links:
            links.append(target)
    return tuple(links)


def _extract_trigger_hint(meta: dict[str, str], body: str) -> str:
    if meta.get("when"):
        return meta["when"].strip()
    m = TRIGGER_LINE_RE.search(body)
    if m:
        line = m.group(1).strip()
        # Keep only the trigger condition sentence before "核心..."
        line = re.split(r"(?:核心(?:定位|判断|事实|原则|身份|底线))[：:]", line)[0].strip()
        return line.rstrip("。；; ")
    return ""


def _tokenize_query(query: str) -> tuple[str, ...]:
    if not query or not query.strip():
        return ()
    q = query.strip().lower()
    tokens: list[str] = []

    def _add(tok: str) -> None:
        t = tok.strip()
        if len(t) >= 2 and t not in tokens:
            tokens.append(t)

    for w in ASCII_WORD_RE.findall(q):
        _add(w)

    # Split on common Chinese functional particles so phrases like "公众号的商业化与定价"
    # decompose cleanly into ("公众号", "商业化", "定价") without needing external NLP libraries.
    stopwords = {"什么", "怎么", "如何", "我们", "可以", "一个", "这个", "那个", "进行", "关于", "以及", "或者"}
    for seq in CJK_SEQ_RE.findall(q):
        sub_chunks = [c for c in re.split(r"[的与和及或在于对把被从向跟给就也都很要会能了着过]", seq) if len(c) >= 2]
        targets = sub_chunks if sub_chunks else [seq]
        for chunk in targets:
            if chunk not in stopwords:
                _add(chunk)
            if len(chunk) > 3:
                for i in range(len(chunk) - 1):
                    bi = chunk[i : i + 2]
                    if bi not in stopwords:
                        _add(bi)
    return tuple(tokens)


def _detect_git_info(cwd: Path) -> tuple[Path | None, str]:
    """Fast git root & remote URL detection without spawning subprocesses when possible."""
    curr = cwd
    home = Path.home().resolve()
    git_root: Path | None = None
    for candidate in (curr, *curr.parents):
        if (candidate / ".git").exists():
            git_root = candidate
            break
        if candidate == home:
            break
    if not git_root:
        return None, ""

    git_dir = git_root / ".git"
    config_path = git_dir / "config" if git_dir.is_dir() else None
    if config_path and config_path.is_file():
        try:
            cfg_text = config_path.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"^\s*url\s*=\s*(.+)$", cfg_text, re.MULTILINE)
            if m:
                return git_root, m.group(1).strip()
        except Exception:
            pass

    try:
        res = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(git_root),
            capture_output=True,
            text=True,
            timeout=0.5,
        )
        if res.returncode == 0:
            return git_root, (res.stdout or "").strip()
    except Exception:
        pass
    return git_root, ""


def probe_workspace(
    tree_root: Path,
    caller_cwd: Path | None = None,
    query: str = "",
) -> WorkspaceSignal:
    """Inspect caller's working directory and query to build a deterministic WorkspaceSignal."""
    cwd = (caller_cwd or Path.cwd()).expanduser().resolve()
    tree_resolved = tree_root.resolve()
    # Also recognize the parent tool repository when tree_root is `<repo>/vault`
    tool_repo_root = tree_resolved.parent if tree_resolved.name == "vault" else tree_resolved
    is_inside_tree = (
        cwd == tree_resolved
        or str(cwd).startswith(str(tree_resolved) + os.sep)
        or cwd == tool_repo_root
        or str(cwd).startswith(str(tool_repo_root) + os.sep)
    )

    git_root, git_remote = _detect_git_info(cwd)
    scan_dir = git_root or cwd
    repo_name = scan_dir.name

    top_files: list[str] = []
    ext_counts: dict[str, int] = {}
    try:
        for entry in sorted(scan_dir.iterdir(), key=lambda p: p.name):
            name = entry.name
            if name.startswith(".") and name not in {".env.example", ".gitignore"}:
                continue
            if entry.is_file():
                top_files.append(name)
                ext = entry.suffix.lower()
                if ext:
                    ext_counts[ext] = ext_counts.get(ext, 0) + 1
            elif entry.is_dir() and name in {"src", "app", "apps", "packages", "bin", "scripts", "slides"}:
                top_files.append(name)
                try:
                    for sub in entry.iterdir():
                        if sub.is_file() and sub.suffix:
                            ext = sub.suffix.lower()
                            ext_counts[ext] = ext_counts.get(ext, 0) + 1
                except Exception:
                    pass
    except Exception:
        pass

    sorted_exts = tuple(k for k, _ in sorted(ext_counts.items(), key=lambda kv: kv[1], reverse=True)[:6])
    q_tokens = _tokenize_query(query)

    scenes: list[str] = []
    top_files_set = set(top_files)
    q_lower = query.lower()
    for scene_id, (_label, markers, exts, kw_list) in SCENE_TAXONOMY.items():
        matched = False
        if markers and any(m in top_files_set for m in markers):
            matched = True
        elif exts and scene_id != "writing_docs" and any(e in ext_counts for e in exts):
            matched = True
        elif scene_id == "writing_docs" and not scenes and (".md" in ext_counts or ".txt" in ext_counts) and len(ext_counts) <= 2:
            matched = True
        if not matched and q_lower:
            if any(kw in q_lower for kw in kw_list):
                matched = True
        if matched and scene_id not in scenes:
            scenes.append(scene_id)

    return WorkspaceSignal(
        cwd=cwd,
        is_inside_tree=is_inside_tree,
        git_root=git_root,
        git_remote=git_remote,
        repo_name=repo_name,
        top_files=tuple(top_files[:25]),
        top_extensions=sorted_exts,
        detected_scenes=tuple(scenes),
        query=query.strip(),
        query_tokens=q_tokens,
    )


def _resolve_glob_pattern(pattern: str) -> str:
    """Resolve symlinks in the static directory prefix of a glob pattern (e.g. /tmp/client-* -> /private/tmp/client-*)."""
    p = Path(pattern)
    static_parts: list[str] = []
    glob_parts: list[str] = []
    hit_glob = False
    for part in p.parts:
        if not hit_glob and not any(ch in part for ch in ("*", "?", "[")):
            static_parts.append(part)
        else:
            hit_glob = True
            glob_parts.append(part)
    if static_parts:
        try:
            resolved_prefix = Path(*static_parts).expanduser().resolve()
            return str(resolved_prefix.joinpath(*glob_parts)) if glob_parts else str(resolved_prefix)
        except Exception:
            pass
    return pattern


def _keyword_in_text(kw: str, text_lower: str) -> bool:
    """Match keyword against lowercase text, enforcing word boundaries for short ASCII keywords (<=4 chars)."""
    k = kw.lower()
    if k.isascii() and len(k) <= 4:
        return bool(re.search(rf"(?<![a-z0-9_]){re.escape(k)}(?![a-z0-9_])", text_lower))
    return k in text_lower


def _path_matches_target(cwd: Path, git_root: Path | None, target_raw: str) -> bool:
    norm = _normalize_path_str(target_raw)
    if not norm or norm.startswith(("http://", "https://")):
        return False
    probe_paths = [cwd]
    if git_root and git_root != cwd:
        probe_paths.append(git_root)

    if any(ch in norm for ch in ("*", "?", "[")):
        resolved_norm = _resolve_glob_pattern(norm)
        return any(
            fnmatch.fnmatch(str(p), norm)
            or fnmatch.fnmatch(str(p), resolved_norm)
            or fnmatch.fnmatch(p.name, norm)
            for p in probe_paths
        )

    try:
        target_path = Path(norm).expanduser().resolve()
    except Exception:
        return False

    for p in probe_paths:
        # Match only when caller is AT target_path or INSIDE target_path (never when caller is a parent directory)
        if p == target_path or str(p).startswith(str(target_path) + os.sep):
            return True
    return False


def _body_mentions_workspace(body: str, cwd: Path, git_root: Path | None) -> str:
    """Check if page body explicitly mentions the current workspace path (with strict path-boundary check)."""
    candidates = [cwd]
    if git_root and git_root != cwd:
        candidates.append(git_root)
    home_str = str(Path.home().resolve())
    # A valid path mention in Markdown must end at EOF, path separator `/`, whitespace, backtick, quote, or punctuation
    boundary_tail = r"(?=$|[/\s`'\"，。；、,;)\]>])"
    for p in candidates:
        p_str = str(p)
        if len(p_str) > 4 and re.search(rf"{re.escape(p_str)}{boundary_tail}", body):
            return p_str
        if p_str.startswith(home_str):
            tilde_str = "~" + p_str[len(home_str) :]
            if len(tilde_str) > 3 and re.search(rf"{re.escape(tilde_str)}{boundary_tail}", body):
                return tilde_str
    return ""


def _match_when_rules(when_rules: tuple[str, ...], signal: WorkspaceSignal) -> list[str]:
    matched: list[str] = []
    top_set = set(signal.top_files)
    for rule in when_rules:
        r = rule.strip()
        if not r:
            continue
        r_low = r.lower()
        if r_low in signal.detected_scenes:
            matched.append(f"场景:{r}")
            continue
        if r.startswith(("/", "~")):
            if _path_matches_target(signal.cwd, signal.git_root, r):
                matched.append(f"路径:{r}")
                continue
        if any(fnmatch.fnmatch(f, r) for f in top_set) or any(
            r.startswith("*.") and r[1:].lower() in signal.top_extensions for _ in (1,)
        ):
            matched.append(f"特征文件:{r}")
            continue
        if signal.git_remote and _keyword_in_text(r, signal.git_remote.lower()):
            matched.append(f"仓库:{r}")
            continue
        if signal.query and _keyword_in_text(r, signal.query.lower()):
            matched.append(f"触发词:{r}")
    return matched


def _resolve_wikilink_node(
    link_target: str,
    nodes_by_rel: dict[str, PageNode],
    nodes_by_stem: dict[str, list[PageNode]],
    source_rel: str = "",
) -> PageNode | None:
    cleaned = link_target.strip().lstrip("/")
    if not cleaned:
        return None
    for candidate in (cleaned, f"{cleaned}.md"):
        if candidate in nodes_by_rel:
            return nodes_by_rel[candidate]
    if "/" in cleaned:
        suffix_md = f"/{cleaned}" if cleaned.endswith(".md") else f"/{cleaned}.md"
        matches = [node for rel, node in nodes_by_rel.items() if rel.endswith(suffix_md)]
        if len(matches) == 1:
            return matches[0]
    if source_rel and "/" in source_rel:
        parent_prefix = source_rel.rsplit("/", 1)[0] + "/"
        stem_name = Path(cleaned).stem
        sibling_rel = f"{parent_prefix}{stem_name}.md"
        if sibling_rel in nodes_by_rel:
            return nodes_by_rel[sibling_rel]
    stem = Path(cleaned).stem
    candidates = nodes_by_stem.get(stem, [])
    if len(candidates) == 1:
        return candidates[0]
    return None


def _node_display_name(node: PageNode) -> str:
    """Disambiguate generic stems like `项目总览` by prefixing their parent folder name."""
    parts = Path(node.rel_path).parts
    if len(parts) >= 3 and node.stem in {"项目总览", "总览", "INDEX", "SKILL"}:
        return f"{parts[-2]}/{node.stem}"
    return node.stem


def build_focus_bundle(
    nodes: list[PageNode],
    signal: WorkspaceSignal,
    max_hits: int = 6,
) -> FocusBundle:
    """
    Execute the 3-Layer Situational Auto-Focus & Cross-Branch Resonance pipeline.
    Only non-always, non-dormant pages are candidates for situational promotion
    (always-on pages are already in L0 Section 1, though their links/paths participate).
    """
    hits_by_rel: dict[str, FocusedHit] = {}
    anchor_nodes: list[PageNode] = []
    matched_known_location = False

    nodes_by_rel: dict[str, PageNode] = {n.rel_path: n for n in nodes}
    nodes_by_stem: dict[str, list[PageNode]] = {}
    for n in nodes:
        nodes_by_stem.setdefault(n.stem, []).append(n)

    def _record_hit(node: PageNode, delta_score: float, kind: str, reason: str, inline: bool = False) -> None:
        if node.is_always:
            return
        existing = hits_by_rel.get(node.rel_path)
        if existing:
            existing.score += delta_score
            if reason not in existing.reasons:
                existing.reasons.append(reason)
            if inline:
                existing.inline_preview = True
            if kind == "anchor":
                existing.match_kind = "anchor"
        else:
            hits_by_rel[node.rel_path] = FocusedHit(
                node=node,
                score=delta_score,
                match_kind=kind,
                reasons=[reason],
                inline_preview=inline,
            )

    # -------------------------------------------------------------------------
    # Layer 1 & 2: Spatial Anchor, When Rules, Scene Trigger & Query Scoring
    # -------------------------------------------------------------------------
    for node in nodes:
        for loc in node.locations:
            if _path_matches_target(signal.cwd, signal.git_root, loc):
                matched_known_location = True
                break
        if matched_known_location:
            break

    for node in nodes:
        # 2A. Explicit frontmatter `location:` match
        for loc in node.locations:
            if _path_matches_target(signal.cwd, signal.git_root, loc):
                if node not in anchor_nodes:
                    anchor_nodes.append(node)
                _record_hit(
                    node,
                    100.0,
                    "anchor",
                    f"当前工作区匹配 location `{loc}`",
                    inline=True,
                )

        # 2B. Body mentions current workspace path (e.g. `01-关于我/设备与资产.md`)
        mentioned_path = _body_mentions_workspace(node.body, signal.cwd, signal.git_root)
        if mentioned_path and not any(_path_matches_target(signal.cwd, signal.git_root, l) for l in node.locations):
            if node.branch.startswith("03-") and node not in anchor_nodes:
                anchor_nodes.append(node)
            _record_hit(
                node,
                65.0,
                "anchor",
                f"正文登记了当前工作目录 `{mentioned_path}`",
                inline=node.branch.startswith("03-"),
            )

        # 2C. Explicit frontmatter `when:` trigger rules
        if node.when_rules:
            when_matches = _match_when_rules(node.when_rules, signal)
            if when_matches:
                _record_hit(
                    node,
                    70.0,
                    "scene",
                    f"命中触发规则 ({', '.join(when_matches[:2])})",
                    inline=node.branch.startswith("04-"),
                )

        # 2D. Scene -> `> 适用时机：` / summary / tags matching (especially powerful for 04 & 01)
        # When an explicit project location is already locked, only auto-trigger 04 (working norms)
        # and 01 (environment assets) by scene so sibling projects in 03 aren't pulled in as noise.
        # Pages in `seed/` (external brain registry & curated collections) only trigger on explicit
        # query (-q), when rules, location, or [[wikilink]] resonance, never on generic file-extension scenes.
        if signal.detected_scenes and not node.is_always and node.branch != "seed":
            if not (matched_known_location and node.branch.startswith("03-")):
                haystack = f"{node.stem} {node.trigger_hint} {' '.join(node.tags)}".lower()
                for scene_id in signal.detected_scenes:
                    scene_label, _markers, _exts, kw_list = SCENE_TAXONOMY[scene_id]
                    matched_kws = [kw for kw in kw_list if _keyword_in_text(kw, haystack)]
                    min_kws = 1 if node.branch.startswith(("04-", "01-")) else 2
                    if len(matched_kws) >= min_kws:
                        _record_hit(
                            node,
                            35.0 + min(len(matched_kws) * 5.0, 20.0),
                            "scene",
                            f"现场特征 [{scene_label}] 匹配适用时机 ({' / '.join(matched_kws[:3])})",
                            inline=node.branch.startswith("04-"),
                        )

        # 2E. Query / Intent Lens (`tree context --query "..."`) across 01~04
        if signal.query_tokens and not node.is_always:
            q_score = 0.0
            matched_tokens: list[str] = []
            stem_low = node.stem.lower()
            rel_parts = Path(node.rel_path).parts
            # Only boost dedicated project/topic subfolders (depth >= 4, e.g. `03/工作/<项目>/项目总览.md`)
            # or Skill folders (`04/<skill>/SKILL.md`), never generic category dirs like `工作/` or `生活/`.
            is_skill_folder = len(rel_parts) == 3 and node.stem.upper() == "SKILL"
            folder_low = rel_parts[-2].lower() if (len(rel_parts) >= 4 or is_skill_folder) else ""
            sub_rel_low = "/".join(rel_parts[2:]).lower() if len(rel_parts) >= 4 else ""
            trig_low = node.trigger_hint.lower()
            sum_low = node.summary.lower()
            body_low = node.body.lower()
            tags_low = " ".join(node.tags).lower()

            for tok in signal.query_tokens:
                tok_hit = False
                if _keyword_in_text(tok, stem_low) or (folder_low and _keyword_in_text(tok, folder_low)):
                    q_score += 28.0
                    tok_hit = True
                elif sub_rel_low and _keyword_in_text(tok, sub_rel_low):
                    q_score += 20.0
                    tok_hit = True
                if _keyword_in_text(tok, tags_low):
                    q_score += 24.0
                    tok_hit = True
                if _keyword_in_text(tok, trig_low) or _keyword_in_text(tok, sum_low):
                    q_score += 18.0
                    tok_hit = True
                elif _keyword_in_text(tok, body_low):
                    q_score += 8.0
                    tok_hit = True
                if tok_hit and tok not in matched_tokens:
                    matched_tokens.append(tok)

            if q_score >= 16.0:
                _record_hit(
                    node,
                    q_score,
                    "query",
                    f"话题透镜命中「{' / '.join(matched_tokens[:4])}」",
                    inline=(q_score >= 40.0),
                )
                if q_score >= 45.0 and node not in anchor_nodes:
                    anchor_nodes.append(node)

    # -------------------------------------------------------------------------
    # Layer 3: 1-Hop Cross-Branch Resonance (Constellation Expansion)
    # -------------------------------------------------------------------------
    # Expand from locked anchor nodes via:
    # 1) Outgoing [[wikilinks]]
    # 2) Incoming [[wikilinks]]
    # 3) Shared #tags or shared domain concept tokens in title/trigger
    for anchor in list(anchor_nodes):
        anchor_label = _node_display_name(anchor)
        # 3A. Outgoing [[wikilinks]] from anchor
        for link_str in anchor.wikilinks:
            target_node = _resolve_wikilink_node(link_str, nodes_by_rel, nodes_by_stem, anchor.rel_path)
            if target_node and target_node.rel_path != anchor.rel_path:
                _record_hit(
                    target_node,
                    55.0,
                    "resonance",
                    f"由锚点 `{anchor_label}` 通过 [[双链]] 联动唤起",
                )

        # 3B. Incoming [[wikilinks]] & shared tags / concept resonance across different branches
        anchor_concepts = _tokenize_query(f"{anchor_label} {anchor.summary}")
        for peer in nodes:
            if peer.rel_path == anchor.rel_path or peer.is_always:
                continue
            # Check if peer links to anchor
            for link_str in peer.wikilinks:
                resolved = _resolve_wikilink_node(link_str, nodes_by_rel, nodes_by_stem, peer.rel_path)
                if resolved and resolved.rel_path == anchor.rel_path:
                    _record_hit(
                        peer,
                        50.0,
                        "resonance",
                        f"反向 [[双链]] 关联至当前锚点 `{anchor_label}`",
                    )

            # Cross-branch tag resonance (e.g., 03 project shares #tag with 02 thinking or 04 skill)
            if peer.branch != anchor.branch and anchor.tags and peer.tags:
                shared_tags = sorted(anchor.tags & peer.tags)
                if shared_tags:
                    _record_hit(
                        peer,
                        42.0,
                        "resonance",
                        f"与 `{anchor_label}` 跨分支共享标签 #{shared_tags[0]}",
                    )

            # Cross-branch domain concept resonance (e.g. 03 `Tree产品定位` <-> 02 `个人上下文系统核心判断`)
            # Never auto-resonate from one 03 project into a different 03 project unless explicitly wikilinked.
            if (
                peer.branch != anchor.branch
                and not (anchor.branch.startswith("03-") and peer.branch.startswith("03-"))
                and anchor_concepts
            ):
                peer_header = f"{peer.stem} {peer.trigger_hint} {peer.summary}".lower()
                generic_terms = {"项目", "产品", "系统", "工作", "决策", "规范", "定位", "架构", "设计", "核心", "判断", "web", "ai", "skill"}
                shared_concepts = [
                    c for c in anchor_concepts
                    if len(c) >= 3 and c not in generic_terms and _keyword_in_text(c, peer_header)
                ]
                if shared_concepts:
                    _record_hit(
                        peer,
                        32.0,
                        "resonance",
                        f"与 `{anchor_label}` 跨维概念共振（「{shared_concepts[0]}」）",
                    )

    sorted_hits = sorted(
        hits_by_rel.values(),
        key=lambda h: (h.score, h.node.heat_score, h.node.updated_iso, h.node.rel_path),
        reverse=True,
    )[:max_hits]

    # Detect untracked active workspace (outside Tree, has git or project markers, not matched in 03)
    untracked_hint = ""
    home_dir = Path.home().resolve()
    cwd_str = str(signal.cwd)
    is_ephemeral_or_root = (
        signal.cwd == home_dir
        or signal.cwd == home_dir / "Desktop"
        or signal.cwd == home_dir / "Downloads"
        or cwd_str in {"/", "/tmp", "/private/tmp", "/var/tmp"}
        or cwd_str.startswith(("/tmp/", "/private/tmp/", "/var/folders/"))
    )
    if (
        not matched_known_location
        and not signal.is_inside_tree
        and not is_ephemeral_or_root
        and (signal.git_root is not None or bool(signal.detected_scenes))
    ):
        target_dir = signal.git_root or signal.cwd
        scene_names = [SCENE_TAXONOMY[s][0] for s in signal.detected_scenes]
        scene_str = f"（识别为 {' / '.join(scene_names)}）" if scene_names else ""
        untracked_hint = (
            f"当前工作目录 `{target_dir}`{scene_str} 尚未在 `03-我正在做的事/` 中绑定 `location`。"
            f"若本次对话产生该项目的长期定位或关键决策，可在达成共识后通过 "
            f"`tree write \"03-我正在做的事/工作/<项目名>/项目总览.md\" --kind project --location \"{target_dir}\"` 纳入追踪。"
        )

    return FocusBundle(
        signal=signal,
        hits=sorted_hits,
        matched_known_location=matched_known_location,
        untracked_workspace_hint=untracked_hint,
    )


def format_focus_section(
    bundle: FocusBundle,
    max_inline_pages: int = 2,
    max_inline_lines: int = 16,
) -> list[str]:
    """Render the Situational Auto-Focus block for `tree context` L0 output."""
    if not bundle.has_focus:
        return []

    sig = bundle.signal
    out: list[str] = []
    header_Badges: list[str] = [f"工作区: `{sig.cwd}`"]
    if sig.detected_scenes:
        scene_labels = [SCENE_TAXONOMY[s][0] for s in sig.detected_scenes]
        header_Badges.append(f"现场特征: {' / '.join(scene_labels)}")
    if sig.query:
        header_Badges.append(f"话题透镜: 「{sig.query}」")

    out.append(f"## 🎯 当前情境自动对焦（{' · '.join(header_Badges)}）")

    if bundle.hits:
        out.append("> 已根据当前现场与跨分支关联（`01` 资产/人际 ↔ `02` 思考尺子 ↔ `03` 战局 ↔ `04` 规矩）自动调出以下按需专页：")
        out.append("")
        inlined_count = 0
        for hit in bundle.hits:
            node = hit.node
            branch_short = node.branch.split("-", 1)[0] if "-" in node.branch else node.branch
            kind_badge = {
                "anchor": "📍空间锚点",
                "scene": "🛠️现场规矩/环境",
                "query": "🔎话题命中",
                "resonance": "🔗跨维共振",
            }.get(hit.match_kind, "🎯自动对焦")
            reason_str = "；".join(hit.reasons[:2])
            out.append(
                f"- **{kind_badge} [{branch_short}]** `{node.rel_path}` [{node.updated_iso}] — {node.summary}"
            )
            out.append(f"  - *触发依据*: {reason_str}")

            if hit.inline_preview and inlined_count < max_inline_pages:
                inlined_count += 1
                body_lines = [ln for ln in node.body.strip().splitlines() if ln.strip()]
                preview_lines = body_lines[:max_inline_lines]
                for pl in preview_lines:
                    out.append(f"  > {pl}")
                if len(body_lines) > max_inline_lines:
                    out.append(
                        f"  > …[余 {len(body_lines) - max_inline_lines} 行，精读全文: `tree read \"{node.rel_path}\"`]"
                    )
        out.append("")

    if bundle.untracked_workspace_hint:
        out.append(f"> 💡 **[新现场感知]**：{bundle.untracked_workspace_hint}")
        out.append("")

    return out


# =============================================================================
# Global Instruction Managed Block Sync (L0-Static Zero-Latency Anchor)
# Note: `TREE-CONTEXT:START/END` markers are intentionally preserved for backward
# compatibility with existing host global instruction files across upgrades.
# =============================================================================

BLOCK_START_MARKER = "<!-- TREE-CONTEXT:START -->"
BLOCK_END_MARKER = "<!-- TREE-CONTEXT:END -->"
MANAGED_BLOCK_RE = re.compile(
    rf"{re.escape(BLOCK_START_MARKER)}[\s\S]*?{re.escape(BLOCK_END_MARKER)}\n?",
    re.MULTILINE,
)


@dataclasses.dataclass(frozen=True)
class GlobalInstructionTarget:
    """Host agent global instruction file specification."""

    label: str
    host_dir: Path
    file_path: Path


def get_global_instruction_targets(home: Path | None = None) -> list[GlobalInstructionTarget]:
    """
    Return supported host agents and their native global instruction Markdown paths.
    Only hosts whose `host_dir` already exists on the user's machine are eligible.
    Includes dynamic discovery for multi-account hosts such as 豆包工作 (Douchat).
    """
    h = (home or Path.home()).expanduser().resolve()
    targets: list[GlobalInstructionTarget] = [
        GlobalInstructionTarget("Claude Code", h / ".claude", h / ".claude" / "CLAUDE.md"),
        GlobalInstructionTarget("Codex", h / ".codex", h / ".codex" / "AGENTS.md"),
        GlobalInstructionTarget("Gemini / Antigravity", h / ".gemini", h / ".gemini" / "GEMINI.md"),
        GlobalInstructionTarget("Hermes Agent", h / ".hermes", h / ".hermes" / "SOUL.md"),
        GlobalInstructionTarget("OpenCode", h / ".config" / "opencode", h / ".config" / "opencode" / "AGENTS.md"),
        GlobalInstructionTarget("Windsurf", h / ".codeium" / "windsurf", h / ".codeium" / "windsurf" / "memories" / "global_rules.md"),
        GlobalInstructionTarget("Universal Agents", h / ".agents", h / ".agents" / "AGENTS.md"),
        GlobalInstructionTarget("WorkBuddy", h / ".workbuddy", h / ".workbuddy" / "MEMORY.md"),
        GlobalInstructionTarget("WorkBuddy (USER.md)", h / ".workbuddy", h / ".workbuddy" / "USER.md"),
        GlobalInstructionTarget("OpenClaw", h / ".openclaw", h / ".openclaw" / "AGENTS.md"),
    ]

    # Dynamic discovery for 豆包工作 (Douchat) account-scoped shared/MEMORY.md
    douchat_bases = [
        h / "Library" / "Application Support" / "douchat",
        h / ".config" / "douchat",
        h / "AppData" / "Roaming" / "douchat",
    ]
    for d_base in douchat_bases:
        accounts_dir = d_base / "accounts"
        if not accounts_dir.is_dir():
            continue
        shared_dirs = sorted(p for p in accounts_dir.glob("*/shared") if p.is_dir())
        for idx, s_dir in enumerate(shared_dirs):
            suffix = f" #{idx + 1}" if len(shared_dirs) > 1 else ""
            targets.append(
                GlobalInstructionTarget(
                    f"豆包工作 (Douchat{suffix})",
                    s_dir,
                    s_dir / "MEMORY.md",
                )
            )
        break

    return targets


def render_global_instruction_block(
    tree_root: Path,
    always_pages: list[tuple[str, str, str]],
    max_lines_per_page: int = 24,
) -> str:
    """
    Build the self-contained `<!-- TREE-CONTEXT:START --> ... <!-- TREE-CONTEXT:END -->` block.
    `always_pages` is a list of `(rel_path, body, updated_iso)` for active `always: true` pages.
    """
    default_vault = Path(__file__).resolve().parent.parent.parent.parent / "vault"
    is_custom_vault = tree_root.resolve() != default_vault.resolve()
    env_prefix = f'TREE_HOME="{tree_root}" ' if is_custom_vault else ""
    lines: list[str] = [
        BLOCK_START_MARKER,
        "## 🌱 Tree 个人上下文系统（L0 常驻底线与情境唤醒契约）",
        f"> **Tree 是用户自有的个人上下文系统**（本地 Markdown + Git 仓库：`{tree_root}`），按 `01-关于我 / 02-我的思考 / 03-我正在做的事 / 04-我如何工作` 维护一棵人机共读的个人知识树，只沉淀模型原本不知道的个人真实信息与判断，供所有 AI 工具共享消费、也由用户直接阅读和演化。请勿手动编辑本受控块，内容变更统一走 `tree write`：",
        "> - **第 0 秒底线**：下述 `always: true` 核心画像与全局协作底线在所有对话中默认生效，无需调用工具即可直接遵守。",
        f"> - **第 1 秒情境点亮**：当对话涉及具体项目、代码仓库、方案写作、部署运维或历史决策时，**首轮立即执行** `{env_prefix}tree context -q \"<当前任务或话题关键词>\"`（若 `tree` 未在 PATH 中则调用 `{env_prefix}~/.local/bin/tree`），系统会自动根据当前工作目录（`cwd`）与话题跨 `01~04` 点亮专属上下文包。",
        f"> - **上下文演进与写回（更新到 Tree，不散落在宿主私有区）**：Tree 不是单一客户端的附属记忆，而是用户跨所有 AI 共用的个人上下文底座——写进 Tree 的内容会按结构落到对应专页、保留 Git 历史、可供用户直接审阅，并自动同步给本机其他 AI 客户端；若只写进当前宿主的私有区域（如 `USER.md` / `MEMORY.md` / 内置 memory 工具），信息会退化成被锁死在单一工具里的黑盒碎片。每当对话中出现新的个人事实、重要思考判断、项目拍板、对 AI 的明确纠偏，或外部上下文源增减（统一由 AI 替用户在 `seed/外部上下文源.md` 动态维护）时，**先执行 `{env_prefix}~/.local/bin/tree rules`（或 `tree read README.md`），再用 `{env_prefix}~/.local/bin/tree write` 原地更新到对应专页**，不要只记在宿主私有记忆里。",
        f"> - **无 Shell / 沙盒异常降级通道**：仅当宿主无 Shell 工具或 Shell 环境异常（如沙盒返回 `Exit Code: 127`）无法运行 `tree` CLI 时，允许降级使用宿主内置的文件工具直接先读 `{tree_root}/README.md` 再原地编辑 `{tree_root}/01~04` 下的对应 `.md` 专页（`tree` 下次运行时会自动检测未提交变更并完成 Git 提交与全端同步）。",
        "",
    ]

    if always_pages:
        for rel_path, body, updated_iso in always_pages:
            lines.append(f"### 📄 `{rel_path}` [{updated_iso}]")
            body_lines = [ln for ln in body.strip().splitlines() if ln.strip()]
            snippet = body_lines[:max_lines_per_page]
            lines.extend(snippet)
            if len(body_lines) > max_lines_per_page:
                lines.append(
                    f"…[余 {len(body_lines) - max_lines_per_page} 行，完整内容见 `tree read \"{rel_path}\"`]"
                )
            lines.append("")
    else:
        lines.append("> *(当前知识库尚未生成 `always: true` 常驻页，可在对话中执行 `tree context` 启动破土初始化)*")
        lines.append("")

    lines.append(BLOCK_END_MARKER)
    return "\n".join(lines) + "\n"


def upsert_managed_block_in_text(existing_text: str, block_text: str) -> str:
    """
    Idempotently replace or append the managed Tree block without touching user content outside it.
    - Uses a callable lambda replacement in `re.sub` so backslashes in `block_text` (e.g. `\\1`, `\\u4e00`, `C:\\Users`)
      are NEVER interpreted as regex escape sequences.
    - Deduplicates multiple managed blocks if present, and strips orphan start/end markers if corrupted.
    """
    normalized = existing_text.replace("\r\n", "\n")
    block_clean = block_text.rstrip("\n") + "\n"

    if MANAGED_BLOCK_RE.search(normalized):
        replaced_first = False

        def _repl(_match: re.Match[str]) -> str:
            nonlocal replaced_first
            if not replaced_first:
                replaced_first = True
                return block_clean
            return ""

        return MANAGED_BLOCK_RE.sub(_repl, normalized)

    # Clean up any corrupted orphan markers (e.g., END before START, or unclosed START) so user lines remain safe
    if BLOCK_START_MARKER in normalized or BLOCK_END_MARKER in normalized:
        kept_lines = [
            ln
            for ln in normalized.splitlines()
            if ln.strip() not in {BLOCK_START_MARKER, BLOCK_END_MARKER}
        ]
        normalized = "\n".join(kept_lines)

    stripped = normalized.rstrip()
    if not stripped:
        return block_clean
    return f"{stripped}\n\n{block_clean}"


def remove_managed_block_from_text(existing_text: str) -> tuple[str, bool]:
    """
    Remove the managed Tree block (or orphan markers) from text, returning (new_text, removed_bool).
    Preserves user whitespace and consecutive blank lines outside the removed block seam.
    """
    normalized = existing_text.replace("\r\n", "\n")
    has_closed_block = bool(MANAGED_BLOCK_RE.search(normalized))
    has_orphan_marker = (BLOCK_START_MARKER in normalized or BLOCK_END_MARKER in normalized)
    if not has_closed_block and not has_orphan_marker:
        return existing_text, False

    # Remove closed managed blocks along with up to one preceding blank line at the seam
    seam_re = re.compile(
        rf"(?:\n)?{re.escape(BLOCK_START_MARKER)}[\s\S]*?{re.escape(BLOCK_END_MARKER)}\n?",
        re.MULTILINE,
    )
    cleaned = seam_re.sub("", normalized)
    if BLOCK_START_MARKER in cleaned or BLOCK_END_MARKER in cleaned:
        cleaned = "\n".join(
            ln
            for ln in cleaned.splitlines()
            if ln.strip() not in {BLOCK_START_MARKER, BLOCK_END_MARKER}
        )
    if not cleaned.strip():
        return "", True
    return cleaned.rstrip(" \t\n") + "\n", True


def check_and_repair_shell_rc_path(
    home: Path | None = None,
    dry_run: bool = False,
) -> list[tuple[Path, str]]:
    """
    Universal environment self-healing:
    Detect and repair broken `$(npm bin -g)` calls in `~/.zshrc` / `~/.bashrc` / `~/.zprofile`
    that output `Unknown command: "bin"` on npm v9+ and corrupt GUI agents' (e.g. WorkBuddy)
    PATH environment variable, causing every sandboxed shell command to fail with Exit Code 127.
    """
    h = (home or Path.home()).expanduser().resolve()
    rc_candidates = [h / ".zshrc", h / ".zprofile", h / ".bashrc", h / ".bash_profile"]
    repaired: list[tuple[Path, str]] = []
    broken_pattern = re.compile(r"\$\(\s*npm\s+bin\s+-g\s*\)")

    for rc in rc_candidates:
        if not rc.is_file():
            continue
        try:
            text = rc.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not broken_pattern.search(text):
            continue
        new_text = broken_pattern.sub("$(npm prefix -g 2>/dev/null)/bin", text)
        if new_text != text:
            if not dry_run:
                rc.write_text(new_text, encoding="utf-8")
            repaired.append((rc, "已将失效的 `$(npm bin -g)` 升级为兼容 npm v9+ 的 `$(npm prefix -g 2>/dev/null)/bin`（修复 GUI Agent 沙盒 PATH 丢失与 Exit 127）"))
    return repaired


def ensure_workbuddy_sandbox_allow_write(
    tree_root: Path,
    home: Path | None = None,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """
    Ensure WorkBuddy's `~/.workbuddy/settings.json` includes `tree_root` and `~/.local/bin`
    in `sandbox.extraAllowWrite` so WorkBuddy can write and commit to the Tree vault cleanly.
    """
    h = (home or Path.home()).expanduser().resolve()
    wb_settings = h / ".workbuddy" / "settings.json"
    if not wb_settings.is_file():
        return False, ""
    try:
        data = json.loads(wb_settings.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False, ""
    except Exception:
        return False, ""

    sbx = data.get("sandbox")
    if not isinstance(sbx, dict):
        sbx = {}
        data["sandbox"] = sbx
    allow_list = sbx.get("extraAllowWrite")
    if not isinstance(allow_list, list):
        allow_list = []
        sbx["extraAllowWrite"] = allow_list

    # Format tree_root as `~/...` if inside home, plus parent repo root if tree_root is `<repo>/vault`
    tree_abs = str(tree_root.resolve())
    home_str = str(h)
    tree_entry = ("~" + tree_abs[len(home_str) :]) if tree_abs.startswith(home_str + os.sep) else tree_abs
    entries_to_allow = [tree_entry]
    if tree_root.resolve().name == "vault":
        parent_abs = str(tree_root.resolve().parent)
        parent_entry = ("~" + parent_abs[len(home_str) :]) if parent_abs.startswith(home_str + os.sep) else parent_abs
        entries_to_allow.append(parent_entry)
    entries_to_allow.append("~/.local/bin")

    added: list[str] = []
    for entry in entries_to_allow:
        if entry not in allow_list:
            allow_list.append(entry)
            added.append(entry)

    if not added:
        return False, ""
    if not dry_run:
        wb_settings.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True, f"已向 `~/.workbuddy/settings.json` 沙盒白名单（extraAllowWrite）添加: {', '.join(added)}"


def sync_global_instructions(

    tree_root: Path,
    always_pages: list[tuple[str, str, str]],
    home: Path | None = None,
    dry_run: bool = False,
    remove: bool = False,
    only_existing_blocks: bool = False,
) -> list[tuple[str, Path, str]]:
    """
    Synchronize (or remove) the L0-Static managed block across installed host agents.
    - When `only_existing_blocks=True` (used by `tree write/delete/move` auto-refresh),
      only updates files that ALREADY contain `<!-- TREE-CONTEXT:START -->`.
    - Returns list of `(label, file_path, status_str)` where status_str is one of:
      `"created" | "updated" | "unchanged" | "removed" | "skipped"`.
    """
    targets = get_global_instruction_targets(home=home)
    block_text = render_global_instruction_block(tree_root, always_pages) if not remove else ""
    results: list[tuple[str, Path, str]] = []

    for tgt in targets:
        if not tgt.host_dir.is_dir():
            continue
        fp = tgt.file_path
        existed = fp.is_file()
        old_text = fp.read_text(encoding="utf-8", errors="ignore") if existed else ""
        has_block = BLOCK_START_MARKER in old_text and BLOCK_END_MARKER in old_text

        if remove:
            if not existed or not has_block:
                continue
            new_text, removed = remove_managed_block_from_text(old_text)
            if removed:
                if not dry_run:
                    fp.write_text(new_text, encoding="utf-8")
                results.append((tgt.label, fp, "removed"))
            continue

        if only_existing_blocks:
            if not has_block:
                continue
            # Guard against temporary/sandboxed TREE_HOME tests overwriting the host's mounted production vault block
            expected_root_tag = f"本地 Markdown + Git 仓库：`{tree_root}`"
            if "本地 Markdown + Git 仓库：`" in old_text and expected_root_tag not in old_text:
                continue

        new_text = upsert_managed_block_in_text(old_text, block_text)
        if existed and new_text == old_text.replace("\r\n", "\n"):
            results.append((tgt.label, fp, "unchanged"))
            continue

        status = "updated" if existed else "created"
        if not dry_run:
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(new_text, encoding="utf-8")
        results.append((tgt.label, fp, status))

    return results

