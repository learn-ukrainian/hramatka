"""Direction-of-flow guard for the private Hramatka repo (Sol review §Item 1).

Enforces that the private repo consumes the public repo ONLY as reviewed,
digest-pinned artifacts — never a live checkout:

  1. no import of / path into a public checkout in code or config;
  2. every vendored artifact's digests verify against its MANIFEST.json;
  3. no corpus DB (`*.db`/`*.sqlite*`) or protected data is committed.

Runnable standalone (`python hramatka/engine/tools/check_boundary.py`, exit 1 on
any violation) and importable as `find_violations()` for the test suite + CI.

Scans CODE/CONFIG only. Archived design/review Markdown legitimately QUOTES the
historical public-checkout paths as evidence, so `*.md` is excluded — those are
immutable records, not runtime coupling.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Extensions that represent runtime coupling (imports, paths, config).
_SCANNED_SUFFIXES = {".py", ".toml", ".yml", ".yaml", ".cfg", ".ini", ".json"}

# Paths excluded from the source scan: this checker (it names the patterns as
# data) and its own test.
_SCAN_EXCLUDES = (
    "hramatka/engine/tools/check_boundary.py",
    "hramatka/engine/tests/test_boundary.py",
)

# (compiled pattern, human description). Import/path patterns are line-anchored
# so that prose mentioning `scripts.verification` etc. does not false-positive —
# only an actual `from/import scripts …` statement or a real filesystem path is a
# violation.
_FORBIDDEN: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?m)^\s*(from|import)\s+scripts\b"), "import from the public `scripts` package"),
    (re.compile(r"_invoke_opencode"), "opencode generator transport coupling"),
    (re.compile(r"_bootstrap_sys_path"), "sys.path bootstrap into a public checkout"),
    (re.compile(r"\.agent/tmp/hramatka"), "path into the public checkout `.agent/tmp/hramatka`"),
    (re.compile(r"/projects/learn-ukrainian(?:/|\b)"), "absolute path into a public checkout"),
    (re.compile(r"Path\.home\(\)"), "dev-home path resolution"),
    (re.compile(r"\.secret/google-ais"), "hardcoded dev-home AIS key path"),
)

_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")


def _tracked_files(repo_root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def find_violations(repo_root: Path | None = None) -> list[str]:
    repo_root = repo_root or REPO_ROOT
    violations: list[str] = []
    tracked = _tracked_files(repo_root)

    # 1) forbidden references in code/config
    for rel in tracked:
        if rel in _SCAN_EXCLUDES:
            continue
        if Path(rel).suffix not in _SCANNED_SUFFIXES:
            continue
        text = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
        for pattern, desc in _FORBIDDEN:
            m = pattern.search(text)
            if m:
                line_no = text.count("\n", 0, m.start()) + 1
                violations.append(f"{rel}:{line_no}: {desc} ({m.group(0)!r})")

    # 2) no committed corpus DB / protected data snapshot
    for rel in tracked:
        if Path(rel).suffix.lower() in _DB_SUFFIXES or Path(rel).name == "sources.db":
            violations.append(f"{rel}: corpus/data DB must never be committed to the repo")

    # 3) vendored artifacts verify against their manifests
    try:
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from hramatka.engine import vendoring
    except ImportError as exc:
        violations.append(f"vendored artifact verification failed: {exc}")
    else:
        try:
            vendoring.verify_all()
        except vendoring.VendorIntegrityError as exc:
            violations.append(f"vendored artifact verification failed: {exc}")

    return violations


def main() -> int:
    violations = find_violations()
    if violations:
        print("Direction-of-flow check FAILED:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1
    print("Direction-of-flow check passed: no public-checkout coupling, "
          "vendor digests verified, no committed corpus DB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
