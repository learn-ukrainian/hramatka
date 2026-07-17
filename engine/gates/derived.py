"""Derived-mode fail-closed gates (grounding-mode v1 §5.4 / §6.2 / §6.3).

Active only when the caller has established ``grounding_mode_v1``. The model
never self-certifies a rule: every ``rule_id`` must resolve through
``RULE_REGISTRY`` to a Python verifier. Empty kit ⇒ fail closed (E4).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .. import retrieval, schema
from . import numeral, vesum, vesum_tags

# ---------------------------------------------------------------------------
# Identity (bake-id coverage via gates/*.py digest)
# ---------------------------------------------------------------------------
DERIVED_GATES_VERSION = "derived.gates.v1"

# Canonical rule ids — model must emit one of these; unknown ⇒ reject.
RULE_NUMERAL_MOAT = "numeral_moat"

_WORD_RE = re.compile(r"[А-ЯҐЄІЇа-яґєіїʼ'’]+", re.UNICODE)
_DIGIT_RE = re.compile(r"\d+(?:[.,]\d+)?")
_SPACE_RE = re.compile(r"\s+")


def _sig(value: object) -> str:
    return _SPACE_RE.sub(" ", str(value or "").casefold()).strip()


def _pass(rule: str, detail: str) -> dict[str, Any]:
    return {"status": "pass", "rule": rule, "detail": detail, "expected": None}


def _fail(rule: str, detail: str, expected: str | None = None) -> dict[str, Any]:
    return {"status": "fail", "rule": rule, "detail": detail, "expected": expected}


# ---------------------------------------------------------------------------
# Kit lemma closure (§6.2.1)
# ---------------------------------------------------------------------------
def kit_lemma_closure(kit: Mapping[str, Any] | None) -> set[str]:
    """Return the closed lemma set a derived item may draw from.

    Tokens must ⊆ (anchor lemmas ∪ synonym banks ∪ aspect pairs ∪ focus forms).
    Empty / missing kit yields an empty set — callers must fail closed.
    """
    if retrieval.kit_is_empty(kit) or not isinstance(kit, Mapping):
        return set()

    closed: set[str] = set()
    for lemma in kit.get("anchor_lemmas") or []:
        if isinstance(lemma, str) and lemma.strip():
            closed.add(lemma.casefold())

    for bank in kit.get("synonym_banks") or []:
        if not isinstance(bank, Mapping):
            continue
        anchor = bank.get("anchor_lemma")
        if isinstance(anchor, str) and anchor.strip():
            closed.add(anchor.casefold())
        for synonym in bank.get("synonyms") or []:
            if isinstance(synonym, str) and synonym.strip():
                closed.add(synonym.casefold())

    aspect = kit.get("aspect_stress") if isinstance(kit.get("aspect_stress"), Mapping) else {}
    for pair in (aspect or {}).get("pairs") or []:
        if isinstance(pair, Mapping):
            for key in ("imperfective", "perfective", "lemma", "partner"):
                value = pair.get(key)
                if isinstance(value, str) and value.strip():
                    closed.add(value.casefold())
        elif isinstance(pair, (list, tuple)):
            for value in pair:
                if isinstance(value, str) and value.strip():
                    closed.add(value.casefold())

    focus = kit.get("focus_citation_forms")
    if isinstance(focus, Mapping):
        for form in focus.get("forms") or []:
            if isinstance(form, str) and form.strip():
                closed.add(form.casefold())

    return closed


def content_lemmas_for_text(text: str) -> set[str]:
    """Content lemmas (noun/adj/verb/adv) for kit-closure comparison."""
    return {
        lemma.casefold()
        for lemmas in retrieval.lemmatize(text or "").values()
        for lemma in lemmas
    }


def check_kit_closure(
    texts: Sequence[str],
    kit: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Fail closed when any content lemma is outside the kit lemma closure."""
    if retrieval.kit_is_empty(kit):
        return _fail(
            "kit_closure_empty",
            "Empty kit — derived items fail closed (grounding-mode v1 E4/§6.2.1).",
        )
    closed = kit_lemma_closure(kit)
    if not closed:
        return _fail(
            "kit_closure_empty",
            "Kit lemma closure is empty — derived items fail closed.",
        )

    offenders: list[str] = []
    for text in texts:
        for lemma in sorted(content_lemmas_for_text(text)):
            if lemma not in closed:
                offenders.append(lemma)
    if offenders:
        sample = ", ".join(sorted(set(offenders), key=str.casefold)[:8])
        return _fail(
            "kit_closure",
            f"Derived tokens outside kit lemma closure: {sample}.",
        )
    return _pass("kit_closure", "All content lemmas ⊆ kit lemma closure.")


# ---------------------------------------------------------------------------
# G1 — decidable entity / numeral bound (NOT semantic "facts")
# ---------------------------------------------------------------------------
def _is_named_entity_token(token: str) -> bool:
    for parsed in vesum_tags.parse_word(token):
        raw = parsed.get("raw") or ""
        if ":prop" in f":{raw}:" or raw.endswith(":prop") or ":prop:" in raw:
            return True
        # VESUM often encodes proper-name flags as prop / geo / lname / fname.
        parts = set(raw.split(":"))
        if parts & {"prop", "geo", "lname", "fname", "pname"}:
            return True
    return False


def _kit_allowed_surfaces(kit: Mapping[str, Any] | None, anchor_body: str) -> set[str]:
    """Surfaces permitted for G1 entity/numeral checks (kit + anchor literal)."""
    allowed: set[str] = set()
    if isinstance(anchor_body, str):
        allowed.update(t.casefold() for t in vesum.content_tokens(anchor_body))
        allowed.update(_DIGIT_RE.findall(anchor_body))
    if not isinstance(kit, Mapping):
        return allowed
    for lemma in kit.get("anchor_lemmas") or []:
        if isinstance(lemma, str):
            allowed.add(lemma.casefold())
    for bank in kit.get("synonym_banks") or []:
        if not isinstance(bank, Mapping):
            continue
        for key in ("anchor_lemma",):
            value = bank.get(key)
            if isinstance(value, str):
                allowed.add(value.casefold())
        for synonym in bank.get("synonyms") or []:
            if isinstance(synonym, str):
                allowed.add(synonym.casefold())
    for tup in kit.get("numeral_tuples") or []:
        if not isinstance(tup, Mapping):
            continue
        for key in ("value", "trigger", "witness_span"):
            value = tup.get(key)
            if isinstance(value, str) and value.strip():
                allowed.add(value.casefold())
                allowed.update(_DIGIT_RE.findall(value))
        for key in ("noun_lemma",):
            lemmas = tup.get(key)
            if isinstance(lemmas, list):
                for lemma in lemmas:
                    if isinstance(lemma, str):
                        allowed.add(lemma.casefold())
            elif isinstance(lemmas, str):
                allowed.add(lemmas.casefold())
    return allowed


def check_g1_entity_numeral_bound(
    texts: Sequence[str],
    kit: Mapping[str, Any] | None,
    anchor_body: str,
) -> dict[str, Any]:
    """Reject new named entities or numerals outside kit/anchor (G1).

    Decidable subset only — no semantic-facts claims.
    """
    allowed = _kit_allowed_surfaces(kit, anchor_body)
    bad_entities: list[str] = []
    bad_numerals: list[str] = []

    for text in texts:
        if not isinstance(text, str) or not text.strip():
            continue
        for digit in _DIGIT_RE.findall(text):
            if digit.casefold() not in allowed and digit not in allowed:
                # Also allow when the digit string is literally in allowed.
                if not any(digit in surface for surface in allowed):
                    bad_numerals.append(digit)
        for token in vesum.content_tokens(text):
            low = token.casefold()
            if _is_named_entity_token(token) and low not in allowed:
                bad_entities.append(token)
            # Spelled-out numerals (VESUM numr) outside kit/anchor.
            parses = vesum_tags.parse_word(token)
            if any(p.get("pos") == "numr" for p in parses) and low not in allowed:
                bad_numerals.append(token)

    if bad_entities or bad_numerals:
        parts = []
        if bad_entities:
            parts.append(
                "named entities outside kit: "
                + ", ".join(sorted(set(bad_entities), key=str.casefold)[:6])
            )
        if bad_numerals:
            parts.append(
                "numerals outside kit: "
                + ", ".join(sorted(set(bad_numerals), key=str.casefold)[:6])
            )
        return _fail("g1_entity_numeral", "; ".join(parts))
    return _pass("g1_entity_numeral", "No new named entities or numerals outside kit.")


# ---------------------------------------------------------------------------
# RULE_REGISTRY — numeral MOAT first (G2 / §6.3)
# ---------------------------------------------------------------------------
def verify_numeral_moat(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Python oracle for numeral case-government. LLM judgment is not used.

    Accepted payload keys:
      - ``phrase`` (preferred): full numeral+noun phrase to verify
      - or compositional ``value`` + ``noun`` / ``noun_form`` (+ optional
        ``trigger``, ``case`` / ``context_case``)

    Status mapping mirrors ``check_numeral_government``: only ``pass`` is
    acceptance; ``warn`` and ``fail`` are non-passing for registry purposes
    (fail-closed for derived publish of rule-governed items).
    """
    if not isinstance(payload, Mapping):
        return _fail("numeral_moat_payload", "numeral_moat payload must be a mapping.")

    phrase = payload.get("phrase")
    if not isinstance(phrase, str) or not phrase.strip():
        parts: list[str] = []
        trigger = payload.get("trigger")
        if isinstance(trigger, str) and trigger.strip():
            parts.append(trigger.strip())
        value = payload.get("value")
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        noun = payload.get("noun") or payload.get("noun_form")
        if isinstance(noun, str) and noun.strip():
            parts.append(noun.strip())
        phrase = " ".join(parts)

    if not isinstance(phrase, str) or not phrase.strip():
        return _fail(
            "numeral_moat_payload",
            "numeral_moat requires phrase or value+noun fields.",
        )

    context_case = payload.get("context_case") or payload.get("case")
    if isinstance(context_case, list):
        # Kit numeral_tuples may carry a set of VESUM case codes — leave
        # auto-detection when multi-valued.
        context_case = None
    if context_case is not None and not isinstance(context_case, str):
        context_case = None

    result = numeral.check_numeral_government(phrase.strip(), context_case=context_case)
    status = result.get("status", "fail")
    if status == "pass":
        return _pass(
            "numeral_moat",
            f"Python MOAT accepted {phrase!r} [{result.get('rule')}]: {result.get('detail')}",
        )
    # Fail-closed for derived: warn is not enough to self-certify a rule.
    mapped = "fail" if status != "pass" else "pass"
    return {
        "status": mapped,
        "rule": f"numeral_moat:{result.get('rule')}",
        "detail": result.get("detail") or "numeral government not verified",
        "expected": result.get("expected"),
        "moat_status": status,
    }


RuleVerifier = Callable[[Mapping[str, Any]], dict[str, Any]]

# Enumerated registry: rule_id → verifier_function. Unknown ids always reject.
RULE_REGISTRY: dict[str, RuleVerifier] = {
    RULE_NUMERAL_MOAT: verify_numeral_moat,
}


def lookup_rule(rule_id: str | None) -> RuleVerifier | None:
    if not isinstance(rule_id, str) or not rule_id.strip():
        return None
    return RULE_REGISTRY.get(rule_id.strip())


def check_rule_id(rule_id: str | None) -> dict[str, Any]:
    """UNKNOWN / missing rule_id ⇒ reject, always (G2)."""
    if not isinstance(rule_id, str) or not rule_id.strip():
        return _fail(
            "rule_registry_missing",
            "Derived error-correction requires a registered rule_id; missing.",
        )
    if rule_id.strip() not in RULE_REGISTRY:
        return _fail(
            "rule_registry_unknown",
            f"Unknown rule_id {rule_id!r} — model cannot self-certify; rejected.",
        )
    return _pass("rule_registry", f"rule_id={rule_id!r} is registered.")


def verify_registered_rule(rule_id: str | None, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve rule_id and run the Python verifier (never model judgment)."""
    id_check = check_rule_id(rule_id)
    if id_check["status"] != "pass":
        return id_check
    assert isinstance(rule_id, str)
    verifier = RULE_REGISTRY[rule_id.strip()]
    return verifier(payload)


# ---------------------------------------------------------------------------
# G5 — per-batch diversity: distinct_core_lemma_clusters >= ceil(N/3)
# ---------------------------------------------------------------------------
def core_lemma_cluster(lemmas: Sequence[str] | None) -> str:
    """Stable cluster id from declared kit_anchors lemmas (fail-closed identity)."""
    cleaned = sorted(
        {
            lemma.casefold().strip()
            for lemma in (lemmas or [])
            if isinstance(lemma, str) and lemma.strip()
        }
    )
    if not cleaned:
        return ""
    return "|".join(cleaned)


def required_diversity_clusters(batch_size: int) -> int:
    if batch_size <= 0:
        return 0
    return math.ceil(batch_size / 3)


def check_batch_diversity(cluster_ids: Sequence[str]) -> dict[str, Any]:
    """Reject mono-cluster (and under-diverse) derived batches (G5 / §5.4)."""
    n = len(cluster_ids)
    if n <= 0:
        return _fail("diversity_empty", "Derived batch is empty — fail closed.")
    required = required_diversity_clusters(n)
    nonempty = [c for c in cluster_ids if c]
    distinct = len(set(nonempty))
    if len(nonempty) < n:
        return _fail(
            "diversity_missing_cluster",
            "Every derived item needs a non-empty core-lemma cluster "
            "(kit_anchors.lemmas).",
        )
    if distinct < required:
        return _fail(
            "diversity_g5",
            f"Derived batch diversity {distinct} < ceil({n}/3)={required} "
            "(mono-cluster / under-diverse batch rejected).",
        )
    return _pass(
        "diversity_g5",
        f"Derived batch has {distinct} distinct core-lemma clusters "
        f"(required >= {required} for N={n}).",
    )


# ---------------------------------------------------------------------------
# Witness span presence (IR agreement with raw validators — E1)
# ---------------------------------------------------------------------------
def check_witness_span(kit_anchor: schema.KitAnchors | Mapping[str, Any] | None) -> dict[str, Any]:
    if kit_anchor is None:
        return _fail("witness_span", "kit_anchors missing — derived IR requires witness_span.")
    if isinstance(kit_anchor, schema.KitAnchors):
        witness = kit_anchor.witness_span
    elif isinstance(kit_anchor, Mapping):
        witness = kit_anchor.get("witness_span")
    else:
        return _fail("witness_span", "kit_anchors unusable shape.")
    if not isinstance(witness, str) or not witness.strip():
        return _fail("witness_span", "kit_anchors.witness_span is required (G3).")
    return _pass("witness_span", "witness_span present.")


# ---------------------------------------------------------------------------
# Orchestration: gate one derived activity into GateResult
# ---------------------------------------------------------------------------
def _texts_for_item(
    activity_type: str,
    item: Mapping[str, Any],
    activity: Mapping[str, Any],
) -> list[str]:
    """Teacher-visible stem/key text used for kit-closure and G1 bounds.

    Distractor *options* are deliberately excluded from kit-closure: they must
    still be VESUM-valid (type gates) but may be vocabulary traps outside the
    closed kit. Intentional error forms are excluded separately for
    error-correction.
    """
    texts: list[str] = []
    if activity_type == "fill-in":
        for key in ("sentence", "answer"):
            value = item.get(key)
            if isinstance(value, str):
                texts.append(value)
    elif activity_type == "error-correction":
        for key in ("sentence", "correction", "explanation"):
            value = item.get(key)
            if isinstance(value, str):
                texts.append(value)
    elif activity_type == "short-writing":
        for key in ("prompt", "model_answer", "rubric_hint"):
            value = activity.get(key)
            if isinstance(value, str):
                texts.append(value)
    elif activity_type == "sentence-builder":
        starters = activity.get("starters")
        if isinstance(starters, list):
            texts.extend(str(s) for s in starters if isinstance(s, str))
        instruction = activity.get("instruction")
        if isinstance(instruction, str):
            texts.append(instruction)
    return texts


def _kit_anchors_by_locator(
    kit_anchors: Sequence[schema.KitAnchors],
) -> dict[str, schema.KitAnchors]:
    return {ka.locator: ka for ka in kit_anchors}


def _add(gr: schema.GateResult, gate: str, result: Mapping[str, Any], locator: str | None) -> None:
    gr.add(gate, result["status"], result["detail"], locator=locator)


def _replace_error_once(sentence: str, error: str, correction: str) -> str | None:
    """Replace exactly one Ukrainian word-form occurrence of *error*."""
    matches = [
        match
        for match in _WORD_RE.finditer(sentence)
        if _sig(match.group(0)) == _sig(error)
    ]
    if len(matches) != 1:
        return None
    match = matches[0]
    return sentence[: match.start()] + correction + sentence[match.end() :]


def _numeral_phrases_in(text: str) -> list[str]:
    """Compact numeral+noun spans from text (inventory raw_span when present)."""
    if not isinstance(text, str) or not text.strip():
        return []
    phrases: list[str] = []
    for entry in retrieval.extract_numeral_inventory(text):
        raw = entry.get("raw_span")
        if isinstance(raw, str) and raw.strip() and entry.get("following_noun"):
            phrases.append(raw.strip())
        else:
            numeral = entry.get("numeral")
            noun = entry.get("following_noun")
            if isinstance(numeral, str) and isinstance(noun, str) and noun:
                phrases.append(f"{numeral} {noun}")
    return phrases


def _error_correction_moat_payloads(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build MOAT payloads from a correction item (CORRECTED numeral phrases).

    Full prose sentences often carry leading tokens that make the MOAT warn
    ``context-undetermined``; inventory spans (e.g. ``17 ділянок``) are the
    decidable unit the Python oracle owns.
    """
    sentence = item.get("sentence") if isinstance(item.get("sentence"), str) else ""
    error = item.get("error") if isinstance(item.get("error"), str) else ""
    correction = item.get("correction") if isinstance(item.get("correction"), str) else ""
    restored = sentence
    if sentence and error and correction:
        replaced = _replace_error_once(sentence, error, correction)
        if replaced is not None:
            restored = replaced
        else:
            restored = sentence.replace(error, correction, 1)
    phrases = _numeral_phrases_in(restored)
    if phrases:
        return [{"phrase": phrase} for phrase in phrases]
    # Fall back to the restored string only when no inventory span exists.
    return [{"phrase": restored or correction or ""}]


def gate_derived_activity(
    activity: dict[str, Any],
    kit_anchors: Sequence[schema.KitAnchors],
    kit: Mapping[str, Any] | None,
    anchor_body: str,
    gr: schema.GateResult,
    *,
    atlas_lookup: dict | None = None,  # noqa: ARG001 — reserved for heritage WARNs
) -> None:
    """Apply fail-closed derived gates to one activity IR.

    Caller must only invoke this under ``grounding_mode_v1`` for derived types.
    """
    activity_type = activity.get("type") if isinstance(activity, dict) else None
    if not isinstance(activity_type, str):
        gr.add("derived_type", "fail", "Derived gate requires an activity type.", locator=None)
        return

    by_loc = _kit_anchors_by_locator(list(kit_anchors))
    cluster_ids: list[str] = []

    # --- short-writing / activity-level derived types ---
    if activity_type in {"short-writing", "sentence-builder"}:
        loc = "text"
        ka = by_loc.get(loc)
        _add(gr, "derived_witness", check_witness_span(ka), loc)
        texts = _texts_for_item(activity_type, {}, activity)
        _add(gr, "kit_closure", check_kit_closure(texts, kit), loc)
        _add(gr, "g1_entity_numeral", check_g1_entity_numeral_bound(texts, kit, anchor_body), loc)
        lemmas = list(ka.lemmas) if ka is not None else []
        cluster_ids.append(core_lemma_cluster(lemmas))
        # short-writing: machine-checkable constraints[] are mandatory in the
        # derived contract, including model output that bypassed the pack.
        if activity_type == "short-writing":
            constraints = activity.get("constraints")
            if not isinstance(constraints, list) or not constraints:
                gr.add(
                    "short_writing_constraints",
                    "fail",
                    "short-writing constraints[] must be a non-empty list.",
                    locator=loc,
                )
            elif any(not isinstance(c, str) or not c.strip() for c in constraints):
                gr.add(
                    "short_writing_constraints",
                    "fail",
                    "short-writing constraints[] entries must be non-empty strings.",
                    locator=loc,
                )
        _add(gr, "diversity_g5", check_batch_diversity(cluster_ids), loc)
        return

    # --- itemized derived types ---
    items = activity.get("items")
    if not isinstance(items, list) or not items:
        gr.add(
            "derived_items",
            "fail",
            f"{activity_type} requires items for derived gating.",
            locator=None,
        )
        return

    for index, item in enumerate(items):
        loc = f"items[{index}]"
        if not isinstance(item, Mapping):
            gr.add("derived_item", "fail", "Item must be an object.", locator=loc)
            cluster_ids.append("")
            continue
        ka = by_loc.get(loc)
        _add(gr, "derived_witness", check_witness_span(ka), loc)
        lemmas = list(ka.lemmas) if ka is not None else []
        cluster_ids.append(core_lemma_cluster(lemmas))

        texts = _texts_for_item(activity_type, item, activity)
        _add(gr, "kit_closure", check_kit_closure(texts, kit), loc)
        _add(
            gr,
            "g1_entity_numeral",
            check_g1_entity_numeral_bound(texts, kit, anchor_body),
            loc,
        )

        if activity_type == "error-correction":
            rule_id = ka.rule_id if ka is not None else None
            # Prefer rule_id on kit_anchors; allow item-level fallback for IR.
            if rule_id is None and isinstance(item.get("rule_id"), str):
                rule_id = item["rule_id"]
            id_check = check_rule_id(rule_id)
            _add(gr, "rule_registry", id_check, loc)
            if id_check["status"] == "pass":
                payloads = _error_correction_moat_payloads(item)
                # All corrected numeral phrases must pass the Python oracle.
                any_pass = False
                last_verdict: dict[str, Any] = _fail(
                    "rule_verifier", "No numeral phrase available for rule verification."
                )
                for payload in payloads:
                    last_verdict = verify_registered_rule(rule_id, payload)
                    if last_verdict["status"] == "pass":
                        any_pass = True
                        break
                if not any_pass:
                    _add(gr, "rule_verifier", last_verdict, loc)
                else:
                    _add(gr, "rule_verifier", last_verdict, loc)
                    # Erroneous source must not also pass the MOAT on its spans.
                    if rule_id == RULE_NUMERAL_MOAT:
                        err_sentence = (
                            item.get("sentence")
                            if isinstance(item.get("sentence"), str)
                            else ""
                        )
                        err_phrases = _numeral_phrases_in(err_sentence) or [err_sentence]
                        if any(
                            verify_numeral_moat({"phrase": phrase})["status"] == "pass"
                            for phrase in err_phrases
                            if phrase
                        ):
                            gr.add(
                                "rule_verifier",
                                "fail",
                                "numeral_moat: erroneous sentence also passes the "
                                "Python MOAT — not a decidable government error.",
                                locator=loc,
                            )

    _add(gr, "diversity_g5", check_batch_diversity(cluster_ids), locator=None)
