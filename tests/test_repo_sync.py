from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "repo_sync.py"
MODULE_SPEC = importlib.util.spec_from_file_location("repo_sync", MODULE_PATH)
assert MODULE_SPEC and MODULE_SPEC.loader
repo_sync = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = repo_sync
MODULE_SPEC.loader.exec_module(repo_sync)


class LocalTestTargetClient(repo_sync.BaseTargetClient):
    platform_name = "localtest"
    web_base = "https://mirror.example"

    def __init__(self, remote_url: str):
        super().__init__("token")
        self.remote_url = remote_url

    def authenticated_git_url(self, namespace: str, repo_name: str) -> str:
        return self.remote_url


class ConfigTests(unittest.TestCase):
    def test_load_config_defaults_rewrite_readme_links_false(self) -> None:
        config = """
defaults:
  release_limit: 1
  max_parallel: 1
repos:
  - id: sample
    source:
      full_name: owner/repo
      private: false
    targets:
      gitcode:
        enabled: true
        namespace: mirror
        name: repo
        visibility: public
"""
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            config_path = Path(tmp_dir_name) / "repos.yaml"
            config_path.write_text(config, encoding="utf-8")
            _, entries = repo_sync.load_config(config_path)
        self.assertFalse(entries[0]["targets"]["gitcode"]["rewrite_readme_links"])

    def test_load_config_keeps_explicit_rewrite_readme_links_true(self) -> None:
        config = """
defaults:
  release_limit: 1
  max_parallel: 1
repos:
  - id: sample
    source:
      full_name: owner/repo
      private: false
    targets:
      gitcode:
        enabled: true
        namespace: mirror
        name: repo
        visibility: public
        rewrite_readme_links: true
"""
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            config_path = Path(tmp_dir_name) / "repos.yaml"
            config_path.write_text(config, encoding="utf-8")
            _, entries = repo_sync.load_config(config_path)
        self.assertTrue(entries[0]["targets"]["gitcode"]["rewrite_readme_links"])


class RewriteTests(unittest.TestCase):
    def test_gitcode_rewrites_same_repo_links_only(self) -> None:
        client = repo_sync.GitCodeTargetClient("token")
        content = "\n".join(
            [
                "repo https://github.com/owner/repo",
                "clone https://github.com/owner/repo.git",
                "blob https://github.com/owner/repo/blob/main/docs/guide.md#L1",
                "tree https://github.com/owner/repo/tree/main/docs",
                "web-raw https://github.com/owner/repo/raw/refs/heads/main/tools/install.sh",
                "raw https://raw.githubusercontent.com/owner/repo/main/assets/logo.svg",
                "external https://github.com/other/repo/blob/main/README.md",
                "issues https://github.com/owner/repo/issues/1",
                "download https://github.com/owner/repo/releases/download/v1.0.0/app.tar.gz",
            ]
        )
        rewritten, replacements = repo_sync.rewrite_readme_links(
            content,
            source_full_name="owner/repo",
            client=client,
            namespace="mirror",
            repo_name="repo",
        )
        self.assertEqual(replacements, 6)
        self.assertIn("https://gitcode.com/mirror/repo", rewritten)
        self.assertIn("https://gitcode.com/mirror/repo.git", rewritten)
        self.assertIn("https://gitcode.com/mirror/repo/-/blob/main/docs/guide.md#L1", rewritten)
        self.assertIn("https://gitcode.com/mirror/repo/-/tree/main/docs", rewritten)
        self.assertIn("https://gitcode.com/mirror/repo/-/raw/refs%2Fheads%2Fmain/tools/install.sh", rewritten)
        self.assertIn("https://gitcode.com/mirror/repo/-/raw/main/assets/logo.svg", rewritten)
        self.assertIn("https://github.com/other/repo/blob/main/README.md", rewritten)
        self.assertIn("https://github.com/owner/repo/issues/1", rewritten)
        self.assertIn("https://github.com/owner/repo/releases/download/v1.0.0/app.tar.gz", rewritten)

    def test_gitee_rewrites_same_repo_links_only(self) -> None:
        client = repo_sync.GiteeTargetClient("token")
        content = (
            "See https://github.com/owner/repo/tree/main/src "
            "and https://raw.githubusercontent.com/owner/repo/refs/heads/main/README.md"
        )
        rewritten, replacements = repo_sync.rewrite_readme_links(
            content,
            source_full_name="owner/repo",
            client=client,
            namespace="mirror",
            repo_name="repo",
        )
        self.assertEqual(replacements, 2)
        self.assertIn("https://gitee.com/mirror/repo/tree/main/src", rewritten)
        self.assertIn("https://gitee.com/mirror/repo/raw/refs%2Fheads%2Fmain/README.md", rewritten)


class ReadmeSelectionTests(unittest.TestCase):
    def test_find_root_readme_path_prefers_priority_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            root_dir = Path(tmp_dir_name)
            (root_dir / "README.rst").write_text("rst", encoding="utf-8")
            (root_dir / "readme.MD").write_text("md", encoding="utf-8")
            selected = repo_sync.find_root_readme_path(root_dir)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.name, "readme.MD")

    def test_find_root_readme_path_returns_none_without_root_readme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            root_dir = Path(tmp_dir_name)
            (root_dir / "docs").mkdir()
            self.assertIsNone(repo_sync.find_root_readme_path(root_dir))


class ReadmeRewriteGitFlowTests(unittest.TestCase):
    def test_rewrite_target_readme_links_commits_and_pushes_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            tmp_dir = Path(tmp_dir_name)
            remote_dir = tmp_dir / "remote.git"
            seed_dir = tmp_dir / "seed"
            repo_sync.run_command(["git", "init", "--bare", str(remote_dir)], safe_command="git init --bare remote.git")
            repo_sync.run_command(
                ["git", "clone", remote_dir.as_uri(), str(seed_dir)],
                safe_command="git clone [redacted-url] seed",
            )
            repo_sync.run_command(
                repo_sync.git_repo_command(seed_dir, "checkout", "-b", "main"),
                safe_command=f"git -C {seed_dir} checkout -b main",
            )
            repo_sync.run_command(
                repo_sync.git_repo_command(seed_dir, "config", "user.name", "seed"),
                safe_command=f"git -C {seed_dir} config user.name seed",
            )
            repo_sync.run_command(
                repo_sync.git_repo_command(seed_dir, "config", "user.email", "seed@example.com"),
                safe_command=f"git -C {seed_dir} config user.email seed@example.com",
            )
            (seed_dir / "README.md").write_text(
                "Docs: https://github.com/source/repo/blob/main/docs/guide.md\n",
                encoding="utf-8",
            )
            repo_sync.run_command(
                repo_sync.git_repo_command(seed_dir, "add", "--", "README.md"),
                safe_command=f"git -C {seed_dir} add -- README.md",
            )
            repo_sync.run_command(
                repo_sync.git_repo_command(seed_dir, "commit", "-m", "seed"),
                safe_command=f"git -C {seed_dir} commit -m seed",
            )
            repo_sync.run_command(
                repo_sync.git_repo_command(seed_dir, "push", "origin", "HEAD:main"),
                safe_command=f"git -C {seed_dir} push origin HEAD:main",
            )

            client = LocalTestTargetClient(remote_dir.as_uri())
            target = {"namespace": "mirror", "name": "repo", "rewrite_readme_links": True}
            source_repo = {"full_name": "source/repo", "default_branch": "main"}

            first = repo_sync.rewrite_target_readme_links(client, target, source_repo, tmp_dir)
            self.assertEqual(first.status, "committed")
            self.assertEqual(first.replacements, 1)

            rewritten_readme = repo_sync.run_command(
                ["git", "--git-dir", str(remote_dir), "show", "main:README.md"],
                safe_command="git --git-dir remote.git show main:README.md",
            ).stdout
            latest_subject = repo_sync.run_command(
                ["git", "--git-dir", str(remote_dir), "log", "-1", "--format=%s", "main"],
                safe_command="git --git-dir remote.git log -1 --format=%s main",
            ).stdout.strip()

            self.assertIn("https://mirror.example/mirror/repo/blob/main/docs/guide.md", rewritten_readme)
            self.assertEqual(latest_subject, "chore(repo-sync): rewrite README links for localtest")

            second = repo_sync.rewrite_target_readme_links(client, target, source_repo, tmp_dir)
            self.assertEqual(second.status, "unchanged")
            self.assertEqual(second.replacements, 0)


if __name__ == "__main__":
    unittest.main()
