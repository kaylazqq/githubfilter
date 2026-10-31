#!/usr/bin/env python3
"""Rewrite Git identities and mirror one or more repositories safely.

Only author, committer, and annotated-tag tagger identities are rewritten.
The source repository is never modified.  Each run uses fresh bare clones and
pushes the rewritten branch plus an audit state branch to the target.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


ZERO_SHA = "0" * 40
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
IDENTITY_RE = re.compile(r"^(?P<name>.*) <(?P<email>[^<>]+)>$")
FILTER_REPO_COMMAND: list[str] | None = None


class SyncError(RuntimeError):
    """Expected configuration, validation, or Git operation failure."""


@dataclass(frozen=True)
class Identity:
    name: str
    email: str

    def render(self) -> str:
        return f"{self.name} <{self.email}>"


@dataclass(frozen=True)
class RepositoryTask:
    repository_id: str
    branch_id: str
    task_id: str
    source_url: str
    source_branch: str
    target_url: str
    target_branch: str
    state_branch: str
    mailmap: Path
    lfs: str
    require_atomic_push: bool
    allowed_email_regex: str
    allowed_name_regex: str | None
    source_auth: Mapping[str, str]
    target_auth: Mapping[str, str]


@dataclass(frozen=True)
class SyncResult:
    task_id: str
    status: str
    source_tip: str
    filtered_tip: str | None
    commit_count: int


def log(message: str) -> None:
    print(message, flush=True)


def safe_url(value: str) -> str:
    """Remove embedded credentials before a URL is logged."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid-url>"
    if not parsed.scheme or not parsed.netloc or "@" not in parsed.netloc:
        return value
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


def run(
    args: Sequence[str | Path],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [str(part) for part in args]
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=process_env,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        detail = stderr or stdout or f"exit code {completed.returncode}"
        raise SyncError(f"命令执行失败：{command[0]} {command[1] if len(command) > 1 else ''}\n{detail}")
    return completed


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def resolve_url(config: Mapping[str, Any], key: str, *, resolve_environment: bool) -> str:
    direct = config.get(f"{key}Url")
    env_name = config.get(f"{key}UrlEnv")
    if direct and env_name:
        raise SyncError(f"{key}Url 与 {key}UrlEnv 不能同时配置")
    if env_name:
        if not resolve_environment:
            return f"${{{env_name}}}"
        value = os.environ.get(str(env_name), "").strip()
        if not value:
            raise SyncError(f"环境变量 {env_name} 未配置")
        return value
    if not direct:
        raise SyncError(f"缺少 {key}Url 或 {key}UrlEnv")
    return str(direct)


def validate_branch(name: str, field: str) -> None:
    if not name or name.startswith("refs/") or name.startswith("-"):
        raise SyncError(f"{field} 必须是分支短名称：{name!r}")
    check = run(["git", "check-ref-format", f"refs/heads/{name}"], check=False)
    if check.returncode != 0:
        raise SyncError(f"{field} 不是合法 Git 分支名：{name!r}")


def load_tasks(
    config_path: Path,
    *,
    include_disabled: bool = False,
    resolve_environment: bool = True,
) -> list[RepositoryTask]:
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"无法读取配置 {config_path}: {exc}") from exc
    if document.get("schemaVersion") != 1:
        raise SyncError("仅支持 repositories.json schemaVersion=1")
    defaults = document.get("defaults", {})
    repositories = document.get("repositories")
    if not isinstance(defaults, Mapping) or not isinstance(repositories, list):
        raise SyncError("defaults 必须是对象，repositories 必须是数组")

    tasks: list[RepositoryTask] = []
    seen_repositories: set[str] = set()
    seen_tasks: set[str] = set()
    seen_writable_refs: dict[tuple[str, str], str] = {}
    for raw in repositories:
        if not isinstance(raw, Mapping):
            raise SyncError("repositories 中的每一项都必须是对象")
        repository = deep_merge(defaults, raw)
        repository_id = str(repository.get("id", ""))
        if not ID_RE.fullmatch(repository_id):
            raise SyncError(f"非法仓库 id：{repository_id!r}")
        if repository_id in seen_repositories:
            raise SyncError(f"重复仓库 id：{repository_id}")
        seen_repositories.add(repository_id)

        branches_value = raw.get("branches")
        legacy = branches_value is None
        if legacy:
            branch_entries: list[Mapping[str, Any]] = [{}]
        else:
            if not isinstance(branches_value, list) or not branches_value:
                raise SyncError(f"仓库 {repository_id} 的 branches 必须是非空数组")
            if not all(isinstance(item, Mapping) for item in branches_value):
                raise SyncError(f"仓库 {repository_id} 的每个 branches 项都必须是对象")
            branch_entries = branches_value

        repository_enabled = bool(repository.get("enabled", False))
        for branch_entry in branch_entries:
            branch = deep_merge(repository, branch_entry)
            if legacy:
                branch_id = repository_id
                task_id = repository_id
                task_enabled = repository_enabled
            else:
                branch_id = str(branch_entry.get("id", ""))
                if not ID_RE.fullmatch(branch_id):
                    raise SyncError(f"仓库 {repository_id} 的非法分支映射 id：{branch_id!r}")
                task_id = f"{repository_id}.{branch_id}"
                if not ID_RE.fullmatch(task_id):
                    raise SyncError(f"组合任务 id 过长或非法：{task_id!r}")
                task_enabled = repository_enabled and bool(branch_entry.get("enabled", True))
            if task_id in seen_tasks:
                raise SyncError(f"重复任务 id：{task_id}")
            seen_tasks.add(task_id)
            if not task_enabled and not include_disabled:
                continue

            source_branch = str(branch.get("sourceBranch", "main"))
            target_branch = str(branch.get("targetBranch", source_branch))
            default_state = (
                f"githubfilter-state/{repository_id}"
                if legacy
                else f"githubfilter-state/{repository_id}/{branch_id}"
            )
            state_branch = str(branch.get("stateBranch", default_state))
            validate_branch(source_branch, "sourceBranch")
            validate_branch(target_branch, "targetBranch")
            validate_branch(state_branch, "stateBranch")
            if target_branch == state_branch:
                raise SyncError(f"任务 {task_id} 的 targetBranch 与 stateBranch 不能相同")

            mailmap_value = branch.get("mailmap")
            if not mailmap_value:
                raise SyncError(f"任务 {task_id} 缺少 mailmap")
            mailmap = Path(str(mailmap_value))
            if not mailmap.is_absolute():
                mailmap = (config_path.parent / mailmap).resolve()

            lfs = str(branch.get("lfs", "auto")).lower()
            if lfs not in {"auto", "true", "false"}:
                raise SyncError(f"任务 {task_id} 的 lfs 只能是 auto、true 或 false")
            policy = branch.get("identityPolicy", {})
            if not isinstance(policy, Mapping) or not policy.get("allowedEmailRegex"):
                raise SyncError(f"任务 {task_id} 缺少 identityPolicy.allowedEmailRegex")
            allowed_email_regex = str(policy["allowedEmailRegex"])
            allowed_name_regex = policy.get("allowedNameRegex")
            try:
                re.compile(allowed_email_regex)
                if allowed_name_regex:
                    re.compile(str(allowed_name_regex))
            except re.error as exc:
                raise SyncError(f"任务 {task_id} 的身份正则无效：{exc}") from exc

            source_auth = branch.get("sourceAuth", {})
            target_auth = branch.get("targetAuth", {})
            if not isinstance(source_auth, Mapping) or not isinstance(target_auth, Mapping):
                raise SyncError(f"任务 {task_id} 的认证配置必须是对象")
            source_url = resolve_url(branch, "source", resolve_environment=resolve_environment)
            target_url = resolve_url(branch, "target", resolve_environment=resolve_environment)
            for writable_ref, role in ((target_branch, "目标"), (state_branch, "状态")):
                key = (target_url, writable_ref)
                if key in seen_writable_refs:
                    raise SyncError(
                        f"任务 {task_id} 的{role}分支与 {seen_writable_refs[key]} 写入同一引用：{writable_ref}"
                    )
                seen_writable_refs[key] = task_id
            tasks.append(
                RepositoryTask(
                    repository_id=repository_id,
                    branch_id=branch_id,
                    task_id=task_id,
                    source_url=source_url,
                    source_branch=source_branch,
                    target_url=target_url,
                    target_branch=target_branch,
                    state_branch=state_branch,
                    mailmap=mailmap,
                    lfs=lfs,
                    require_atomic_push=bool(branch.get("requireAtomicPush", True)),
                    allowed_email_regex=allowed_email_regex,
                    allowed_name_regex=str(allowed_name_regex) if allowed_name_regex else None,
                    source_auth={str(k): str(v) for k, v in source_auth.items()},
                    target_auth={str(k): str(v) for k, v in target_auth.items()},
                )
            )
    return tasks


def write_askpass(directory: Path, auth: Mapping[str, str]) -> tuple[dict[str, str], Path | None]:
    username_env = auth.get("usernameEnv")
    token_env = auth.get("tokenEnv")
    if not username_env and not token_env:
        return {"GIT_TERMINAL_PROMPT": "0"}, None
    if not username_env or not token_env:
        raise SyncError("认证配置必须同时包含 usernameEnv 和 tokenEnv")
    username = os.environ.get(username_env, "")
    token = os.environ.get(token_env, "")
    if not username or not token:
        raise SyncError(f"认证环境变量未完整配置：{username_env}、{token_env}")
    script = directory / f"askpass-{hashlib.sha256((username_env + token_env).encode()).hexdigest()[:10]}.sh"
    script.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *Username*|*username*) printf '%s\\n' \"$GITHUBFILTER_AUTH_USERNAME\" ;;\n"
        "  *) printf '%s\\n' \"$GITHUBFILTER_AUTH_TOKEN\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": str(script),
        "GIT_ASKPASS_REQUIRE": "force",
        "GITHUBFILTER_AUTH_USERNAME": username,
        "GITHUBFILTER_AUTH_TOKEN": token,
    }, script


def require_tools() -> dict[str, str]:
    global FILTER_REPO_COMMAND
    versions: dict[str, str] = {}
    git_version = run(["git", "--version"], check=False)
    if git_version.returncode != 0:
        raise SyncError("缺少必需工具：git")
    versions["git"] = (git_version.stdout or git_version.stderr).strip()

    candidates: list[list[str]] = []
    executable = shutil.which("git-filter-repo")
    if executable:
        candidates.append([executable])
    if importlib.util.find_spec("git_filter_repo") is not None:
        candidates.append([sys.executable, "-m", "git_filter_repo"])
    for candidate in candidates:
        result = run([*candidate, "--version"], check=False)
        if result.returncode == 0:
            FILTER_REPO_COMMAND = candidate
            versions["gitFilterRepo"] = (result.stdout or result.stderr).strip()
            break
    if FILTER_REPO_COMMAND is None:
        raise SyncError("缺少必需工具：git-filter-repo")
    lfs = run(["git", "lfs", "version"], check=False)
    if lfs.returncode == 0:
        versions["gitLfs"] = (lfs.stdout or lfs.stderr).strip()
    return versions


def validate_mailmap_file(mailmap: Path) -> None:
    try:
        lines = mailmap.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SyncError(f"无法读取 mailmap {mailmap}: {exc}") from exc
    records = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if not records:
        raise SyncError(f"mailmap 尚未配置有效映射：{mailmap}")
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not re.search(r"<[^<>\s]+@[^<>\s]+>", stripped):
            raise SyncError(f"mailmap 第 {number} 行格式无效：{line}")


def git(repo: Path, *args: str, env: Mapping[str, str] | None = None, input_text: str | None = None) -> str:
    return run(["git", "-C", repo, *args], env=env, input_text=input_text).stdout


def collect_identities(repo: Path, ref: str) -> list[Identity]:
    output = git(repo, "log", "--format=%aN%x09%aE%x09%cN%x09%cE", ref)
    identities: set[Identity] = set()
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            raise SyncError(f"无法解析 Git 身份记录：{line!r}")
        identities.add(Identity(fields[0], fields[1]))
        identities.add(Identity(fields[2], fields[3]))
    return sorted(identities, key=lambda item: (item.email.casefold(), item.name.casefold()))


def check_identity_policy(task: RepositoryTask, identities: Iterable[Identity], label: str) -> None:
    email_rule = re.compile(task.allowed_email_regex)
    name_rule = re.compile(task.allowed_name_regex) if task.allowed_name_regex else None
    violations = [
        identity.render()
        for identity in identities
        if not email_rule.search(identity.email) or (name_rule and not name_rule.search(identity.name))
    ]
    if violations:
        preview = "\n".join(f"  - {item}" for item in violations[:30])
        suffix = f"\n  ... 另有 {len(violations) - 30} 条" if len(violations) > 30 else ""
        raise SyncError(f"{label}存在不符合公司身份策略的记录：\n{preview}{suffix}")


def canonicalize_identities(task: RepositoryTask, repo: Path, identities: list[Identity]) -> list[Identity]:
    payload = "\n".join(identity.render() for identity in identities) + "\n"
    result = run(
        ["git", "-C", repo, "-c", f"mailmap.file={task.mailmap}", "check-mailmap", "--stdin"],
        input_text=payload,
    )
    lines = result.stdout.splitlines()
    if len(lines) != len(identities):
        raise SyncError("git check-mailmap 返回的身份数量异常")
    canonical: list[Identity] = []
    for line in lines:
        match = IDENTITY_RE.fullmatch(line)
        if not match:
            raise SyncError(f"git check-mailmap 返回了无法解析的身份：{line!r}")
        canonical.append(Identity(match.group("name"), match.group("email")))
    check_identity_policy(task, canonical, "映射后")
    return canonical


def parse_ls_remote(output: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split("\t", 1)
        if len(fields) == 2:
            refs[fields[1]] = fields[0]
    return refs


def read_json_from_ref(repo: Path, ref: str, path: str) -> dict[str, Any]:
    result = run(["git", "-C", repo, "show", f"{ref}:{path}"], check=False)
    if result.returncode != 0:
        raise SyncError(f"状态分支 {ref} 缺少或无法读取 {path}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SyncError(f"状态文件 {path} 不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise SyncError(f"状态文件 {path} 必须是 JSON 对象")
    return value


def parse_commit_map(text: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        fields = line.split()
        if not fields or fields == ["old", "new"]:
            continue
        if len(fields) != 2 or not all(re.fullmatch(r"[0-9a-f]{40}", field) for field in fields):
            raise SyncError(f"commit-map 第 {number} 行格式无效")
        mapping[fields[0]] = fields[1]
    if not mapping:
        raise SyncError("commit-map 为空")
    return mapping


def verify_mapping_stability(previous: Mapping[str, str], current: Mapping[str, str]) -> None:
    drift: list[tuple[str, str, str | None]] = []
    for old_sha, previous_new in previous.items():
        current_new = current.get(old_sha)
        if current_new != previous_new:
            drift.append((old_sha, previous_new, current_new))
    if drift:
        preview = "\n".join(
            f"  - {old}: {before} -> {after or '<missing>'}" for old, before, after in drift[:20]
        )
        raise SyncError(
            "检测到历史提交映射漂移，已阻断推送。通常是旧作者映射或工具版本发生变化：\n" + preview
        )


def commit_tree_map(repo: Path, ref: str) -> dict[str, str]:
    output = git(repo, "log", "--format=%H%x09%T", ref)
    result: dict[str, str] = {}
    for line in output.splitlines():
        commit, tree = line.split("\t", 1)
        result[commit] = tree
    return result


def verify_rewrite(raw_repo: Path, filtered_repo: Path, source_ref: str, filtered_ref: str, mapping: Mapping[str, str]) -> None:
    source_commits = commit_tree_map(raw_repo, source_ref)
    filtered_commits = commit_tree_map(filtered_repo, filtered_ref)
    if len(source_commits) != len(filtered_commits):
        raise SyncError(f"重写前后提交数不一致：{len(source_commits)} != {len(filtered_commits)}")
    if set(source_commits) != set(mapping):
        raise SyncError("commit-map 未完整覆盖源分支提交")
    for old_sha, source_tree in source_commits.items():
        new_sha = mapping.get(old_sha)
        if not new_sha or new_sha == ZERO_SHA:
            raise SyncError(f"源提交被意外删除：{old_sha}")
        if filtered_commits.get(new_sha) != source_tree:
            raise SyncError(f"提交对应的文件树发生变化：{old_sha} -> {new_sha}")


def has_lfs_objects(repo: Path, ref: str, versions: Mapping[str, str]) -> bool:
    if "gitLfs" not in versions:
        result = git(repo, "ls-tree", "-r", "--name-only", ref)
        if any(path.endswith(".gitattributes") for path in result.splitlines()):
            raise SyncError("仓库可能使用 Git LFS，但执行环境未安装 git-lfs")
        return False
    result = run(["git", "-C", repo, "lfs", "ls-files", "--all"], check=False)
    if result.returncode != 0:
        raise SyncError(f"无法检查 Git LFS 对象：{result.stderr.strip()}")
    return bool(result.stdout.strip())


def create_state_commit(
    repo: Path,
    *,
    state_ref: str,
    previous_state_sha: str | None,
    commit_map_file: Path,
    metadata_file: Path,
    identities_file: Path,
) -> str:
    blobs: dict[str, str] = {}
    for name, path in (
        ("commit-map.tsv", commit_map_file),
        ("metadata.json", metadata_file),
        ("identities.tsv", identities_file),
    ):
        blobs[name] = git(repo, "hash-object", "-w", str(path)).strip()
    # -z avoids Windows text-mode CRLF translation becoming part of a tree filename.
    tree_input = "".join(f"100644 blob {blobs[name]}\t{name}\0" for name in sorted(blobs))
    tree = run(["git", "-C", repo, "mktree", "-z"], input_text=tree_input).stdout.strip()
    command: list[str] = ["git", "-C", str(repo), "commit-tree", tree]
    if previous_state_sha:
        command.extend(["-p", previous_state_sha])
    command.extend(["-m", "Record repository rewrite state"])
    identity_env = {
        "GIT_AUTHOR_NAME": "GitHub Filter Sync",
        "GIT_AUTHOR_EMAIL": "github-filter-sync@invalid",
        "GIT_COMMITTER_NAME": "GitHub Filter Sync",
        "GIT_COMMITTER_EMAIL": "github-filter-sync@invalid",
    }
    commit = run(command, env=identity_env).stdout.strip()
    git(repo, "update-ref", state_ref, commit)
    return commit


def write_report(
    report_dir: Path | None,
    task: RepositoryTask,
    commit_map_file: Path,
    metadata_file: Path,
    identities_file: Path,
) -> None:
    if not report_dir:
        return
    destination = report_dir / task.task_id
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(commit_map_file, destination / "commit-map.tsv")
    shutil.copy2(metadata_file, destination / "metadata.json")
    shutil.copy2(identities_file, destination / "identities.tsv")


def sync_one(
    task: RepositoryTask,
    *,
    versions: Mapping[str, str],
    dry_run: bool,
    report_dir: Path | None,
    allow_bootstrap_over_existing_target: bool,
) -> SyncResult:
    log(f"[{task.task_id}] 开始：{safe_url(task.source_url)} -> {safe_url(task.target_url)}")
    validate_mailmap_file(task.mailmap)
    work_root_value = os.environ.get("GITHUBFILTER_WORK_ROOT")
    work_root = Path(work_root_value).resolve() if work_root_value else None
    if work_root:
        work_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"githubfilter-{task.task_id}-", dir=work_root) as temporary:
        work = Path(temporary)
        source_env, _ = write_askpass(work, task.source_auth)
        target_env, _ = write_askpass(work, task.target_auth)
        raw_repo = work / "raw.git"
        filtered_repo = work / "filtered.git"
        source_ref = f"refs/heads/{task.source_branch}"
        target_ref = f"refs/heads/{task.target_branch}"
        state_ref = f"refs/heads/{task.state_branch}"

        run(
            [
                "git",
                "clone",
                "--bare",
                "--no-local",
                "--single-branch",
                "--branch",
                task.source_branch,
                "--no-tags",
                task.source_url,
                raw_repo,
            ],
            env={**source_env, "GIT_LFS_SKIP_SMUDGE": "1"},
        )
        source_tip = git(raw_repo, "rev-parse", source_ref).strip()
        commit_count = int(git(raw_repo, "rev-list", "--count", source_ref).strip())
        mailmap_sha256 = hashlib.sha256(task.mailmap.read_bytes()).hexdigest()

        git(raw_repo, "remote", "add", "target", task.target_url)
        remote_output = run(
            ["git", "-C", raw_repo, "ls-remote", "--refs", "target", target_ref, state_ref],
            env=target_env,
        ).stdout
        remote_refs = parse_ls_remote(remote_output)
        old_target_sha = remote_refs.get(target_ref)
        old_state_sha = remote_refs.get(state_ref)
        if old_state_sha and not old_target_sha:
            raise SyncError(
                f"状态分支存在但目标代码分支缺失，拒绝自动恢复：{state_ref}={old_state_sha}"
            )
        if old_target_sha and not old_state_sha and not allow_bootstrap_over_existing_target:
            raise SyncError(
                f"目标代码分支已存在但状态分支缺失，拒绝自动接管：{target_ref}={old_target_sha}"
            )
        previous_metadata: dict[str, Any] | None = None
        previous_map: dict[str, str] = {}
        if old_state_sha:
            git(raw_repo, "fetch", "--no-tags", "target", f"{state_ref}:refs/githubfilter/previous-state", env=target_env)
            previous_metadata = read_json_from_ref(raw_repo, "refs/githubfilter/previous-state", "metadata.json")
            previous_map_text = git(raw_repo, "show", "refs/githubfilter/previous-state:commit-map.tsv")
            previous_map = parse_commit_map(previous_map_text)
            unchanged_inputs = (
                previous_metadata.get("sourceTip") == source_tip
                and previous_metadata.get("mailmapSha256") == mailmap_sha256
                and previous_metadata.get("tools") == dict(versions)
            )
            if unchanged_inputs:
                expected_target = previous_metadata.get("filteredTip")
                if old_target_sha != expected_target:
                    raise SyncError(
                        "源分支未变化，但目标分支与已记录状态不一致；请先调查目标仓是否被人工修改"
                    )
                log(f"[{task.task_id}] 源分支未变化，跳过重写与推送")
                return SyncResult(task.task_id, "unchanged", source_tip, str(expected_target), commit_count)
        source_identities = collect_identities(raw_repo, source_ref)
        canonical_identities = canonicalize_identities(task, raw_repo, source_identities)
        identity_pairs = sorted(
            zip(source_identities, canonical_identities),
            key=lambda pair: (pair[0].email.casefold(), pair[0].name.casefold()),
        )

        run(["git", "init", "--bare", filtered_repo])
        if FILTER_REPO_COMMAND is None:
            raise SyncError("git-filter-repo 尚未完成初始化")
        run(
            [
                *FILTER_REPO_COMMAND,
                "--source",
                raw_repo,
                "--target",
                filtered_repo,
                "--mailmap",
                task.mailmap,
                "--refs",
                source_ref,
                "--preserve-commit-hashes",
                "--preserve-commit-encoding",
                "--prune-empty",
                "never",
                "--prune-degenerate",
                "never",
            ]
        )
        filtered_tip = git(filtered_repo, "rev-parse", source_ref).strip()
        commit_map_file = filtered_repo / "filter-repo" / "commit-map"
        if not commit_map_file.is_file():
            raise SyncError("git-filter-repo 未生成 commit-map")
        current_map = parse_commit_map(commit_map_file.read_text(encoding="utf-8"))
        verify_mapping_stability(previous_map, current_map)
        verify_rewrite(raw_repo, filtered_repo, source_ref, source_ref, current_map)
        filtered_identities = collect_identities(filtered_repo, source_ref)
        check_identity_policy(task, filtered_identities, "重写后仓库")

        identities_file = work / "identities.tsv"
        identities_file.write_text(
            "source_name\tsource_email\tcanonical_name\tcanonical_email\n"
            + "".join(
                f"{source.name}\t{source.email}\t{canonical.name}\t{canonical.email}\n"
                for source, canonical in identity_pairs
            ),
            encoding="utf-8",
        )
        metadata = {
            "schemaVersion": 1,
            "taskId": task.task_id,
            "repositoryId": task.repository_id,
            "branchId": task.branch_id,
            "sourceUrl": safe_url(task.source_url),
            "sourceBranch": task.source_branch,
            "sourceTip": source_tip,
            "targetBranch": task.target_branch,
            "filteredTip": filtered_tip,
            "commitCount": commit_count,
            "mailmapSha256": mailmap_sha256,
            "tools": dict(versions),
            "generatedAt": datetime.now(timezone.utc).isoformat(),
        }
        metadata_file = work / "metadata.json"
        metadata_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_report(report_dir, task, commit_map_file, metadata_file, identities_file)

        git(filtered_repo, "remote", "add", "target", task.target_url)
        if old_state_sha:
            git(
                filtered_repo,
                "fetch",
                "--no-tags",
                "target",
                f"{state_ref}:refs/githubfilter/previous-state",
                env=target_env,
            )
        create_state_commit(
            filtered_repo,
            state_ref=state_ref,
            previous_state_sha=old_state_sha,
            commit_map_file=commit_map_file,
            metadata_file=metadata_file,
            identities_file=identities_file,
        )

        lfs_required = task.lfs == "true" or (
            task.lfs == "auto" and has_lfs_objects(raw_repo, source_ref, versions)
        )
        if dry_run:
            log(f"[{task.task_id}] dry-run 验证通过，未推送任何对象")
            return SyncResult(task.task_id, "dry-run", source_tip, filtered_tip, commit_count)

        if lfs_required:
            if "gitLfs" not in versions:
                raise SyncError("任务要求同步 Git LFS，但执行环境未安装 git-lfs")
            log(f"[{task.task_id}] 先同步 LFS 对象")
            git(raw_repo, "lfs", "fetch", "--all", "origin", env=source_env)
            git(raw_repo, "lfs", "push", "--all", "target", env=target_env)

        push_args = ["git", "-C", str(filtered_repo), "push"]
        if task.require_atomic_push:
            push_args.append("--atomic")
        push_args.extend(
            [
                f"--force-with-lease={target_ref}:{old_target_sha or ''}",
                f"--force-with-lease={state_ref}:{old_state_sha or ''}",
                "target",
                f"{source_ref}:{target_ref}",
                f"{state_ref}:{state_ref}",
            ]
        )
        run(push_args, env=target_env)
        log(f"[{task.task_id}] 同步完成：{source_tip} -> {filtered_tip}")
        return SyncResult(task.task_id, "pushed", source_tip, filtered_tip, commit_count)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/repositories.json"))
    parser.add_argument("--task-id", action="append", help="执行指定的仓库.分支任务；可重复")
    parser.add_argument("--repository-id", action="append", help="执行指定仓库下的全部分支任务；可重复")
    parser.add_argument("--all-enabled", action="store_true", help="执行所有 enabled=true 的分支任务")
    parser.add_argument("--include-disabled", action="store_true", help="仅用于显式选择或配置校验")
    parser.add_argument("--print-matrix", action="store_true", help="输出 GitHub Actions matrix JSON")
    parser.add_argument("--validate-config", action="store_true", help="只校验配置")
    parser.add_argument("--dry-run", action="store_true", help="完成克隆、重写和校验，但不推送")
    parser.add_argument("--report-dir", type=Path, help="保存非敏感审计报告")
    parser.add_argument(
        "--allow-bootstrap-over-existing-target",
        action="store_true",
        help="首次接管已有目标分支；仅限人工确认后的迁移",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = args.config.resolve()
    try:
        include_disabled = bool(
            args.include_disabled or args.task_id or args.repository_id or args.validate_config
        )
        tasks = load_tasks(
            config_path,
            include_disabled=include_disabled,
            resolve_environment=not (args.print_matrix or args.validate_config),
        )
        if args.task_id or args.repository_id:
            requested_tasks = set(args.task_id or [])
            requested_repositories = set(args.repository_id or [])
            available_tasks = {task.task_id for task in tasks}
            available_repositories = {task.repository_id for task in tasks}
            missing_tasks = requested_tasks - available_tasks
            missing_repositories = requested_repositories - available_repositories
            if missing_tasks:
                raise SyncError(f"配置中不存在任务：{', '.join(sorted(missing_tasks))}")
            if missing_repositories:
                raise SyncError(f"配置中不存在仓库：{', '.join(sorted(missing_repositories))}")
            tasks = [
                task
                for task in tasks
                if task.task_id in requested_tasks or task.repository_id in requested_repositories
            ]
        if args.print_matrix:
            print(json.dumps({"task_id": [task.task_id for task in tasks]}, separators=(",", ":")))
            return 0
        if args.validate_config:
            print(f"配置有效，共 {len(tasks)} 个可执行任务")
            return 0
        if not args.task_id and not args.repository_id and not args.all_enabled:
            raise SyncError(
                "必须指定 --task-id、--repository-id、--all-enabled、--print-matrix 或 --validate-config"
            )
        if not tasks:
            log("没有启用的同步任务")
            return 0
        versions = require_tools()
        results: list[SyncResult] = []
        failures: list[str] = []
        for task in tasks:
            try:
                results.append(
                    sync_one(
                        task,
                        versions=versions,
                        dry_run=args.dry_run,
                        report_dir=args.report_dir.resolve() if args.report_dir else None,
                        allow_bootstrap_over_existing_target=args.allow_bootstrap_over_existing_target,
                    )
                )
            except SyncError as exc:
                failures.append(f"[{task.task_id}] {exc}")
                print(failures[-1], file=sys.stderr, flush=True)
        if results:
            log("执行结果：" + ", ".join(f"{result.task_id}={result.status}" for result in results))
        if failures:
            raise SyncError("以下任务失败：\n" + "\n".join(failures))
        return 0
    except SyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
