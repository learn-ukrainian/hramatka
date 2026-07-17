"""Frozen pilot activity registry versus the landed public contract."""

from __future__ import annotations

from jsonschema import Draft7Validator

from hramatka.contracts import PILOT_ACTIVITY_TYPES
from hramatka.engine import fixtures, registry, schema, vendoring


def test_pilot_registry_matches_landed_golden_fixture_exactly() -> None:
    fixtures = vendoring.read_json(vendoring.PILOT_LU_ACTIVITY, "lu.activity.v1.fixtures.json")
    fixture_types = tuple(fixture["type"] for fixture in fixtures)

    assert fixture_types == PILOT_ACTIVITY_TYPES
    assert len(fixture_types) == len(set(fixture_types)) == 9
    assert tuple(registry.ACTIVITY_REGISTRY) == PILOT_ACTIVITY_TYPES


def test_pilot_fixture_is_valid_and_every_type_is_schema_permitted() -> None:
    contract = vendoring.read_json(vendoring.PILOT_LU_ACTIVITY, "lu.activity.v1.schema.json")
    fixtures = vendoring.read_json(vendoring.PILOT_LU_ACTIVITY, "lu.activity.v1.fixtures.json")
    permitted_types = set(contract["properties"]["type"]["enum"])

    assert set(PILOT_ACTIVITY_TYPES) <= permitted_types
    validator = Draft7Validator(contract)
    for fixture in fixtures:
        validator.validate(fixture)
        assert fixture["payload"]["type"] == fixture["type"]


EXPECTED_GATE_SUBMISSIONS = {
    "cloze": {
        "check_tokens": [
            ["книжок", "читання", "мозку"],
            ["книжки", "читання", "людей"],
            ["дві третини", "третина", "мозку"]
        ],
        "evidence_span": [
            "На думку вчених, читання є одним з найскладніших завдань для мозку. "
            "Під час читання активізуються одразу 17 ділянок головного мозку"
        ],
        "matchup_left": [],
        "matchup_semantics": [],
        "numeral": [],
        "schema_tokens": [
            ("blanks[0]", ["завдань", "завдань", "книжок", "читання", "мозку"]),
            ("blanks[1]", ["мозку", "мозку", "книжки", "читання", "людей"]),
            ("blanks[2]", ["17 ділянок", "17 ділянок", "дві третини", "третина", "мозку"])
        ],
        "vesum": []
    },
    "error-correction": {
        "check_tokens": [],
        "evidence_span": [
            "Регулярне читання знижує в 2,5 рази ризик розвитку хвороби Альцгеймера",
            "Багато людей втратили насолоду від неспішного читання книжок"
        ],
        "matchup_left": [],
        "matchup_semantics": [],
        "numeral": [],
        "schema_tokens": [
            ("items[0]", ["У тексті саме «хвороби Альцгеймера»."]),
            ("items[1]", ["У тексті саме «насолоду»."])
        ],
        "vesum": [
            ("items[0]", ["хвороба", "хвороби", "книжки"], "error_correction_vesum"),
            ("items[1]", ["насолода", "насолоду", "книжки"], "error_correction_vesum")
        ]
    },
    "fill-in": {
        "check_tokens": [],
        "evidence_span": [
            "Багато людей втратили насолоду від неспішного читання книжок, "
            "бо простіше увімкнути телевізор",
            "Регулярне читання знижує в 2,5 рази ризик розвитку хвороби Альцгеймера",
            "На думку вчених, читання є одним з найскладніших завдань для мозку"
        ],
        "matchup_left": [],
        "matchup_semantics": [],
        "numeral": [],
        "schema_tokens": [
            ("items[0]", ["У тексті саме «насолоду»."]),
            ("items[1]", ["У тексті саме «хвороби Альцгеймера»."]),
            ("items[2]", ["У тексті саме «завдань для мозку»."])
        ],
        "vesum": [
            ("items[0]", ["насолоду", "книжки", "читання", "телевізор"], "fill_in_vesum"),
            ("items[1]", ["хвороби", "книжки", "читання", "телевізор"], "fill_in_vesum"),
            ("items[2]", ["завдань", "книжок", "читання", "телевізор"], "fill_in_vesum")
        ]
    },
    "mark-the-words": {
        "check_tokens": [],
        "evidence_span": [
            "Під час читання активізуються одразу 17 ділянок головного мозку. "
            "Третина українців за рік не прочитує жодної книжки, зате дві третини "
            "щодня знаходять час увімкнути телевізор.",
            "Під час читання активізуються одразу 17 ділянок головного мозку. "
            "Третина українців за рік не прочитує жодної книжки, зате дві третини "
            "щодня знаходять час увімкнути телевізор.",
            "активізуються", "прочитує", "знаходять", "увімкнути", "рік"
        ],
        "matchup_left": [],
        "matchup_semantics": [],
        "numeral": [],
        "schema_tokens": [],
        "vesum": []
    },
    "match-up": {
        "check_tokens": [["книга"], ["книга"], ["чимало"], ["непевність"]],
        "evidence_span": [
            "не прочитує жодної книжки",
            "читання книжок",
            "Багато людей втратили",
            "ризик розвитку хвороби"
        ],
        "matchup_left": ["книжки", "книжок", "багато", "ризик"],
        "matchup_semantics": [
            ("книжки", "книга"),
            ("книжок", "книга"),
            ("багато", "чимало"),
            ("ризик", "непевність")
        ],
        "numeral": [],
        "schema_tokens": [
            ("pairs[0]", ["книжки", "книга"]),
            ("pairs[1]", ["книжок", "книга"]),
            ("pairs[2]", ["багато", "чимало"]),
            ("pairs[3]", ["ризик", "непевність"])
        ],
        "vesum": []
    },
    "quiz": {
        "check_tokens": [
            ["третина", "телевізор", "мозку"],
            ["читання", "телевізор", "книжки"],
            ["насолоду", "телевізор", "мозку"]
        ],
        "evidence_span": [
            "Третина українців за рік не прочитує жодної книжки",
            "Регулярне читання знижує в 2,5 рази ризик розвитку хвороби Альцгеймера",
            "Багато людей втратили насолоду від неспішного читання книжок"
        ],
        "matchup_left": [],
        "matchup_semantics": [],
        "numeral": [
            ("items[0]", "Скільки українців не прочитує жодної книжки?"),
            ("items[0]", "третина телевізор мозку"),
            ("items[1]", "Що знижує ризик хвороби Альцгеймера?"),
            ("items[1]", "читання телевізор книжки"),
            ("items[2]", "Що втратили багато людей?"),
            ("items[2]", "насолоду телевізор мозку")
        ],
        "schema_tokens": [
            (
                "items[0]",
                [
                    "Скільки українців не прочитує жодної книжки?",
                    "третина",
                    "телевізор",
                    "мозку",
                ],
            ),
            (
                "items[1]",
                ["Що знижує ризик хвороби Альцгеймера?", "читання", "телевізор", "книжки"],
            ),
            (
                "items[2]",
                ["Що втратили багато людей?", "насолоду", "телевізор", "мозку"],
            ),
        ],
        "vesum": []
    },
    "short-writing": {
        "check_tokens": [],
        "evidence_span": ["Багато людей втратили насолоду від неспішного читання книжок"],
        "matchup_left": [],
        "matchup_semantics": [],
        "numeral": [],
        "schema_tokens": [
            ("text", [
                "читання і телевізор: (1) ризик хвороби; (2) телевізор; (3) книжки",
                "Читання корисне для мозку.",
                "Є три змістові частини і зв'язок з опорою."
            ])
        ],
        "vesum": [
            (
                "text",
                ["читання", "телевізор", "ризик", "хвороби", "телевізор", "книжки"],
                "open_task_vesum",
            )
        ]
    },
    "text-questions": {
        "check_tokens": [],
        "evidence_span": [
            "Під час читання активізуються одразу 17 ділянок головного мозку.",
            "Регулярне читання знижує в 2,5 рази ризик розвитку хвороби Альцгеймера",
            "Багато людей втратили насолоду від неспішного читання книжок"
        ],
        "matchup_left": [],
        "matchup_semantics": [],
        "numeral": [],
        "schema_tokens": [
            (
                "items[0]",
                ["Що активізується під час читання?", "17 ділянок головного мозку."],
            ),
            (
                "items[1]",
                ["Що знижує ризик хвороби Альцгеймера?", "Регулярне читання."],
            ),
            (
                "items[2]",
                ["Що втратили багато людей?", "Насолоду від читання книжок."],
            ),
        ],
        "vesum": [
            (
                "items[0]",
                ["Що", "активізується", "під", "час", "читання"],
                "open_task_vesum",
            ),
            (
                "items[1]",
                ["Що", "знижує", "ризик", "хвороби", "Альцгеймера"],
                "open_task_vesum",
            ),
            (
                "items[2]",
                ["Що", "втратили", "багато", "людей"],
                "open_task_vesum",
            )
        ]
    },
    "true-false": {
        "check_tokens": [
            ["Багато", "людей", "втратили", "насолоду", "від", "читання", "книжок"]
        ],
        "evidence_span": ["Багато людей втратили насолоду від неспішного читання книжок"],
        "matchup_left": [],
        "matchup_semantics": [],
        "numeral": [
            ("items[0]", "Багато людей втратили насолоду від читання книжок.")
        ],
        "schema_tokens": [
            (
                "items[0]",
                ["Багато людей втратили насолоду від читання книжок.", None],
            )
        ],
        "vesum": []
    }
}


def test_pilot_gate_submitted_fields_pin(monkeypatch) -> None:
    schema_tokens_calls = []
    numeral_calls = []
    vesum_calls = []
    matchup_lemma_calls = []
    matchup_semantics_calls = []
    evidence_span_calls = []
    check_tokens_calls = []

    def mock_schema_tokens(strings, anchor_body, gr, locator):
        schema_tokens_calls.append((locator, strings))

    def mock_run_numeral(text, gr, locator):
        numeral_calls.append((locator, text))

    def mock_add_verified_vesum(tokens, anchor_body, gr, locator, missing_gate, **kw):
        vesum_calls.append((locator, tokens, missing_gate))

    def mock_check_left(left, anchor_body):
        matchup_lemma_calls.append(left)
        return {"status": "pass", "detail": "ok"}

    def mock_check_pair(left, right, **kw):
        matchup_semantics_calls.append((left, right))
        return {"status": "pass", "detail": "ok"}

    def mock_check_evidence(quote, anchor_body):
        evidence_span_calls.append(quote)
        return {
            "status": "pass",
            "detail": "ok",
            "char_start": 0,
            "char_end": len(quote),
            "kind": "literal",
        }

    def mock_check_tokens(tokens, anchor_body, **kw):
        check_tokens_calls.append(tokens)
        return [{"token": t, "status": "pass", "detail": "ok"} for t in tokens]

    monkeypatch.setattr(registry, "_gate_schema_tokens", mock_schema_tokens)
    monkeypatch.setattr(registry, "_run_numeral_gate", mock_run_numeral)
    monkeypatch.setattr(registry, "_add_verified_vesum_tokens", mock_add_verified_vesum)
    monkeypatch.setattr(registry.matchup_lemma, "check_left_side", mock_check_left)
    monkeypatch.setattr(registry.matchup_semantics, "check_pair", mock_check_pair)
    monkeypatch.setattr(registry.evidence_span, "check_evidence", mock_check_evidence)
    monkeypatch.setattr(registry.vesum_gate, "check_tokens", mock_check_tokens)
    monkeypatch.setattr(registry.vesum_gate, "worst_status", lambda verdicts: "pass")
    monkeypatch.setattr(
        registry.vesum_tags,
        "parse_word",
        lambda word, db_path=None: [{"pos": "noun", "lemma": word}],
    )
    monkeypatch.setattr(registry.vesum_tags, "parse_criterion", lambda text: {"pos": "noun"})
    monkeypatch.setattr(registry.vesum_tags, "matches_criterion", lambda parsed, criterion: True)

    anchor = fixtures.load_anchor()
    actual_calls = {}

    for name, loader in fixtures._READY_CANDIDATES.items():
        raw = loader(0)
        clean, evidence, _kit_anchors = schema.parse_raw_activity(raw)
        gr = schema.GateResult()
        
        schema_tokens_calls.clear()
        numeral_calls.clear()
        vesum_calls.clear()
        matchup_lemma_calls.clear()
        matchup_semantics_calls.clear()
        evidence_span_calls.clear()
        check_tokens_calls.clear()
        
        registry.ACTIVITY_REGISTRY[name].gate(clean, evidence, anchor, gr)
        
        actual_calls[name] = {
            "schema_tokens": list(schema_tokens_calls),
            "numeral": list(numeral_calls),
            "vesum": list(vesum_calls),
            "matchup_left": list(matchup_lemma_calls),
            "matchup_semantics": list(matchup_semantics_calls),
            "evidence_span": list(evidence_span_calls),
            "check_tokens": list(check_tokens_calls),
        }

    assert actual_calls == EXPECTED_GATE_SUBMISSIONS


def test_pilot_gate_submitted_fields_pin_catches_dropped_field(monkeypatch) -> None:
    # Red-proof check: simulate a silently dropped/modified "statement" field on true-false
    original_loader = fixtures._READY_CANDIDATES["true-false"]

    def tampered_loader(idx):
        import copy
        raw = original_loader(idx)
        tampered_raw = copy.deepcopy(raw)
        for item in tampered_raw.get("items", []):
            if "statement" in item:
                item["statement"] = "silently dropped or modified statement"
        return tampered_raw

    monkeypatch.setitem(fixtures._READY_CANDIDATES, "true-false", tampered_loader)

    import pytest
    with pytest.raises(AssertionError):
        test_pilot_gate_submitted_fields_pin(monkeypatch)



