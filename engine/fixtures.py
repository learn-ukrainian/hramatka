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

import json
from pathlib import Path

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
