#!/usr/bin/env python3
"""Comprehensive regression tests for Tree CLI, tree_seed, and tree_focus (stdlib unittest only)."""

from __future__ import annotations

import datetime
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "tree" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import tree_focus  # noqa: E402
import tree_seed  # noqa: E402


def _load_tree_cli_module():
    cli_path = SCRIPTS_DIR / "tree"
    loader = importlib.machinery.SourceFileLoader("tree_cli", str(cli_path))
    spec = importlib.util.spec_from_loader("tree_cli", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


tree_cli = _load_tree_cli_module()


class TestTreeCore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.vault = self.tmp_path / "vault"
        self.vault.mkdir(parents=True, exist_ok=True)
        # Initialize a clean vault
        tree_cli.cmd_init(self.vault, type("Args", (), {})())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_cli(self, *args: str, stdin_data: str | None = None, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        for k in (
            "TREE_AGENT", "AHUB_AGENT_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT",
            "CODEX_CI", "CODEX_SANDBOX", "CODEX_THREAD_ID", "CODEX_SHELL",
            "ANTIGRAVITY_AGENT", "ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_CLI_ALIAS",
            "GEMINI_CLI", "GEMINI_API_KEY_SOURCE",
            "CURSOR_TRACE_ID", "CURSOR_AGENT", "CURSOR_SESSION_ID",
            "TRAE_AGENT", "TRAE_IDE", "WINDSURF_AGENT", "CODEIUM_EDITOR",
            "OPENCODE", "OPENCODE_SESSION", "HERMES_AGENT", "HERMES_SESSION",
            "WORKBUDDY_SESSION", "WORKBUDDY_AGENT", "OPENCLAW_SESSION", "OPENCLAW_AGENT",
            "TERM_PROGRAM",
        ):
            env.pop(k, None)
        env["TREE_HOME"] = str(self.vault)
        env["TREE_CONFIG_HOME"] = str(self.tmp_path / "tree_cfg")
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "tree"), *args],
            input=stdin_data,
            capture_output=True,
            text=True,
            env=env,
        )

    def _config_git_identity(self) -> None:
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(self.vault), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.vault), check=True)

    def test_safe_resolve_blocks_prefix_collision_and_traversal(self) -> None:
        sibling = self.tmp_path / "vault_other"
        sibling.mkdir()
        (sibling / "secret.md").write_text("secret", encoding="utf-8")

        ok_path = tree_cli.safe_resolve(self.vault, "01-关于我/基本信息.md")
        self.assertTrue(ok_path.is_relative_to(self.vault.resolve()))

        with self.assertRaises(ValueError):
            tree_cli.safe_resolve(self.vault, "../vault_other/secret.md")
        with self.assertRaises(ValueError):
            tree_cli.safe_resolve(self.vault, "../../etc/passwd")

    def test_upsert_frontmatter_handles_empty_and_trailing_spaces(self) -> None:
        empty_fm = "---\n---\n\n# 标题\n正文\n"
        out_empty = tree_cli.upsert_frontmatter(empty_fm, "2026-11-25")
        self.assertEqual(out_empty.count("\n---\n"), 1)
        meta, body = tree_cli.parse_frontmatter(out_empty)
        self.assertEqual(meta.get("updated"), "2026-11-25")
        self.assertIn("# 标题", body)

        trailing_fm = "---\nstatus: active\n---   \n\n# 标题2\n"
        out_trailing = tree_cli.upsert_frontmatter(trailing_fm, "2026-11-25")
        self.assertEqual(out_trailing.count("---"), 2)
        meta2, body2 = tree_cli.parse_frontmatter(out_trailing)
        self.assertEqual(meta2.get("status"), "active")
        self.assertEqual(meta2.get("updated"), "2026-11-25")
        self.assertIn("# 标题2", body2)

    def test_readme_gate_requires_same_agent_and_rejects_future_ts(self) -> None:
        # 1. Without reading rules/README, write must fail
        res_fail = self._run_cli("write", "02-我的思考/test.md", "# 测试\n内容", "--agent", "agent-a")
        self.assertNotEqual(res_fail.returncode, 0)

        # 2. Agent-A reads live README rules directly (single source of truth, no hardcoded cap)
        res_rules = self._run_cli("rules", "--agent", "agent-a")
        self.assertEqual(res_rules.returncode, 0)
        self.assertIn("Tree 维护宪法", res_rules.stdout)
        self.assertIn("写权分级", res_rules.stdout)

        # 3. Agent-B still cannot bypass gate using Agent-A's read
        res_b = self._run_cli("write", "02-我的思考/test.md", "# 测试\n内容", "--agent", "agent-b")
        self.assertNotEqual(res_b.returncode, 0)

        # 4. Future timestamp cannot bypass gate
        readme_hash = tree_cli.get_readme_hash(self.vault / "README.md")
        future_ts = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat()
        tree_cli.append_jsonl(
            self.vault / ".tree" / "audit.jsonl",
            {
                "timestamp": future_ts,
                "action": "read",
                "target": "README.md",
                "agent": "agent-future",
                "detail": f"sha256:{readme_hash}",
            },
        )
        res_future = self._run_cli("write", "02-我的思考/test.md", "# 测试\n内容", "--agent", "agent-future")
        self.assertNotEqual(res_future.returncode, 0)

        # 5. Agent-A succeeds
        self._config_git_identity()
        res_a = self._run_cli("write", "02-我的思考/test.md", "# 测试\n内容足够长一些", "--agent", "agent-a")
        self.assertEqual(res_a.returncode, 0, msg=res_a.stderr)
        self.assertTrue((self.vault / "02-我的思考" / "test.md").is_file())

    def test_git_commit_failure_is_reported_honestly(self) -> None:
        self._run_cli("rules", "--agent", "cli-user")
        # Force git commit failure using an empty GIT_CONFIG_GLOBAL and missing local user.name/email
        clean_env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_AUTHOR_NAME": "", "GIT_COMMITTER_NAME": ""}
        res = self._run_cli("write", "02-我的思考/nogit.md", "# 测试\n正文", env_extra=clean_env)
        self.assertEqual(res.returncode, 0)
        self.assertIn("Git 提交失败", res.stderr)
        self.assertNotIn("并完成 Git 版本提交", res.stdout)

    def test_move_delete_directory_guard_and_corrupt_audit_log(self) -> None:
        self._config_git_identity()
        self._run_cli("rules")
        self._run_cli("write", "02-我的思考/a.md", "# A\n参考 [[b]]")
        self._run_cli("write", "02-我的思考/b.md", "# B\n内容")

        # Move b -> c updates wikilink in a.md
        mv_res = self._run_cli("move", "02-我的思考/b.md", "02-我的思考/c.md")
        self.assertEqual(mv_res.returncode, 0, msg=mv_res.stderr)
        self.assertIn("[[c]]", (self.vault / "02-我的思考" / "a.md").read_text(encoding="utf-8"))

        # Delete directory should fail cleanly instead of IsADirectoryError
        sub = self.vault / "02-我的思考" / "subdir"
        sub.mkdir()
        del_dir = self._run_cli("delete", "02-我的思考/subdir")
        self.assertNotEqual(del_dir.returncode, 0)
        self.assertIn("不支持直接删除目录", del_dir.stderr)

        # Corrupt audit.jsonl line should not crash `tree log` or `tree log --audit`
        with open(self.vault / ".tree" / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write("{broken-json\n\n")
        log_res = self._run_cli("log", "-n", "5")
        self.assertEqual(log_res.returncode, 0, msg=log_res.stderr)
        log_audit_res = self._run_cli("log", "-n", "5", "--audit")
        self.assertEqual(log_audit_res.returncode, 0, msg=log_audit_res.stderr)

    def test_context_respects_budget_cap(self) -> None:
        self._config_git_identity()
        self._run_cli("rules")
        for i in range(40):
            p = self.vault / "02-我的思考" / f"topic-{i:02d}.md"
            p.write_text(f"---\nupdated: 2026-11-25\n---\n\n# Topic {i}\n> 适用时机：测试话题 {i}\n" + ("内容 " * 40), encoding="utf-8")
        res = self._run_cli("context")
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertLess(len(res.stdout), tree_cli.DEFAULT_L0_MAX_CHARS + 3000)

    def test_tree_seed_redaction_and_incremental_skip(self) -> None:
        raw = (
            "Cookie: sessionid=abcdef1234567890xyz\n"
            "token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTYifQ.signature123456\n"
            "key=LTAI5tAbCdEfGhIjKlMnOpQr\n"
            "phone=13812345678 id=11010119900101123X\n"
        )
        cleaned = tree_seed.clean_chat_text(raw)
        self.assertNotIn("abcdef1234567890xyz", cleaned)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", cleaned)
        self.assertNotIn("LTAI5tAbCdEfGhIjKlMnOpQr", cleaned)
        self.assertNotIn("13812345678", cleaned)
        self.assertIn("138****5678", cleaned)
        self.assertNotIn("11010119900101123X", cleaned)

        # Test incremental skip_ids in extract_jsonl_source_chats
        chat_file = self.tmp_path / "sess.jsonl"
        chat_file.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")
        fake_parser = lambda p: ("2026-11-25", "sess", ["你好测试消息"])  # noqa: E731
        _, cnt1, pids1 = tree_seed.extract_jsonl_source_chats(
            "claude", "Claude", [str(chat_file)], fake_parser, days=30, max_sessions=10
        )
        self.assertEqual(cnt1, 1)
        self.assertEqual(len(pids1), 1)
        _, cnt2, pids2 = tree_seed.extract_jsonl_source_chats(
            "claude", "Claude", [str(chat_file)], fake_parser, days=30, max_sessions=10, skip_ids=set(pids1)
        )
        self.assertEqual(cnt2, 0)
        self.assertEqual(pids2, [])

    def test_tree_focus_word_boundary_and_root_norm(self) -> None:
        self.assertEqual(tree_focus._normalize_path_str("/"), "/")
        sig = tree_focus.WorkspaceSignal(
            cwd=self.vault,
            is_inside_tree=True,
            git_root=None,
            git_remote="",
            repo_name="Tree",
            top_files=("scripts",),
            top_extensions=(".py",),
            detected_scenes=(),
            query="build pipeline",
            query_tokens=("build", "pipeline"),
        )
        # Short keyword `ui` must NOT substring-match `build`
        self.assertEqual(tree_focus._match_when_rules(("ui",), sig), [])
        # Directory name `scripts` without trailing slash matches
        self.assertEqual(tree_focus._match_when_rules(("scripts",), sig), ["特征文件:scripts"])

    def test_rules_surfaces_custom_section_only_when_filled(self) -> None:
        # Default placeholder should NOT trigger section 3
        default_res = self._run_cli("rules", "--agent", "cli-user")
        self.assertEqual(default_res.returncode, 0)
        self.assertNotIn("我的个性化定制规则", default_res.stdout)

        # Adding a custom rule under section 5 must be surfaced by compact `tree rules`
        readme_path = self.vault / "README.md"
        text = readme_path.read_text(encoding="utf-8")
        updated = text.replace(
            "- *(暂无额外定制，可直接在此追加你的目录约定、写作格式要求或禁改目录清单)*",
            "- 禁止自动修改 `01-关于我/价值观.md`",
        )
        readme_path.write_text(updated, encoding="utf-8")
        custom_res = self._run_cli("rules", "--agent", "cli-user")
        self.assertEqual(custom_res.returncode, 0)
        self.assertIn("我的个性化定制规则", custom_res.stdout)
        self.assertIn("禁止自动修改 `01-关于我/价值观.md`", custom_res.stdout)

    def test_focus_folder_name_query_and_summary_underscore(self) -> None:
        self._config_git_identity()
        self._run_cli("rules")
        write_res = self._run_cli(
            "write",
            "03-我正在做的事/工作/Demo/项目总览.md",
            "# Demo\n\n> 适用时机：调试时读取；核心脚本是 `deploy_cloud.sh`。\n\n## 一、背景\n这是测试项目。\n",
        )
        self.assertEqual(write_res.returncode, 0, msg=write_res.stderr)

        # Body-line fallback must also preserve underscores inside identifiers
        body_only_res = self._run_cli(
            "write",
            "03-我正在做的事/工作/Alpha/项目总览.md",
            "# Alpha\n\n> 适用时机：需要发布时查看。\n\n使用 `deploy_cloud.sh` 执行灰度发布。\n",
        )
        self.assertEqual(body_only_res.returncode, 0, msg=body_only_res.stderr)
        # Single-level category page (`len(parts) == 3`) with `build` in body must not false-hit `ui` or `工作`
        cat_page = self._run_cli(
            "write",
            "03-我正在做的事/工作/常规单页.md",
            "# 常规单页\n\n> 适用时机：常规构建。\n\n需要 build 流程。\n",
        )
        self.assertEqual(cat_page.returncode, 0, msg=cat_page.stderr)

        ctx_res = self._run_cli("context", "-q", "Demo")
        self.assertEqual(ctx_res.returncode, 0, msg=ctx_res.stderr)
        self.assertIn("当前情境自动对焦", ctx_res.stdout)
        self.assertIn("03-我正在做的事/工作/Demo/项目总览.md", ctx_res.stdout)
        self.assertIn("deploy_cloud.sh", ctx_res.stdout)

        ctx_alpha = self._run_cli("context", "-q", "Alpha")
        self.assertEqual(ctx_alpha.returncode, 0, msg=ctx_alpha.stderr)
        self.assertIn("使用 deploy_cloud.sh 执行灰度发布。", ctx_alpha.stdout)

        # Short word `ui` must NOT match `build`, and `ai` must NOT match `tree-daily`
        ctx_ui = self._run_cli("context", "-q", "ui")
        self.assertNotIn("常规单页.md", ctx_ui.stdout.split("## 二、")[0])
        ctx_ai = self._run_cli("context", "-q", "ai")
        self.assertNotIn("tree-daily/SKILL.md", ctx_ai.stdout.split("## 二、")[0])
        # Generic category folder `工作` must not boost all pages under `03-我正在做的事/工作/`
        ctx_work = self._run_cli("context", "-q", "工作")
        self.assertNotIn("常规单页.md", ctx_work.stdout.split("## 二、")[0])

    def test_install_skill_before_init_upgrades_daily_link_and_custom_tree_home(self) -> None:
        fake_home = self.tmp_path / "fake_home"
        (fake_home / ".claude").mkdir(parents=True)
        (fake_home / ".gemini").mkdir(parents=True)
        custom_vault = self.tmp_path / "custom_vault"

        env_extra = {"HOME": str(fake_home), "TREE_HOME": str(custom_vault)}
        inst_res = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "tree"), "install-skill", "--bin-dir", str(fake_home / "bin")],
            capture_output=True,
            text=True,
            env={**os.environ, **env_extra},
        )
        self.assertEqual(inst_res.returncode, 0, msg=inst_res.stderr)
        self.assertFalse((custom_vault / ".tree" / "audit.jsonl").exists())
        claude_md = (fake_home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn(f'TREE_HOME="{custom_vault.resolve()}"', claude_md)
        self.assertTrue((fake_home / ".gemini" / "skills" / "tree").is_symlink())
        self.assertTrue((fake_home / ".gemini" / "GEMINI.md").is_file())

        init_res = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "tree"), "init"],
            capture_output=True,
            text=True,
            env={**os.environ, **env_extra},
        )
        self.assertEqual(init_res.returncode, 0, msg=init_res.stderr)
        # `tree-daily` stays inside vault `04-我如何工作/tree-daily/` and is NOT registered to local host skills
        self.assertFalse((fake_home / ".claude" / "skills" / "tree-daily").exists())
        self.assertTrue((custom_vault / "04-我如何工作" / "tree-daily" / "SKILL.md").is_file())

    def test_outputs_daily_isolated_from_git_and_pure_markdown_template(self) -> None:
        self._config_git_identity()
        daily_dir = self.vault / "outputs" / "daily"
        daily_dir.mkdir(parents=True, exist_ok=True)
        (daily_dir / "2026-11-25-测试日报主题.md").write_text(
            "---\ndate: 2026-11-25\ntheme: \"测试日报主题\"\n---\n\n# 2026-11-25 · 测试日报主题\n\n## 昨日复盘\n- **工作**：完成测试\n",
            encoding="utf-8",
        )
        # Direct file writes under `outputs/daily/` never dirty vault Git status
        git_st = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.vault), capture_output=True, text=True)
        self.assertEqual(git_st.stdout.strip(), "")
        # `tree daily` CLI subcommand is removed; pure Markdown template has zero `tree daily` dependency
        removed_cmd = self._run_cli("daily")
        self.assertNotEqual(removed_cmd.returncode, 0)
        tpl_skill = (REPO_ROOT / "skills" / "tree" / "assets" / "default-skills" / "tree-daily" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("tree daily", tpl_skill)

    def test_wiki_subfolder_collapse_and_lint_graph_checks(self) -> None:
        wiki_dir = self.vault / "02-我的思考" / "经济学"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        (wiki_dir / "INDEX.md").write_text(
            "---\nupdated: 2026-11-25\nalways: false\n---\n\n# 经济学\n\n> 适用时机：研究经济学概念时读取。\n\n- [[边际效用]]\n- [[机会成本]]\n",
            encoding="utf-8",
        )
        (wiki_dir / "边际效用.md").write_text(
            "---\nupdated: 2026-11-25\nalways: false\norigin: external\nsource: 曼昆经济学\n---\n\n# 边际效用\n\n> 适用时机：分析消费决策时读取。\n\n关联 [[机会成本]]。\n",
            encoding="utf-8",
        )
        (wiki_dir / "机会成本.md").write_text(
            "---\nupdated: 2026-11-25\nalways: false\norigin: external\nsource: 曼昆经济学\n---\n\n# 机会成本\n\n> 适用时机：权衡选择代价时读取。\n\n对应 [[不存在的页面]]。\n",
            encoding="utf-8",
        )
        (wiki_dir / "孤儿概念.md").write_text(
            "---\nupdated: 2026-11-25\nalways: false\norigin: external\nsource: 曼昆经济学\n---\n\n# 孤儿概念\n\n> 适用时机：测试孤儿检测。\n\n没有被任何页面链接。\n",
            encoding="utf-8",
        )

        # 1. L0 directory map collapses `02-我的思考/<topic>/` subfolder into INDEX.md
        ctx_res = self._run_cli("context", "--no-auto-focus")
        self.assertEqual(ctx_res.returncode, 0, msg=ctx_res.stderr)
        self.assertIn("02-我的思考/经济学/INDEX.md", ctx_res.stdout)
        self.assertIn("Wiki 专题 · 含 4 篇互链页", ctx_res.stdout)
        dir_section = ctx_res.stdout.split("## 三、知识树全景目录", 1)[1]
        self.assertNotIn("经济学/边际效用.md", dir_section)

        # 2. Query lens still surfaces individual concept pages inside 02-我的思考/
        q_res = self._run_cli("context", "-q", "边际效用")
        self.assertEqual(q_res.returncode, 0, msg=q_res.stderr)
        self.assertIn("02-我的思考/经济学/边际效用.md", q_res.stdout)

        # 3. tree lint detects broken wikilink and Wiki orphan page
        lint_res = self._run_cli("lint")
        self.assertEqual(lint_res.returncode, 0, msg=lint_res.stderr)
        self.assertIn("[断链]", lint_res.stdout)
        self.assertIn("[[不存在的页面]]", lint_res.stdout)
        self.assertIn("[Wiki 孤儿页]", lint_res.stdout)
        self.assertIn("孤儿概念.md", lint_res.stdout)

        # 4. tree init initializes pure 01~04 structure without creating legacy `seed/`
        fresh_vault = self.tmp_path / "fresh_init_vault"
        tree_cli.cmd_init(fresh_vault, type("Args", (), {})())
        self.assertFalse((fresh_vault / "seed").exists())
        self.assertTrue((fresh_vault / "02-我的思考").is_dir())

    def test_summary_provenance_no_always_and_df_resonance(self) -> None:
        self._config_git_identity()
        self._run_cli("rules")
        # 1. `extract_first_summary` keeps substantive clause before `;整理自...`
        self._run_cli(
            "write",
            "seed/工具箱.md",
            "# 工具箱\n\n> 适用时机：查找开源提效工具或审美锚点时读取；整理自即刻收藏。\n\n正文条目。\n",
        )
        ls_res = self._run_cli("list", "seed")
        self.assertEqual(ls_res.returncode, 0, msg=ls_res.stderr)
        self.assertIn("查找开源提效工具或审美锚点时读取", ls_res.stdout)
        self.assertNotIn("(整理自即刻收藏。)", ls_res.stdout)

        # 2. `--no-always` skips duplicating always: true bodies in `tree context`
        self._run_cli(
            "write",
            "01-关于我/基本信息.md",
            "# 基本信息\n\n> 适用时机：默认生效；我是测试用户。\n\n## 详细档案\nUNIQUE_ALWAYS_BODY_TOKEN\n",
            "--always",
        )
        full_ctx = self._run_cli("context")
        self.assertIn("UNIQUE_ALWAYS_BODY_TOKEN", full_ctx.stdout)
        compact_ctx = self._run_cli("context", "--no-always")
        self.assertEqual(compact_ctx.returncode, 0, msg=compact_ctx.stderr)
        self.assertNotIn("UNIQUE_ALWAYS_BODY_TOKEN", compact_ctx.stdout)
        self.assertIn("--no-always", compact_ctx.stdout)

        # 3. `render_global_instruction_block` instructs `--no-always` and uses 45-line cap
        long_rules = "\n".join(f"- 规则 {i}" for i in range(1, 36))
        rendered = tree_focus.render_global_instruction_block(
            self.vault,
            [("04-我如何工作/工作原则.md", f"# 工作原则\n{long_rules}", "2026-11-25")],
        )
        self.assertIn("tree context --no-always", rendered)
        self.assertIn("规则 35", rendered)

        # 4. DF filter prevents ubiquitous vault-wide token (`mybrand`) from triggering noisy resonance
        for i in range(6):
            self._run_cli(
                "write",
                f"02-我的思考/note-{i}.md",
                f"# MyBrand 观察 {i}\n\n> 适用时机：讨论 MyBrand 生态时读取。\n\n内容 {i}。\n",
            )
        self._run_cli(
            "write",
            "03-我正在做的事/工作/MyBrand/项目总览.md",
            "# MyBrand 项目总览\n\n> 适用时机：开发 MyBrand 核心工程时读取。\n\n聚焦核心推进。\n",
            "--kind",
            "project",
        )
        q_brand = self._run_cli("context", "-q", "MyBrand 项目总览")
        focus_part = q_brand.stdout.split("## 二、")[0]
        self.assertNotIn("跨维概念共振（「mybrand」）", focus_part)

    def test_lint_broken_wikilinks_and_builtin_sources(self) -> None:
        self._config_git_identity()
        self._run_cli("rules")
        self._run_cli("write", "01-关于我/长期目标.md", "# 长期目标\n\n> 适用时机：默认。\n\n关联 [[不存在页面]]。\n")
        _, warns = tree_cli._collect_tree_lint(self.vault)
        self.assertTrue(any("不存在页面" in w for w in warns))

        # Verify all 12 built-in local sources (11 AI clients + Chrome) remain registered
        discovered = tree_seed.scan_all_local_sources(self.vault)
        for builtin_id in (
            "claude", "codex", "antigravity", "cursor", "trae", "workbuddy",
            "douchat", "hermes", "openclaw", "eden", "deepseek", "chrome",
        ):
            self.assertIn(builtin_id, discovered)

    def test_origin_source_metadata_focus_heat_and_lint(self) -> None:
        self._config_git_identity()
        self._run_cli("rules")
        # External reference under 02-我的思考/ should require source/url and carry origin: external
        ext_no_src = self._run_cli(
            "write",
            "02-我的思考/认知框架.md",
            "# 认知框架\n\n> 适用时机：梳理通用思考模型时读取。\n\n纯外部摘抄内容。\n",
            "--origin",
            "external",
        )
        self.assertEqual(ext_no_src.returncode, 0, msg=ext_no_src.stderr)
        lint_warn = self._run_cli("lint")
        self.assertIn("[缺少出处]", lint_warn.stdout)

        # Add explicit source and a matching self-authored page in same branch
        self._run_cli(
            "write",
            "02-我的思考/认知框架.md",
            "# 认知框架\n\n> 适用时机：梳理通用思考模型时读取。\n\n外部系统框架内容。\n",
            "--origin",
            "external",
            "--source",
            "https://example.com/thinking",
        )
        self._run_cli(
            "write",
            "02-我的思考/个人思考主张.md",
            "# 个人思考主张\n\n> 适用时机：写作或思考方法决策时读取。\n\n我自己验证过的主张。\n",
            "--origin",
            "self",
        )
        lint_ok = self._run_cli("lint")
        self.assertNotIn("[缺少出处]", lint_ok.stdout)

        # `compute_page_heat` discounts `external` pages relative to `self`
        h_self, _ = tree_cli.compute_page_heat("02-我的思考/个人思考主张.md", 0, "", {}, origin="self")
        h_ext, _ = tree_cli.compute_page_heat("02-我的思考/认知框架.md", 0, "", {}, origin="external")
        self.assertGreater(h_self, h_ext)

        # `tree search --origin external` filters cleanly
        sr = self._run_cli("search", "--origin", "external")
        self.assertEqual(sr.returncode, 0, msg=sr.stderr)
        self.assertIn("02-我的思考/认知框架.md", sr.stdout)
        self.assertNotIn("个人思考主张.md", sr.stdout)

        # External page does NOT auto-trigger on generic writing scene without `-q`,
        # yet DOES surface when `-q` explicitly queries its topic!
        ctx_scene = self._run_cli("context", "--cwd", str(self.tmp_path))
        focus_part_scene = ctx_scene.stdout.split("## 二、")[0]
        self.assertNotIn("02-我的思考/认知框架.md", focus_part_scene)

        ctx_q = self._run_cli("context", "-q", "认知框架")
        self.assertIn("02-我的思考/认知框架.md", ctx_q.stdout)
        self.assertIn("外部参考", ctx_q.stdout)

    def test_audit_bugfixes_move_index_human_note_recent_and_sync_guard(self) -> None:
        self._config_git_identity()
        self._run_cli("rules")

        # 1. `_has_tag` handles `#note` with Chinese punctuation and ignores inline/fenced code mentions
        self.assertTrue(tree_cli._has_tag({}, "这是手写随笔 #note。", "note"))
        self.assertTrue(tree_cli._has_tag({}, "这是手写随笔（#note）。", "note"))
        self.assertFalse(tree_cli._has_tag({}, "说明：手写原稿可打 `#note` 标签。", "note"))
        self.assertFalse(tree_cli._has_tag({}, "```md\n#note\n```", "note"))

        # 2. `tree move` upgrading a page to `<topic>/INDEX.md` must disambiguate link as `[[topic/INDEX]]` and keep `#anchor`
        self._run_cli("write", "02-我的思考/写作方法.md", "# 写作方法\n\n> 适用时机：写作时读取；核心方法。\n\n正文。")
        self._run_cli("write", "02-我的思考/引用页.md", "# 引用页\n\n> 适用时机：测试。\n\n参见 [[写作方法]] 与 [[写作方法#核心]]。")
        mv = self._run_cli("move", "02-我的思考/写作方法.md", "02-我的思考/写作方法/INDEX.md")
        self.assertEqual(mv.returncode, 0, msg=mv.stderr)
        ref_txt = (self.vault / "02-我的思考" / "引用页.md").read_text(encoding="utf-8")
        self.assertIn("[[写作方法/INDEX]]", ref_txt)
        self.assertIn("[[写作方法/INDEX#核心]]", ref_txt)
        self.assertNotIn("[[INDEX]]", ref_txt)

        # 3. `cmd_recent` discounts `origin: external` pages so self page ranks ahead of external page on same day
        self._run_cli("write", "02-我的思考/a_外部参考.md", "# 外部参考\n\n> 适用时机：外部参考。\n\n内容。", "--origin", "external", "--source", "https://example.com")
        self._run_cli("write", "02-我的思考/z_个人判断.md", "# 个人判断\n\n> 适用时机：个人判断。\n\n内容。", "--origin", "self")
        rec = self._run_cli("recent", "--days", "7")
        self.assertEqual(rec.returncode, 0, msg=rec.stderr)
        self.assertLess(rec.stdout.index("z_个人判断.md"), rec.stdout.index("a_外部参考.md"))

        # 4. Managed block sync guard must protect another vault root with the new `本地目录：` header
        fake_home = self.tmp_path / "fake_home"
        gemini_dir = fake_home / ".gemini"
        gemini_dir.mkdir(parents=True)
        target_file = gemini_dir / "GEMINI.md"
        prod_block = tree_focus.render_global_instruction_block(Path("/Users/prod/Tree/vault"), [])
        target_file.write_text(prod_block, encoding="utf-8")
        res = tree_focus.sync_global_instructions(
            self.vault,
            [],
            home=fake_home,
            only_existing_blocks=True,
        )
        self.assertEqual(res, [])
        self.assertIn("/Users/prod/Tree/vault", target_file.read_text(encoding="utf-8"))

    def test_flexible_custom_top_level_branch_auto_discovered_and_indexed(self) -> None:
        self._config_git_identity()
        self._run_cli("rules")

        # Write directly into a brand new custom top-level branch outside 01~04
        w = self._run_cli(
            "write",
            "05-客户档案/张三.md",
            "# 张三\n\n> 适用时机：对接张三合作前读取；核心渠道伙伴。\n\n偏好简洁汇报。\n",
        )
        self.assertEqual(w.returncode, 0, msg=w.stderr)

        # Auto-registered into .tree/config.json and visible across context/list/search/recent/lint
        cfg = json.loads((self.vault / ".tree" / "config.json").read_text(encoding="utf-8"))
        self.assertIn("05-客户档案", cfg["branches"])

        ctx = self._run_cli("context")
        self.assertIn("05-客户档案/张三.md", ctx.stdout)

        sr = self._run_cli("search", "渠道伙伴")
        self.assertIn("05-客户档案/张三.md", sr.stdout)

        lt = self._run_cli("lint")
        self.assertEqual(lt.returncode, 0, msg=lt.stderr)
        self.assertIn("0 error / 0 warning", lt.stdout)

    def test_ux_wave1_frontmatter_merge_default_no_always_stdin_agent_and_reconcile(self) -> None:
        self._config_git_identity()
        self._run_cli("rules")

        # 1. `01-关于我/` defaults to `always: false` unless explicitly marked `--always`
        w_id = self._run_cli(
            "write",
            "01-关于我/人际关系.md",
            "# 人际关系\n\n> 适用时机：联系朋友前读取；按需页不应隐式常驻。\n\n朋友列表。\n",
        )
        self.assertEqual(w_id.returncode, 0, msg=w_id.stderr)
        ctx_no_always = self._run_cli("context")
        self.assertNotIn("朋友列表。", ctx_no_always.stdout)

        # 2. Key-level frontmatter merge preserves untouched metadata on partial YAML overwrite
        w1 = self._run_cli(
            "write",
            "02-我的思考/测试合并.md",
            "# 测试合并\n\n> 适用时机：验证字段合并；第一版。\n\n正文内容足够长用于测试覆盖。\n",
            "--origin",
            "external",
            "--source",
            "https://example.com/src",
            "--when",
            "deploy_script.sh",
        )
        self.assertEqual(w1.returncode, 0, msg=w1.stderr)
        w2 = self._run_cli(
            "write",
            "02-我的思考/测试合并.md",
            "---\nupdated: 2026-11-26\n---\n\n# 测试合并\n\n> 适用时机：验证字段合并；第二版。\n\n更新后的正文内容同样足够长。\n",
        )
        self.assertEqual(w2.returncode, 0, msg=w2.stderr)
        m2, _ = tree_cli.parse_frontmatter((self.vault / "02-我的思考" / "测试合并.md").read_text(encoding="utf-8"))
        self.assertEqual(m2.get("origin"), "external")
        self.assertEqual(m2.get("source"), "https://example.com/src")
        self.assertEqual(m2.get("when"), "deploy_script.sh")

        # 3. Auto-read piped stdin without requiring explicit `--stdin` + short-name/no-ext `tree read`
        w_pipe = self._run_cli(
            "write",
            "02-我的思考/管道测试.md",
            stdin_data="# 管道测试\n\n> 适用时机：测试自动读取管道；无需显式传 stdin。\n\n管道正文。\n",
        )
        self.assertEqual(w_pipe.returncode, 0, msg=w_pipe.stderr)
        rd_short = self._run_cli("read", "管道测试")
        self.assertEqual(rd_short.returncode, 0, msg=rd_short.stderr)
        self.assertIn("管道正文。", rd_short.stdout)

        # 4. Auto-detect host agent via environment & surface mismatch hint on gate rejection
        r_claude = self._run_cli("rules", env_extra={"CLAUDECODE": "1"})
        self.assertEqual(r_claude.returncode, 0, msg=r_claude.stderr)
        w_mismatch = self._run_cli(
            "write",
            "02-我的思考/身份测试.md",
            "# 身份测试\n\n正文。\n",
            env_extra={"CODEX_CI": "1"},
        )
        self.assertNotEqual(w_mismatch.returncode, 0)
        self.assertIn("claude-code", w_mismatch.stderr)

        # 5. Human CLI `tree revert` succeeds without AI gate; `tree log` hides audit by default
        latest_hash = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(self.vault),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        # Clear audit.jsonl to ensure revert works for human `cli-user` without prior `tree rules`
        (self.vault / ".tree" / "audit.jsonl").unlink()
        rev = self._run_cli("revert", latest_hash)
        self.assertEqual(rev.returncode, 0, msg=rev.stderr)
        lg_clean = self._run_cli("log", "-n", "3")
        self.assertNotIn("Agent 访问与读写审计", lg_clean.stdout)
        lg_aud = self._run_cli("log", "-n", "3", "--audit")
        self.assertIn("Agent 访问与读写审计", lg_aud.stdout)

        # 6. Untracked (`??`) direct file creation in content branch is auto-reconciled & committed
        direct_file = self.vault / "02-我的思考" / "无壳新建.md"
        direct_file.write_text("# 无壳新建\n\n> 适用时机：测试无 Shell 降级自愈；新建页应自动收拢。\n\n内容。\n", encoding="utf-8")
        reconciled = tree_cli.auto_reconcile_external_file_edits(self.vault)
        self.assertIn("02-我的思考/无壳新建.md", reconciled)
        rec_meta, _ = tree_cli.parse_frontmatter(direct_file.read_text(encoding="utf-8"))
        self.assertTrue(bool(rec_meta.get("updated")))

        # 7. Lint flags oversized `always: true` (>45 non-empty lines) and generic `when:` token
        self._run_cli("rules")
        long_always_body = "# 超长常驻\n\n> 适用时机：默认。\n\n" + "\n".join(f"- 条目 {i}" for i in range(1, 50))
        w_warn = self._run_cli("write", "01-关于我/超长常驻.md", long_always_body, "--always", "--when", "AI, deploy_custom.sh")
        self.assertEqual(w_warn.returncode, 0, msg=w_warn.stderr)
        self.assertIn("超 45 行上限", w_warn.stdout)
        self.assertIn("高泛化词", w_warn.stdout)
        _, lint_warns = tree_cli._collect_tree_lint(self.vault)
        self.assertTrue(any("[常驻超长]" in w for w in lint_warns))
        self.assertTrue(any("[when 泛词风险]" in w for w in lint_warns))

        # 8. Custom TREE_HOME persistence pointer resolves without TREE_HOME env
        cfg_home = self.tmp_path / "custom_cfg_home"
        old_cfg_env = os.environ.get("TREE_CONFIG_HOME")
        old_tree_home = os.environ.get("TREE_HOME")
        try:
            os.environ["TREE_CONFIG_HOME"] = str(cfg_home)
            self.assertTrue(tree_cli._save_tree_home_pointer(self.vault))
            os.environ.pop("TREE_HOME", None)
            # Temporarily run from tmp_path (outside vault) so cwd detection doesn't shortcut
            prev_cwd = Path.cwd()
            os.chdir(self.tmp_path)
            try:
                self.assertEqual(tree_cli.get_tree_root(), self.vault.resolve())
            finally:
                os.chdir(prev_cwd)
        finally:
            if old_cfg_env is None:
                os.environ.pop("TREE_CONFIG_HOME", None)
            else:
                os.environ["TREE_CONFIG_HOME"] = old_cfg_env
            if old_tree_home is not None:
                os.environ["TREE_HOME"] = old_tree_home

    def test_ux_wave2_branch_evolution_roles_and_ui_protection(self) -> None:
        self._config_git_identity()
        self._run_cli("rules")

        # Create a project page in 03 and a cross-branch reference to it
        self._run_cli(
            "write",
            "03-我正在做的事/工作/Alpha/项目总览.md",
            "# Alpha\n\n> 适用时机：推进 Alpha 时读取；核心项目。\n\n进度正常。\n",
            "--kind",
            "project",
        )
        self._run_cli(
            "write",
            "02-我的思考/关联笔记.md",
            "# 关联笔记\n\n> 适用时机：复盘时读取；引用 Alpha。\n\n参见 [[03-我正在做的事/工作/Alpha/项目总览]]。\n",
        )

        # 1. Rename top-level branch via `tree branch rename` — preserves `active` role & updates explicit wikilink
        ren = self._run_cli("branch", "rename", "03-我正在做的事", "项目与行动", "--reason", "自定义主干名")
        self.assertEqual(ren.returncode, 0, msg=ren.stderr)
        self.assertTrue((self.vault / "项目与行动" / "工作" / "Alpha" / "项目总览.md").is_file())
        roles = tree_cli.get_branch_roles(self.vault)
        self.assertIn("项目与行动", roles["active"])
        note_text = (self.vault / "02-我的思考" / "关联笔记.md").read_text(encoding="utf-8")
        self.assertIn("[[项目与行动/工作/Alpha/项目总览]]", note_text)

        # 2. Add & remove custom branch cleanly
        add_r = self._run_cli("branch", "add", "健康与运动", "--reason", "新增生活主干")
        self.assertEqual(add_r.returncode, 0, msg=add_r.stderr)
        ls_r = self._run_cli("branch", "list")
        self.assertIn("健康与运动/", ls_r.stdout)
        rem_r = self._run_cli("branch", "remove", "健康与运动", "--reason", "清理空主干")
        self.assertEqual(rem_r.returncode, 0, msg=rem_r.stderr)
        self.assertNotIn("健康与运动", tree_cli.get_branches(self.vault))

    def test_qa_p0_and_p1_regression_fixes(self) -> None:
        self._config_git_identity()
        self._run_cli("rules")

        # P0-1: Reverting a non-HEAD commit succeeds cleanly despite append-only `.tree/changelog.jsonl`
        self._run_cli("write", "02-我的思考/页A.md", "# 页A\n\n> 适用时机：测试。\n\n版本1内容足够长避免截断。\n")
        h_a = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(self.vault), capture_output=True, text=True, check=True
        ).stdout.strip()
        self._run_cli("write", "02-我的思考/页B.md", "# 页B\n\n> 适用时机：测试。\n\n无关页B内容。\n")
        rev_non_head = self._run_cli("revert", h_a)
        self.assertEqual(rev_non_head.returncode, 0, msg=rev_non_head.stderr)
        self.assertFalse((self.vault / ".git" / "REVERT_HEAD").exists())
        self.assertFalse((self.vault / "02-我的思考" / "页A.md").exists())
        self.assertTrue((self.vault / "02-我的思考" / "页B.md").exists())
        self.assertNotIn("<<<<<<<", (self.vault / ".tree" / "changelog.jsonl").read_text(encoding="utf-8"))

        # P0-1b: Real content conflict during revert aborts cleanly without leaving REVERT_HEAD stuck
        self._run_cli("write", "02-我的思考/冲突页.md", "# 冲突页\n\nline1\nline2\n")
        self._run_cli("write", "02-我的思考/冲突页.md", "# 冲突页\n\nline1-v2\nline2\n")
        h_v2 = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(self.vault), capture_output=True, text=True, check=True
        ).stdout.strip()
        self._run_cli("write", "02-我的思考/冲突页.md", "# 冲突页\n\nline1-v3\nline2\n")
        rev_conflict = self._run_cli("revert", h_v2)
        self.assertNotEqual(rev_conflict.returncode, 0)
        self.assertFalse((self.vault / ".git" / "REVERT_HEAD").exists())

        # P0-2 & P1-3: DB seed extractors honor `skip_ids` hashes and `importlib` is imported
        self.assertTrue(hasattr(tree_seed, "importlib"))
        import sqlite3
        db_p = self.tmp_path / "eden_test.db"
        conn = sqlite3.connect(str(db_p))
        conn.execute("CREATE TABLE rooms (id TEXT, name TEXT)")
        conn.execute("CREATE TABLE messages (room_id TEXT, sender_id TEXT, type TEXT, content TEXT, timestamp TEXT)")
        conn.execute("INSERT INTO rooms VALUES ('r1', '测试房间')")
        now_s = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn.execute("INSERT INTO messages VALUES ('r1', 'WebUser', 'chat', '这是一条足够长的测试人类发言', ?)", (now_s,))
        conn.commit()
        conn.close()
        _, cnt1, pids1 = tree_seed.extract_eden_chats(str(db_p), days=30, max_sessions=10)
        self.assertEqual(cnt1, 1)
        _, cnt2, _ = tree_seed.extract_eden_chats(str(db_p), days=30, max_sessions=10, skip_ids=set(pids1))
        self.assertEqual(cnt2, 0)

        # P0-4: Renaming `04-我如何工作` preserves role semantics and moves vault skill directory cleanly
        ren4 = self._run_cli("branch", "rename", "04-我如何工作", "04-工作方法")
        self.assertEqual(ren4.returncode, 0, msg=ren4.stderr)
        self.assertTrue((self.vault / "04-工作方法" / "tree-daily" / "SKILL.md").is_file())

        # P0-5: Non-.md files protected on remove; ghost branch fails; empty branch rename commits cleanly
        att_dir = self.vault / "07-附件分支"
        att_dir.mkdir()
        (att_dir / "important.pdf").write_bytes(b"%PDF-1.4")
        rem_pdf = self._run_cli("branch", "remove", "07-附件分支")
        self.assertNotEqual(rem_pdf.returncode, 0)
        self.assertTrue((att_dir / "important.pdf").is_file())
        rem_ghost = self._run_cli("branch", "remove", "99-不存在幽灵")
        self.assertNotEqual(rem_ghost.returncode, 0)
        self._run_cli("branch", "add", "06-空分支")
        ren_empty = self._run_cli("branch", "rename", "06-空分支", "06-空分支改名")
        self.assertEqual(ren_empty.returncode, 0, msg=ren_empty.stderr)
        self.assertNotIn("Git 提交失败", ren_empty.stderr)

        # P1-1 & P1-2: `upsert_frontmatter` strips old YAML block list children; `when` generic check handles `;`/`[]`/paths
        blk_md = "---\nupdated: 2026-11-25\nwhen:\n  - old_rule_1\n  - old_rule_2\n---\n\n# 列表测试\n\n正文。\n"
        updated_blk = tree_cli.upsert_frontmatter(blk_md, "2026-11-26", when="new_rule")
        self.assertIn("when: new_rule", updated_blk)
        self.assertNotIn("old_rule_1", updated_blk)
        self.assertEqual(tree_cli._detect_generic_when_tokens("AI; 产品"), ["AI", "产品"])
        self.assertEqual(tree_cli._detect_generic_when_tokens("[AI, python]"), ["AI", "python"])
        self.assertEqual(tree_cli._detect_generic_when_tokens("~/Desktop/AI/my-special-repo"), [])
        self.assertEqual(tree_focus._split_multi_values("[foo, bar]"), ("foo", "bar"))

        # P1-8: Non-interactive AI cannot bypass gate via `--yes`
        rev_ai_bypass = self._run_cli("revert", h_a, "--yes", env_extra={"CLAUDECODE": "1"})
        self.assertNotEqual(rev_ai_bypass.returncode, 0)

    def test_full_audit_p0_security_and_protection_guards(self) -> None:
        self._config_git_identity()
        self._run_cli("rules")

        # 1. Block writing into reserved system dirs & stray root files; block deleting metadata
        for bad_target in (".git/evil.md", ".tree/config.json", "outputs/leak.md", "stray_root.md"):
            r = self._run_cli("write", bad_target, "# Bad\n\n内容。\n")
            self.assertNotEqual(r.returncode, 0, msg=f"Should block write to {bad_target}")
        self.assertFalse((self.vault / ".git" / "evil.md").exists())
        self.assertFalse((self.vault / "stray_root.md").exists())

        del_cfg = self._run_cli("delete", ".tree/config.json")
        self.assertNotEqual(del_cfg.returncode, 0)
        self.assertTrue((self.vault / ".tree" / "config.json").is_file())
        del_ig = self._run_cli("delete", ".gitignore")
        self.assertNotEqual(del_ig.returncode, 0)

        # 2. Block path traversal cleanly (no raw Python traceback)
        trav = self._run_cli("write", "../escape.md", "# Bad\n\n内容。\n")
        self.assertNotEqual(trav.returncode, 0)
        self.assertNotIn("Traceback", trav.stderr)

        # 3. Refuse reverting root commit & keep append-only changelog on HEAD revert
        root_sha = subprocess.run(
            ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=str(self.vault), capture_output=True, text=True, check=True
        ).stdout.strip().splitlines()[0]
        rev_root = self._run_cli("revert", root_sha)
        self.assertNotEqual(rev_root.returncode, 0)
        self.assertTrue((self.vault / "README.md").is_file())
        self.assertTrue((self.vault / ".tree" / "config.json").is_file())

        self._run_cli("write", "02-我的思考/可回滚页.md", "# 可回滚页\n\n> 适用时机：测试。\n\n正文内容。\n")
        before_cl = len((self.vault / ".tree" / "changelog.jsonl").read_text(encoding="utf-8").splitlines())
        rev_head = self._run_cli("revert", "HEAD")
        self.assertEqual(rev_head.returncode, 0, msg=rev_head.stderr)
        after_cl = len((self.vault / ".tree" / "changelog.jsonl").read_text(encoding="utf-8").splitlines())
        self.assertEqual(after_cl, before_cl + 1)

        # 4. Lightweight `#note` tagging is searchable via `tree search --tag note` without heavy CLI locks
        self._run_cli("write", "02-我的思考/手写笔记.md", "# 随笔\n\n这是一篇手写随笔（#note）。\n")
        s_note = self._run_cli("search", "--tag", "note")
        self.assertEqual(s_note.returncode, 0, msg=s_note.stderr)
        self.assertIn("手写笔记.md", s_note.stdout)

        # 5. CLI metadata newline injection blocked & YAML colon quoted
        inj = self._run_cli(
            "write",
            "02-我的思考/防注入.md",
            "# 防注入\n\n> 适用时机：测试。\n\n正文。\n",
            "--source",
            "ok\nowner: human",
            "--when",
            "foo: bar",
        )
        self.assertEqual(inj.returncode, 0, msg=inj.stderr)
        inj_text = (self.vault / "02-我的思考" / "防注入.md").read_text(encoding="utf-8")
        inj_meta, _ = tree_cli.parse_frontmatter(inj_text)
        self.assertNotEqual(inj_meta.get("owner"), "human")
        self.assertIn('when: "foo: bar"', inj_text)

        # 6. `tree sync` blocks `.env` / secrets instead of pushing them
        bare_repo = self.tmp_path / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare_repo)], check=True)
        self._run_cli("remote", str(bare_repo))
        (self.vault / ".env").write_text("OPENAI_API_KEY=sk-proj-AbCdEfGhIjKlMnOpQrStUvWx\n", encoding="utf-8")
        sync_bad = self._run_cli("sync")
        self.assertNotEqual(sync_bad.returncode, 0)
        self.assertIn("同步安全拦截", sync_bad.stderr)
        (self.vault / ".env").unlink()

    def test_full_audit_cli_p1_edge_cases(self) -> None:
        self._config_git_identity()
        self._run_cli("rules")

        # 1. Editing README.md refreshes agent gate automatically
        r_readme = self._run_cli("write", "README.md", "\n- 定制条款测试\n", "--append")
        self.assertEqual(r_readme.returncode, 0, msg=r_readme.stderr)
        r_after = self._run_cli("write", "02-我的思考/改宪后续写.md", "# 后续\n\n> 适用时机：测试。\n\n正文。\n")
        self.assertEqual(r_after.returncode, 0, msg=r_after.stderr)

        # 2. `--append` preserves incoming frontmatter keys & `write` deduplicates repeated status keys
        self._run_cli("write", "02-我的思考/追加合并.md", "# 追加\n\n> 适用时机：测试。\n\n首段。\n")
        self._run_cli(
            "write",
            "02-我的思考/追加合并.md",
            "---\ntags: [extra_tag]\nkind: idea\n---\n\n追加段落。\n",
            "--append",
            "--status",
            "paused",
        )
        m_app, _ = tree_cli.parse_frontmatter((self.vault / "02-我的思考" / "追加合并.md").read_text(encoding="utf-8"))
        self.assertEqual(m_app.get("kind"), "idea")
        self.assertIn("extra_tag", m_app.get("tags", ""))
        self.assertEqual(m_app.get("status"), "paused")

        dup_fm = "---\nstatus: active\nstatus: paused\n---\n\n# 重复\n\n正文。\n"
        dedup_out = tree_cli.upsert_frontmatter(dup_fm, "2026-11-27", status="archived")
        self.assertEqual(dedup_out.count("status:"), 1)

        # 3. `tree move` rejects trailing slash / existing dir and skips code blocks when rewriting wikilinks
        (self.vault / "03-我正在做的事" / "TargetDir").mkdir(parents=True, exist_ok=True)
        self._run_cli("write", "02-我的思考/源页.md", "# 源页\n\n> 适用时机：测试。\n\n内容。\n")
        self._run_cli(
            "write",
            "02-我的思考/引源页.md",
            "# 引源页\n\n> 适用时机：测试。\n\n正文引用 [[源页]]，行内代码 `[[源页]]`。\n```md\n[[源页]]\n```\n",
        )
        mv_slash = self._run_cli("move", "02-我的思考/源页.md", "03-我正在做的事/TargetDir/")
        self.assertNotEqual(mv_slash.returncode, 0)
        mv_dir = self._run_cli("move", "02-我的思考/源页.md", "03-我正在做的事/TargetDir")
        self.assertNotEqual(mv_dir.returncode, 0)
        mv_ok = self._run_cli("move", "02-我的思考/源页.md", "02-我的思考/新源页.md")
        self.assertEqual(mv_ok.returncode, 0, msg=mv_ok.stderr)
        ref_txt = (self.vault / "02-我的思考" / "引源页.md").read_text(encoding="utf-8")
        self.assertIn("正文引用 [[新源页]]", ref_txt)
        self.assertIn("`[[源页]]`", ref_txt)
        self.assertIn("```md\n[[源页]]\n```", ref_txt)

        # 4. `delete` warns about backlinks; `search --tag` handles Chinese punctuation; `lint` depth/mixed/codeblock/orphan
        del_warn = self._run_cli("delete", "02-我的思考/新源页.md")
        self.assertEqual(del_warn.returncode, 0, msg=del_warn.stderr)
        self.assertIn("引源页.md", del_warn.stdout)

        self._run_cli("write", "02-我的思考/标签页.md", "# 标签页\n\n> 适用时机：测试。\n\n正文含 #中文标签，紧跟逗号。\n")
        sr_tag = self._run_cli("search", "--tag", "中文标签")
        self.assertIn("标签页.md", sr_tag.stdout)

        self._run_cli(
            "write",
            "02-我的思考/L2/L3/L4/L5/五层页.md",
            "# 五层页\n\n> 适用时机：测试。\n\n```txt\n用户在对话中提到示例\n```\n",
            "--origin",
            "mixed",
        )
        _, warns = tree_cli._collect_tree_lint(self.vault)
        self.assertTrue(any("[路径过深]" in w for w in warns))
        self.assertTrue(any("[缺少出处]" in w and "mixed" in w for w in warns))
        self.assertFalse(any("[记账腔]" in w and "五层页.md" in w for w in warns))

        # 5. `branch add` idempotent, `recent --days -1` fails, `--always --no-always` mutually exclusive
        self._run_cli("branch", "add", "05-幂等测试")
        cnt_before = int(
            subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=str(self.vault), capture_output=True, text=True, check=True).stdout.strip()
        )
        add_again = self._run_cli("branch", "add", "05-幂等测试")
        self.assertEqual(add_again.returncode, 0)
        self.assertIn("已存在", add_again.stdout)
        cnt_after = int(
            subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=str(self.vault), capture_output=True, text=True, check=True).stdout.strip()
        )
        self.assertEqual(cnt_before, cnt_after)
        self.assertNotEqual(self._run_cli("recent", "--days", "-5").returncode, 0)
        self.assertNotEqual(self._run_cli("write", "02-我的思考/互斥.md", "# x\n\n1\n2\n3", "--always", "--no-always").returncode, 0)

    def test_full_audit_focus_and_install_fixes(self) -> None:
        # 1. Custom `~/vault` must NOT leak `$HOME` into WorkBuddy sandbox or mark sibling projects as `is_inside_tree`
        fake_home = self.tmp_path / "home_vault_guard"
        home_vault = fake_home / "vault"
        home_vault.mkdir(parents=True)
        wb_dir = fake_home / ".workbuddy"
        wb_dir.mkdir(parents=True)
        (wb_dir / "settings.json").write_text("{}", encoding="utf-8")
        tree_focus.ensure_workbuddy_sandbox_allow_write(home_vault, home=fake_home)
        wb_cfg = json.loads((wb_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertNotIn(str(fake_home.resolve()), wb_cfg["sandbox"]["extraAllowWrite"])

        sibling_proj = fake_home / "other_project"
        sibling_proj.mkdir()
        sig = tree_focus.probe_workspace(home_vault, caller_cwd=sibling_proj)
        self.assertFalse(sig.is_inside_tree)

        # 2. `_body_mentions_workspace` must not match parent dir; `_extract_tags` strips code blocks;
        # `_resolve_wikilink_node` must not degrade broken `Dir/Stem` into sibling `Stem`
        work_dir = fake_home / "Work"
        (work_dir / "app").mkdir(parents=True)
        self.assertEqual(tree_focus._body_mentions_workspace(f"项目位于 `{work_dir}/app`", work_dir.resolve(), None), "")
        self.assertEqual(tree_focus._extract_tags({}, "```python\n#code_tag\n```\n正文 #real_tag"), frozenset({"real_tag"}))

        n1 = tree_focus.PageNode(
            path=self.vault / "03-我正在做的事/工作/Eden/定位.md",
            rel_path="03-我正在做的事/工作/Eden/定位.md",
            branch="03-我正在做的事",
            stem="定位",
            meta={},
            body="",
            updated_iso="2026-11-27",
            status="active",
            age_days=0,
            is_always=False,
            heat_score=10.0,
            summary="",
            trigger_hint="",
            locations=(),
            when_rules=(),
            tags=frozenset(),
            wikilinks=(),
        )
        self.assertIsNone(
            tree_focus._resolve_wikilink_node(
                "OtherProject/定位",
                {n1.rel_path: n1},
                {"定位": [n1]},
                "03-我正在做的事/工作/Eden/项目总览.md",
            )
        )

        # 3. `sync_global_instructions` ignores user text outside managed block & `--remove` strips orphan START marker
        claude_dir = fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        claude_md = claude_dir / "CLAUDE.md"
        prod_block = tree_focus.render_global_instruction_block(self.vault, [("01-关于我/基本信息.md", "# A\nv1", "2026-11-27")])
        claude_md.write_text("我的备注：本地目录：`/Users/other/proj`\n\n" + prod_block, encoding="utf-8")
        res = tree_focus.sync_global_instructions(
            self.vault,
            [("01-关于我/基本信息.md", "# A\nv2-updated", "2026-11-27")],
            home=fake_home,
            only_existing_blocks=True,
        )
        self.assertEqual(len(res), 1)
        self.assertIn("v2-updated", claude_md.read_text(encoding="utf-8"))

        claude_md.write_text("用户内容\n\n<!-- TREE-CONTEXT:START -->\n残留\n", encoding="utf-8")
        rem = tree_focus.sync_global_instructions(self.vault, [], home=fake_home, remove=True)
        self.assertEqual(len(rem), 1)
        self.assertNotIn("TREE-CONTEXT:START", claude_md.read_text(encoding="utf-8"))

        # 4. `.git/config` submodule URL before `[remote "origin"]` must not hijack git_remote
        cfg_txt = '[submodule "vendor"]\n\turl = https://github.com/vendor/lib.git\n[remote "origin"]\n\turl = https://github.com/me/myrepo.git\n'
        self.assertEqual(tree_focus._parse_git_config_remote_url(cfg_txt), "https://github.com/me/myrepo.git")

    def test_full_audit_seed_chat_redaction_and_cursor_order(self) -> None:
        # 1. Secret & PII redaction covers PKCS#8, AWS, github_pat, multi-part Cookie, and does not false-positive Chinese token discussion
        raw = (
            "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----\n"
            "AKIAIOSFODNN7EXAMPLE github_pat_11ABCDEFG0123456789_abcdefg123456\n"
            "Cookie: a=1; session=abcdef1234567890; foo=bar\n"
            "https://api.example.com/v1?api_key=mysecretkey12345&user=suhe\n"
            "phone 13812345678 id 11010119900101123X\n"
            "token: 120000上下文窗口\n"
        )
        red = tree_seed.redact_all(raw)
        self.assertIn("[REDACTED_PRIVATE_KEY]", red)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", red)
        self.assertNotIn("github_pat_11ABCDEFG", red)
        self.assertNotIn("abcdef1234567890", red)
        self.assertIn("&user=suhe", red)
        self.assertIn("138****5678", red)
        self.assertIn("[REDACTED_ID]", red)
        self.assertIn("token: 120000上下文窗口", red)

        # 2. `seed scan` ignores host files that contain ONLY Tree's managed block (no false positive)
        fake_home = self.tmp_path / "seed_fake_home"
        (fake_home / ".claude").mkdir(parents=True)
        (fake_home / ".claude" / "CLAUDE.md").write_text(
            tree_focus.render_global_instruction_block(self.vault, []),
            encoding="utf-8",
        )
        scan_res = self._run_cli("seed", "scan", env_extra={"HOME": str(fake_home)})
        self.assertEqual(scan_res.returncode, 0, msg=scan_res.stderr)
        self.assertIn("[claude     ] —  未发现", scan_res.stdout)

        # 3. `extract_cursor_chats` scans newest rows first (`ORDER BY rowid DESC`) & respects `lastUpdatedAt`
        import sqlite3
        cdb = self.tmp_path / "cursor_state.vscdb"
        conn = sqlite3.connect(str(cdb))
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        now_ms = int(datetime.datetime.now().timestamp() * 1000)
        old_ms = int((datetime.datetime.now() - datetime.timedelta(days=90)).timestamp() * 1000)
        for i in range(420):
            payload = json.dumps({"composerId": f"old-{i:04d}", "createdAt": old_ms, "conversation": [{"type": 1, "text": f"旧对话内容 {i}"}]})
            conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"composerData:old-{i:04d}", payload))
        newest_payload = json.dumps(
            {"composerId": "zzzz-newest", "createdAt": old_ms, "lastUpdatedAt": now_ms, "name": "最新续聊", "conversation": [{"type": 1, "text": "今天刚刚更新的重要对话"}]}
        )
        conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("composerData:zzzz-newest", newest_payload))
        conn.commit()
        conn.close()
        md_cur, cnt_cur, _ = tree_seed.extract_cursor_chats(str(cdb), days=30, max_sessions=5)
        self.assertEqual(cnt_cur, 1)
        self.assertIn("今天刚刚更新的重要对话", md_cur)

        # 4. Malformed overflow timestamps never crash `_safe_fromtimestamp`; huge single-file import truncates instead of dropping to 0
        self.assertEqual(tree_seed._safe_fromtimestamp(999999999999999999, "%Y-%m-%d", fallback="recent"), "recent")
        huge_f = self.tmp_path / "huge.md"
        huge_f.write_text("测试长文本" * 15000 + "\n手机 13912345678", encoding="utf-8")
        out_p, imp_cnt = tree_seed.import_external_path(self.vault, huge_f, label="hugemd")
        self.assertEqual(imp_cnt, 1)
        self.assertNotIn("13912345678", out_p.read_text(encoding="utf-8"))

        # 5. Empty chat read prefix rejected cleanly
        empty_ref = self._run_cli("chat", "read", "claude:")
        self.assertNotEqual(empty_ref.returncode, 0)


if __name__ == "__main__":
    unittest.main()
