#!/usr/bin/env python3
"""Collect unique author/committer identities before creating a mailmap."""

from __future__ import annotations

import argparse
import collections
import os
import tempfile
from pathlib import Path

from sync import SyncError, run, safe_url, write_askpass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--username-env", default="SOURCE_GIT_USERNAME")
    parser.add_argument("--token-env", default="SOURCE_GIT_TOKEN")
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix="githubfilter-identities-") as temporary:
            work = Path(temporary)
            auth: dict[str, str] = {}
            if os.environ.get(args.token_env) or os.environ.get(args.username_env):
                auth = {"usernameEnv": args.username_env, "tokenEnv": args.token_env}
            source_env, _ = write_askpass(work, auth)
            repo = work / "source.git"
            run(
                [
                    "git",
                    "clone",
                    "--bare",
                    "--no-local",
                    "--single-branch",
                    "--branch",
                    args.branch,
                    "--no-tags",
                    args.source_url,
                    repo,
                ],
                env={**source_env, "GIT_LFS_SKIP_SMUDGE": "1"},
            )
            result = run(
                [
                    "git",
                    "-C",
                    repo,
                    "log",
                    "--format=%aN%x09%aE%x09%cN%x09%cE",
                    f"refs/heads/{args.branch}",
                ]
            )
            counts: collections.Counter[tuple[str, str, str]] = collections.Counter()
            for line in result.stdout.splitlines():
                author_name, author_email, committer_name, committer_email = line.split("\t")
                counts[(author_name, author_email, "author")] += 1
                counts[(committer_name, committer_email, "committer")] += 1
            args.output.parent.mkdir(parents=True, exist_ok=True)
            rows = ["name\temail\trole\tcommit_count\n"]
            for (name, email, role), count in sorted(
                counts.items(), key=lambda item: (item[0][1].casefold(), item[0][0].casefold(), item[0][2])
            ):
                rows.append(f"{name}\t{email}\t{role}\t{count}\n")
            args.output.write_text("".join(rows), encoding="utf-8")
            print(f"已从 {safe_url(args.source_url)} 收集 {len(counts)} 条身份/角色记录：{args.output}")
            return 0
    except (SyncError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
