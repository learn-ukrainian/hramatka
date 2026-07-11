"""Step-7 measurement harness tests (offline fixtures, no network)."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

from hramatka.engine import fixtures, measure, schema


def _measure(tmp_path, anchors=None, **kwargs):
    generator = kwargs.pop("generator", fixtures.mock_generator)
    return measure.measure(
        anchors or [fixtures.load_anchor()],
        generator=generator,
        out_dir=tmp_path / "measure",
        cache_dir=tmp_path / "cache",
        include_russianism_fixtures=False,
        **kwargs,
    )


def test_measure_reports_tri_state_header_and_bake_observability(tmp_path):
    report = _measure(tmp_path)

    counts = report["kpi"]["tri_state_counts"]
    assert sum(counts.values()) == report["kpi"]["total_items"] == 3
    assert counts[schema.GATE_REVIEW] == 2  # FALSE + semantic review need teacher confirmation
    assert counts[schema.GATE_CLEAN] == 1
    assert report["anchors"][0]["tri_state_counts"] == counts
    assert report["kpi"]["mark_distribution"] == {"ok": 1, "warn": 2, "shipped": 3}
    assert report["anchors"][0]["mark_distribution"] == {"ok": 1, "warn": 2, "shipped": 3}

    header = report["header"]
    assert {
        "report_contract",
        "generated_at",
        "engine_sha",
        "engine_version",
        "model",
        "package_versions",
        "vendor_artifact_digests",
        "data_bundle_digests",
    } <= set(header)
    bake = report["anchors"][0]["bake"]
    assert bake["wall_clock_seconds"] >= 0
    assert bake["cost_usd"] == 0.0
    assert bake["retry_count"] == 0

    report_json = tmp_path / "measure" / "measure-report.json"
    persisted = json.loads(report_json.read_text(encoding="utf-8"))
    assert "passed" not in json.dumps(persisted, ensure_ascii=False)
    assert (
        "measurement report"
        in (tmp_path / "measure" / "measure-report.html").read_text(encoding="utf-8").lower()
    )


def test_measure_asserts_header_against_slates_pinned_runtime_identity(tmp_path):
    baseline = _measure(tmp_path / "baseline")
    expected = {
        field: baseline["header"][field]
        for field in (
            "engine_sha",
            "engine_version",
            "model",
            "package_versions",
            "vendor_artifact_digests",
            "data_bundle_digests",
        )
    }
    matched = _measure(tmp_path / "matched", expected_pins=expected)
    assertion = matched["header"]["slate_pin_assertion"]
    assert assertion["configured"] is True
    assert assertion["pins_match"] is True
    assert all(assertion["matches"].values())

    mismatched = _measure(
        tmp_path / "mismatched",
        expected_pins={**expected, "model": "different-model"},
    )
    assertion = mismatched["header"]["slate_pin_assertion"]
    assert assertion["pins_match"] is False
    assert assertion["matches"]["model"] is False


def test_measure_russianism_fixture_catches_anchor_and_generated_task_cases(tmp_path):
    report = measure.measure(
        [fixtures.load_anchor()],
        generator=fixtures.mock_generator,
        out_dir=tmp_path / "measure",
        cache_dir=tmp_path / "cache",
    )

    russianism = report["russianism_e2e"]
    assert russianism["fixture_total"] == 2
    assert russianism["catch_count"] == 2
    assert russianism["uncaught_count"] == 0
    assert russianism["generated_task_fixture_caught"] is True
    assert russianism["hard_gate_met"] is True
    assert {fixture["id"] for fixture in russianism["fixtures"]} == {
        "quoted-anchor-russianism",
        "generated-task-russianism",
    }
    assert all(fixture["items"][0]["mark"] == "warn" for fixture in russianism["fixtures"])


def test_russianism_kpi_excludes_non_russian_vesum_warnings():
    activities = [
        SimpleNamespace(
            gate_result=SimpleNamespace(
                checks=[
                    SimpleNamespace(
                        gate="vesum_token",
                        status="warn",
                        detail="'або' flagged heritage/russianism='dialect' (teacher-confirm).",
                    )
                ]
            )
        ),
        SimpleNamespace(
            gate_result=SimpleNamespace(
                checks=[
                    SimpleNamespace(
                        gate="vesum_token",
                        status="warn",
                        detail=(
                            "'получку' flagged heritage/russianism='russianism' (teacher-confirm)."
                        ),
                    )
                ]
            )
        ),
    ]
    assert measure._russianism_counts(activities) == {
        "warned_items": 1,
        "total_items": 2,
        "warn_rate": 0.5,
    }


def test_numeral_bank_includes_bake_regressions_and_keeps_warns_honest(tmp_path):
    positive, negative = measure.default_numeral_bank()
    positive_phrases = {row["phrase"] for row in positive}
    negative_phrases = {row["phrase"] for row in negative}
    assert {
        "двадцять тисяч гривень",
        "близько двадцяти тисяч гривень",
        "дві-три хвилини",
        "обоє працюємо",
        "двадцять тисяч п'ять гривень",
        "дві-три тисячі гривень",
        "одна тисяча двісті тридцять чотири книги",
    } <= positive_phrases
    assert "Близько три годин" in negative_phrases

    report = _measure(tmp_path)
    bank = report["numeral_bank"]
    assert bank["positive_accept"] == bank["positive_total"]
    assert bank["negative_reject"] == bank["negative_total"]
    assert bank["positive_clean"] + bank["positive_review_required"] == bank["positive_total"]
    assert bank["positive_review_required"] > 0


def test_measure_surfaces_salvage_rejected_content_and_rater_contract(tmp_path):
    true_false = {
        "type": "true-false",
        "instruction": "Познач правильні твердження.",
        "items": [
            {
                "statement": "Третина українців не прочитує книжки.",
                "correct": True,
                "evidence": "Третина українців за рік не прочитує жодної книжки",
            },
            {
                "statement": "У тексті йдеться про пробіжку.",
                "correct": True,
                "evidence": "щоденна ранкова пробіжка корисна для серця",
            },
        ],
    }

    report = _measure(
        tmp_path,
        generator=lambda _prompt: json.dumps({"activities": [true_false]}, ensure_ascii=False),
    )
    anchor = report["anchors"][0]
    assert anchor["salvage_counts"] == {"generated": 1, "shipped": 0, "flagged": 1, "rejected": 0}
    item = anchor["items"][0]
    assert {
        "gate_outcome",
        "gate_checks",
        "mark",
        "note",
        "block_provenance",
        "activity_provenance",
        "evidence",
        "rejected",
        "rater_controls",
    } <= set(item)
    assert item["gate_outcome"] == schema.GATE_REVIEW
    assert item["activity_provenance"]["external_options"] is True
    rejected = item["rejected"][0]
    assert rejected["kind"] == "flagged"
    assert rejected["machine_reason_class"] == "gate-failed:evidence_span"
    assert rejected["content"]["statement"] == "У тексті йдеться про пробіжку."
    assert any("located_failure_reason" in evidence for evidence in item["evidence"])

    html = (tmp_path / "measure" / "measure-report.html").read_text(encoding="utf-8")
    assert "Teacher mark" in html
    assert "Block + activity provenance" in html
    assert "Rejected or flagged content" in html
    assert "edit-content" in html


def test_measure_reports_whole_activity_rejection(tmp_path):
    rejected_activity = {
        "type": "true-false",
        "instruction": "Познач правильні твердження.",
        "items": [
            {
                "statement": "У тексті йдеться про пробіжку.",
                "correct": True,
            }
        ],
    }
    report = _measure(
        tmp_path,
        generator=lambda _prompt: json.dumps(
            {"activities": [rejected_activity]}, ensure_ascii=False
        ),
    )
    item = report["anchors"][0]["items"][0]
    assert item["gate_outcome"] == schema.GATE_FAILED
    assert item["mark"] == "blocked"
    evidence = item["evidence"]
    assert evidence == []
    assert item["rejected"] == [
        {
            "kind": "rejected",
            "locator": None,
            "machine_reason_class": "gate-failed:raw_contract",
            "content": item["activity"],
        }
    ]


def test_numeral_transfer_counts_generated_pass_warn_and_fail_items(tmp_path):
    true_false = {
        "type": "true-false",
        "instruction": "Познач правильні твердження.",
        "items": [
            {
                "statement": "У тексті названо п'ять столів.",
                "correct": True,
                "evidence": "Третина українців за рік не прочитує жодної книжки",
            },
            {
                "statement": "У тексті згадано 2,5 рази.",
                "correct": True,
                "evidence": "дві третини щодня знаходять час увімкнути телевізор",
            },
            {
                "statement": "Близько три годин тривала розмова.",
                "correct": True,
                "evidence": "активізуються одразу 17 ділянок головного мозку",
            },
        ],
    }
    report = _measure(
        tmp_path,
        generator=lambda _prompt: json.dumps({"activities": [true_false]}, ensure_ascii=False),
    )
    transfer = report["kpi"]["numeral_transfer"]
    assert transfer["numeral_bearing_items"] == 3
    assert transfer["pass"] == transfer["warn"] == transfer["fail"] == 1
    assert transfer["pass_rate"] == transfer["warn_rate"] == transfer["fail_rate"] == 0.333


def test_determinism_probes_are_class_labelled_cache_off_and_byte_identical(tmp_path):
    anchors = [
        {
            "anchor_id": f"{anchor_class}-1",
            "body_uk": fixtures.load_anchor(),
            "measurement_class": anchor_class,
        }
        for anchor_class in ("R", "T", "P", "A")
    ]
    report = _measure(tmp_path, anchors=anchors)
    determinism = report["determinism"]
    assert determinism["cache_enabled"] is False
    assert determinism["runs_per_anchor"] == 2
    assert determinism["complete"] is True
    assert determinism["stable"] is True
    assert set(determinism["classes"]) == {"R", "T", "P", "A"}
    for probe in determinism["classes"].values():
        assert probe["fingerprint_identical"] is True
        assert probe["ir_byte_identical"] is True
        assert [run["bake"]["cache_enabled"] for run in probe["runs"]] == [False, False]


def test_determinism_records_an_unstable_probe(tmp_path):
    calls = 0

    def alternating_generator(_prompt):
        nonlocal calls
        calls += 1
        activities = copy.deepcopy(fixtures.GOOD_ACTIVITIES)
        if calls % 2 == 0:
            activities[0]["instruction"] = "Інша, але чинна інструкція."
        return json.dumps({"activities": activities}, ensure_ascii=False)

    report = measure.measure(
        [{"body_uk": fixtures.load_anchor(), "measurement_class": "R"}],
        generator=alternating_generator,
        out_dir=tmp_path / "measure",
        cache_dir=tmp_path / "cache",
        include_russianism_fixtures=False,
        determinism_probes={"R": 0},
    )
    probe = report["determinism"]["classes"]["R"]
    assert probe["fingerprint_identical"] is True
    assert probe["ir_byte_identical"] is False
    assert probe["stable"] is False
