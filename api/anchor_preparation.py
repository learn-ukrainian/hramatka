"""Deterministic preparation of teacher-provided lesson anchors."""

from __future__ import annotations

import re

from hramatka.engine.gates.vesum_tags import parse_word

_SENTENCE_RE = re.compile(r"[^.!?…]+[.!?…]*")
_UKRAINIAN_WORD_RE = re.compile(r"[А-Яа-яІіЇїЄєҐґ][А-Яа-яІіЇїЄєҐґ’'\-]*")
_HYPHENATED_UKRAINIAN_WORD_RE = re.compile(
    r"[А-Яа-яІіЇїЄєҐґ]+(?:-[А-Яа-яІіЇїЄєҐґ]+)+"
)
_ALPHA_RE = re.compile(r"[A-Za-zА-Яа-яІіЇїЄєҐґ]")
_CYRILLIC_RE = re.compile(r"[А-Яа-яІіЇїЄєҐґ]")
_PAGE_BOUNDARY_RE = re.compile(
    r"\b(?:залишити\s+коментар|коментувати\s+тут|рекомендовані\s+матеріали|"
    r"схожі\s+матеріали|читайте\s+також|попередня\s+стаття|"
    r"наступна\s+стаття|політика\s+конфіденційності|усі\s+права\s+захищено)\b",
    re.IGNORECASE,
)
_PAGE_LABEL_RE = re.compile(
    r"^(?:коментарі|опублікувати|рекомендовані)\s*[:.!?…-]*$",
    re.IGNORECASE,
)


class AnchorPreparationError(ValueError):
    """Raised when page contamination leaves no usable narrative anchor."""

    def __init__(self, message: str, *, suspicious_tokens: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.suspicious_tokens = suspicious_tokens


def _vesum_attested(surface: str) -> bool:
    return bool(parse_word(surface))


def _vesum_parts_of_speech(surface: str) -> frozenset[str]:
    return frozenset(
        str(row["pos"])
        for row in parse_word(surface)
        if isinstance(row.get("pos"), str)
    )


def _has_suspicious_line_break(parts: tuple[str, ...], attested: tuple[bool, ...]) -> bool:
    """Recognize a short broken prefix before a long, lower-case OCR fragment."""
    return len(parts) >= 3 and any(
        len(left) <= 3
        and len(right) >= 6
        and right[:1].islower()
        and not right_ok
        for left, right, right_ok in zip(parts[:-1], parts[1:], attested[1:], strict=True)
    )


def _has_suspicious_noun_adjective_hyphen(parts: tuple[str, ...]) -> bool:
    """Catch a likely lost dash between a noun and its following description."""
    return (
        len(parts) == 2
        and "noun" in _vesum_parts_of_speech(parts[0])
        and "adj" in _vesum_parts_of_speech(parts[1])
    )


def _has_lost_dash_before_repeated_sound(
    parts: tuple[str, ...], _attested: tuple[bool, ...]
) -> bool:
    """Catch prose accidentally hyphenated to a repeated sound effect."""
    repeated = {part.casefold() for part in parts[1:]}
    return (
        len(parts) >= 3
        and len(repeated) == 1
        and parts[0].casefold() not in repeated
        and "adv" in _vesum_parts_of_speech(parts[0])
        and "noninfl" in _vesum_parts_of_speech(parts[1])
    )


def _normalize_hyphenated_ocr(text: str) -> str:
    """Repair only dictionary-proved joins and reject unresolved OCR-like splits."""
    unresolved: list[str] = []

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if _vesum_attested(token):
            return token
        parts = tuple(token.split("-"))
        part_attested = tuple(_vesum_attested(part) for part in parts)
        if len(parts) == 2 and not all(part_attested):
            joined = "".join(parts)
            if _vesum_attested(joined):
                return joined
        if (
            _has_suspicious_line_break(parts, part_attested)
            or _has_suspicious_noun_adjective_hyphen(parts)
            or _has_lost_dash_before_repeated_sound(parts, part_attested)
        ) and token not in unresolved:
            unresolved.append(token)
        return token

    normalized = _HYPHENATED_UKRAINIAN_WORD_RE.sub(replace, text)
    if unresolved:
        raise AnchorPreparationError(
            "Unresolved OCR-like hyphenation in anchor text.",
            suspicious_tokens=tuple(token[:64] for token in unresolved[:5]),
        )
    return normalized


def _word_count(sentence: str) -> int:
    return len(_UKRAINIAN_WORD_RE.findall(sentence))


def _is_hard_boundary(sentence: str) -> bool:
    lowered = sentence.casefold()
    if _PAGE_BOUNDARY_RE.search(lowered) or _PAGE_LABEL_RE.fullmatch(lowered.strip()):
        return True
    if any(marker in sentence for marker in ("{", "}", "</", "/>")):
        return True
    letters = _ALPHA_RE.findall(sentence)
    if not letters:
        return bool(sentence.strip())
    cyrillic_ratio = len(_CYRILLIC_RE.findall(sentence)) / len(letters)
    code_punctuation = sum(sentence.count(marker) for marker in (";", "=", "(", ")"))
    return cyrillic_ratio < 0.45 and code_punctuation >= 2


def _candidate_score(sentences: list[str]) -> tuple[int, int, int]:
    counts = [_word_count(sentence) for sentence in sentences]
    # Cap one giant navigation/recommendation blob so it cannot outweigh a
    # real sequence of complete narrative sentences.
    useful_words = sum(min(count, 24) for count in counts)
    complete_sentences = sum(
        count >= 4 and sentence.rstrip().endswith((".", "!", "?", "…"))
        for sentence, count in zip(sentences, counts, strict=True)
    )
    giant_rows = sum(count > 40 for count in counts)
    return useful_words - (giant_rows * 24), complete_sentences, -giant_rows


def prepare_anchor_text(text: str) -> str:
    """Remove an obvious webpage tail while preserving ordinary teacher text.

    Plain prose is returned byte-for-byte apart from outer whitespace and
    dictionary-proved OCR line-break joins. Multi-mark terminal punctuation is
    preserved. Selection is activated only by a strong page or code boundary,
    then chooses one contiguous Ukrainian narrative segment.
    """
    stripped = text.strip()
    if not stripped:
        raise AnchorPreparationError("Anchor text must not be blank.")
    sentences = [part.strip() for part in _SENTENCE_RE.findall(stripped) if part.strip()]
    boundaries = [index for index, sentence in enumerate(sentences) if _is_hard_boundary(sentence)]
    if not boundaries:
        return _normalize_hyphenated_ocr(stripped)
    # Sentence splitting can leave the first prose sentence attached to a
    # script fragment because JavaScript often has no terminal punctuation.
    # Preserve the Ukrainian suffix after the final closing code delimiter.
    # Build a new sequence so one insertion cannot invalidate later boundary
    # indexes or make ordinary prose masquerade as a code row.
    expanded: list[str] = []
    for sentence in sentences:
        expanded.append(sentence)
        if not _is_hard_boundary(sentence):
            continue
        suffix_start = max(sentence.rfind("}"), sentence.rfind(";"))
        if suffix_start < 0:
            continue
        suffix = sentence[suffix_start + 1 :].strip()
        if _word_count(suffix) >= 4 and not _is_hard_boundary(suffix):
            expanded.append(suffix)
    sentences = expanded
    boundaries = [index for index, sentence in enumerate(sentences) if _is_hard_boundary(sentence)]

    prefix = sentences[: boundaries[0]]
    prefix_words = sum(_word_count(sentence) for sentence in prefix)
    prefix_complete = sum(
        _word_count(sentence) >= 4
        and sentence.rstrip().endswith((".", "!", "?", "…"))
        for sentence in prefix
    )
    if prefix_words >= 80 and prefix_complete >= 8:
        return _normalize_hyphenated_ocr(" ".join(prefix))

    chunks: list[list[str]] = []
    current: list[str] = []
    for sentence in sentences:
        if _is_hard_boundary(sentence):
            if current:
                chunks.append(current)
                current = []
            continue
        current.append(sentence)
    if current:
        chunks.append(current)
    viable = [chunk for chunk in chunks if sum(_word_count(row) for row in chunk) >= 4]
    if not viable:
        raise AnchorPreparationError("No usable Ukrainian narrative remains after preparation.")
    return _normalize_hyphenated_ocr(" ".join(max(viable, key=_candidate_score)))
