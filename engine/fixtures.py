"""Deterministic OFFLINE fixtures — shared by unit tests and measure.py.

NO network / NO real Gemma. Every Ukrainian form here is a VESUM-verified
real form (#M-4), and every `evidence` quote is a verbatim substring of the
SYNTHETIC `tests/fixtures/anchor01.txt` (a fabricated B1 text — no real teacher
material is committed, per the teacher-privacy rule). Contains:
  - GOOD_ACTIVITIES: a well-formed extractive lesson incl. one numeral
    positive-probe true-false item (must PASS the moat).
  - a `mock_generator` that returns the GOOD_ACTIVITIES JSON regardless of
    prompt (the injectable stand-in for `call_gemma`).
  - a HALLUCINATED item (evidence absent) for the evidence-span FAIL path.
  - NEGATIVE_NUMERAL_BANK: hand-labelled wrong-government strings that live
    ONLY in eval (never learner-facing) — the numeral gate must REJECT them.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from hramatka.engine import data

_ANCHORS_DIR = Path(__file__).resolve().parent / "tests" / "fixtures"


def load_anchor(name: str = "anchor01") -> str:
    return (_ANCHORS_DIR / f"{name}.txt").read_text(encoding="utf-8")


# One well-formed lesson (evidence-carrying SUPERSET, as Gemma would emit).
GOOD_ACTIVITIES: list[dict] = [
    {
        "type": "true-false",
        "instruction": "Познач, чи правильні твердження за текстом.",
        "items": [
            {
                "statement": "Третина українців за рік не прочитує жодної книжки.",
                "correct": True,
                "explanation": "Прямо сказано в тексті.",
                "evidence": "Третина українців за рік не прочитує жодної книжки",
            },
            {
                "statement": "Усі українці щодня читають книжки.",
                "correct": False,
                "explanation": "Текст говорить протилежне.",
                "evidence": "дві третини щодня знаходять час увімкнути телевізор",
            },
            {
                # numeral POSITIVE probe — restates the anchor number in a new
                # sentence keeping correct government (17 ділянок = gen pl).
                "statement": "Під час читання активізуються 17 ділянок мозку.",
                "correct": True,
                "explanation": "Число переказане з правильним керуванням.",
                "evidence": "активізуються одразу 17 ділянок головного мозку",
            },
        ],
    },
    {
        "type": "cloze",
        "instruction": "Заповни пропуск словом із тексту.",
        "text": "На думку вчених, читання є одним з найскладніших {gap} для мозку.",
        "blanks": [
            {
                "id": 1,
                "answer": "завдань",
                "options": ["завдань", "вправ", "задач", "питань"],
            }
        ],
        "evidence": "На думку вчених, читання є одним з найскладніших завдань для мозку",
    },
    {
        "type": "match-up",
        "instruction": "З'єднай слово з опори з його значенням.",
        "pairs": [
            {
                "left": "насолода",
                "right": "велике задоволення",
                "evidence": "насолоду від неспішного читання книжок",
            },
            {
                "left": "телевізор",
                "right": "пристрій для перегляду передач",
                "evidence": "увімкнути телевізор",
            },
        ],
    },
]

# A hallucinated true-false item — evidence quote NOT in the anchor. Used to
# exercise the evidence-span FAIL path (must be dropped from lesson.b1.json).
HALLUCINATED_ACTIVITY: dict = {
    "type": "true-false",
    "instruction": "Познач, чи правильні твердження за текстом.",
    "items": [
        {
            "statement": "У тексті йдеться про користь ранкової пробіжки.",
            "correct": True,
            "explanation": "(вигадано — цього немає в опорі)",
            "evidence": "щоденна ранкова пробіжка корисна для серця",
        }
    ],
}

# Offline NEGATIVE numeral bank (never learner-facing). Each tuple is
# (phrase, context_case). All forms VESUM-verified; the gate must return
# status == 'fail' for every one of them.
NEGATIVE_NUMERAL_BANK: list[tuple[str, str | None]] = [
    ("п'ять студенти", None),      # expected gen-pl студентів
    ("два студентів", None),       # plain nom expected nom-pl студенти
    ("дві столи", None),           # gender: masc noun needs два
    ("двоє студенти", None),       # collective expects gen-pl
    ("півтора років", None),       # expects gen-SG року
]

# Positive numeral probes (learner-facing side) the gate must ACCEPT.
POSITIVE_NUMERAL_BANK: list[tuple[str, str | None]] = [
    ("17 ділянок", None),
    ("дві третини", None),
    ("п'ять столів", None),
    ("близько ста людей", None),
    ("23 квітня", None),  # DATE (day ordinal + genitive month) — not cardinal
]


def mock_generator(_prompt: str) -> str:
    """Injectable stand-in for `call_gemma` — returns the GOOD lesson JSON."""
    return json.dumps({"activities": GOOD_ACTIVITIES}, ensure_ascii=False)


def mock_generator_with_hallucination(_prompt: str) -> str:
    return json.dumps(
        {"activities": [*GOOD_ACTIVITIES, HALLUCINATED_ACTIVITY]}, ensure_ascii=False
    )


# --- Relocated shared bake fixtures (issue #97) ---
# The 9 `_ready_*` builders, `_READY_CANDIDATES`, `_bundle_with_matchup_vocabulary`,
# plus the pytest-free helpers `_build_fixture_bundle`, `_sha_size`, `_seed`
# (and their internal DB builders) are now here so that runtime entrypoints
# (real_backend_server) can import without pulling in any test modules or pytest.
#
# conftest.py re-exports the pytest-free helpers for test-local use.
# No behavior change; pure relocation.

_FIXTURES_DIR = Path(__file__).resolve().parent / "tests" / "fixtures"


def _sha_size(path: Path) -> tuple[str, int]:
    b = path.read_bytes()
    return hashlib.sha256(b).hexdigest(), len(b)


def _seed() -> dict:
    """Extra rows NOT extracted from the real corpus (e.g. the seeded
    russianism for the defect-5 e2e), kept in their own file so a
    `_build_fixtures` regeneration of the extracted JSON never clobbers them."""
    path = _FIXTURES_DIR / "seeded_russianism.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _build_vesum_db(path: Path) -> None:
    rows = json.loads((_FIXTURES_DIR / "vesum_forms.json").read_text(encoding="utf-8"))
    rows = [*rows, *_seed().get("vesum_forms", [])]
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE forms (word_form TEXT NOT NULL, lemma TEXT NOT NULL, "
            "tags TEXT NOT NULL, pos TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO forms (word_form, lemma, tags, pos) VALUES (?, ?, ?, ?)",
            [(r["word_form"], r["lemma"], r["tags"], r["pos"]) for r in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _build_atlas_db(path: Path) -> None:
    payloads = json.loads((_FIXTURES_DIR / "atlas_rows.json").read_text(encoding="utf-8"))
    payloads = [*payloads, *_seed().get("atlas_rows", [])]
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE article_payloads (slug TEXT PRIMARY KEY, "
            "route_order INTEGER NOT NULL, payload_json TEXT NOT NULL, "
            "is_public_route INTEGER NOT NULL CHECK (is_public_route IN (0, 1)))"
        )
        conn.executemany(
            "INSERT INTO article_payloads (slug, route_order, payload_json, is_public_route) "
            "VALUES (?, ?, ?, ?)",
            [
                (
                    p.get("slug") or p["lemma"],
                    i,
                    json.dumps(p, ensure_ascii=False),
                    1,
                )
                for i, p in enumerate(payloads)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _build_fixture_bundle(root: Path) -> data.DataBundle:
    vesum = root / "vesum.db"
    atlas = root / "atlas.db"
    _build_vesum_db(vesum)
    _build_atlas_db(atlas)
    v_sha, v_size = _sha_size(vesum)
    a_sha, a_size = _sha_size(atlas)
    manifest = {
        "bundle": "lu-runtime-data-fixture",
        "version": "test",
        "inputs": {
            "vesum.db": {"path": "vesum.db", "sha256": v_sha, "size": v_size, "required": True},
            "atlas.db": {"path": "atlas.db", "sha256": a_sha, "size": a_size, "required": True},
            # sources.db is not opened by slice-1; absent + optional, but its
            # pinned digest still contributes to the bake fingerprint identity.
            "sources.db": {
                "path": "sources.db",
                "sha256": "0" * 64,
                "size": 0,
                "required": False,
            },
        },
    }
    return data.resolve_bundle(data_dir=root, manifest=manifest, verify=True)


def _ready_true_false(index: int) -> dict:
    evidence = [
        "Третина українців за рік не прочитує жодної книжки",
        "На думку вчених, читання є одним з найскладніших завдань для мозку",
    ][index % 2]
    return {
        "type": "true-false",
        "instruction": "Познач правильне твердження за текстом.",
        "items": [{"statement": evidence, "correct": True, "evidence": evidence}],
    }


def _ready_quiz(_index: int) -> dict:
    return {
        "type": "quiz",
        "instruction": "Обери правильну відповідь за текстом.",
        "items": [
            {
                "question": "Що активізується під час читання?",
                "options": ["ділянок", "книжки", "телевізор"],
                "correct": 0,
                "evidence": "активізуються одразу 17 ділянок головного мозку",
            }
        ],
    }


def _ready_error_correction(_index: int) -> dict:
    return {
        "type": "error-correction",
        "instruction": "Виправ помилку.",
        "items": [
            {
                "sentence": "Під час читання активізуються одразу 17 ділянки головного мозку.",
                "error": "ділянки",
                "correction": "ділянок",
                "options": ["ділянки", "ділянок", "книжки"],
                "explanation": "Після 17 потрібна форма родового множини.",
                "evidence": "Під час читання активізуються одразу 17 ділянок головного мозку.",
            }
        ],
    }


def _ready_fill_in(_index: int) -> dict:
    return {
        "type": "fill-in",
        "instruction": "Обери правильну форму.",
        "items": [
            {
                "sentence": "На думку вчених, читання є одним з найскладніших ____ для мозку.",
                "answer": "завдань",
                "options": ["завдань", "вправ", "задач", "питань"],
                "explanation": "Вибери форму з речення опори.",
                "evidence": "На думку вчених, читання є одним з найскладніших завдань для мозку.",
            }
        ],
    }


def _ready_cloze(_index: int) -> dict:
    return json.loads(json.dumps(next(a for a in GOOD_ACTIVITIES if a["type"] == "cloze")))


def _ready_mark_the_words(_index: int) -> dict:
    evidence = "Під час читання активізуються одразу 17 ділянок головного мозку."
    return {
        "type": "mark-the-words",
        "instruction": "Познач усі дієслова.",
        "text": evidence,
        "target_words": ["активізуються"],
        "criteria": "pos=verb",
        "evidence": evidence,
    }


def _ready_text_questions(_index: int) -> dict:
    return {
        "type": "text-questions",
        "instruction": "Обговоріть запитання за текстом.",
        "source_ref": "Текст-опора",
        "items": [
            {
                "question": "Що активізується під час читання?",
                "model_answer": "17 ділянок головного мозку.",
                "evidence": "Під час читання активізуються одразу 17 ділянок головного мозку.",
            },
            {
                "question": "Що знижує ризик хвороби Альцгеймера?",
                "model_answer": "Регулярне читання.",
                "evidence": (
                    "Регулярне читання знижує в 2,5 рази ризик розвитку хвороби Альцгеймера."
                ),
            },
        ],
        "teacher_guidance": "Приймайте змістовні відповіді учнів.",
    }


def _ready_match_up(_index: int) -> dict:
    return {
        "type": "match-up",
        "instruction": "З'єднай слово з опори з синонімом.",
        "pairs": [
            {
                "left": "книжки",
                "right": "книга",
                "evidence": "не прочитує жодної книжки",
            },
            {
                "left": "багато",
                "right": "чимало",
                "evidence": "Багато людей втратили",
            },
        ],
    }


def _ready_short_writing(_index: int) -> dict:
    return {
        "type": "short-writing",
        "instruction": "Напиши короткий текст.",
        "prompt": "Напиши три речення про читання своїми словами.",
        "source_ref": "Текст-опора",
        "word_count_guidance": "3 речення (30–40 слів)",
        "model_answer": "Читання корисне для мозку.",
        "rubric_hint": "Є три речення і зв'язок з опорою.",
        "teacher_guidance": "Оцінюйте зміст і зв'язність.",
        "evidence": "На думку вчених, читання є одним з найскладніших завдань для мозку.",
    }


_READY_CANDIDATES = {
    "true-false": _ready_true_false,
    "quiz": _ready_quiz,
    "error-correction": _ready_error_correction,
    "fill-in": _ready_fill_in,
    "cloze": _ready_cloze,
    "match-up": _ready_match_up,
    "mark-the-words": _ready_mark_the_words,
    "text-questions": _ready_text_questions,
    "short-writing": _ready_short_writing,
}


def _bundle_with_matchup_vocabulary(root):
    """The normal fixture bundle omits two right-side synonym surface forms."""
    root.mkdir()
    original = _build_fixture_bundle(root)
    connection = sqlite3.connect(root / "vesum.db")
    try:
        connection.executemany(
            "INSERT INTO forms (word_form, lemma, tags, pos) VALUES (?, ?, ?, ?)",
            [
                ("книга", "книга", "noun:inanim:f:v_naz", "noun"),
                ("чимало", "чимало", "adv:", "adverb"),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    manifest = json.loads(json.dumps(original.manifest))
    sha256, size = _sha_size(root / "vesum.db")
    manifest["inputs"]["vesum.db"].update({"sha256": sha256, "size": size})
    return data.resolve_bundle(data_dir=root, manifest=manifest, verify=True)
