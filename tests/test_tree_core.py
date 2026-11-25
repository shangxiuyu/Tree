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
        env["TREE_HOME"] = str(self.vault)
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

        # 2. Agent-A reads compact rules (< 4 KB)
        res_rules = self._run_cli("rules", "--agent", "agent-a")
        self.assertEqual(res_rules.returncode, 0)
        self.assertLess(len(res_rules.stdout.encode("utf-8")), 4096)

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

        # Corrupt audit.jsonl line should not crash `tree log`
        with open(self.vault / ".tree" / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write("{broken-json\n\n")
        log_res = self._run_cli("log", "-n", "5")
        self.assertEqual(log_res.returncode, 0, msg=log_res.stderr)

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
        daily_link = (fake_home / ".claude" / "skills" / "tree-daily").resolve()
        expected_vault_daily = (custom_vault / "04-我如何工作" / "tree-daily").resolve()
        self.assertEqual(daily_link, expected_vault_daily)

    def test_daily_write_does_not_dirty_git_and_lint_checks_sections(self) -> None:
        self._config_git_identity()
        valid_daily = (
            "---\n"
            "schema_version: 1\n"
            "date: 2026-11-25\n"
            'theme: "测试日报主题"\n'
            "status: ready\n"
            'generated_at: "2026-11-25T09:00:00+08:00"\n'
            'updated_at: "2026-11-25T09:00:00+08:00"\n'
            'sources_ok: ["tree"]\n'
            "sources_skipped:\n"
            "feed_count: 0\n"
            "---\n\n"
            "# 2026-11-25 · 测试日报主题\n\n"
            "> 测试摘要。\n\n"
            "## 昨日复盘\n\n"
            "- **工作**：完成测试\n"
            "- **学习**：读完源码\n"
            "- **生活**：作息规律\n\n"
            "## 候选沉淀\n\n"
            "- 今日无须入树\n\n"
            "## 今日聚焦\n\n"
            "- [ ] **跑通单测** — 全绿通过\n"
        )
        wr_res = self._run_cli("daily", "write", "--date", "2026-11-25", "--theme", "测试日报主题", "--stdin", stdin_data=valid_daily)
        self.assertEqual(wr_res.returncode, 0, msg=wr_res.stderr)
        git_st = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.vault), capture_output=True, text=True)
        self.assertEqual(git_st.stdout.strip(), "")

        missing_sec_daily = valid_daily.replace("## 今日聚焦\n\n- [ ] **跑通单测** — 全绿通过\n", "")
        bad_res = self._run_cli("daily", "write", "--date", "2026-11-26", "--theme", "测试日报主题", "--stdin", stdin_data=missing_sec_daily)
        self.assertNotEqual(bad_res.returncode, 0)
        self.assertIn("今日聚焦", bad_res.stderr)

        empty_ok_daily = valid_daily.replace('sources_ok: ["tree"]', "sources_ok: []")
        bad_ok = self._run_cli("daily", "write", "--date", "2026-11-26", "--theme", "测试日报主题", "--stdin", stdin_data=empty_ok_daily)
        self.assertNotEqual(bad_ok.returncode, 0)
        self.assertIn("sources_ok", bad_ok.stderr)

        # Colon inside bold `- **工作：**` and missing dimension warning
        colon_inside = (
            valid_daily.replace("date: 2026-11-25", "date: 2026-11-27")
            .replace("- **工作**：完成测试", "- **工作：** 完成测试")
            .replace("- **生活**：作息规律\n", "")
        )
        warn_res = self._run_cli("daily", "write", "--date", "2026-11-27", "--theme", "测试日报主题", "--stdin", stdin_data=colon_inside)
        self.assertEqual(warn_res.returncode, 0, msg=warn_res.stderr)
        self.assertIn("缺少维度条目：生活", warn_res.stderr)
        self.assertEqual(warn_res.stderr.split("缺少维度条目：")[-1].split("（")[0].strip(), "生活")

    def test_seed_add_atomic_rollback_and_remove(self) -> None:
        water_dir = self.tmp_path / "mock_water"
        (water_dir / "connectors" / "demo").mkdir(parents=True)
        (water_dir / "connectors" / "demo" / "connector.py").write_text('CONNECTOR_ID = "demo"\n', encoding="utf-8")
        (water_dir / "connectors" / "good").mkdir(parents=True)
        (water_dir / "connectors" / "good" / "connector.py").write_text('CONNECTOR_ID = "good"\n', encoding="utf-8")
        reg = {
            "connectors": [
                {
                    "id": "demo",
                    "name": "Demo",
                    "files": ["connectors/demo/connector.py"],
                    "sha256": {"connectors/demo/connector.py": "0" * 64},
                },
                {
                    "id": "good",
                    "name": "Good",
                    "files": ["connectors/good/connector.py"],
                },
            ]
        }
        (water_dir / "registry.json").write_text(json.dumps(reg), encoding="utf-8")

        fail_add = self._run_cli("seed", "add", "demo", "--from", str(water_dir))
        self.assertNotEqual(fail_add.returncode, 0)
        self.assertFalse((self.vault / "seed" / "connectors" / "demo").exists())

        ok_add = self._run_cli("seed", "add", "good", "--from", str(water_dir))
        self.assertEqual(ok_add.returncode, 0, msg=ok_add.stderr)
        self.assertTrue((self.vault / "seed" / "connectors" / "good" / "connector.py").is_file())

        # Failed --force upgrade must restore prior good connector instead of deleting it
        reg["connectors"][1]["sha256"] = {"connectors/good/connector.py": "f" * 64}
        (water_dir / "registry.json").write_text(json.dumps(reg), encoding="utf-8")
        fail_force = self._run_cli("seed", "add", "good", "--force", "--from", str(water_dir))
        self.assertNotEqual(fail_force.returncode, 0)
        self.assertTrue((self.vault / "seed" / "connectors" / "good" / "connector.py").is_file())

        # Traversal & drive-letter paths must be rejected
        with self.assertRaises(ValueError):
            tree_seed._safe_rel_subpath("C:/evil.py")
        with self.assertRaises(ValueError):
            tree_seed._safe_rel_subpath("connectors/good/.", strip_prefix="connectors/good")
        with self.assertRaises(ValueError):
            tree_seed._safe_rel_subpath("connectors/good/../../evil.py", strip_prefix="connectors/good")

        rm_res = self._run_cli("seed", "remove", "good")
        self.assertEqual(rm_res.returncode, 0, msg=rm_res.stderr)
        self.assertFalse((self.vault / "seed" / "connectors" / "good").exists())

    def test_gate_hint_includes_agent_and_revert_accepts_agent(self) -> None:
        self._config_git_identity()
        fail_res = self._run_cli("write", "02-我的思考/a.md", "# A\n内容", "--agent", "custom-bot")
        self.assertNotEqual(fail_res.returncode, 0)
        self.assertIn("--agent custom-bot", fail_res.stderr)

        self._run_cli("rules", "--agent", "custom-bot")
        ok_res = self._run_cli("write", "02-我的思考/a.md", "# A\n内容足够长", "--agent", "custom-bot")
        self.assertEqual(ok_res.returncode, 0, msg=ok_res.stderr)
        head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.vault), capture_output=True, text=True, check=True).stdout.strip()
        rev_res = self._run_cli("revert", head_sha, "--agent", "custom-bot")
        self.assertEqual(rev_res.returncode, 0, msg=rev_res.stderr)


if __name__ == "__main__":
    unittest.main()
