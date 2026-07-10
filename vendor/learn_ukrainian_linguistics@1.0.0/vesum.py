"""VESUM SQLite lookup helpers — vendored, self-contained linguistics adapter.

Pinned adaptation of the public `scripts/verification/vesum.py`
(learn-ukrainian.github.io @ 28c257d8). The public original bootstrapped
`sys.path` and imported `scripts.rag.config` for a default DB path; this
vendored copy is self-contained and requires an explicit `db_path` on every
call, so it works with NO public checkout on `sys.path`. It is loaded ONLY via
the digest-verifying vendoring loader (see `hramatka/engine/vendoring.py`).

Adaptation (recorded in MANIFEST.json): removed the `sys.path` insertion and
the `from scripts.rag.config import VESUM_DB_PATH` default; `db_path` is now
required; dropped the argparse CLI `main()`. The query API and result shapes
are byte-for-byte compatible with the public source.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None


def _require_db_path(db_path: str | Path | None) -> Path:
    if db_path is None:
        raise ValueError(
            "learn_ukrainian_linguistics.vesum requires an explicit db_path "
            "(resolved from the digest-pinned data manifest) — there is no "
            "checkout-relative default."
        )
    return Path(db_path)


def get_vesum_conn(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Lazy, path-keyed SQLite connection to the VESUM dictionary."""
    global _conn, _conn_path
    resolved = _require_db_path(db_path)
    if _conn is None or _conn_path != resolved:
        if _conn is not None:
            _conn.close()
            _conn = None
        if not resolved.exists():
            raise FileNotFoundError(f"VESUM database not found at {resolved}.")
        _conn = sqlite3.connect(str(resolved), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn_path = resolved
    return _conn


def verify_word(
    word: str,
    pos_filter: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict]:
    """Look up a word form. Returns [{lemma, pos, tags}]; empty = not found."""
    conn = get_vesum_conn(db_path)
    if pos_filter:
        rows = conn.execute(
            "SELECT lemma, pos, tags FROM forms WHERE word_form = ? AND pos = ?",
            (word, pos_filter),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT lemma, pos, tags FROM forms WHERE word_form = ?",
            (word,),
        ).fetchall()
    return [{"lemma": r["lemma"], "pos": r["pos"], "tags": r["tags"]} for r in rows]


def verify_words(
    words: list[str],
    pos_filter: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, list[dict]]:
    """Batch-verify word forms in one query. Missing words map to []."""
    if not words:
        return {}
    conn = get_vesum_conn(db_path)
    placeholders = ",".join("?" * len(words))
    if pos_filter:
        rows = conn.execute(
            f"SELECT word_form, lemma, pos, tags FROM forms "
            f"WHERE word_form IN ({placeholders}) AND pos = ?",
            (*words, pos_filter),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT word_form, lemma, pos, tags FROM forms "
            f"WHERE word_form IN ({placeholders})",
            words,
        ).fetchall()
    result: dict[str, list[dict]] = {w: [] for w in words}
    for r in rows:
        result[r["word_form"]].append(
            {"lemma": r["lemma"], "pos": r["pos"], "tags": r["tags"]}
        )
    return result


def verify_lemma(lemma: str, db_path: str | Path | None = None) -> list[dict]:
    """All inflected forms of a lemma. Returns [{word_form, pos, tags}]."""
    conn = get_vesum_conn(db_path)
    rows = conn.execute(
        "SELECT word_form, pos, tags FROM forms WHERE lemma = ? ORDER BY pos, tags",
        (lemma,),
    ).fetchall()
    return [
        {"word_form": r["word_form"], "pos": r["pos"], "tags": r["tags"]} for r in rows
    ]
