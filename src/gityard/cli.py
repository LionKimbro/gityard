from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import sysconfig
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import lionscliapp as app
import machineroot

from gityard import __version__


SAFE = "safe"
UNSAFE_LOCAL_CHANGES = "unsafe-local-changes"
PROTECTED_LOCAL_ONLY = "protected-local-only"
BLOCKED_EDITABLE_INSTALL = "blocked-editable-install"
UNKNOWN = "unknown"


def main():
    _declare_app()
    app.main()


def _declare_app():
    app.declare_app("gityard", __version__)
    app.describe_app("Evaluate and manage a machine-global yard of Git repositories.")
    app.describe_app(
        (
            "Git Yard scans one level of repositories, records local and GitHub state, "
            "analyzes deletion safety, and only deletes when explicitly forced."
        ),
        "l",
    )
    app.declare_projectdir(".gityard")

    app.declare_key("github.user", "")
    app.declare_key("path.root", None)
    app.declare_key("force.delete", "0")
    app.declare_key("target.repo", "")

    app.describe_key("github.user", "GitHub username for scan-github")
    app.describe_key(
        "path.root",
        "Override the managed root directory; otherwise use machineroot github-checkouts",
    )
    app.describe_key(
        "force.delete",
        "Set to 1 to allow delete to remove a repository classified as non-safe",
    )
    app.describe_key(
        "target.repo",
        "Target repository selector for delete/clone: name, owner/repo, or path",
    )

    app.declare_cmd("scan-local", cmd_scan_local)
    app.declare_cmd("scan-github", cmd_scan_github)
    app.declare_cmd("scan", cmd_scan)
    app.declare_cmd("analyze", cmd_analyze)
    app.declare_cmd("status", cmd_status)
    app.declare_cmd("repos", cmd_repos)
    app.declare_cmd("pull", cmd_pull)
    app.declare_cmd("available", cmd_available)
    app.declare_cmd("delete", cmd_delete)
    app.declare_cmd("clone", cmd_clone)

    app.describe_cmd("scan-local", "Scan the yard root and write local-repos.json")
    app.describe_cmd("scan-github", "Fetch the configured GitHub repository list")
    app.describe_cmd("scan", "Run scan-local, scan-github, and analyze in sequence")
    app.describe_cmd("analyze", "Compute deletion safety for scanned local repositories")
    app.describe_cmd("status", "Print deletion-analysis.json grouped by safety")
    app.describe_cmd("repos", "Print a table showing local repository git state")
    app.describe_cmd("pull", "Pull clean repositories that are behind their upstream")
    app.describe_cmd("available", "List GitHub repositories not currently present locally")
    app.describe_cmd("delete", "Delete a local repository selected by --target.repo")
    app.describe_cmd("clone", "Clone --target.repo into the yard root")


def cmd_scan_local():
    root = _get_root_path()
    repos = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        if not (child / ".git").exists():
            continue
        repos.append(_scan_local_repo(child))

    app.write_json("local-repos.json", repos, "p2")
    print(f"scanned {len(repos)} local repositories into {app.get_path('local-repos.json', 'p')}")


def cmd_scan_github():
    user = _get_github_user()

    repos = _fetch_github_repos(user)
    app.write_json("github-repos.json", repos, "p2")
    print(f"fetched {len(repos)} GitHub repositories into {app.get_path('github-repos.json', 'p')}")


def cmd_scan():
    cmd_scan_local()
    cmd_scan_github()
    cmd_analyze()


def cmd_analyze():
    local_repos = app.read_json("local-repos.json", "p")
    github_repos = app.read_json("github-repos.json", "p")
    github_ids = {repo["repo_id"] for repo in github_repos}

    analysis = []
    for repo in local_repos:
        analysis.append(_analyze_repo(repo, github_ids))

    app.write_json("deletion-analysis.json", analysis, "p2")
    print(f"wrote {len(analysis)} deletion analyses to {app.get_path('deletion-analysis.json', 'p')}")


def cmd_status():
    analyses = app.read_json("deletion-analysis.json", "p")
    grouped = _group_by_safety(analyses)
    order = [
        SAFE,
        UNSAFE_LOCAL_CHANGES,
        PROTECTED_LOCAL_ONLY,
        BLOCKED_EDITABLE_INSTALL,
        UNKNOWN,
    ]

    total = len(analyses)
    print(f"gityard status: {total} repositories analyzed")
    for key in order:
        items = grouped[key]
        print("")
        print(f"[{key}] {len(items)}")
        for item in items:
            label = item["repo_id"] or item["path"]
            reason_text = "; ".join(item["reasons"])
            print(f"- {label}")
            print(f"  path: {item['path']}")
            print(f"  reasons: {reason_text}")


def cmd_repos():
    root = _get_root_path()
    repos = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        if not (child / ".git").exists():
            continue
        repos.append(_scan_local_repo(child))

    rows = [
        [
            repo["repo_name"],
            _repo_worktree_status(repo),
            repo["branch"] or "(detached)",
            repo["default_remote"] or "-",
            _format_sync_status(repo),
        ]
        for repo in repos
    ]
    _print_table(
        ["REPOSITORY", "DIRTY?", "BRANCH", "REMOTE", "AHEAD/BEHIND"],
        rows,
    )


def cmd_pull():
    root = _get_root_path()
    pulled = []
    skipped_dirty = []

    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        if not (child / ".git").exists():
            continue

        repo = _scan_local_repo(child)
        if repo["behind_count"] <= 0:
            continue
        if repo["ahead_count"] > 0:
            continue
        if _repo_has_local_changes(repo):
            skipped_dirty.append(repo)
            continue

        print(f"pulling {repo['repo_name']} ({_format_sync_status(repo)})")
        _run_git(["pull", "--ff-only"], child)
        pulled.append(repo)

    print("")
    print(f"pulled: {len(pulled)}")
    for repo in pulled:
        print(f"- {repo['repo_name']}")
        print(f"  branch: {repo['branch'] or '(detached)'}")
        print(f"  remote: {repo['default_remote'] or '-'}")
        print(f"  was: {_format_sync_status(repo)}")

    print("")
    print(f"behind but skipped because dirty: {len(skipped_dirty)}")
    for repo in skipped_dirty:
        print(f"- {repo['repo_name']}")
        print(f"  branch: {repo['branch'] or '(detached)'}")
        print(f"  remote: {repo['default_remote'] or '-'}")
        print(f"  status: {_repo_worktree_status(repo)}")
        print(f"  sync: {_format_sync_status(repo)}")


def cmd_available():
    local_repos = app.read_json("local-repos.json", "p")
    github_repos = app.read_json("github-repos.json", "p")
    available = _find_available_repos(local_repos, github_repos)

    print(f"gityard available: {len(available)} repositories on GitHub not present locally")
    for repo in available:
        print(f"- {repo['repo_id']}")
        print(f"  clone_url: {repo['clone_url']}")
        print(f"  archived: {'yes' if repo['is_archived'] else 'no'}")
        if repo["updated_at"]:
            print(f"  updated_at: {repo['updated_at']}")


def cmd_delete():
    target = app.ctx["target.repo"].strip()
    if not target:
        raise ValueError("delete requires --target.repo")

    analyses = app.read_json("deletion-analysis.json", "p")
    record = _find_analysis(analyses, target)
    if record is None:
        raise ValueError(f"No analyzed repository matched target {target!r}")

    if record["delete_safety"] != SAFE and app.ctx["force.delete"] != "1":
        print(f"refusing to delete {record['path']}")
        for reason in record["reasons"]:
            print(f"- {reason}")
        print("set --force.delete 1 to override this protection")
        return

    repo_path = Path(record["path"])
    if not repo_path.exists():
        raise FileNotFoundError(f"Repository path does not exist: {repo_path}")

    shutil.rmtree(repo_path, onerror=_handle_remove_readonly)
    print(f"deleted {repo_path}")


def cmd_clone():
    repo_id = app.ctx["target.repo"].strip()
    if not repo_id:
        raise ValueError("clone requires --target.repo owner/repo")
    if "/" not in repo_id:
        raise ValueError("clone expects --target.repo in owner/repo form")

    root = _get_root_path()
    target_path = root / repo_id.split("/", 1)[1]
    if target_path.exists():
        raise ValueError(f"Clone target already exists: {target_path}")

    clone_url = f"https://github.com/{repo_id}.git"
    _run_git(["clone", clone_url, str(target_path)], root)
    print(f"cloned {repo_id} into {target_path}")


def _get_root_path():
    configured = app.ctx["path.root"]
    if configured:
        root = Path(configured).expanduser()
    else:
        root = Path(machineroot.get("github-checkouts")).expanduser()

    if not root.exists():
        raise FileNotFoundError(
            f"Managed root does not exist: {root}. Set path.root or update machineroot github-checkouts."
        )
    if not root.is_dir():
        raise NotADirectoryError(f"Managed root is not a directory: {root}")
    return root


def _scan_local_repo(repo_path):
    remotes = _read_remotes(repo_path)
    default_remote = _choose_default_remote(remotes)
    remote_url = remotes.get(default_remote) if default_remote else None
    repo_id = _extract_repo_id(remote_url) if remote_url else None
    status_lines = _read_status_lines(repo_path)
    ahead_count, behind_count = _read_ahead_behind(repo_path)
    editable_sources = _find_editable_install_sources(repo_path)

    return {
        "path": str(repo_path),
        "repo_id": repo_id,
        "repo_name": repo_path.name,
        "remotes": remotes,
        "default_remote": default_remote,
        "remote_url": remote_url,
        "branch": _read_current_branch(repo_path),
        "is_dirty": _has_tracked_changes(status_lines),
        "ahead_count": ahead_count,
        "behind_count": behind_count,
        "has_untracked_files": _has_untracked_files(status_lines),
        "last_commit_time": _read_last_commit_time(repo_path),
        "editable_install_detected": bool(editable_sources),
        "editable_install_sources": editable_sources,
    }


def _read_remotes(repo_path):
    output = _run_git(["remote", "-v"], repo_path, allow_failure=True)
    if output.returncode != 0:
        return {}

    remotes = {}
    for line in output.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0]
        url = parts[1]
        if name not in remotes:
            remotes[name] = url
    return remotes


def _choose_default_remote(remotes):
    if "origin" in remotes:
        return "origin"
    if remotes:
        return sorted(remotes)[0]
    return None


def _extract_repo_id(remote_url):
    url = remote_url.strip()
    if url.endswith(".git"):
        url = url[:-4]

    if url.startswith("git@github.com:"):
        path = url.split(":", 1)[1]
    elif "github.com/" in url:
        path = url.split("github.com/", 1)[1]
    else:
        return None

    path = path.strip("/")
    parts = path.split("/")
    if len(parts) != 2:
        return None
    owner, repo = parts
    if not owner or not repo:
        return None
    return f"{owner}/{repo}"


def _read_status_lines(repo_path):
    output = _run_git(["status", "--porcelain"], repo_path)
    return [line for line in output.stdout.splitlines() if line.strip()]


def _read_current_branch(repo_path):
    output = _run_git(["branch", "--show-current"], repo_path, allow_failure=True)
    value = output.stdout.strip()
    return value or None


def _has_tracked_changes(status_lines):
    for line in status_lines:
        if not line.startswith("??"):
            return True
    return False


def _has_untracked_files(status_lines):
    for line in status_lines:
        if line.startswith("??"):
            return True
    return False


def _read_ahead_behind(repo_path):
    output = _run_git(
        ["rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
        repo_path,
        allow_failure=True,
    )
    if output.returncode != 0:
        return 0, 0

    parts = output.stdout.strip().split()
    if len(parts) != 2:
        return 0, 0
    behind_count = int(parts[0])
    ahead_count = int(parts[1])
    return ahead_count, behind_count


def _read_last_commit_time(repo_path):
    output = _run_git(["log", "-1", "--format=%cI"], repo_path, allow_failure=True)
    value = output.stdout.strip()
    return value or None


def _find_editable_install_sources(repo_path):
    repo_resolved = repo_path.resolve()
    hits = []
    seen = set()
    for directory in _site_packages_dirs():
        if not directory.exists():
            continue
        for pattern in ("*.egg-link", "*.pth"):
            for file_path in directory.glob(pattern):
                if _file_points_to_repo(file_path, repo_resolved):
                    text = str(file_path)
                    if text not in seen:
                        seen.add(text)
                        hits.append(text)
    return sorted(hits)


def _site_packages_dirs():
    dirs = []
    for key in ("purelib", "platlib"):
        path = sysconfig.get_paths().get(key)
        if path:
            dirs.append(Path(path))
    user_site = _safe_getusersitepackages()
    if user_site:
        dirs.append(Path(user_site))

    unique = []
    seen = set()
    for path in dirs:
        text = str(path)
        if text not in seen:
            seen.add(text)
            unique.append(path)
    return unique


def _safe_getusersitepackages():
    try:
        import site

        return site.getusersitepackages()
    except Exception:
        return None


def _file_points_to_repo(file_path, repo_path):
    try:
        for raw_line in file_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("import "):
                continue
            try:
                candidate = Path(line).expanduser()
                if not candidate.is_absolute():
                    candidate = (file_path.parent / candidate).resolve()
                else:
                    candidate = candidate.resolve()
            except OSError:
                continue
            if candidate == repo_path:
                return True
            if candidate.is_dir():
                try:
                    candidate.relative_to(repo_path)
                    return True
                except ValueError:
                    pass
                try:
                    repo_path.relative_to(candidate)
                    return True
                except ValueError:
                    pass
    except OSError:
        return False
    return False


def _fetch_github_repos(user):
    repos = []
    page = 1
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "gityard",
    }
    token = _read_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    while True:
        params = urllib.parse.urlencode({"per_page": "100", "page": str(page), "type": "owner"})
        url = f"https://api.github.com/users/{user}/repos?{params}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            message = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"GitHub request failed for {url}: {exc.code} {message}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GitHub request failed for {url}: {exc.reason}") from exc

        if not payload:
            break

        for item in payload:
            full_name = item.get("full_name")
            if not full_name:
                continue
            repos.append(
                {
                    "repo_id": full_name,
                    "clone_url": item.get("clone_url") or "",
                    "ssh_url": item.get("ssh_url") or "",
                    "is_archived": bool(item.get("archived")),
                    "updated_at": item.get("updated_at") or "",
                }
            )

        page += 1

    return repos


def _get_github_user():
    configured = app.ctx["github.user"]
    if configured:
        return configured.strip()

    github_user = _read_git_config_value("github.user")
    if github_user:
        return github_user

    email = _read_git_config_value("user.email")
    if email and "@" in email:
        return email.split("@", 1)[0]

    raise ValueError(
        "Could not determine GitHub user. Set github.user or configure git github.user/user.email."
    )


def _read_git_config_value(key):
    result = subprocess.run(
        ["git", "config", "--get", key],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _read_github_token():
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = getattr(__import__("os"), "environ").get(key, "").strip()
        if value:
            return value
    return ""


def _analyze_repo(repo, github_ids):
    reasons = []
    on_github = bool(repo["repo_id"] and repo["repo_id"] in github_ids)
    delete_safety = UNKNOWN

    if repo["repo_id"] is None:
        delete_safety = PROTECTED_LOCAL_ONLY
        reasons.append("repository has no GitHub identity; local-only repositories are protected")
    elif repo["editable_install_detected"]:
        delete_safety = BLOCKED_EDITABLE_INSTALL
        reasons.append("repository is referenced by an editable Python install")
    elif repo["ahead_count"] > 0 or repo["is_dirty"]:
        delete_safety = UNSAFE_LOCAL_CHANGES
        if repo["ahead_count"] > 0:
            reasons.append(f"repository is ahead of remote by {repo['ahead_count']} commit(s)")
        if repo["is_dirty"]:
            reasons.append("repository has uncommitted tracked changes")
    elif on_github:
        delete_safety = SAFE
        reasons.append("repository is present on GitHub and has no tracked local-only changes")
    else:
        delete_safety = UNKNOWN
        reasons.append("repository has a GitHub remote identity but was not found in the GitHub scan")

    if repo["has_untracked_files"]:
        reasons.append("repository has untracked files")
    if repo["behind_count"] > 0:
        reasons.append(f"repository is behind remote by {repo['behind_count']} commit(s)")

    return {
        "repo_id": repo["repo_id"],
        "path": repo["path"],
        "on_github": on_github,
        "is_dirty": repo["is_dirty"],
        "ahead_count": repo["ahead_count"],
        "behind_count": repo["behind_count"],
        "editable_install_detected": repo["editable_install_detected"],
        "delete_safety": delete_safety,
        "reasons": reasons,
    }


def _group_by_safety(analyses):
    grouped = {
        SAFE: [],
        UNSAFE_LOCAL_CHANGES: [],
        PROTECTED_LOCAL_ONLY: [],
        BLOCKED_EDITABLE_INSTALL: [],
        UNKNOWN: [],
    }
    for item in analyses:
        grouped.setdefault(item["delete_safety"], []).append(item)
    return grouped


def _find_available_repos(local_repos, github_repos):
    local_ids = set()
    for repo in local_repos:
        repo_id = repo.get("repo_id")
        if repo_id:
            local_ids.add(repo_id)

    available = []
    for repo in github_repos:
        repo_id = repo.get("repo_id")
        if not repo_id or repo_id in local_ids:
            continue
        available.append(repo)

    return sorted(available, key=lambda repo: repo["repo_id"].lower())


def _find_analysis(analyses, target):
    target_path = None
    if any(sep in target for sep in ("/", "\\")) or ":" in target:
        try:
            target_path = str(Path(target).resolve())
        except OSError:
            target_path = target

    matches = []
    for item in analyses:
        if item["repo_id"] == target:
            matches.append(item)
            continue
        item_path = Path(item["path"])
        if item_path.name == target:
            matches.append(item)
            continue
        if target_path and str(item_path.resolve()) == target_path:
            matches.append(item)

    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(f"Target {target!r} matched multiple repositories; use an absolute path")
    return matches[0]


def _repo_worktree_status(repo):
    if _repo_has_local_changes(repo):
        return "dirty"
    return "-"


def _repo_has_local_changes(repo):
    return repo["is_dirty"] or repo["has_untracked_files"]


def _format_sync_status(repo):
    if not repo["default_remote"]:
        return "no remote"

    ahead_count = repo["ahead_count"]
    behind_count = repo["behind_count"]
    if ahead_count == 0 and behind_count == 0:
        return "up to date"
    if ahead_count > 0 and behind_count > 0:
        return f"ahead {ahead_count}, behind {behind_count}"
    if ahead_count > 0:
        return f"ahead {ahead_count}"
    return f"behind {behind_count}"


def _print_table(headers, rows):
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))

    def format_row(row):
        return "  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row))

    print(format_row(headers))
    for row in rows:
        print(format_row(row))


def _run_git(args, cwd, allow_failure=False):
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0 and not allow_failure:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        detail = stderr or stdout or f"git exited with code {result.returncode}"
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {detail}")
    return result


def _handle_remove_readonly(fn, path, exc_info):
    exc = exc_info[1]
    if isinstance(exc, PermissionError):
        os.chmod(path, stat.S_IWRITE)
        fn(path)
        return
    raise exc


if __name__ == "__main__":
    main()
