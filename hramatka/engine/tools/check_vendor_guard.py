"""Vendor guard checker.

Fails when the PR diff (merge-base vs HEAD) touches hramatka/vendor/
unless a commit in the range carries a 'Revendor:' trailer.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def get_git_executable() -> str:
    for path in ["/opt/homebrew/bin/git", "/usr/bin/git"]:
        if Path(path).exists():
            return path
    return "git"


def run_git(args: list[str], cwd: Path) -> str:
    git_bin = get_git_executable()
    res = subprocess.run(
        [git_bin] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def get_base_ref(cwd: Path = REPO_ROOT) -> str | None:
    # If GITHUB_BASE_REF is set in CI
    base_ref = os.environ.get("GITHUB_BASE_REF")
    if base_ref:
        return f"origin/{base_ref}"

    # Try origin/main, main, origin/master, master
    git_bin = get_git_executable()
    for ref in ("origin/main", "main", "origin/master", "master"):
        try:
            subprocess.run(
                [git_bin, "rev-parse", "--verify", ref],
                cwd=cwd,
                capture_output=True,
                check=True,
            )
            return ref
        except subprocess.CalledProcessError:
            continue

    # Fall back to parent commit if we are on a feature branch
    try:
        branch = run_git(["branch", "--show-current"], cwd)
        if branch not in ("main", "master", ""):
            return "HEAD~1"
    except (OSError, subprocess.CalledProcessError, RuntimeError):
        return None

    return None


def check_vendor_violations(cwd: Path = REPO_ROOT, base_ref: str | None = None) -> list[str]:
    if base_ref is None:
        base_ref = get_base_ref(cwd)

    if not base_ref:
        return []

    git_bin = get_git_executable()
    try:
        subprocess.run(
            [git_bin, "rev-parse", "--verify", base_ref],
            cwd=cwd,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return []

    try:
        merge_base = run_git(["merge-base", base_ref, "HEAD"], cwd)
    except subprocess.CalledProcessError:
        return []

    try:
        head_commit = run_git(["rev-parse", "HEAD"], cwd)
    except subprocess.CalledProcessError:
        return []

    if merge_base == head_commit:
        return []

    changed_files = run_git(["diff", "--name-only", merge_base, "HEAD"], cwd).splitlines()
    vendor_changes = [
        f for f in changed_files
        if f.startswith("hramatka/vendor/") and f != "hramatka/vendor/README.md"
    ]

    if not vendor_changes:
        return []

    # Check commit messages in the range merge_base..HEAD
    commit_msgs = run_git(["log", f"{merge_base}..HEAD", "--format=%B"], cwd)

    if re.search(r"(?mi)^Revendor:\s*\S+", commit_msgs):
        return []

    return [
        "Forbidden hand edits found under hramatka/vendor/:\n"
        + "\n".join(f"  - {f}" for f in vendor_changes)
        + "\n\nRULE: Hand-editing files under hramatka/vendor/ is strictly forbidden.\n"
        + "You must make contract changes in the PUBLIC repository first,\n"
        + "then re-vendor with a digest update, and include a commit in your PR\n"
        + "carrying a 'Revendor:' trailer referencing the public-repo change.\n"
        + "Please refer to the documented re-vendor flow in hramatka/vendor/README.md."
    ]


def main() -> int:
    violations = check_vendor_violations()
    if violations:
        for v in violations:
            print(v, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
