from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYNC = ROOT / "scripts" / "sync.py"
COLLECT = ROOT / "scripts" / "collect_identities.py"


def run(command: list[str | Path], *, cwd: Path | None = None, env: dict[str, str] | None = None, check: bool = True):
    merged = os.environ.copy()
    merged["PYTHONUTF8"] = "1"
    if env:
        merged.update(env)
    result = subprocess.run(
        [str(item) for item in command],
        cwd=cwd,
        env=merged,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"command failed: {command}\nstdout={result.stdout}\nstderr={result.stderr}")
    return result


class SyncIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="githubfilter-test-")
        self.root = Path(self.temp.name)
        self.source_work = self.root / "source-work"
        self.source_bare = self.root / "source.git"
        self.target_bare = self.root / "target.git"
        self.mailmap = self.root / "identity.mailmap"
        self.config = self.root / "repositories.json"
        run(["git", "init", "--initial-branch=main", self.source_work])
        run(["git", "-C", self.source_work, "config", "user.name", "Test Committer"])
        run(["git", "-C", self.source_work, "config", "user.email", "committer@external.example"])
        (self.source_work / "README.md").write_text("one\n", encoding="utf-8")
        run(["git", "-C", self.source_work, "add", "README.md"])
        self.commit("first", "Alice External", "alice@users.noreply.github.com")
        run(["git", "-C", self.source_work, "branch", "release"])
        run(["git", "clone", "--bare", self.source_work, self.source_bare])
        run(["git", "init", "--bare", self.target_bare])
        self.mailmap.write_text(
            "Alice Corp（E001） <alice@corp.example.com> Alice External <alice@users.noreply.github.com>\n"
            "Alice Corp（E001） <alice@corp.example.com> Test Committer <committer@external.example>\n",
            encoding="utf-8",
        )
        self.write_config()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def commit(self, message: str, name: str, email: str) -> None:
        env = {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": "Test Committer",
            "GIT_COMMITTER_EMAIL": "committer@external.example",
        }
        run(["git", "-C", self.source_work, "commit", "-m", message], env=env)

    def write_config(self) -> None:
        document = {
            "schemaVersion": 1,
            "defaults": {
                "mailmap": str(self.mailmap),
                "lfs": "false",
                "requireAtomicPush": True,
                "identityPolicy": {
                    "allowedEmailRegex": "@corp\\.example\\.com$",
                    "allowedNameRegex": ".+（E[0-9]+）$",
                },
            },
            "repositories": [
                {
                    "id": "one",
                    "enabled": True,
                    "sourceUrl": str(self.source_bare),
                    "targetUrl": str(self.target_bare),
                    "branches": [
                        {
                            "id": "main",
                            "sourceBranch": "main",
                            "targetBranch": "mirror-main",
                        },
                        {
                            "id": "release",
                            "sourceBranch": "release",
                            "targetBranch": "mirror-release",
                        },
                    ],
                },
                {
                    "id": "disabled-two",
                    "enabled": False,
                    "sourceUrl": str(self.source_bare),
                    "targetUrl": str(self.target_bare),
                    "branches": [
                        {
                            "id": "main",
                            "sourceBranch": "main",
                            "targetBranch": "unused-main",
                        }
                    ],
                },
            ],
        }
        self.config.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    def sync(self, *extra: str, check: bool = True):
        return run(
            [sys.executable, SYNC, "--config", self.config, "--task-id", "one.main", *extra],
            check=check,
        )

    def target_tip(self) -> str:
        return run(["git", "--git-dir", self.target_bare, "rev-parse", "refs/heads/mirror-main"]).stdout.strip()

    def test_matrix_only_contains_enabled_tasks(self) -> None:
        result = run([sys.executable, SYNC, "--config", self.config, "--print-matrix"])
        self.assertEqual({"task_id": ["one.main", "one.release"]}, json.loads(result.stdout))

    def test_repository_selection_expands_all_branches(self) -> None:
        result = run(
            [
                sys.executable,
                SYNC,
                "--config",
                self.config,
                "--repository-id",
                "one",
                "--print-matrix",
            ]
        )
        self.assertEqual({"task_id": ["one.main", "one.release"]}, json.loads(result.stdout))

    def test_duplicate_target_branch_is_rejected(self) -> None:
        document = json.loads(self.config.read_text(encoding="utf-8"))
        document["repositories"][0]["branches"][1]["targetBranch"] = "mirror-main"
        self.config.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        result = run(
            [sys.executable, SYNC, "--config", self.config, "--validate-config"],
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("写入同一引用", result.stderr)

    def test_second_source_target_branch_mapping(self) -> None:
        result = run(
            [
                sys.executable,
                SYNC,
                "--config",
                self.config,
                "--task-id",
                "one.release",
            ]
        )
        self.assertIn("one.release=pushed", result.stdout)
        release_tip = run(
            ["git", "--git-dir", self.target_bare, "rev-parse", "refs/heads/mirror-release"]
        ).stdout.strip()
        state_tip = run(
            [
                "git",
                "--git-dir",
                self.target_bare,
                "rev-parse",
                "refs/heads/githubfilter-state/one/release",
            ]
        ).stdout.strip()
        self.assertRegex(release_tip, r"^[0-9a-f]{40}$")
        self.assertRegex(state_tip, r"^[0-9a-f]{40}$")

    def test_collect_identities(self) -> None:
        output = self.root / "identities.tsv"
        run(
            [
                sys.executable,
                COLLECT,
                "--source-url",
                self.source_bare,
                "--branch",
                "main",
                "--output",
                output,
            ],
            env={"SOURCE_GIT_USERNAME": "", "SOURCE_GIT_TOKEN": ""},
        )
        content = output.read_text(encoding="utf-8")
        self.assertIn("Alice External\talice@users.noreply.github.com\tauthor\t1", content)
        self.assertIn("Test Committer\tcommitter@external.example\tcommitter\t1", content)

    def test_initial_incremental_and_drift_guard(self) -> None:
        first = self.sync()
        self.assertIn("one.main=pushed", first.stdout)
        first_tip = self.target_tip()
        author = run(
            ["git", "--git-dir", self.target_bare, "show", "-s", "--format=%an <%ae>|%cn <%ce>", first_tip]
        ).stdout.strip()
        self.assertEqual(
            "Alice Corp（E001） <alice@corp.example.com>|Alice Corp（E001） <alice@corp.example.com>",
            author,
        )
        unchanged = self.sync()
        self.assertIn("one.main=unchanged", unchanged.stdout)

        with (self.source_work / "README.md").open("a", encoding="utf-8") as stream:
            stream.write("two\n")
        run(["git", "-C", self.source_work, "add", "README.md"])
        self.commit("second", "Alice External", "alice@users.noreply.github.com")
        run(["git", "-C", self.source_work, "push", str(self.source_bare), "main"])
        second = self.sync()
        self.assertIn("one.main=pushed", second.stdout)
        second_tip = self.target_tip()
        self.assertNotEqual(first_tip, second_tip)

        self.mailmap.write_text(
            "Alice Renamed（E001） <alice.renamed@corp.example.com> Alice External <alice@users.noreply.github.com>\n"
            "Alice Renamed（E001） <alice.renamed@corp.example.com> Test Committer <committer@external.example>\n",
            encoding="utf-8",
        )
        drift = self.sync(check=False)
        self.assertNotEqual(0, drift.returncode)
        self.assertIn("历史提交映射漂移", drift.stderr)
        self.assertEqual(second_tip, self.target_tip())

    def test_unknown_identity_is_rejected_before_push(self) -> None:
        initial = self.sync()
        self.assertEqual(0, initial.returncode)
        before = self.target_tip()
        (self.source_work / "unknown.txt").write_text("unknown\n", encoding="utf-8")
        run(["git", "-C", self.source_work, "add", "unknown.txt"])
        self.commit("unknown", "Bob Unknown", "bob@users.noreply.github.com")
        run(["git", "-C", self.source_work, "push", str(self.source_bare), "main"])
        rejected = self.sync(check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("不符合公司身份策略", rejected.stderr)
        self.assertEqual(before, self.target_tip())


if __name__ == "__main__":
    unittest.main()
