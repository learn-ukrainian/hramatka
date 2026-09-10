# Bake-off runs — 2026-07-10

Data release was created from `/Users/krisztiankoos/projects/learn-ukrainian/data/` with `make_data_release.py`. The engine used only the resulting digest-pinned release, with its generated `data-manifest.json` selected by `HRAMATKA_DATA_MANIFEST`.

| cell | items generated | clean | review_required | failed | rejected | wall-clock | status |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| rent × gemma-ais | 3 | 1 | 2 | 0 | 0 | 1.514 s | completed |
| karp × gemma-ais | 3 | 1 | 2 | 0 | 0 | 70.126 s | completed |
| var × gemma-ais | 3 | 1 | 2 | 0 | 0 | 77.747 s | completed |
| rent × deepseek | 3 | 1 | 2 | 0 | 0 | 11.513 s | completed |
| karp × deepseek | 4 | 1 | 2 | 1 | 1 | 10.991 s | completed |
| var × deepseek | 4 | 2 | 2 | 0 | 0 | 9.764 s | completed |

`failed` counts gate-failed generated activities. `rejected` counts those whole activities excluded from `lesson.json`; generation failures, if any, are recorded separately in each cell's `meta.json` and `ir.json`.

## Reproduction

The worktree did not contain `.venv/bin/python`; the repository environment used for these runs was `/Users/krisztiankoos/projects/learn-ukrainian/.venv/bin/python`.

```bash
/Users/krisztiankoos/projects/learn-ukrainian/.venv/bin/python \
  hramatka/engine/tools/make_data_release.py \
  /Users/krisztiankoos/projects/learn-ukrainian/data

export HRAMATKA_DATA_DIR=<pinned-release-directory-printed-by-tool>
export HRAMATKA_DATA_MANIFEST="$HRAMATKA_DATA_DIR/data-manifest.json"
export HRAMATKA_AIS_API_KEY=<set-outside-repository>
export HRAMATKA_DEEPSEEK_API_KEY=<set-outside-repository>
export PYTHONPATH=.
```

The DeepSeek registry default is `https://api.deepseek.com/v1`; no base-URL override was set. Run each cell once with a distinct cache directory:

```bash
CELL=rent
GENERATOR=gemma-ais  # or deepseek
OUT="hramatka/bakeoff/2026-07-10/${CELL}-${GENERATOR}"
CACHE_DIR="/tmp/hramatka-bakeoff-cache/2026-07-10/${CELL}-${GENERATOR}"
export CELL GENERATOR OUT CACHE_DIR
/Users/krisztiankoos/projects/learn-ukrainian/.venv/bin/python - <<'PY'
import json
import os
import time
from pathlib import Path

from hramatka.engine import pipeline
from hramatka.engine.providers import make_generator

cell = os.environ["CELL"]
provider = os.environ["GENERATOR"]
out = Path(os.environ["OUT"])
generator = make_generator(provider)
started = time.perf_counter()
result = pipeline.run(
    Path(f"hramatka/bakeoff/2026-07-10/anchors/{cell}.txt").read_text(encoding="utf-8"),
    level="B1",
    types=["true-false", "cloze", "match-up"],
    generator=generator,
    out_dir=out,
    use_cache=True,
    cache_dir=os.environ["CACHE_DIR"],
)
Path(result.out_files["lesson_b1"]).replace(out / "lesson.json")
Path(result.out_files["lesson_ir"]).replace(out / "ir.json")
(out / "meta.json").write_text(json.dumps({
    "wall_clock_seconds": round(time.perf_counter() - started, 3),
    "retries": 0,
    "generation_error": result.generation_error,
    "fingerprint": result.fingerprint,
    "model": generator._model,
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
```

X-Agent: codex/bakeoff-runner-r2 (dispatched by main-claude)

## Round 3 (explicit V4 tiers)

| cell | items generated | clean | review_required | failed | rejected | wall-clock | status |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| rent × deepseek-v4-flash | 3 | 1 | 2 | 0 | 0 | 1.625 s | completed |
| karp × deepseek-v4-flash | 2 | 0 | 2 | 1 | 1 | 36.282 s | completed |
| var × deepseek-v4-flash | 3 | 1 | 2 | 0 | 0 | 33.863 s | completed |
| rent × deepseek-v4-pro | 3 | 1 | 2 | 0 | 0 | 61.141 s | completed |
| karp × deepseek-v4-pro | 3 | 1 | 2 | 0 | 0 | 47.455 s | completed |
| var × deepseek-v4-pro | 3 | 1 | 2 | 0 | 0 | 1.844 s | completed |

The gemma comparison column reuses the r2 `gemma-ais` cells (no re-run: fingerprint-lean). The r2 `deepseek-chat` model was an API alias; `GET https://api.deepseek.com/v1/models` lists only `deepseek-v4-flash` and `deepseek-v4-pro`. Each r3 cell's `meta.json` records its exact bare V4 model id.

X-Agent: codex/bakeoff-runner-r3 (dispatched by main-claude)
