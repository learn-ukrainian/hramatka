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
import re
import sqlite3
from collections import Counter
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
        "text": (
            "На думку вчених, читання є одним з найскладніших {gap} для {gap2}. "
            "Під час читання активізуються одразу {gap3}."
        ),
        "blanks": [
            {
                "id": 1,
                "answer": "завдань",
                "options": ["завдань", "вправ", "задач", "питань"],
            },
            {
                "id": 2,
                "answer": "мозку",
                "options": ["мозку", "книжки", "читання", "людей"],
            },
            {
                "id": 3,
                "answer": "17 ділянок",
                "options": ["17 ділянок", "дві третини", "третина", "мозку"],
            },
        ],
        "evidence": (
            "На думку вчених, читання є одним з найскладніших завдань для мозку. "
            "Під час читання активізуються одразу 17 ділянок головного мозку"
        ),
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
            {
                "left": "книжки",
                "right": "книга",
                "evidence": "не прочитує жодної книжки",
            },
            {
                "left": "ризик",
                "right": "непевність",
                "evidence": "ризик розвитку хвороби",
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
    variants = [
        {
            "statement": "Багато людей втратили насолоду від читання книжок.",
            "evidence": "Багато людей втратили насолоду від неспішного читання книжок",
        },
        {
            "statement": "Третина українців за рік не прочитує жодної книжки.",
            "evidence": "Третина українців за рік не прочитує жодної книжки",
        },
        {
            "statement": "Під час читання активізуються 17 ділянок мозку.",
            "evidence": "активізуються одразу 17 ділянок головного мозку",
        },
        {
            "statement": "Регулярне читання знижує ризик хвороби Альцгеймера.",
            "evidence": "Регулярне читання знижує в 2,5 рази ризик розвитку хвороби Альцгеймера",
        },
    ]
    item = variants[index % len(variants)]
    return {
        "type": "true-false",
        "instruction": f"Познач правильне твердження за текстом. #{index + 1}",
        "items": [{"statement": item["statement"], "correct": True, "evidence": item["evidence"]}],
    }


def _ready_quiz(index: int) -> dict:
    variants = [
        [
            {
                "question": "Скільки українців не прочитує жодної книжки?",
                "options": ["третина", "телевізор", "мозку"],
                "correct": 0,
                "evidence": "Третина українців за рік не прочитує жодної книжки",
            },
            {
                "question": "Що знижує ризик хвороби Альцгеймера?",
                "options": ["читання", "телевізор", "книжки"],
                "correct": 0,
                "evidence": (
                    "Регулярне читання знижує в 2,5 рази ризик розвитку хвороби Альцгеймера"
                ),
            },
            {
                "question": "Що втратили багато людей?",
                "options": ["насолоду", "телевізор", "мозку"],
                "correct": 0,
                "evidence": "Багато людей втратили насолоду від неспішного читання книжок",
            },
        ],
        [
            {
                "question": "Що знижує ризик хвороби Альцгеймера?",
                "options": ["читання", "телевізор", "книжки"],
                "correct": 0,
                "evidence": (
                    "Регулярне читання знижує в 2,5 рази ризик розвитку хвороби Альцгеймера"
                ),
            },
            {
                "question": "Що частина людей обирає замість книжок?",
                "options": ["телевізор", "книжки", "читання"],
                "correct": 0,
                "evidence": "дві третини щодня знаходять час увімкнути телевізор",
            },
            {
                "question": "Що втратили багато людей?",
                "options": ["насолоду", "телевізор", "мозку"],
                "correct": 0,
                "evidence": "Багато людей втратили насолоду від неспішного читання книжок",
            },
        ],
        # Phase-3 bank: evidence/answer pairs kept disjoint from earlier phases so
        # lesson-wide duplicate-evidence composition can still fill the final slot.
        [
            {
                "question": "Що активізується під час читання?",
                "options": ["ділянок", "телевізор", "насолоду"],
                "correct": 0,
                "evidence": "активізуються одразу 17 ділянок головного мозку",
            },
            {
                "question": "Що вчені вважають складним для мозку?",
                "options": ["читання", "телевізор", "книжки"],
                "correct": 0,
                "evidence": "читання є одним з найскладніших завдань для мозку",
            },
            {
                "question": "Що людям простіше зробити замість читання?",
                "options": ["увімкнути", "телевізор", "книжки"],
                "correct": 0,
                "evidence": "бо простіше увімкнути телевізор",
            },
        ],
        [
            {
                "question": "Яке завдання вчені вважають складним?",
                "options": ["завдань", "книжок", "телевізор"],
                "correct": 0,
                "evidence": "найскладніших завдань для мозку",
            },
            {
                "question": "Що знижується через регулярне читання?",
                "options": ["ризик", "телевізор", "насолоду"],
                "correct": 0,
                "evidence": (
                    "Регулярне читання знижує в 2,5 рази ризик розвитку хвороби Альцгеймера"
                ),
            },
            {
                "question": "Що дві третини людей роблять щодня?",
                "options": ["знаходять", "читання", "книжки"],
                "correct": 0,
                "evidence": "дві третини щодня знаходять час увімкнути телевізор",
            },
        ],
        [
            {
                "question": "Що люди втратили через телевізор?",
                "options": ["насолоду", "телевізор", "книжки"],
                "correct": 0,
                "evidence": "втратили насолоду від неспішного читання книжок",
            },
            {
                "question": "Яку хворобу згадує текст?",
                "options": ["Альцгеймера", "телевізор", "читання"],
                "correct": 0,
                "evidence": "ризик розвитку хвороби Альцгеймера",
            },
            {
                "question": "Яку частину мозку активізує читання?",
                "options": ["ділянок", "книжок", "телевізор"],
                "correct": 0,
                "evidence": "17 ділянок головного мозку",
            },
        ],
    ]
    items = variants[index % len(variants)]
    return {
        "type": "quiz",
        "instruction": f"Обери правильну відповідь за текстом. #{index + 1}",
        "items": items,
    }



def _ready_error_correction(index: int) -> dict:
    item_sets = [
        [
            {
                "sentence": "Під час читання активізуються одразу 17 ділянки головного мозку.",
                "error": "ділянки",
                "correction": "ділянок",
                "options": ["ділянки", "ділянок", "книжки"],
                "explanation": "Після 17 потрібна форма родового множини.",
                "evidence": "Під час читання активізуються одразу 17 ділянок головного мозку.",
            },
            {
                "sentence": "Третина українців за рік не прочитує жодної книжка",
                "error": "книжка",
                "correction": "книжки",
                "options": ["книжка", "книжки", "книжок"],
                "explanation": "Після «жодної» потрібна форма родового однини.",
                "evidence": "Третина українців за рік не прочитує жодної книжки",
            },
        ],
        [
            {
                "sentence": (
                    "Регулярне читання знижує в 2,5 рази ризик розвитку "
                    "хвороба Альцгеймера"
                ),
                "error": "хвороба",
                "correction": "хвороби",
                "options": ["хвороба", "хвороби", "книжки"],
                "explanation": "У тексті саме «хвороби Альцгеймера».",
                "evidence": (
                    "Регулярне читання знижує в 2,5 рази ризик розвитку хвороби Альцгеймера"
                ),
            },
            {
                "sentence": "Багато людей втратили насолода від неспішного читання книжок",
                "error": "насолода",
                "correction": "насолоду",
                "options": ["насолода", "насолоду", "книжки"],
                "explanation": "У тексті саме «насолоду».",
                "evidence": "Багато людей втратили насолоду від неспішного читання книжок",
            },
        ],
        [
            {
                "sentence": "На думку вчених, читання є одним з найскладніших завдання для мозку",
                "error": "завдання",
                "correction": "завдань",
                "options": ["завдання", "завдань", "книжки"],
                "explanation": "У тексті саме «завдань для мозку».",
                "evidence": "На думку вчених, читання є одним з найскладніших завдань для мозку",
            },
            {
                "sentence": "Дві третини щодня знаходять час увімкнути телевізора",
                "error": "телевізора",
                "correction": "телевізор",
                "options": ["телевізора", "телевізор", "книжки"],
                "explanation": "У тексті саме «увімкнути телевізор».",
                "evidence": "дві третини щодня знаходять час увімкнути телевізор",
            },
        ],
    ]
    return {
        "type": "error-correction",
        "instruction": f"Виправ помилку. #{index + 1}",
        "items": item_sets[(index + 1) % len(item_sets)],
    }



def _ready_fill_in(index: int) -> dict:
    return {
        "type": "fill-in",
        "instruction": f"Обери правильну форму. #{index + 1}",
        "items": [
            {
                "sentence": (
                    "Багато людей втратили ____ від неспішного читання книжок, "
                    "бо простіше увімкнути телевізор"
                ),
                "answer": "насолоду",
                "options": ["насолоду", "книжки", "читання", "телевізор"],
                "explanation": "У тексті саме «насолоду».",
                "evidence": (
                    "Багато людей втратили насолоду від неспішного читання книжок, "
                    "бо простіше увімкнути телевізор"
                ),
            },
            {
                "sentence": "Регулярне читання знижує в 2,5 рази ризик розвитку ____ Альцгеймера",
                "answer": "хвороби",
                "options": ["хвороби", "книжки", "читання", "телевізор"],
                "explanation": "У тексті саме «хвороби Альцгеймера».",
                "evidence": (
                    "Регулярне читання знижує в 2,5 рази ризик розвитку хвороби Альцгеймера"
                ),
            },
            {
                "sentence": "На думку вчених, читання є одним з найскладніших ____ для мозку",
                "answer": "завдань",
                "options": ["завдань", "книжок", "читання", "телевізор"],
                "explanation": "У тексті саме «завдань для мозку».",
                "evidence": "На думку вчених, читання є одним з найскладніших завдань для мозку",
            },
        ],
    }



def _ready_cloze(index: int) -> dict:
    variants = [
        {
            "text": (
                "На думку вчених, читання є одним з найскладніших {gap} для {gap2}. "
                "Під час читання активізуються одразу {gap3}."
            ),
            "blanks": [
                {
                    "id": 1,
                    "answer": "завдань",
                    "options": ["завдань", "книжок", "читання", "мозку"],
                },
                {"id": 2, "answer": "мозку", "options": ["мозку", "книжки", "читання", "людей"]},
                {
                    "id": 3,
                    "answer": "17 ділянок",
                    "options": ["17 ділянок", "дві третини", "третина", "мозку"],
                },
            ],
            "evidence": (
                "На думку вчених, читання є одним з найскладніших завдань для мозку. "
                "Під час читання активізуються одразу 17 ділянок головного мозку"
            ),
        },
        {
            "text": (
                "Третина українців за рік не прочитує жодної {gap}, зате дві третини "
                "щодня знаходять час увімкнути {gap2}. Регулярне читання знижує "
                "{gap3} розвитку хвороби."
            ),
            "blanks": [
                {
                    "id": 1,
                    "answer": "книжки",
                    "options": ["книжки", "книжок", "читання", "телевізор"],
                },
                {"id": 2, "answer": "телевізор", "options": [
                    "телевізор", "книжки", "читання", "мозку",
                ]},
                {"id": 3, "answer": "ризик", "options": [
                    "ризик", "читання", "книжки", "телевізор",
                ]},
            ],
            "evidence": (
                "Третина українців за рік не прочитує жодної книжки, зате дві третини "
                "щодня знаходять час увімкнути телевізор. Регулярне читання знижує "
                "в 2,5 рази ризик розвитку хвороби Альцгеймера"
            ),
        },
    ]
    variant = variants[index % len(variants)]
    return {
        "type": "cloze",
        "instruction": f"Заповни пропуск словом із тексту. #{index + 1}",
        **variant,
    }


def _ready_mark_the_words(index: int) -> dict:
    variants = [
        (
            "Під час читання активізуються одразу 17 ділянок головного мозку. "
            "Третина українців за рік не прочитує жодної книжки, зате дві третини "
            "щодня знаходять час увімкнути телевізор.",
            ["активізуються", "прочитує", "знаходять", "увімкнути", "рік"],
        ),
        (
            "Третина українців за рік не прочитує жодної книжки, зате дві третини "
            "щодня знаходять час увімкнути телевізор. "
            "Регулярне читання знижує в 2,5 рази ризик розвитку хвороби Альцгеймера.",
            ["прочитує", "знаходять", "увімкнути", "знижує", "рік"],
        ),
    ]
    text, target_words = variants[index % len(variants)]
    return {
        "type": "mark-the-words",
        "instruction": f"Познач усі дієслова. #{index + 1}",
        "text": text,
        "target_words": target_words,
        "criteria": "pos=verb",
        "evidence": text,
    }



def _ready_text_questions(index: int) -> dict:
    item_sets = [
        [
            {
                "question": "Що активізується під час читання?",
                "model_answer": "17 ділянок головного мозку.",
                "evidence": "Під час читання активізуються одразу 17 ділянок головного мозку.",
            },
            {
                "question": "Що знижує ризик хвороби Альцгеймера?",
                "model_answer": "Регулярне читання.",
                "evidence": (
                    "Регулярне читання знижує в 2,5 рази ризик розвитку "
                    "хвороби Альцгеймера"
                ),
            },
            {
                "question": "Що втратили багато людей?",
                "model_answer": "Насолоду від читання книжок.",
                "evidence": "Багато людей втратили насолоду від неспішного читання книжок",
            },
        ],
        [
            {
                "question": "Що є одним з найскладніших завдань для мозку?",
                "model_answer": "Читання.",
                "evidence": "На думку вчених, читання є одним з найскладніших завдань для мозку",
            },
            {
                "question": "Що не прочитує третина українців?",
                "model_answer": "Жодної книжки.",
                "evidence": "Третина українців за рік не прочитує жодної книжки",
            },
            {
                "question": "Що знаходять дві третини щодня?",
                "model_answer": "Час увімкнути телевізор.",
                "evidence": "дві третини щодня знаходять час увімкнути телевізор",
            },
        ],
    ]
    return {
        "type": "text-questions",
        "instruction": f"Обговоріть запитання за текстом. #{index + 1}",
        "source_ref": "Текст-опора",
        "items": item_sets[index % len(item_sets)],
        "teacher_guidance": "Приймайте змістовні відповіді учнів.",
    }



def _ready_match_up(index: int) -> dict:
    pair_sets = [
        [
            {"left": "книжки", "right": "книга", "evidence": "не прочитує жодної книжки"},
            {"left": "книжок", "right": "книга", "evidence": "читання книжок"},
            {"left": "багато", "right": "чимало", "evidence": "Багато людей втратили"},
            {"left": "ризик", "right": "непевність", "evidence": "ризик розвитку хвороби"},
        ],
        [
            {"left": "завдань", "right": "задача", "evidence": "найскладніших завдань для мозку"},
            {"left": "книжки", "right": "книга", "evidence": "не прочитує жодної книжки"},
            {"left": "багато", "right": "чимало", "evidence": "Багато людей втратили"},
            {"left": "ризик", "right": "непевність", "evidence": "ризик розвитку хвороби"},
        ],
    ]
    return {
        "type": "match-up",
        "instruction": f"З'єднай слово з опори з синонімом. #{index + 1}",
        "pairs": pair_sets[index % len(pair_sets)],
    }


def _ready_short_writing(index: int) -> dict:
    variants = [
        {
            "prompt": "читання і телевізор: (1) ризик хвороби; (2) телевізор; (3) книжки",
            "evidence": "Багато людей втратили насолоду від неспішного читання книжок",
        },
        {
            "prompt": "читання і мозку: (1) завдань; (2) мозку; (3) читання",
            "evidence": "На думку вчених, читання є одним з найскладніших завдань для мозку",
        },
        {
            "prompt": "телевізор і книжки: (1) третина; (2) дві третини; (3) телевізор",
            "evidence": "бо простіше увімкнути телевізор",
        },
        {
            "prompt": "мозок і читання: (1) 17 ділянок; (2) мозку; (3) читання",
            "evidence": "Під час читання активізуються одразу 17 ділянок головного мозку",
        },
    ]
    variant = variants[index % len(variants)]
    return {
        "type": "short-writing",
        "instruction": f"Напиши короткий текст. #{index + 1}",
        "prompt": variant["prompt"],
        "source_ref": "Текст-опора",
        "word_count_guidance": "40–60 слів",
        "model_answer": "Читання корисне для мозку.",
        "rubric_hint": "Є три змістові частини і зв'язок з опорою.",
        "teacher_guidance": "Оцінюйте зміст і зв'язність.",
        "evidence": variant["evidence"],
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

_COUNT_PLAN_RE = re.compile(r"^- ([a-z-]+): (\d+)$", re.MULTILINE)


def _fixture_index(counters: Counter[str], activity_type: str) -> int:
    """Monotonic per-type index, shifted by telemetry phase when phases run concurrently."""
    index = counters[activity_type]
    counters[activity_type] += 1
    try:
        from .providers import telemetry_ctx

        ctx = telemetry_ctx.get()
        if ctx is not None and ctx.phase:
            index += (int(ctx.phase) - 1) * 16
    except Exception:
        pass
    return index


def activities_for_prompt(prompt: str, counters: Counter[str]) -> list[dict]:
    """Build gate-ready fixture activities with monotonic per-type indices."""
    activities: list[dict] = []
    for activity_type, count in _COUNT_PLAN_RE.findall(prompt):
        for _ in range(int(count)):
            index = _fixture_index(counters, activity_type)
            activities.append(_READY_CANDIDATES[activity_type](index))
    return activities


# Fixed variant indices per TTT phase for the real-backend browser contract.
# They keep lesson-wide duplicate-evidence and sentence-reuse floors satisfiable
# under concurrent phase generation (see hramatka/app/e2e/real_backend_server.py).
_E2E_VARIANTS_BY_PHASE: dict[int, dict[str, int]] = {
    1: {
        "true-false": 0,
        "cloze": 0,
        "match-up": 0,
        "quiz": 0,
        "mark-the-words": 0,
    },
    2: {
        "true-false": 1,
        "cloze": 0,
        "match-up": 1,
        "fill-in": 0,
        "error-correction": 1,
        "text-questions": 0,
        "short-writing": 1,
    },
    3: {
        "quiz": 3,
        "short-writing": 2,
    },
}


def e2e_activities_for_prompt(prompt: str, *, phase: int) -> list[dict]:
    """Deterministic fixture bank for the real-backend teacher loop."""
    variants = _E2E_VARIANTS_BY_PHASE.get(phase, {})
    activities: list[dict] = []
    for activity_type, count in _COUNT_PLAN_RE.findall(prompt):
        index = variants.get(activity_type, 0)
        for offset in range(int(count)):
            activities.append(_READY_CANDIDATES[activity_type](index + offset))
    return activities


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
                ("непевність", "непевність", "noun:inanim:f:v_naz", "noun"),
                ("задача", "задача", "noun:inanim:f:v_naz", "noun"),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    manifest = json.loads(json.dumps(original.manifest))
    sha256, size = _sha_size(root / "vesum.db")
    manifest["inputs"]["vesum.db"].update({"sha256": sha256, "size": size})
    return data.resolve_bundle(data_dir=root, manifest=manifest, verify=True)
