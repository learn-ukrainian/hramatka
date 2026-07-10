"""Explicit local configuration; no secrets are stored in the repository."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    teacher_token: str
    mock_delay_seconds: int = 20
    mock_fail: bool = False
    bake_hard_timeout_seconds: int = 1800

    @classmethod
    def from_env(cls) -> Settings:
        token = os.environ.get("HRAMATKA_TEACHER_TOKEN")
        if not token:
            raise RuntimeError("HRAMATKA_TEACHER_TOKEN must be set before starting the API.")

        delay_seconds = int(os.environ.get("HRAMATKA_MOCK_DELAY_SECONDS", "20"))
        if not 20 <= delay_seconds <= 240:
            raise RuntimeError("HRAMATKA_MOCK_DELAY_SECONDS must be between 20 and 240.")

        hard_timeout_seconds = int(os.environ.get("HRAMATKA_BAKE_HARD_TIMEOUT_SECONDS", "1800"))
        if hard_timeout_seconds <= 0:
            raise RuntimeError("HRAMATKA_BAKE_HARD_TIMEOUT_SECONDS must be positive.")

        return cls(
            database_path=Path(os.environ.get("HRAMATKA_DB_PATH", ".local/hramatka/jobs.sqlite3")),
            teacher_token=token,
            mock_delay_seconds=delay_seconds,
            mock_fail=os.environ.get("HRAMATKA_MOCK_FAIL", "0") == "1",
            bake_hard_timeout_seconds=hard_timeout_seconds,
        )
