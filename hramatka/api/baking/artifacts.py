"""Private artifact-directory configuration shared by production bakers."""

from __future__ import annotations

import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

_ENGINE_OUT_RETENTION_DAYS = 14


def configured_engine_out_dir() -> Path | None:
    """Resolve the writable engine-artifact root for this process.

    Resolution order is ``HRAMATKA_ENGINE_OUT_DIR`` then systemd's first
    ``STATE_DIRECTORY`` entry plus ``engine-out``.  ``None`` preserves the
    development default of no external artifact directory.
    """
    explicit = os.environ.get("HRAMATKA_ENGINE_OUT_DIR")
    if explicit:
        return Path(explicit)
    state_dir = os.environ.get("STATE_DIRECTORY")
    if state_dir:
        return Path(state_dir.split(":", 1)[0]) / "engine-out"
    return None


def prune_engine_out(
    root: Path,
    *,
    protected_names: set[str],
    now: datetime | None = None,
) -> None:
    """Remove only aged, completed UUID artifact directories from ``root``."""
    if not root.is_dir():
        return
    cutoff = (now or datetime.now(UTC)) - timedelta(days=_ENGINE_OUT_RETENTION_DAYS)
    for candidate in root.iterdir():
        if candidate.name in protected_names or candidate.is_symlink() or not candidate.is_dir():
            continue
        try:
            uuid.UUID(candidate.name)
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC)
        except (OSError, ValueError):
            continue
        if modified >= cutoff:
            continue
        try:
            shutil.rmtree(candidate)
        except OSError:
            # Artifact retention must not make a new bake fail because an old
            # directory is locked or has an unexpected filesystem error.
            continue


def bake_artifact_dir(root: str | Path, *, bake_id: str | None = None) -> Path:
    """Allocate one private, retention-bounded UUID directory per bake."""
    root_path = Path(root)
    if bake_id is not None:
        try:
            name = str(uuid.UUID(bake_id))
        except (TypeError, ValueError, AttributeError):
            name = str(uuid.uuid4())
    else:
        name = str(uuid.uuid4())
    artifact_dir = root_path / name
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prune_engine_out(root_path, protected_names={name})
    return artifact_dir
