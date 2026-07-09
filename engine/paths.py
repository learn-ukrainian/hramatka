"""Path resolution + preflight checks for the Hramatka engine.

Hard requirement (build brief §R, fleet P0): the engine must run correctly
regardless of the process's current working directory — whether invoked
from the repo root or from `.agent/tmp/hramatka/engine/` itself. Nothing in
this module (or anything that reads `ATLAS_DB` / `VESUM_DB`) may rely on a
cwd-relative path.

`get_project_root()` walks UP from this file's own on-disk location (never
from cwd) looking for a repo marker (`.git/` or `pyproject.toml`), so the
resolved root is stable no matter where the interpreter was launched from.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

_REPO_MARKERS = (".git", "pyproject.toml")


def get_project_root(start: Path | None = None) -> Path:
    """Walk up from `start` (default: this file's location) to the nearest
    ancestor containing a repo marker. Raises RuntimeError if none is found.

    Never uses `Path.cwd()` — resolution is anchored to this file's location
    on disk, so it is correct whether the process cwd is the repo root or
    `.agent/tmp/hramatka/engine/` (or anywhere else).
    """
    here = (start or Path(__file__)).resolve()
    search_from = here if here.is_dir() else here.parent
    for candidate in (search_from, *search_from.parents):
        if any((candidate / marker).exists() for marker in _REPO_MARKERS):
            return candidate
    raise RuntimeError(
        f"Could not locate the project root walking up from {here} "
        f"(looked for one of {_REPO_MARKERS}). The Hramatka engine expects "
        "to live under <repo>/.agent/tmp/hramatka/engine/."
    )


PROJECT_ROOT = get_project_root()
DATA_DIR = PROJECT_ROOT / "data"
ATLAS_DB = DATA_DIR / "atlas.db"
VESUM_DB = DATA_DIR / "vesum.db"
OPENCODE_CONFIG = Path.home() / ".config" / "opencode" / "opencode.jsonc"
GOOGLE_AIS_KEY = Path.home() / ".secret" / "google-ais.key"

# The engine's dedicated cache dir (checkpointing, raw Gemma output). Created
# lazily by callers that need it — paths.py itself never writes to disk.
ENGINE_DIR = Path(__file__).resolve().parent
CACHE_DIR = ENGINE_DIR / ".cache"


class PreflightError(RuntimeError):
    """Raised by `preflight()` when a required engine dependency is missing.

    The message lists every failing check (not just the first) with an
    actionable fix, so a single run surfaces the whole punch list.
    """


def _chat_agent_configured() -> str | None:
    """Best-effort, offline check that the toolless `chat` opencode agent is
    defined in the user's opencode config. Returns an error string if the
    check fails to confirm it, else None. Never raises — this is advisory
    (the authoritative check is the real `_invoke_opencode` call at
    generation time, which is caught separately per §R SystemExit-wrap).
    """
    if not OPENCODE_CONFIG.exists():
        return (
            f"opencode config not found at {OPENCODE_CONFIG} — cannot confirm "
            "the toolless `chat` agent is defined. Gemma generation requires "
            "`agent=\"chat\"` (toolless) or it will break on the aggregate "
            "tool schema. See docs/agent-runtime-guide.md."
        )
    try:
        raw = OPENCODE_CONFIG.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Could not read {OPENCODE_CONFIG}: {exc}"
    # Strip // line comments (opencode config is JSONC) before a cheap
    # substring check — this is advisory, not a full JSONC parse.
    stripped = re.sub(r"//.*", "", raw)
    if '"chat"' not in stripped:
        return (
            f"No `chat` agent entry found in {OPENCODE_CONFIG}. Gemma "
            "generation requires a toolless `chat` agent (see "
            "docs/guardrails/agent-fleet-tooling.md)."
        )
    return None


def preflight() -> None:
    """Assert every runtime dependency the engine needs exists, with
    actionable errors. Fail fast — raises `PreflightError` listing ALL
    failing checks (not just the first hit).

    Checks: atlas.db exists, vesum.db exists, `opencode` on PATH, the Google
    AI Studio key file exists, and (best-effort) the `chat` agent looks
    configured.
    """
    errors: list[str] = []

    if not ATLAS_DB.exists():
        errors.append(
            f"atlas.db not found at {ATLAS_DB}. Build it: "
            "`npm --prefix site run atlas:build-db` (from repo root)."
        )
    if not VESUM_DB.exists():
        errors.append(
            f"vesum.db not found at {VESUM_DB}. Import it: "
            "`.venv/bin/python scripts/rag/import_vesum.py` (from repo root)."
        )
    if shutil.which("opencode") is None:
        errors.append(
            "`opencode` not found on PATH. Install/link the opencode CLI "
            "before running Gemma generation (see docs/agent-runtime-guide.md)."
        )
    if not GOOGLE_AIS_KEY.exists():
        errors.append(
            f"Google AI Studio key not found at {GOOGLE_AIS_KEY}. Gemma "
            "generation (`google-ais/gemma-4-31b-it`) requires it — see "
            "docs/guardrails/agent-fleet-tooling.md."
        )
    chat_agent_error = _chat_agent_configured()
    if chat_agent_error:
        errors.append(chat_agent_error)

    if errors:
        bullet_list = "\n".join(f"  - {e}" for e in errors)
        raise PreflightError(f"Hramatka engine preflight failed:\n{bullet_list}")
