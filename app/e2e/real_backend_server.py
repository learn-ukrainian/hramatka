"""Test-only real API process for the browser contract exercise.

It uses the ordinary FastAPI factory and durable runner.  Only the LessonBaker
implementation is deterministic, so the browser still crosses the actual
session, Origin/CSRF, SQLite, status-polling, and revision routes.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import uvicorn

from hramatka.api.app import create_app
from hramatka.api.baking.engine_adapter import EngineLessonBaker
from hramatka.api.config import Settings
from hramatka.engine import fixtures


class FixtureBaker(EngineLessonBaker):
    """Fast deterministic LessonBaker port implementation; it never calls a provider.

    It reuses the engine E2E fake-generator seam and its miniature digest-verified
    bundle. The submitted anchor is deliberately replaced with the synthetic
    fixture anchor inside the baker; API materialization still binds the actual
    browser paste to the durable lesson resource.
    """

    def __init__(self, runtime_dir: Path) -> None:
        def generator(prompt: str) -> str:
            counts = {
                activity_type: int(count)
                for activity_type, count in re.findall(
                    r"^- ([a-z-]+): (\d+)$", prompt, re.MULTILINE
                )
            }
            activities = [
                candidate(index)
                for activity_type, count in counts.items()
                for index in range(count)
                for candidate in [fixtures._READY_CANDIDATES[activity_type]]
            ]
            return json.dumps({"activities": activities}, ensure_ascii=False)

        super().__init__(
            generator=generator,
            bundle=fixtures._bundle_with_matchup_vocabulary(runtime_dir / "data"),
            cache_dir=runtime_dir / "cache",
        )

    def bake(self, anchor: str | dict, duration: int, focus: str | None) -> dict[str, Any]:
        del anchor
        return super().bake(fixtures.load_anchor(), duration, focus)


def main() -> None:
    origin = os.environ["HRAMATKA_E2E_ORIGIN"]
    database_path = Path(os.environ["HRAMATKA_E2E_DB_PATH"])
    token_path = Path(os.environ["HRAMATKA_E2E_INVITE_PATH"])
    app = create_app(
        settings=Settings(
            database_path=database_path,
            pilot_origin=origin,
            csrf_hmac_key=b"e2e-only-csrf-key-not-a-deployment-secret",
        ),
        baker=FixtureBaker(database_path.parent),
    )
    teacher = app.state.store.create_teacher("E2E викладач")
    _, invite_token = app.state.store.create_invite(teacher.id)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(invite_token, encoding="ascii")
    token_path.chmod(0o600)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("HRAMATKA_E2E_API_PORT", "8788")))


if __name__ == "__main__":
    main()
