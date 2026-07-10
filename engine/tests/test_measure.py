"""Tests for measure.py — offline harness (MOCK generator, NO network).

Verifies the report aggregates KPIs, the numeral bank recall separates the
positive/negative sides of the moat, and an HTML sheet is written.
"""

from __future__ import annotations

import json

from hramatka.engine import fixtures, measure


def test_measure_produces_report_and_html(tmp_path):
    rep = measure.measure(
        [fixtures.load_anchor()],
        generator=fixtures.mock_generator,
        out_dir=tmp_path / "m",
        cache_dir=tmp_path / "c",
    )
    kpi = rep["kpi"]
    assert kpi["total_items"] == 3
    assert kpi["gate_passed_items"] == 3
    assert kpi["gate_pass_rate"] == 1.0
    html_path = tmp_path / "m" / "measure-report.html"
    assert html_path.exists()
    content = html_path.read_text(encoding="utf-8")
    assert "measurement report" in content.lower()
    assert "Would a teacher accept" in content


def test_numeral_bank_recall_separates_good_and_bad(tmp_path):
    rep = measure.measure(
        [fixtures.load_anchor()],
        generator=fixtures.mock_generator,
        out_dir=tmp_path / "m",
        cache_dir=tmp_path / "c",
    )
    bank = rep["numeral_bank"]
    # every positive probe accepted; every negative-bank string rejected
    assert bank["positive_pass"] == bank["positive_total"]
    assert bank["negative_reject"] == bank["negative_total"]
    assert bank["positive_clean"] + bank["positive_warn"] == bank["positive_total"]
    assert bank["negative_total"] >= 8


def test_default_numeral_corpus_covers_step7_classes_and_keeps_warns_honest(tmp_path):
    # The hard metric is 100% accepted / 100% rejected. Legal but
    # context-dependent forms remain visible as review-required instead of
    # being mislabeled clean passes.
    positive, negative = measure.default_numeral_bank()
    classes = {row["class"] for row in [*positive, *negative]}
    assert {
        "date",
        "range",
        "mixed-fraction",
        "decimal",
        "percentage",
        "ordinal",
        "ambiguous-preposition",
        "collective",
        "gender-agreement",
    } <= classes
    assert {
        "23 квітня",
        "1939–1940 роки",
        "два з половиною рази",
        "два з чвертю метри",
        "три з третиною роки",
        "2,5 рази",
        "на сім відсотків",
        "сімнадцятий поверх",
        "з двома студентами",
        "двоє студентів",
        "дві столи",
    } <= {row["phrase"] for row in [*positive, *negative]}

    rep = measure.measure(
        [fixtures.load_anchor()],
        generator=fixtures.mock_generator,
        out_dir=tmp_path / "m",
        cache_dir=tmp_path / "c",
    )
    bank = rep["numeral_bank"]
    assert bank["positive_pass"] == bank["positive_total"]
    assert bank["negative_reject"] == bank["negative_total"]
    assert bank["positive_warn"] > 0
    assert "review-required" in (tmp_path / "m" / "measure-report.html").read_text(
        encoding="utf-8"
    )


def test_measure_html_surfaces_flagged_items(tmp_path):
    import json

    # true-false with 4 good + 1 hallucinated item -> ships 4, flags 1; the
    # review sheet must SHOW the flagged item rather than silently shrink.
    tf = {
        "type": "true-false",
        "instruction": "Познач правильні твердження.",
        "items": [
            {"statement": "Третина українців не прочитує книжки.", "correct": True,
             "evidence": "Третина українців за рік не прочитує жодної книжки"},
            {"statement": "Дві третини вмикають телевізор.", "correct": True,
             "evidence": "дві третини щодня знаходять час увімкнути телевізор"},
            {"statement": "Активізуються 17 ділянок мозку.", "correct": True,
             "evidence": "активізуються одразу 17 ділянок головного мозку"},
            {"statement": "Читання є одним з найскладніших завдань.", "correct": True,
             "evidence": "читання є одним з найскладніших завдань для мозку"},
            {"statement": "У тексті йдеться про пробіжку.", "correct": True,
             "evidence": "щоденна ранкова пробіжка корисна для серця"},  # hallucinated
        ],
    }

    def gen(_p):
        return json.dumps({"activities": [tf]})

    measure.measure(
        [fixtures.load_anchor()], generator=gen, out_dir=tmp_path / "m", cache_dir=tmp_path / "c"
    )
    html = (tmp_path / "m" / "measure-report.html").read_text(encoding="utf-8")
    assert "need teacher attention" in html
    assert "Flagged items filtered out" in html


def test_measure_report_json_persisted(tmp_path):
    measure.measure(
        [fixtures.load_anchor()],
        generator=fixtures.mock_generator,
        out_dir=tmp_path / "m",
        cache_dir=tmp_path / "c",
    )
    data = json.loads((tmp_path / "m" / "measure-report.json").read_text(encoding="utf-8"))
    assert data["kpi"]["total_items"] == 3
    assert "numeral_bank" in data
