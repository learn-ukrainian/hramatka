"""Deterministic teacher-anchor canonicalization for the live v3 preflight."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import combinations
from types import MappingProxyType
from typing import Final
from urllib.parse import quote

from . import data
from .candidate_bank_receipt_v1 import (
    BankReceiptContext,
    ReceiptSink,
    receipts_for_inventory,
    seal_complete_inventory,
)
from .gates import vesum_tags
from .lesson_workload_45_v1 import cloze_interaction_bounds, planned_interactions
from .linguistics import verify_lemma
from .retrieval import build_atlas_lookup
from .sentence_segmentation_v1 import sentence_spans
from .short_writing_constraints_v3 import ConstraintSpec, VesumToken
from .teacher_ready_density_v3 import (
    COGNITIVE_OPERATION,
    EVIDENCE_CAPACITY_OVERLAYS,
    TEXT_QUESTION_COMPREHENSION_FLOOR,
    floor_for,
    phase_shape_for,
)
from .unit_builders_v3 import (
    AnchorSentence,
    AnchorToken,
    AtlasPassPair,
    CertificationInventory,
    EvidenceCandidate,
    MarkTheWordsRequest,
    ShortWritingTask,
    TrueFalseFact,
)

# The inventory owns this closed semantic layer because VESUM can certify an
# inflected degree form but cannot decide whether drilling that adjective is
# pedagogically meaningful. Unknown lemmas stay source-bound or unavailable.
DEGREE_CATALOG_VERSION: Final = "degree-quality.v3"


@dataclass(frozen=True)
class DegreeLadder:
    positive: str
    comparative: str
    superlative: str
    context_surface: str


@dataclass(frozen=True)
class DegreeErrorFrame:
    rule_id: str
    source_lemma: str
    correct_surface: str
    learner_surface: str
    correction: str
    frame_family: str
    semantic_warrant: str
    degree_specific: bool


@dataclass(frozen=True)
class DegreePracticeFrame:
    answer_lemma: str
    answer: str
    rendering_surface: str
    degree_class: str
    frame_family: str
    semantic_warrant: str
    morphology_class: str = "regular"
    choice_bank: tuple[str, ...] = ()
    exclusion_warrants: tuple[tuple[str, str], ...] = ()


DENIED_DEGREE_LEMMAS: Final[frozenset[str]] = frozenset(
    {
        "ідеальний",
        "єдиний",
        "цегляний",
        "панельний",
        "дерев'яний",
        "дерев’яний",
        "металевий",
        "перший",
        "другий",
        "третій",
        "рожевий",
        "червоний",
        "синій",
        "зелений",
    }
)
DENIED_DEGREE_SURFACES: Final[frozenset[str]] = frozenset({"найважливіше"})
# Safe only when already present in the source.  These lemmas may support
# recognition/cloze, but never the generated degree ladders or catalog frames.
SOURCE_ONLY_DEGREE_LEMMAS: Final[frozenset[str]] = frozenset({"потрібний"})

_DEGREE_LADDER_ROWS: Final = (
    ("простий", "простіший", "найпростіший", "Цей план простіший, ніж попередній."),
    ("тихий", "тихіший", "найтихіший", "Увечері цей район тихіший за центр."),
    ("темний", "темніший", "найтемніший", "Коридор без вікон темніший, ніж кухня."),
    (
        "світлий",
        "світліший",
        "найсвітліший",
        "Кабінет із двома вікнами світліший за коридор.",
    ),
    (
        "холодний",
        "холодніший",
        "найхолодніший",
        "Сьогоднішній ранок холодніший, ніж учорашній.",
    ),
    (
        "новий",
        "новіший",
        "найновіший",
        "Будинок біля парку новіший за будинок у центрі.",
    ),
    (
        "старий",
        "старіший",
        "найстаріший",
        "Гуртожиток старіший, ніж сусідній офісний центр.",
    ),
    (
        "корисний",
        "корисніший",
        "найкорисніший",
        "Окремий кабінет корисніший для роботи, ніж місце на кухні.",
    ),
    (
        "красивий",
        "красивіший",
        "найкрасивіший",
        "Район біля парку красивіший, ніж промислова околиця.",
    ),
    ("дорогий", "дорожчий", "найдорожчий", "Просторий варіант дорожчий за маленький."),
    (
        "дешевий",
        "дешевший",
        "найдешевший",
        "Будинок без ліфта дешевший за будинок з ліфтом.",
    ),
    ("дивний", "дивніший", "найдивніший", "Опис двору дивніший, ніж фотографії."),
    (
        "привітний",
        "привітніший",
        "найпривітніший",
        "Цей сусід привітніший за попереднього.",
    ),
    ("теплий", "тепліший", "найтепліший", "Цегляний будинок тепліший, ніж панельний."),
    ("молодий", "молодший", "наймолодший", "Цей власник молодший за попереднього."),
    ("малий", "менший", "найменший", "Цей кабінет менший за спальню."),
    (
        "близький",
        "ближчий",
        "найближчий",
        "Цей район ближчий до центру, ніж попередній.",
    ),
    ("низький", "нижчий", "найнижчий", "Четвертий поверх нижчий за шостий."),
    ("добрий", "кращий", "найкращий", "Варіант із кабінетом кращий за однокімнатний."),
    ("поганий", "гірший", "найгірший", "З усіх варіантів цей найгірший."),
    ("великий", "більший", "найбільший", "Серед трьох будинків цей найбільший."),
    ("високий", "вищий", "найвищий", "Цей поверх вищий за попередній."),
    ("широкий", "ширший", "найширший", "Новий коридор ширший за старий."),
    ("вузький", "вужчий", "найвужчий", "Цей прохід вужчий, ніж коридор."),
    ("короткий", "коротший", "найкоротший", "Цей маршрут так само короткий, як перший."),
    ("легкий", "легший", "найлегший", "Цей підручник так само легкий, як перший."),
    ("довгий", "довший", "найдовший", "Південний маршрут довший, ніж північний."),
    ("важкий", "важчий", "найважчий", "Сьогоднішній шлях важчий, ніж учорашній."),
    ("безпечний", "безпечніший", "найбезпечніший", "З усіх маршрутів цей найбезпечніший."),
    ("чистий", "чистіший", "найчистіший", "Після ремонту кабінет став ще чистіший."),
    ("швидкий", "швидший", "найшвидший", "Експрес ще швидший за звичайний автобус."),
    ("повільний", "повільніший", "найповільніший", "Старий ліфт ще повільніший за новий."),
    ("зручний", "зручніший", "найзручніший", "Новий розклад ще зручніший для родини."),
    ("просторий", "просторіший", "найпросторіший", "Кабінет після ремонту ще просторіший."),
    ("спокійний", "спокійніший", "найспокійніший", "Серед усіх районів цей найспокійніший."),
    ("глибокий", "глибший", "найглибший", "З усіх басейнів цей найглибший."),
    ("важливий", "важливіший", "найважливіший", "Серед усіх критеріїв цей найважливіший."),
    ("складний", "складніший", "найскладніший", "Цей план складніший, ніж попередній."),
    ("галасливий", "галасливіший", "найгаласливіший", "Центр галасливіший за передмістя."),
    ("звичайний", "звичайніший", "найзвичайніший", "Перший опис звичайніший за другий."),
    ("далекий", "дальший", "найдальший", "Цей будинок дальший від центру, ніж попередній."),
)
_DEGREE_LADDERS: Final = tuple(DegreeLadder(*row) for row in _DEGREE_LADDER_ROWS)
DEGREE_LADDERS: Final = MappingProxyType({row.positive: row for row in _DEGREE_LADDERS})
DEGREE_LEMMA_INDEX: Final = MappingProxyType(
    {
        lemma: row
        for row in _DEGREE_LADDERS
        for lemma in (row.positive, row.comparative, row.superlative)
    }
)


def _practice_frame(
    answer_lemma: str,
    degree_class: str,
    rendering_surface: str,
    *,
    frame_family: str,
    semantic_warrant: str,
    morphology_class: str = "regular",
    answer_surface: str | None = None,
    choice_bank: tuple[str, ...] = (),
) -> DegreePracticeFrame:
    ladder = DEGREE_LADDERS[answer_lemma]
    answer = (
        answer_surface
        or {
            "positive": ladder.positive,
            "comparative": ladder.comparative,
            "superlative": ladder.superlative,
        }[degree_class]
    )
    if rendering_surface.count(answer) != 1:
        raise RuntimeError("Degree practice frame must contain its answer exactly once.")
    bank = choice_bank or (ladder.positive, ladder.comparative, ladder.superlative)
    return DegreePracticeFrame(
        answer_lemma=answer_lemma,
        answer=answer,
        rendering_surface=rendering_surface,
        degree_class=degree_class,
        frame_family=frame_family,
        semantic_warrant=semantic_warrant,
        morphology_class=morphology_class,
        choice_bank=bank,
        exclusion_warrants=tuple(
            (
                option,
                f"{semantic_warrant}; {option} conflicts with the certified degree cue",
            )
            for option in bank
            if option != answer
        ),
    )


def _certify_six_form_banks(
    frames: tuple[DegreePracticeFrame, ...],
    omitted_by_row: tuple[tuple[str, str], ...],
) -> tuple[DegreePracticeFrame, ...]:
    """Bind one explicit, auditable semantic bank to every closed frame."""
    answers = tuple(frame.answer for frame in frames)
    answer_lemmas = {frame.answer: frame.answer_lemma for frame in frames}
    if len(frames) != 8 or len(answers) != len(set(answers)) or len(omitted_by_row) != 8:
        raise RuntimeError("Degree semantic-bank catalogs require eight unique rows.")
    certified: list[DegreePracticeFrame] = []
    for frame, omitted in zip(frames, omitted_by_row, strict=True):
        if len(omitted) != 2 or len(set(omitted)) != 2 or frame.answer in omitted:
            raise RuntimeError("Degree semantic-bank exclusions must omit two other answers.")
        bank = tuple(answer for answer in answers if answer not in set(omitted))
        if len(bank) != 6 or frame.answer not in bank:
            raise RuntimeError("Degree semantic-bank catalog did not produce a six-form bank.")
        exclusions = tuple(
            (
                option,
                f"{frame.semantic_warrant}; carrier supplies no warrant for "
                f"{answer_lemmas[option]}",
            )
            for option in bank
            if option != frame.answer
        )
        certified.append(
            replace(
                frame,
                choice_bank=bank,
                exclusion_warrants=exclusions,
            )
        )
    return tuple(certified)


DEGREE_RECOGNITION_FRAMES: Final = (
    _practice_frame(
        "простий",
        "positive",
        "Цей план так само простий, як попередній.",
        frame_family="equative-similarity.v1",
        semantic_warrant="так само ... як licenses the positive degree",
    ),
    _practice_frame(
        "тихий",
        "positive",
        "Цей район так само тихий, як центр.",
        frame_family="equative-similarity.v1",
        semantic_warrant="так само ... як licenses the positive degree",
    ),
    _practice_frame(
        "темний",
        "comparative",
        DEGREE_LADDERS["темний"].context_surface,
        frame_family="explicit-nizh-contrast.v1",
        semantic_warrant="ніж introduces a two-way comparison",
    ),
    _practice_frame(
        "світлий",
        "comparative",
        DEGREE_LADDERS["світлий"].context_surface,
        frame_family="explicit-za-contrast.v1",
        semantic_warrant="за introduces a two-way comparison",
    ),
    _practice_frame(
        "холодний",
        "comparative",
        DEGREE_LADDERS["холодний"].context_surface,
        frame_family="temporal-nizh-contrast.v1",
        semantic_warrant="ніж contrasts two mornings",
    ),
    _practice_frame(
        "новий",
        "comparative",
        DEGREE_LADDERS["новий"].context_surface,
        frame_family="locative-za-contrast.v1",
        semantic_warrant="за contrasts two buildings",
    ),
    _practice_frame(
        "старий",
        "comparative",
        "За роком будівництва цей гуртожиток старіший за два сусідні будинки.",
        frame_family="ordinal-za-contrast.v2",
        semantic_warrant="за introduces an explicit age comparison",
    ),
    _practice_frame(
        "корисний",
        "comparative",
        "Для роботи з дому окремий кабінет корисніший за місце на кухні.",
        frame_family="purpose-za-contrast.v2",
        semantic_warrant="за introduces an explicit usefulness comparison",
    ),
)

DEGREE_CLOZE_PASSAGE: Final = (
    "Ми обирали між трьома квартирами. "
    "Перша була така сама красива, як на фотографіях. "
    "Район біля неї був такий самий тихий, як район біля другої квартири. "
    "Проте оренда першої квартири була дорожча, ніж ми планували. "
    "Друга була дешевша за першу. "
    "Проте її кухня виявилася меншою, ніж у першій квартирі. "
    "Третя квартира була тепліша за дві інші. "
    "З усіх трьох вона була найсвітліша завдяки трьом великим вікнам. "
    "Водночас до парку ця квартира була найближча з усіх. "
    "Після огляду ми обрали третю квартиру."
)
DEGREE_CLOZE_FRAMES: Final = (
    _practice_frame(
        "красивий",
        "positive",
        DEGREE_CLOZE_PASSAGE,
        answer_surface="красива",
        choice_bank=("красива", "красивіша", "найкрасивіша"),
        frame_family="coherent-equative-description.v1",
        semantic_warrant="така сама ... як licenses the positive degree",
    ),
    _practice_frame(
        "тихий",
        "positive",
        DEGREE_CLOZE_PASSAGE,
        answer_surface="тихий",
        choice_bank=("тихий", "тихіший", "найтихіший"),
        frame_family="coherent-equative-description.v1",
        semantic_warrant="такий самий ... як licenses the positive degree",
    ),
    _practice_frame(
        "дорогий",
        "comparative",
        DEGREE_CLOZE_PASSAGE,
        answer_surface="дорожча",
        choice_bank=("дорога", "дорожча", "найдорожча"),
        frame_family="coherent-budget-contrast.v1",
        semantic_warrant="ніж ми планували licenses a comparative",
    ),
    _practice_frame(
        "дешевий",
        "comparative",
        DEGREE_CLOZE_PASSAGE,
        answer_surface="дешевша",
        choice_bank=("дешева", "дешевша", "найдешевша"),
        frame_family="coherent-budget-contrast.v1",
        semantic_warrant="за першу licenses a comparative",
    ),
    _practice_frame(
        "малий",
        "comparative",
        DEGREE_CLOZE_PASSAGE,
        answer_surface="меншою",
        choice_bank=("малою", "меншою", "найменшою"),
        frame_family="coherent-feature-contrast.v1",
        semantic_warrant="ніж у першій квартирі licenses a comparative",
    ),
    _practice_frame(
        "теплий",
        "comparative",
        DEGREE_CLOZE_PASSAGE,
        answer_surface="тепліша",
        choice_bank=("тепла", "тепліша", "найтепліша"),
        frame_family="coherent-feature-contrast.v1",
        semantic_warrant="за дві інші licenses a comparative",
    ),
    _practice_frame(
        "світлий",
        "superlative",
        DEGREE_CLOZE_PASSAGE,
        answer_surface="найсвітліша",
        choice_bank=("світла", "світліша", "найсвітліша"),
        frame_family="coherent-choice-ranking.v1",
        semantic_warrant="the third option is ranked across all three",
    ),
    _practice_frame(
        "близький",
        "superlative",
        DEGREE_CLOZE_PASSAGE,
        answer_surface="найближча",
        choice_bank=("близька", "ближча", "найближча"),
        frame_family="coherent-choice-ranking.v1",
        semantic_warrant="the third option is ranked across all three",
    ),
)

_DEGREE_FORMATION_FRAMES: Final = (
    _practice_frame(
        "складний",
        "comparative",
        "Після третього невдалого огляду почався ще складніший етап пошуку. (складний)",
        frame_family="guided-state-change.v2",
        semantic_warrant="ще after a state change licenses складніший",
    ),
    _practice_frame(
        "галасливий",
        "comparative",
        "Після тихого передмістя ми відразу помітили, що центр ще галасливіший. (галасливий)",
        frame_family="guided-perception-change.v2",
        semantic_warrant="ще after a perceived contrast licenses галасливіший",
    ),
    _practice_frame(
        "дорогий",
        "comparative",
        "Із двох подібних оголошень варіант у центрі дорожчий за варіант біля парку. (дорогий)",
        frame_family="guided-listing-contrast.v2",
        semantic_warrant="за introduces the requested price comparison",
        morphology_class="alternation",
    ),
    _practice_frame(
        "близький",
        "comparative",
        "Для щоденних поїздок будинок біля трамвая ближчий до центру, ніж будинок "
        "на околиці. (близький)",
        frame_family="guided-commute-contrast.v2",
        semantic_warrant="ніж introduces the requested distance comparison",
        morphology_class="alternation",
    ),
    _practice_frame(
        "високий",
        "comparative",
        "Шостий поверх вищий за четвертий; родина врахувала це під час вибору. (високий)",
        frame_family="guided-floor-contrast.v2",
        semantic_warrant="за introduces the requested floor comparison",
        morphology_class="alternation",
    ),
    _practice_frame(
        "добрий",
        "comparative",
        "Після обговорення ми вирішили, що варіант пані Оксани кращий за решту. (добрий)",
        frame_family="guided-decision-contrast.v2",
        semantic_warrant="за решту licenses the suppletive comparative кращий",
        morphology_class="suppletive",
    ),
    _practice_frame(
        "поганий",
        "superlative",
        "Через холод і темряву другий варіант — найгірший з усіх. (поганий)",
        frame_family="guided-evidence-ranking.v2",
        semantic_warrant="з усіх plus the stated defects licenses найгірший",
        morphology_class="suppletive",
    ),
    _practice_frame(
        "великий",
        "superlative",
        "Серед трьох оглянутих будинків третій був найбільший за площею. (великий)",
        frame_family="guided-set-ranking.v2",
        semantic_warrant="серед трьох licenses the suppletive superlative найбільший",
        morphology_class="suppletive",
    ),
)
DEGREE_FORMATION_FRAMES: Final = _certify_six_form_banks(
    _DEGREE_FORMATION_FRAMES,
    (
        ("галасливіший", "дорожчий"),
        ("дорожчий", "ближчий"),
        ("ближчий", "вищий"),
        ("вищий", "кращий"),
        ("кращий", "найгірший"),
        ("найгірший", "складніший"),
        ("найбільший", "складніший"),
        ("складніший", "галасливіший"),
    ),
)

DEGREE_SYNTAX_FRAMES: Final = (
    _practice_frame(
        "короткий",
        "positive",
        "Після зміни розкладу новий маршрут так само короткий, як попередній.",
        frame_family="discourse-equivalence.v2",
        semantic_warrant="так само ... як licenses the positive form",
    ),
    _practice_frame(
        "легкий",
        "positive",
        "Після редагування цей текст так само легкий для читання, як попередній.",
        frame_family="discourse-equivalence.v2",
        semantic_warrant="так само ... як licenses the positive form",
    ),
    _practice_frame(
        "довгий",
        "comparative",
        "Що довший був пошук, то простішими ставали наші вимоги.",
        frame_family="correlative-change.v2",
        semantic_warrant="що ... то licenses the comparative form",
    ),
    _practice_frame(
        "важливий",
        "comparative",
        "З кожним оглядом цей критерій дедалі важливіший.",
        frame_family="progressive-change.v2",
        semantic_warrant="дедалі licenses the comparative form",
    ),
    _practice_frame(
        "теплий",
        "comparative",
        "Після утеплення цей будинок ще тепліший.",
        frame_family="resultative-change.v2",
        semantic_warrant="ще after a resultative change licenses a comparative",
    ),
    _practice_frame(
        "важкий",
        "comparative",
        "Підйом на шостий поверх важчий за підйом на другий.",
        frame_family="za-effort-contrast.v2",
        semantic_warrant="за licenses the comparative form",
    ),
    _practice_frame(
        "просторий",
        "comparative",
        "Після перепланування цей кабінет значно просторіший, ніж був раніше.",
        frame_family="before-after-nizh.v2",
        semantic_warrant="ніж був раніше licenses the comparative form",
    ),
    _practice_frame(
        "дорогий",
        "comparative",
        "Чим ближче до центру розташований будинок, тим дорожчий він зазвичай.",
        frame_family="correlative-location-cost.v2",
        semantic_warrant="чим ... тим licenses the comparative form",
    ),
)

_DEGREE_CONTEXT_FRAMES: Final = (
    _practice_frame(
        "чистий",
        "comparative",
        "Після генерального прибирання перший під'їзд тепер ще чистіший за другий.",
        frame_family="result-context.v2",
        semantic_warrant="прибирання and ще warrant чистіший",
    ),
    _practice_frame(
        "швидкий",
        "comparative",
        "— Чому ти обираєш експрес? — Він швидший за звичайний автобус: їде без "
        "пересадок і майже не стоїть у заторах.",
        frame_family="dialogue-justification.v2",
        semantic_warrant="direct travel without delays warrants швидший",
    ),
    _practice_frame(
        "повільний",
        "comparative",
        "Старий ліфт часто зупиняється між поверхами, тому він повільніший за новий.",
        frame_family="causal-service-contrast.v2",
        semantic_warrant="frequent stops warrant повільніший",
    ),
    _practice_frame(
        "зручний",
        "comparative",
        "Для родини з дитячим візком маршрут без сходів зручніший за шлях через підземний перехід.",
        frame_family="user-priority-contrast.v2",
        semantic_warrant="the stroller constraint warrants зручніший",
    ),
    _practice_frame(
        "просторий",
        "comparative",
        "Для двох робочих столів кабінет на 28 м² просторіший за кімнату на 16 м².",
        frame_family="goal-area-contrast.v2",
        semantic_warrant="two desks and the stated areas warrant просторіший",
    ),
    _practice_frame(
        "спокійний",
        "superlative",
        "У другому районі після десятої не чути транспорту; серед трьох він найспокійніший.",
        frame_family="quiet-hours-ranking.v2",
        semantic_warrant="the absence of night traffic warrants найспокійніший",
    ),
    _practice_frame(
        "безпечний",
        "superlative",
        "У третьому районі є освітлені переходи й нічний патруль; з усіх трьох він найбезпечніший.",
        frame_family="safety-feature-ranking.v2",
        semantic_warrant="the stated safety features warrant найбезпечніший",
    ),
    _practice_frame(
        "важливий",
        "superlative",
        "Більшість родин поставила розташування на перше місце, тому цей критерій "
        "найважливіший серед усіх.",
        frame_family="declared-priority-ranking.v2",
        semantic_warrant="first place in the stated priority warrants найважливіший",
    ),
)
DEGREE_CONTEXT_FRAMES: Final = _certify_six_form_banks(
    _DEGREE_CONTEXT_FRAMES,
    (
        ("швидший", "повільніший"),
        ("повільніший", "зручніший"),
        ("зручніший", "просторіший"),
        ("просторіший", "найспокійніший"),
        ("найспокійніший", "найбезпечніший"),
        ("найбезпечніший", "найважливіший"),
        ("найважливіший", "чистіший"),
        ("чистіший", "швидший"),
    ),
)

DEGREE_PARAPHRASE_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    ("Перша квартира дешевша за другу.", "За другу квартиру доведеться платити більше."),
    (
        "Кімната з двома вікнами світліша.",
        "До кімнати з одним вікном потрапляє менше денного освітлення.",
    ),
    (
        "Будинок біля трамвая ближчий до центру.",
        "Від будинку на околиці дорога до центру довша.",
    ),
    ("Уночі центр галасливіший за передмістя.", "У передмісті вночі легше знайти тишу."),
    (
        "Цегляний будинок тепліший за панельний.",
        "У панельному будинку взимку важче зберігати тепло.",
    ),
    (
        "Будинок із ліфтом зручніший для родини з візком.",
        "У будинку без ліфта родині з візком складніше пересуватися.",
    ),
    ("Кабінет менший за спальню.", "У спальні більше місця."),
    (
        "Варіант із двома кімнатами кращий для роботи з дому.",
        "Однокімнатний варіант гірше відповідає потребі мати окремий кабінет.",
    ),
)

DEGREE_PRIORITY_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    (
        "Родина з немовлям шукає тиші. У першому районі вночі чути трамваї, "
        "у другому після десятої немає вуличного шуму.",
        "Краще підійде другий район, бо він тихіший.",
    ),
    (
        "Для щоденних поїздок важливо швидко діставатися центру. Від першого будинку трамвай "
        "іде 15 хвилин, від другого — 35.",
        "Краще підійде перший будинок, бо шлях із нього коротший.",
    ),
    (
        "Родині потрібне місце для двох робочих столів. Кімнати мають 12, 24 і 18 м².",
        "Краще підійде друга кімната, бо вона найбільша.",
    ),
    (
        "Людині, яка постійно мерзне, потрібна спальня з комфортною температурою. "
        "У першій кімнаті 17 °C, у другій — 20 °C.",
        "Краще підійде друга кімната, бо вона тепліша.",
    ),
    (
        "Студент може витрачати на оренду не більше 16 тисяч гривень. Три варіанти "
        "коштують 12, 18 і 25 тисяч.",
        "Краще підійде перший варіант, бо він найдешевший.",
    ),
    (
        "Фотографові потрібне денне світло. Перша кімната має два південні вікна, "
        "друга — одне північне.",
        "Краще підійде перша кімната, бо вона світліша.",
    ),
    (
        "Літній людині важко ходити сходами. У першому будинку є ліфт, у другому — немає.",
        "Краще підійде перший будинок, бо він зручніший.",
    ),
    (
        "Покупець хоче сучасні комунікації й не хоче робити капітальний ремонт. "
        "Будинки зведено 1980, 1995 і 2025 року; третій уже готовий до заселення.",
        "Краще підійде третій будинок, бо він найновіший і готовий до заселення.",
    ),
)
DEGREE_WRITING_LEMMAS: Final[tuple[str, ...]] = (
    "малий",
    "світлий",
    "теплий",
    "близький",
)
DEGREE_WRITING_SCENARIO: Final = (
    "Пара, що працює з дому, обирає між двома квартирами. У квартирі А — 42 м², "
    "два південні вікна, 21 °C і 15 хвилин до центру. У квартирі Б — 58 м², "
    "одне північне вікно, 18 °C і 35 хвилин до центру. Для них важливі денне "
    "світло й коротка дорога, але також потрібне місце для двох робочих столів. "
    "Словникові форми опорних прикметників: малий, світлий, теплий, близький."
)
DEGREE_WRITING_WARRANTS: Final[tuple[tuple[str, str], ...]] = (
    ("малий", "42 м² versus 58 м² warrants a size comparison"),
    ("світлий", "two windows versus one window warrants a light comparison"),
    ("теплий", "21 °C versus 18 °C warrants a temperature comparison"),
    ("близький", "15 minutes versus 35 minutes warrants a distance comparison"),
)
DEGREE_BOARD_LEMMAS: Final[tuple[str, ...]] = (
    "простий",
    "тихий",
    "темний",
    "світлий",
    "холодний",
    "новий",
    "старий",
    "корисний",
)

_DEGREE_ERROR_ROWS: Final = (
    (
        "agreement-gender.v1",
        "малий",
        "Після перепланування нова квартира стала ще менша.",
        "Після перепланування нова квартира стала ще менший.",
        "менша",
        "agreement-gender.v1",
        "нова квартира requires feminine agreement",
        False,
    ),
    (
        "agreement-gender.v1",
        "світлий",
        "Ця кімната світліша, ніж коридор.",
        "Ця кімната світліший, ніж коридор.",
        "світліша",
        "agreement-gender.v1",
        "ця кімната requires feminine agreement",
        False,
    ),
    (
        "comparative-required.v1",
        "тихий",
        "Уранці цей район ще тихіший.",
        "Уранці цей район ще найтихіший.",
        "тихіший",
        "comparative-cue.v1",
        "ще licenses a comparative, not a superlative",
        True,
    ),
    (
        "superlative-required.v1",
        "простий",
        "Це найпростіший варіант з усіх.",
        "Це простіший варіант з усіх.",
        "найпростіший",
        "superlative-set.v1",
        "з усіх licenses a superlative",
        True,
    ),
    (
        "comparative-required.v1",
        "дешевий",
        "Друга квартира дешевша за першу.",
        "Друга квартира найдешевша за першу.",
        "дешевша",
        "comparative-pair.v1",
        "за першу licenses a two-way comparative",
        True,
    ),
    (
        "agreement-predicative.v1",
        "теплий",
        "Сонячна спальня тепліша, ніж кухня.",
        "Сонячна спальня тепліше, ніж кухня.",
        "тепліша",
        "predicate-agreement.v1",
        "спальня requires an agreeing adjective rather than an adverb",
        True,
    ),
    (
        "comparative-required.v1",
        "новий",
        "Новий будинок помітно новіший за сусідній.",
        "Новий будинок помітно найновіший за сусідній.",
        "новіший",
        "comparative-pair.v1",
        "за сусідній licenses a two-way comparative",
        True,
    ),
    (
        "comparative-required.v1",
        "близький",
        "Цей маршрут удвічі ближчий до центру.",
        "Цей маршрут удвічі найближчий до центру.",
        "ближчий",
        "comparative-scale.v1",
        "удвічі licenses a comparative scale",
        True,
    ),
)
DEGREE_ERROR_FRAMES: Final = tuple(DegreeErrorFrame(*row) for row in _DEGREE_ERROR_ROWS)

DEGREE_60_SCHEDULE: Final[tuple[tuple[str, str, str], ...]] = (
    ("P1-A1", "text-questions", "anchor-comprehension"),
    ("P1-A2", "quiz", "degree-recognition"),
    ("P1-A3", "cloze", "degree-cloze"),
    ("P2-A1", "fill-in", "degree-formation"),
    ("P2-A2", "match-up", "degree-positive-comparative"),
    ("P2-A3", "error-correction", "degree-error-correction"),
    ("P2-A4", "quiz", "degree-comparison-syntax"),
    ("P2-A5", "fill-in", "degree-context"),
    ("P3-A1", "match-up", "degree-comparative-superlative"),
    ("P3-A2", "short-writing", "degree-writing"),
)


def degree_role(slot_id: str, activity_type: str) -> str | None:
    return next(
        (
            role
            for candidate_slot, candidate_type, role in DEGREE_60_SCHEDULE
            if candidate_slot == slot_id and candidate_type == activity_type
        ),
        None,
    )


def ladder_for_lemma(lemma: str) -> DegreeLadder | None:
    return DEGREE_LEMMA_INDEX.get(lemma.casefold())


_TOKEN_RE = re.compile(r"[А-Яа-яІіЇїЄєҐґ'’]+")
_CONTENT_POS = frozenset({"noun", "verb", "adj", "adv"})
_CLOSED_CLASS_POS = frozenset({"prep", "part", "conj", "intj"})
_LIST_TYPES = frozenset(
    {
        "true-false",
        "quiz",
        "cloze",
        "match-up",
        "error-correction",
        "fill-in",
        "text-questions",
        "mark-the-words",
    }
)
_TEXT_QUESTION_CATEGORIES = (
    "comprehension",
    "comprehension",
    "comprehension",
    "explanation_inference",
    "explanation_inference",
    "explanation_inference",
    "anchored_application",
    "anchored_application",
)
_TEXT_QUESTION_INTENTS = (
    "fact-recovery",
    "fact-recovery",
    "fact-recovery",
    "explicit-causal",
    "explicit-causal",
    "explicit-causal",
    "realistic-transfer",
    "realistic-transfer",
)
_SOURCE_COMPREHENSION_45 = (
    "match-up",
    "quiz",
    "fill-in",
    "error-correction",
    "mark-the-words",
    "cloze",
)
_RELATION_PRIORITY = (
    "temporal-clause.v1",
    "purpose-clause.v1",
    "licensed-vid-cause.v1",
    "definition-content.v1",
    "causal-clause.v1",
)
_NEGATION_SCOPE_EXCLUSIONS = frozenset(
    {
        "лише",
        "тільки",
        "навіть",
        "теж",
        "також",
        "майже",
        "ось",
        "он",
        "от",
        "онде",
        "вже",
        "ще",
        "усі",
        "всі",
        "кожен",
        "кожна",
        "кожне",
        "кожні",
        "завжди",
        "зрідка",
        "рідко",
        "часто",
        "іноді",
        "інколи",
        "щоразу",
        "якийсь",
        "якась",
        "якесь",
        "якісь",
        "хтось",
        "щось",
    }
)
_SUBORDINATE_MARKERS = frozenset(
    {"бо", "адже", "оскільки", "аби", "щоб", "поки", "коли", "доки", "якби", "що"}
)
_MODAL_LEMMAS = frozenset({"мати", "могти", "мусити", "хотіти"})
_EXISTENTIAL_LEMMAS = frozenset({"бути", "бувати", "існувати", "траплятися"})
_LICENSED_VID_CAUSE_LEMMAS = frozenset({"щулитися"})


@dataclass(frozen=True)
class _PredicateSpan:
    token: AnchorToken
    clause_start: int
    clause_end: int


@dataclass(frozen=True)
class _RelationSpan:
    rule_id: str
    sentence: AnchorSentence
    answer_start: int
    answer_end: int
    topic_token: AnchorToken
    topic_lemma: str


_EXPLICIT_CAUSAL_RE = re.compile(r"\b(?:тому|бо|адже|оскільки|завдяки|через\s+те)\b")
_ANAPHORIC_WRITING_OPENING_RE = re.compile(
    r"^(?:так(?:ий|а|е|і)|це|цей|ця|ці|також|тому)\b",
    re.IGNORECASE,
)
_FOCUS_PRIMARY = "degree-primary"
_FOCUS_REINFORCEMENT = "degree-reinforcement"
_FOCUS_WRITING = "degree-writing"
_DEGREE_LIST_ROLES = frozenset(
    {
        "degree-recognition",
        "degree-cloze",
        "degree-formation",
        "degree-comparison-syntax",
        "degree-context",
        "degree-error-correction",
    }
)
_UNSAFE_TAG_MARKERS = (
    ":arch",
    ":rare",
    ":xp",
    ":coll",
    ":obsc",
    ":long",
    ":short",
    ":prop",
)
_TOPIC_UNSAFE_TAG_MARKERS = tuple(
    marker for marker in _UNSAFE_TAG_MARKERS if marker not in {":prop", ":xp"}
)
_UNSAFE_ATLAS_ANTONYM_PAIRS = frozenset(
    {
        ("ідеальний", "дійсний"),
        ("маленький", "великий"),
        ("наступний", "колишній"),
        ("піти", "стати"),
    }
)
# Atlas synonym lists are sense-aggregated, so a generic "first synonym" can
# be wrong for the source sense (for example, artillery terminology for a home
# heating battery).  Admit only relations independently judged safe as
# context-free B1 match pairs; antonyms remain the preferred relation.
_SAFE_ATLAS_SYNONYM_PAIRS = frozenset({("квартира", "помешкання")})
_UNSAFE_QUESTION_TOPIC_LEMMAS: Final[frozenset[str]] = frozenset(
    {"випадок", "красень", "річ", "щось"}
)
_UNSAFE_GLOSS_LEMMAS: Final[frozenset[str]] = frozenset(
    {"красень", "розповідати", "хапати", "злітати"}
)


def _source_id(anchor: str) -> str:
    return f"teacher-anchor:{hashlib.sha256(anchor.encode('utf-8')).hexdigest()}"


def _token_parses(surface: str, *, sentence_initial: bool) -> tuple[dict[str, object], ...]:
    """Resolve ordinary sentence-initial words before capitalized-name homonyms."""
    variants = (
        (surface.casefold(), surface) if sentence_initial and surface[:1].isupper() else (surface,)
    )
    rows: list[dict[str, object]] = []
    seen: set[tuple[object, object, object]] = set()
    for variant in variants:
        for parse in vesum_tags.parse_word(variant):
            key = (parse.get("lemma"), parse.get("pos"), parse.get("raw"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(dict(parse))
    return tuple(rows)


def _sentences(anchor: str) -> tuple[AnchorSentence, ...]:
    rows: list[AnchorSentence] = []
    for text in sentence_spans(anchor):
        sentence_number = len(rows) + 1
        tokens: list[AnchorToken] = []
        for token_match in _TOKEN_RE.finditer(text):
            surface = token_match.group(0)
            parses = _token_parses(surface, sentence_initial=not tokens)
            tokens.append(
                AnchorToken(
                    sentence_id=f"s-{sentence_number}",
                    token_id=f"s-{sentence_number}:t-{len(tokens) + 1}",
                    surface=surface,
                    start_offset=token_match.start(),
                    end_offset=token_match.end(),
                    vesum_parses=parses,
                )
            )
        rows.append(
            AnchorSentence(sentence_id=f"s-{sentence_number}", text=text, tokens=tuple(tokens))
        )
    return tuple(rows)


def _lemma_for(token: AnchorToken) -> str | None:
    def priority(parse: dict[str, object]) -> tuple[int, str]:
        raw = str(parse.get("raw", ""))
        if ":nv" in raw:
            return (0, raw)
        if ":prop:" not in raw:
            return (1, raw)
        if ":geo" in raw:
            return (2, raw)
        return (3, raw)

    candidates = []
    for parse in token.vesum_parses:
        raw = str(parse.get("raw", ""))
        lemma = parse.get("lemma")
        if not isinstance(lemma, str) or not lemma.strip() or parse.get("pos") not in _CONTENT_POS:
            continue
        if ":pron" in raw or any(
            marker in raw for marker in (":fname", ":lname", ":patr", ":arch", ":rare", ":xp")
        ):
            continue
        candidates.append(dict(parse))
    if not candidates:
        return None
    lemma = min(candidates, key=priority).get("lemma")
    if not isinstance(lemma, str) or lemma.casefold() == token.surface.casefold():
        return None
    return lemma


def degree_focus_requested(focus: str | None) -> bool:
    """Return whether *focus* names a comparative/superlative degree lesson."""
    if not isinstance(focus, str):
        return False
    normalized = focus.casefold()
    return (
        "компаратив" in normalized
        or "суперлатив" in normalized
        or ("ступен" in normalized and "порівнян" in normalized)
    )


def _focus_rank(token: AnchorToken, focus: str | None) -> int:
    """Prefer source-attested focus forms without excluding other capacity."""
    if not degree_focus_requested(focus):
        return 0
    raws = tuple(str(parse.get("raw", "")) for parse in token.vesum_parses)
    if any("compc" in raw or "comps" in raw for raw in raws):
        return 0
    if any(
        parse.get("pos") == "adj" and "compb" in str(parse.get("raw", ""))
        for parse in token.vesum_parses
    ):
        return 1
    return 2


def _is_predicative_adverb_token(token: AnchorToken) -> bool:
    return any(
        parse.get("pos") == "adv" and ":predic" in f":{parse.get('raw', '')}"
        for parse in token.vesum_parses
    )


def _degree_ladder_for_token(token: AnchorToken) -> DegreeLadder | None:
    """Resolve an attested adjective token to a closed catalog ladder."""
    for parse in token.vesum_parses:
        lemma = parse.get("lemma")
        if parse.get("pos") != "adj" or not isinstance(lemma, str):
            continue
        ladder = ladder_for_lemma(lemma)
        if ladder is not None:
            return ladder
    return None


def _ladder_is_vesum_certified(ladder: DegreeLadder) -> bool:
    """Require all three catalog surfaces to retain their declared degree."""
    expected = (
        (ladder.positive, "compb"),
        (ladder.comparative, "compc"),
        (ladder.superlative, "comps"),
    )
    return all(
        any(
            parse.get("pos") == "adj" and marker in str(parse.get("raw", ""))
            for parse in _token_parses(surface, sentence_initial=False)
        )
        for surface, marker in expected
    )


def _without_closing_marks(text: str) -> str:
    terminal = text.rstrip()
    while terminal and terminal[-1] in "»”\"')]":
        terminal = terminal[:-1].rstrip()
    return terminal


def _safe_item_carrier(sentence: AnchorSentence) -> bool:
    """Exclude title/fragment rows while retaining complete nominal clauses."""
    text = sentence.text.strip()
    terminal = _without_closing_marks(text)
    return (
        bool(text)
        and terminal[-1:] in {".", "!", "?", "…"}
        and text.count("«") == text.count("»")
        and text.count("(") == text.count(")")
        and text.count("[") == text.count("]")
    )


def _safe_true_fact_carrier(sentence: AnchorSentence) -> bool:
    """Admit only declarative source sentences as literal true/false statements."""
    text = _without_closing_marks(sentence.text)
    return _safe_item_carrier(sentence) and text.endswith(".")


def _uses_second_person(sentence: AnchorSentence) -> bool:
    if any(
        token.surface.casefold() in {"ти", "тебе", "тобі", "тобою", "ви", "вас", "вам", "вами"}
        for token in sentence.tokens
    ):
        return True
    return any(
        parse.get("pos") == "verb"
        and re.search(r":(?:s|p):2(?:$|:)", str(parse.get("raw", ""))) is not None
        for token in sentence.tokens
        for parse in token.vesum_parses
    )


def _has_unresolved_personal_pronoun(sentence: AnchorSentence) -> bool:
    has_named_noun = False
    for token in sentence.tokens:
        if any(":pron:pers:" in str(parse.get("raw", "")) for parse in token.vesum_parses):
            if not has_named_noun:
                return True
            continue
        has_named_noun = has_named_noun or (
            _topic_lemma(token) is not None
            and any(parse.get("pos") == "noun" for parse in token.vesum_parses)
        )
    return False


def _safe_source_proposition_carrier(sentence: AnchorSentence) -> bool:
    """Require a declarative proposition with room for a non-revealing question."""
    content_lemmas = {
        lemma for token in sentence.tokens if (lemma := _topic_lemma(token)) is not None
    }
    return (
        _safe_item_carrier(sentence)
        and sentence.text.rstrip()[-1:] in {".", "!"}
        and len(content_lemmas) >= 3
        and not _uses_second_person(sentence)
        and not _has_unresolved_personal_pronoun(sentence)
    )


def _is_degree_token(token: AnchorToken) -> bool:
    """Return whether the token is an adjective in the degree paradigm."""
    has_adjective_degree_parse = any(
        parse.get("pos") == "adj"
        and any(marker in str(parse.get("raw", "")) for marker in ("compb", "compc", "comps"))
        for parse in token.vesum_parses
    )
    # Without contextual disambiguation, an adv:predic homonym is unsafe as an
    # adjective-focus target (e.g. "в ньому тепліше").
    raw_lemmas = {
        str(parse["lemma"]).casefold()
        for parse in token.vesum_parses
        if parse.get("pos") == "adj" and isinstance(parse.get("lemma"), str)
    }
    ladder = _degree_ladder_for_token(token)
    semantic_lemma = ladder.positive if ladder is not None else None
    source_only = bool(raw_lemmas & SOURCE_ONLY_DEGREE_LEMMAS)
    return (
        has_adjective_degree_parse
        and (ladder is not None or source_only)
        and not _is_predicative_adverb_token(token)
        and token.surface.casefold() not in DENIED_DEGREE_SURFACES
        and not bool(raw_lemmas & DENIED_DEGREE_LEMMAS)
        and (semantic_lemma is None or semantic_lemma not in DENIED_DEGREE_LEMMAS)
    )


def _is_comparison_form(token: AnchorToken) -> bool:
    """Return whether the token is an actual comparative or superlative adjective."""
    return _is_degree_token(token) and any(
        parse.get("pos") == "adj"
        and any(marker in str(parse.get("raw", "")) for marker in ("compc", "comps"))
        for parse in token.vesum_parses
    )


def _safe_inflection_rows(
    token: AnchorToken,
) -> tuple[tuple[str, str, set[str], set[str]], ...]:
    """Return safe same-lemma/POS alternatives with source and replacement tags."""
    identity = _unambiguous_content_lemma_pos(token)
    if identity is None:
        return ()
    lemma, source_pos = identity
    source_parses = tuple(
        parse
        for parse in token.vesum_parses
        if str(parse.get("lemma", "")).casefold() == lemma
        and parse.get("pos") == source_pos
        and isinstance(parse.get("raw"), str)
    )
    candidates: list[tuple[int, int, str, set[str], set[str]]] = []
    for row in verify_lemma(lemma, db_path=data.active_bundle().vesum_db):
        form = row.get("word_form")
        tags = row.get("tags")
        if (
            not isinstance(form, str)
            or not form.strip()
            or form.casefold() == token.surface.casefold()
            or not isinstance(tags, str)
            or "v_kly" in tags.split(":")
            or any(marker in f":{tags}" for marker in _UNSAFE_TAG_MARKERS)
            or row.get("pos") != source_pos
        ):
            continue
        row_features = set(tags.split(":"))
        source_features = min(
            (set(str(parse["raw"]).split(":")) for parse in source_parses),
            key=lambda features: len(row_features.symmetric_difference(features)),
        )
        distance = len(row_features.symmetric_difference(source_features))
        replacement = form[:1].upper() + form[1:] if token.surface[:1].isupper() else form
        candidates.append(
            (
                distance,
                abs(len(form) - len(token.surface)),
                replacement,
                source_features,
                row_features,
            )
        )
    return tuple(
        (form, source_pos, source_features, replacement_features)
        for _distance, _length_delta, form, source_features, replacement_features in sorted(
            candidates,
            key=lambda row: (row[0], row[1], row[2].casefold()),
        )
    )


_CASE_TAGS: Final[frozenset[str]] = frozenset(
    {"v_naz", "v_rod", "v_dav", "v_zna", "v_oru", "v_mis", "v_kly"}
)
_NUMBER_TAGS: Final[frozenset[str]] = frozenset({"s", "p"})
_GENDER_TAGS: Final[frozenset[str]] = frozenset({"m", "f", "n"})
_PERSON_TAGS: Final[frozenset[str]] = frozenset({"1", "2", "3"})
_UNAMBIGUOUS_PREPOSITION_CASES: Final[dict[str, frozenset[str]]] = {
    "без": frozenset({"v_rod"}),
    "біля": frozenset({"v_rod"}),
    "від": frozenset({"v_rod"}),
    "для": frozenset({"v_rod"}),
    "до": frozenset({"v_rod"}),
    "завдяки": frozenset({"v_dav"}),
    "коло": frozenset({"v_rod"}),
    "перед": frozenset({"v_oru"}),
    "після": frozenset({"v_rod"}),
    "попри": frozenset({"v_zna"}),
    "проти": frozenset({"v_rod"}),
    "серед": frozenset({"v_rod"}),
    "усупереч": frozenset({"v_dav"}),
    "через": frozenset({"v_zna"}),
}


def _feature(tags: set[str], family: frozenset[str]) -> str | None:
    values = tags & family
    return next(iter(values)) if len(values) == 1 else None


def _agreement_frame(tags: set[str]) -> tuple[str, str, str] | None:
    case = _feature(tags, _CASE_TAGS)
    number = _feature(tags, _NUMBER_TAGS) or ("s" if tags & _GENDER_TAGS else None)
    gender = _feature(tags, _GENDER_TAGS) if number == "s" else ""
    return (case, number, gender) if case and number and gender is not None else None


def _token_tag_sets(token: AnchorToken, *, pos: str) -> tuple[set[str], ...]:
    return tuple(
        set(str(parse["raw"]).split(":"))
        for parse in token.vesum_parses
        if parse.get("pos") == pos
        and isinstance(parse.get("raw"), str)
        and not any(marker in f":{parse['raw']}" for marker in _UNSAFE_TAG_MARKERS)
    )


def _replacement_tag_sets(token: AnchorToken, form: str, *, pos: str) -> tuple[set[str], ...]:
    """Return every safe same-lemma/POS analysis licensed by one surface."""
    identity = _unambiguous_content_lemma_pos(token)
    if identity is None or identity[1] != pos:
        return ()
    lemma = identity[0]
    return tuple(
        set(str(row["tags"]).split(":"))
        for row in verify_lemma(lemma, db_path=data.active_bundle().vesum_db)
        if isinstance(row.get("word_form"), str)
        and str(row["word_form"]).casefold() == form.casefold()
        and row.get("pos") == pos
        and isinstance(row.get("tags"), str)
        and not any(marker in f":{row['tags']}" for marker in _UNSAFE_TAG_MARKERS)
    )


_SUBORDINATORS: Final[frozenset[str]] = frozenset(
    {
        "аби",
        "адже",
        "бо",
        "де",
        "коли",
        "оскільки",
        "поки",
        "тому",
        "хоч",
        "хоча",
        "що",
        "щоб",
        "яка",
        "який",
        "які",
        "яке",
    }
)
_CLAUSE_BOUNDARY_RE: Final = re.compile(r"[,;:—–]")


def _is_finite_verb(token: AnchorToken) -> bool:
    return any(
        parse.get("pos") == "verb" and ":inf" not in f":{parse.get('raw', '')}"
        for parse in token.vesum_parses
    )


def _case_ambiguous_nominative(tags_rows: Sequence[set[str]]) -> bool:
    cases = {case for tags in tags_rows for case in tags & (_CASE_TAGS - {"v_kly"})}
    return "v_naz" in cases and bool(cases - {"v_naz"})


def _same_local_clause(sentence: AnchorSentence, first_index: int, second_index: int) -> bool:
    """Return whether two nearby tokens have no visible clause boundary between them."""
    left_index, right_index = sorted((first_index, second_index))
    left = sentence.tokens[left_index]
    right = sentence.tokens[right_index]
    between = sentence.text[left.end_offset : right.start_offset]
    if _CLAUSE_BOUNDARY_RE.search(between):
        return False
    return not any(
        token.surface.casefold() in _SUBORDINATORS
        for token in sentence.tokens[left_index + 1 : right_index]
    )


def _subject_frames_before(sentence: AnchorSentence, token_index: int) -> set[tuple[str, str]]:
    """Return every unambiguous subject frame in the same local clause."""
    frames: set[tuple[str, str]] = set()
    verb = sentence.tokens[token_index]
    right_boundary = verb.start_offset
    for subject in reversed(sentence.tokens[:token_index]):
        between = sentence.text[subject.end_offset : right_boundary]
        if _CLAUSE_BOUNDARY_RE.search(between):
            break
        if subject.surface.casefold() in _SUBORDINATORS or _is_finite_verb(subject):
            break
        nominal_rows = (
            *_token_tag_sets(subject, pos="noun"),
            *_token_tag_sets(subject, pos="pron"),
        )
        if nominal_rows and not _case_ambiguous_nominative(nominal_rows):
            for parse in subject.vesum_parses:
                raw = str(parse.get("raw", ""))
                tags = set(raw.split(":"))
                if "v_naz" not in tags or any(
                    marker in f":{raw}" for marker in _UNSAFE_TAG_MARKERS
                ):
                    continue
                number = _feature(tags, _NUMBER_TAGS) or ("s" if tags & _GENDER_TAGS else None)
                if number is None:
                    continue
                person = _feature(tags, _PERSON_TAGS) if ":pers:" in f":{raw}:" else "3"
                if person is not None:
                    frames.add((person, number))
        right_boundary = subject.start_offset
    return frames


def _subject_frames_after(sentence: AnchorSentence, token_index: int) -> set[tuple[str, str]]:
    """Return an overt post-verbal subject before another finite predicate."""
    frames: set[tuple[str, str]] = set()
    verb = sentence.tokens[token_index]
    left_boundary = verb.end_offset
    for subject in sentence.tokens[token_index + 1 :]:
        between = sentence.text[left_boundary : subject.start_offset]
        if _is_finite_verb(subject) or _CLAUSE_BOUNDARY_RE.search(between):
            break
        for parse in subject.vesum_parses:
            raw = str(parse.get("raw", ""))
            tags = set(raw.split(":"))
            if (
                parse.get("pos") not in {"noun", "pron"}
                or "v_naz" not in tags
                or any(marker in f":{raw}" for marker in _UNSAFE_TAG_MARKERS)
            ):
                continue
            number = _feature(tags, _NUMBER_TAGS) or ("s" if tags & _GENDER_TAGS else None)
            person = _feature(tags, _PERSON_TAGS) if ":pers:" in f":{raw}:" else "3"
            if number is not None and person is not None:
                frames.add((person, number))
        left_boundary = subject.end_offset
    return frames


def _contextual_error_replacements(
    sentence: AnchorSentence,
    token: AnchorToken,
    *,
    one_per_mismatch_class: bool = True,
) -> tuple[tuple[str, str, str], ...]:
    """Certify only a one-token error whose local dependency proves it wrong.

    The previous generic policy chose the nearest real form in a paradigm.  A
    real form can remain grammatical in the sentence (for example ``буде`` in
    place of ``є``), so formhood alone is not an error warrant.  These closed
    rules mutate a dependent while keeping its visible head in place.
    """
    try:
        token_index = next(
            index
            for index, candidate in enumerate(sentence.tokens)
            if candidate.token_id == token.token_id
        )
    except StopIteration:
        return ()
    rows: list[tuple[str, str, str]] = []
    safe_rows = _safe_inflection_rows(token)
    all_noun_frames = {
        frame
        for candidate in sentence.tokens
        for tags in _token_tag_sets(candidate, pos="noun")
        if (frame := _agreement_frame(tags)) is not None
    }

    # Adjective--noun agreement: change exactly one agreement feature on the
    # adjective while the agreeing source noun remains visible.
    if any(parse.get("pos") == "adj" for parse in token.vesum_parses):
        neighbour_indexes = (
            *range(max(0, token_index - 2), token_index),
            *range(token_index + 1, min(len(sentence.tokens), token_index + 3)),
        )
        neighbours = tuple(
            sentence.tokens[index]
            for index in neighbour_indexes
            if _same_local_clause(sentence, token_index, index)
        )
        head_frames = {
            frame
            for neighbour in neighbours
            for tags in _token_tag_sets(neighbour, pos="noun")
            if (frame := _agreement_frame(tags)) is not None
        }
        for form, pos, source_tags, replacement_tags in safe_rows:
            if pos != "adj":
                continue
            source_frame = _agreement_frame(source_tags)
            replacement_frame = _agreement_frame(replacement_tags)
            if source_frame is None or source_frame not in head_frames or replacement_frame is None:
                continue
            replacement_frames = {
                frame
                for tags in _replacement_tag_sets(token, form, pos="adj")
                if (frame := _agreement_frame(tags)) is not None
            }
            changes = tuple(
                name
                for name, source_value, replacement_value in zip(
                    ("case", "number", "gender"), source_frame, replacement_frame, strict=True
                )
                if source_value != replacement_value
            )
            exclusion_frames = all_noun_frames if one_per_mismatch_class else head_frames
            if changes and replacement_frames and replacement_frames.isdisjoint(exclusion_frames):
                mismatch_class = next(
                    name for name in ("number", "case", "gender") if name in changes
                )
                rows.append(
                    (
                        form,
                        f"agreement-{mismatch_class}",
                        "the mutated adjective no longer agrees with its visible source noun",
                    )
                )

    # Explicit subject--finite-verb agreement.  Noun subjects license third
    # person; personal pronouns also contribute their attested person.
    if any(parse.get("pos") == "verb" for parse in token.vesum_parses):
        subject_frames = _subject_frames_before(sentence, token_index) or _subject_frames_after(
            sentence, token_index
        )
        if len(subject_frames) != 1:
            subject_frames = set()
        for form, pos, source_tags, replacement_tags in safe_rows:
            if pos != "verb":
                continue
            source_frame = (
                _feature(source_tags, _PERSON_TAGS),
                _feature(source_tags, _NUMBER_TAGS),
            )
            replacement_frame = (
                _feature(replacement_tags, _PERSON_TAGS),
                _feature(replacement_tags, _NUMBER_TAGS),
            )
            replacement_frames = {
                (
                    _feature(tags, _PERSON_TAGS),
                    _feature(tags, _NUMBER_TAGS),
                )
                for tags in _replacement_tag_sets(token, form, pos="verb")
            }
            replacement_frames = {frame for frame in replacement_frames if None not in frame}
            if (
                None in source_frame
                or source_frame not in subject_frames
                or None in replacement_frame
            ):
                continue
            changes = tuple(
                name
                for name, source_value, replacement_value in zip(
                    ("person", "number"), source_frame, replacement_frame, strict=True
                )
                if source_value != replacement_value
            )
            if (
                len(changes) == 1
                and replacement_frames
                and replacement_frames.isdisjoint(subject_frames)
            ):
                rows.append(
                    (
                        form,
                        f"subject-verb-{changes[0]}",
                        "the mutated finite verb no longer agrees with its visible source subject",
                    )
                )

    # Modal predicates visibly require an infinitive complement.
    if token_index > 0 and any(
        item.surface.casefold() in {"можна", "треба", "варто", "слід"}
        for item in sentence.tokens[max(0, token_index - 2) : token_index]
    ):
        source_is_infinitive = any(
            parse.get("pos") == "verb" and ":inf" in f":{parse.get('raw', '')}"
            for parse in token.vesum_parses
        )
        if source_is_infinitive:
            for form, pos, _source_tags, _replacement_tags in safe_rows:
                if pos != "verb":
                    continue
                replacement_is_infinitive = any(
                    ":inf" in f":{row.get('tags', '')}"
                    for row in verify_lemma(
                        _unambiguous_content_lemma_pos(token)[0],
                        db_path=data.active_bundle().vesum_db,
                    )
                    if isinstance(row.get("word_form"), str)
                    and str(row["word_form"]).casefold() == form.casefold()
                )
                if not replacement_is_infinitive:
                    rows.append(
                        (
                            form,
                            "modal-infinitive",
                            "the visible modal predicate requires an infinitive complement",
                        )
                    )

    # ``кілька`` visibly requires a genitive-plural nominal complement.
    if token_index > 0 and sentence.tokens[token_index - 1].surface.casefold() == "кілька":
        source_rows = _token_tag_sets(token, pos="noun")
        source_is_genitive_plural = any({"v_rod", "p"} <= tags for tags in source_rows)
        if source_is_genitive_plural:
            for form, pos, _source_tags, _replacement_tags in safe_rows:
                if pos != "noun":
                    continue
                replacement_rows = _replacement_tag_sets(token, form, pos="noun")
                if replacement_rows and not any(
                    {"v_rod", "p"} <= tags for tags in replacement_rows
                ):
                    rows.append(
                        (
                            form,
                            "quantifier-genitive-plural",
                            "the visible quantifier кілька requires genitive plural",
                        )
                    )

    # A title immediately before a nominative personal name must agree with it.
    if token_index + 1 < len(sentence.tokens):
        following = sentence.tokens[token_index + 1]
        following_is_name = any(
            parse.get("pos") == "noun"
            and ":prop:" in f":{parse.get('raw', '')}:"
            and ":v_naz" in f":{parse.get('raw', '')}"
            for parse in following.vesum_parses
        )
        source_rows = _token_tag_sets(token, pos="noun")
        source_is_nominative_singular = any(
            "v_naz" in tags and ("s" in tags or bool(tags & _GENDER_TAGS)) for tags in source_rows
        )
        if following_is_name and source_is_nominative_singular:
            for form, pos, _source_tags, _replacement_tags in safe_rows:
                if pos != "noun":
                    continue
                replacement_rows = _replacement_tag_sets(token, form, pos="noun")
                if replacement_rows and not any(
                    "v_naz" in tags and ("s" in tags or bool(tags & _GENDER_TAGS))
                    for tags in replacement_rows
                ):
                    rows.append(
                        (
                            form,
                            "title-name-apposition",
                            "the title must remain nominative singular before the visible name",
                        )
                    )

    # Closed, single-case prepositions only.  Ambiguous government such as
    # ``в``, ``на``, ``за`` and ``під`` is deliberately excluded.
    if token_index > 0:
        preposition = sentence.tokens[token_index - 1].surface.casefold()
        governed_cases = _UNAMBIGUOUS_PREPOSITION_CASES.get(preposition)
        if governed_cases:
            for form, pos, source_tags, replacement_tags in safe_rows:
                if pos not in {"noun", "adj"}:
                    continue
                source_case = _feature(source_tags, _CASE_TAGS)
                replacement_case = _feature(replacement_tags, _CASE_TAGS)
                replacement_cases = {
                    case
                    for tags in _replacement_tag_sets(token, form, pos=pos)
                    if (case := _feature(tags, _CASE_TAGS)) is not None
                }
                if (
                    source_case in governed_cases
                    and replacement_case not in governed_cases
                    and replacement_cases
                    and replacement_cases.isdisjoint(governed_cases)
                ):
                    rows.append(
                        (
                            form,
                            "government-case",
                            "the mutated nominal violates the visible preposition's "
                            "closed case government",
                        )
                    )

    # Keep one best surface per independently named mismatch class, then rotate
    # deterministically across tokens so an eight-item board can exercise
    # several constructions instead of eight case errors.
    if one_per_mismatch_class:
        best_by_class: dict[str, tuple[str, str, str]] = {}
        for row in rows:
            best_by_class.setdefault(row[1], row)
        candidates = best_by_class.values()
    else:
        best_by_surface: dict[str, tuple[str, str, str]] = {}
        for row in rows:
            best_by_surface.setdefault(row[0].casefold(), row)
        candidates = best_by_surface.values()
    ordered = sorted(candidates, key=lambda row: (row[1], row[0].casefold()))
    if not ordered:
        return ()
    numeric_id = sum(int(value) for value in re.findall(r"\d+", token.token_id))
    offset = numeric_id % len(ordered)
    return tuple((*ordered[offset:], *ordered[:offset]))


def _error_replacement(sentence: AnchorSentence, token: AnchorToken) -> tuple[str, str, str] | None:
    """Return one locally provable dependency mismatch, never an arbitrary form."""
    rows = _contextual_error_replacements(sentence, token)
    return rows[0] if rows else None


def _contextual_choice_bank(
    sentence: AnchorSentence, token: AnchorToken
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]] | None:
    """Return two forms that the complete visible carrier deterministically excludes."""
    identity = _unambiguous_content_lemma_pos(token)
    if identity is None:
        return None

    def surface_rank(form: str) -> tuple[bool, bool, str]:
        normalized = form.casefold()
        # Prefer the full standard reflexive future (дивитимемося) over
        # syncopated or short variants (дивитимемся / дивитимемось) when VESUM
        # licenses several spellings for the same grammatical feature set.
        return (
            re.search(r"тимем(?:ся|сь)$", normalized) is not None,
            normalized.endswith("сь"),
            normalized,
        )

    rows = _contextual_error_replacements(
        sentence,
        token,
        one_per_mismatch_class=False,
    )
    best_by_signature: dict[tuple[tuple[str, ...], ...], tuple[str, str, str]] = {}
    for row in rows:
        form = row[0]
        signature = tuple(
            sorted(
                tuple(sorted(tags)) for tags in _replacement_tag_sets(token, form, pos=identity[1])
            )
        )
        if not signature:
            continue
        incumbent = best_by_signature.get(signature)
        if incumbent is None or surface_rank(form) < surface_rank(incumbent[0]):
            best_by_signature[signature] = row

    selected: list[tuple[str, str]] = []
    seen = {token.surface.casefold()}
    for form, _mismatch_class, warrant in sorted(
        best_by_signature.values(),
        key=lambda row: (row[1], surface_rank(row[0])),
    ):
        if form.casefold() in seen:
            continue
        seen.add(form.casefold())
        selected.append((form, warrant))
        if len(selected) == 2:
            break
    if len(selected) != 2:
        return None
    return (
        (token.surface, *(form for form, _warrant in selected)),
        tuple(selected),
    )


def _visible_carrier_choice_bank(
    sentence: AnchorSentence, token: AnchorToken
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]] | None:
    """Use contextual warrants in production and retain old synthetic fixtures.

    Test bundles intentionally contain compact morphology tables and no full
    sentence semantics.  They remain useful for transport/shape tests, but a
    production bundle must prove every rejected option from the visible
    carrier rather than merely choosing a different form of the same lemma.
    """
    if data.active_bundle().manifest.get("version") == "test":
        return _certified_choice_bank(token)
    return _contextual_choice_bank(sentence, token)


def _unambiguous_content_lemma_pos(token: AnchorToken) -> tuple[str, str] | None:
    if any(parse.get("pos") in _CLOSED_CLASS_POS for parse in token.vesum_parses):
        return None
    all_content_candidates = {
        (str(parse["lemma"]).casefold(), str(parse["pos"]))
        for parse in token.vesum_parses
        if isinstance(parse.get("lemma"), str)
        and str(parse["lemma"]).strip()
        and parse.get("pos") in _CONTENT_POS
    }
    candidates = {
        (str(parse["lemma"]).casefold(), str(parse["pos"]))
        for parse in token.vesum_parses
        if isinstance(parse.get("lemma"), str)
        and str(parse["lemma"]).strip()
        and parse.get("pos") in _CONTENT_POS
        and ":pron" not in str(parse.get("raw", ""))
        and not any(marker in str(parse.get("raw", "")) for marker in _UNSAFE_TAG_MARKERS)
    }
    return (
        next(iter(candidates))
        if len(all_content_candidates) == 1 and len(candidates) == 1
        else None
    )


_APPLICATION_TRANSFER_LEMMAS: Final[frozenset[str]] = frozenset(
    {
        "бажати",
        "берегтися",
        "вибирати",
        "виправлятися",
        "вирішити",
        "вчинити",
        "дізнатися",
        "думати",
        "знати",
        "могти",
        "обирати",
        "планувати",
        "порадити",
        "пояснити",
        "розповідати",
        "слухати",
        "уявляти",
        "хотіти",
    }
)
_APPLICATION_NATURAL_LEMMAS: Final[frozenset[str]] = frozenset(
    {
        "вітер",
        "вода",
        "дощ",
        "зоря",
        "місяць",
        "небо",
        "річка",
        "сніг",
        "сонце",
        "туман",
        "хмара",
        "хвилина",
    }
)
_APPLICATION_STATIVE_LEMMAS: Final[frozenset[str]] = frozenset({"знати"})
_APPLICATION_PERSONAL_FORMS: Final[frozenset[str]] = frozenset(
    {
        "я",
        "мене",
        "мені",
        "мною",
        "ти",
        "тебе",
        "тобі",
        "тобою",
        "ми",
        "нас",
        "нам",
        "нами",
        "ви",
        "вас",
        "вам",
        "вами",
    }
)
_APPLICATION_GROUNDING_EXCLUDED_LEMMAS: Final[frozenset[str]] = frozenset({"людина", "світ"})


def _application_question_grounding_terms(sentence: AnchorSentence) -> tuple[str, ...]:
    """Expose a compact, non-personal source handle for an application question."""
    terms: list[str] = []
    seen_lemmas: set[str] = set()
    for token in sentence.tokens:
        lemma = _topic_lemma(token)
        if (
            lemma is None
            or lemma in seen_lemmas
            or lemma in _APPLICATION_GROUNDING_EXCLUDED_LEMMAS
            or len(token.surface) < 4
            or not any(
                parse.get("pos") == "noun"
                and ":inanim" in str(parse.get("raw", ""))
                and ":pron" not in str(parse.get("raw", ""))
                for parse in token.vesum_parses
            )
        ):
            continue
        terms.append(token.surface)
        seen_lemmas.add(lemma)
        if len(terms) == 4:
            break
    return tuple(terms)


def _application_carrier_rank(sentence: AnchorSentence) -> tuple[int, int, int, int]:
    """Rank transferable human propositions ahead of incidental scenery."""
    lemmas = {lemma for token in sentence.tokens if (lemma := _content_lemma(token)) is not None}
    words = {token.surface.casefold() for token in sentence.tokens}
    has_transfer = bool(lemmas & _APPLICATION_TRANSFER_LEMMAS)
    has_person = bool(words & _APPLICATION_PERSONAL_FORMS) or any(
        ":anim" in str(parse.get("raw", ""))
        for token in sentence.tokens
        for parse in token.vesum_parses
    )
    natural_only = bool(lemmas & _APPLICATION_NATURAL_LEMMAS) and not (has_transfer or has_person)
    starts_with_connector = bool(
        sentence.tokens
        and sentence.tokens[0].surface.casefold() in {"а", "але", "адже", "проте", "тому", "тож"}
    )
    return (
        0 if has_transfer else 1 if has_person else 2,
        int(natural_only),
        int(starts_with_connector and not has_transfer),
        abs(len(sentence.tokens) - 12),
    )


def _application_topic(sentence: AnchorSentence) -> tuple[AnchorToken, str] | None:
    """Prefer an event, or a concrete source object when the predicate is stative."""
    preferred = _topic_token(sentence, prefer_nearest_verb=True)
    if preferred is None:
        return None
    if preferred[1] == "вчинити":
        for token in sentence.tokens[sentence.tokens.index(preferred[0]) + 1 :]:
            lemma = _topic_lemma(token)
            if lemma is not None and any(
                parse.get("pos") in {"noun", "adj"} and ":pron" not in str(parse.get("raw", ""))
                for parse in token.vesum_parses
            ):
                return token, lemma
    if preferred[1] not in _APPLICATION_STATIVE_LEMMAS:
        return preferred
    for token in sentence.tokens[sentence.tokens.index(preferred[0]) + 1 :]:
        lemma = _topic_lemma(token)
        if lemma is not None and any(
            parse.get("pos") == "noun"
            and ":inanim" in str(parse.get("raw", ""))
            and ":pron" not in str(parse.get("raw", ""))
            for parse in token.vesum_parses
        ):
            return token, lemma
    return preferred


def _atlas_semantic_pairs(
    sentences: Sequence[AnchorSentence],
) -> dict[str, tuple[str, str, str]]:
    """Bind source tokens to unambiguous Atlas antonym/synonym lemma pairs."""
    token_records = {
        token.token_id: record
        for sentence in sentences
        for token in sentence.tokens
        if (record := _unambiguous_content_lemma_pos(token)) is not None
    }
    needed = sorted({lemma for lemma, _pos in token_records.values()})
    if not needed:
        return {}
    connection = sqlite3.connect(
        f"file:{quote(str(data.active_bundle().atlas_db))}?mode=ro&immutable=1", uri=True
    )
    try:
        placeholders = ",".join("?" for _ in needed)
        rows = connection.execute(
            f"SELECT slug, payload_json FROM article_payloads "  # noqa: S608 - placeholders only
            f"WHERE is_public_route=1 AND slug IN ({placeholders})",
            needed,
        )
        by_lemma: dict[str, dict[str, tuple[str, ...]]] = {}
        for slug, payload_json in rows:
            try:
                payload = json.loads(payload_json)
            except (TypeError, ValueError):
                continue
            sections = payload.get("sections")
            if not isinstance(sections, dict):
                continue
            record: dict[str, tuple[str, ...]] = {}
            for section_name in ("antonyms", "synonyms"):
                section = sections.get(section_name)
                items = section.get("items") if isinstance(section, dict) else None
                if isinstance(items, list):
                    record[section_name] = tuple(
                        item.strip()
                        for item in items
                        if isinstance(item, str) and re.fullmatch(_TOKEN_RE, item.strip())
                    )
            if record:
                by_lemma[str(slug).casefold()] = record
    finally:
        connection.close()

    def collect(section_name: str, relation: str) -> dict[str, tuple[str, str, str]]:
        pairs: dict[str, tuple[str, str, str]] = {}
        for token_id, (lemma, source_pos) in token_records.items():
            for related in by_lemma.get(lemma, {}).get(section_name, ()):
                pair = (lemma, related.casefold())
                if section_name == "antonyms" and pair in _UNSAFE_ATLAS_ANTONYM_PAIRS:
                    continue
                if section_name == "synonyms" and pair not in _SAFE_ATLAS_SYNONYM_PAIRS:
                    continue
                parses = _token_parses(related, sentence_initial=False)
                if not any(
                    parse.get("pos") == source_pos
                    and not any(
                        marker in str(parse.get("raw", "")) for marker in _UNSAFE_TAG_MARKERS
                    )
                    for parse in parses
                ):
                    continue
                if related.casefold() != lemma:
                    pairs[token_id] = (lemma, related, relation)
                    break
        return pairs

    antonyms = collect("antonyms", "atlas_antonym.v1")
    synonyms = collect("synonyms", "atlas_synonym.v1")
    # Prefer an antonym for any source token that has both relations, while a
    # narrowly approved synonym can supply otherwise missing semantic capacity.
    return {**synonyms, **antonyms}


def _learner_gloss(value: str) -> str | None:
    """Reduce an approved Atlas definition to one short matching-board gloss."""
    text = unicodedata.normalize("NFC", value).strip()
    text = re.sub(r"^\([^)]*\)\.\s*", "", text)
    text = re.sub(r"^\d+\.\s*", "", text)
    text = text.split(".", 1)[0].strip()
    text = text.split(";", 1)[0].strip()
    text = re.sub(r"\s*\([^)]{20,}\)\s*$", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    if (
        not 2 <= len(_TOKEN_RE.findall(text)) <= 14
        or re.search(r"\b(?:зменш|те саме|див\.)\b", text, re.IGNORECASE)
        or re.search(r"(?:\bі\s+т|\bнапр)$", text, re.IGNORECASE)
        or "\N{COMBINING ACUTE ACCENT}" in text
        or not text[:1].isalpha()
    ):
        return None
    return text[:1].lower() + text[1:]


def _atlas_gloss_pairs(
    sentences: Sequence[AnchorSentence],
) -> dict[str, tuple[str, str, str]]:
    """Bind source lexis above A2 to a short approved Atlas definition."""
    token_records = {
        token.token_id: record
        for sentence in sentences
        for token in sentence.tokens
        if (record := _unambiguous_content_lemma_pos(token)) is not None
    }
    needed = sorted({lemma for lemma, _pos in token_records.values()})
    if not needed:
        return {}
    connection = sqlite3.connect(
        f"file:{quote(str(data.active_bundle().atlas_db))}?mode=ro&immutable=1", uri=True
    )
    try:
        placeholders = ",".join("?" for _ in needed)
        rows = connection.execute(
            f"SELECT slug, payload_json FROM article_payloads "  # noqa: S608 - placeholders only
            f"WHERE is_public_route=1 "
            f"AND slug IN ({placeholders})",
            needed,
        )
        definitions: dict[str, str] = {}
        for slug, payload_json in rows:
            try:
                payload = json.loads(payload_json)
            except (TypeError, ValueError):
                continue
            enrichment = payload.get("enrichment")
            cefr_record = enrichment.get("cefr") if isinstance(enrichment, dict) else None
            cefr = cefr_record.get("level") if isinstance(cefr_record, dict) else None
            raw_gloss = payload.get("gloss")
            gloss = _learner_gloss(raw_gloss) if isinstance(raw_gloss, str) else None
            if str(cefr).upper() not in {"A1", "A2"} and gloss is not None:
                definitions[str(slug).casefold()] = gloss
    finally:
        connection.close()
    return {
        token_id: (lemma, definitions[lemma], "atlas_gloss.v1")
        for token_id, (lemma, _pos) in token_records.items()
        if lemma in definitions and lemma not in _UNSAFE_GLOSS_LEMMAS
    }


def _attested_degree_ladders(
    sentences: Sequence[AnchorSentence],
    *,
    preferred: Sequence[str] = (),
    excluded: frozenset[str] = frozenset(),
) -> tuple[tuple[AnchorSentence, AnchorToken, DegreeLadder], ...]:
    """Return unique, safe, VESUM-certified source lemma ladders."""
    rows: list[tuple[AnchorSentence, AnchorToken, DegreeLadder]] = []
    seen: set[str] = set()
    for sentence in sentences:
        if not _safe_item_carrier(sentence):
            continue
        for token in sentence.tokens:
            ladder = _degree_ladder_for_token(token)
            if (
                ladder is None
                or ladder.positive in seen
                or ladder.positive in excluded
                or ladder.positive in DENIED_DEGREE_LEMMAS
                or not _is_degree_token(token)
                or not _ladder_is_vesum_certified(ladder)
            ):
                continue
            seen.add(ladder.positive)
            rows.append((sentence, token, ladder))
    preferred_index = {lemma: index for index, lemma in enumerate(preferred)}
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                preferred_index.get(row[2].positive, len(preferred_index)),
                int(row[0].sentence_id.removeprefix("s-")),
                row[1].start_offset,
            ),
        )
    )


def _degree_match_pairs(
    sentences: Sequence[AnchorSentence], role: str
) -> dict[str, tuple[str, str, str]]:
    """Build the two closed cross-root semantic degree boards."""
    if role not in {"degree-positive-comparative", "degree-comparative-superlative"}:
        return {}
    rows = _attested_degree_ladders(sentences, preferred=DEGREE_BOARD_LEMMAS)
    if len(rows) < 8:
        return {}
    pairs = (
        DEGREE_PARAPHRASE_PAIRS if role == "degree-positive-comparative" else DEGREE_PRIORITY_PAIRS
    )
    relation = (
        "degree-comparison-paraphrase.v1"
        if role == "degree-positive-comparative"
        else "degree-priority-recommendation.v2"
    )
    return {
        token.token_id: (left, right, relation)
        for (_sentence, token, _ladder), (left, right) in zip(rows[:8], pairs, strict=True)
    }


def _degree_kit_candidates(
    sentences: Sequence[AnchorSentence], *, activity_type: str, role: str, group_number: int
) -> tuple[EvidenceCandidate, ...]:
    """Build exact lemma-bound units for degree roles that are not carriers."""
    rows = _attested_degree_ladders(sentences, preferred=DEGREE_BOARD_LEMMAS)
    by_lemma = {ladder.positive: (sentence, token, ladder) for sentence, token, ladder in rows}

    if role == "degree-error-correction":
        candidates: list[EvidenceCandidate] = []
        for index, frame in enumerate(DEGREE_ERROR_FRAMES, start=1):
            bound = by_lemma.get(frame.source_lemma)
            if bound is None:
                continue
            sentence, token, _ladder = bound
            candidates.append(
                EvidenceCandidate(
                    activity_type=activity_type,
                    candidate_id=f"{activity_type}:{group_number}:{index}",
                    sentence_id=sentence.sentence_id,
                    token_id=token.token_id,
                    literal_evidence=sentence.text,
                    expected_key=frame.correction,
                    semantic_target=f"{role}:{frame.rule_id}:{frame.source_lemma}",
                    certified_error_count=1,
                    derived_surface=frame.learner_surface,
                    focus_alignment=role,
                    source_lemma=frame.source_lemma,
                    kit_rule_id=f"{DEGREE_CATALOG_VERSION}:{frame.rule_id}",
                    rendering_surface=frame.correct_surface,
                    frame_family=frame.frame_family,
                    semantic_warrant=frame.semantic_warrant,
                    question_intent=(
                        "degree-specific" if frame.degree_specific else "agreement-support"
                    ),
                )
            )
        return tuple(candidates) if len(candidates) >= 8 else ()

    frames_by_role = {
        "degree-recognition": DEGREE_RECOGNITION_FRAMES,
        "degree-cloze": DEGREE_CLOZE_FRAMES,
        "degree-formation": DEGREE_FORMATION_FRAMES,
        "degree-comparison-syntax": DEGREE_SYNTAX_FRAMES,
        "degree-context": DEGREE_CONTEXT_FRAMES,
    }
    frames = frames_by_role.get(role)
    if frames is None:
        return ()
    if len(frames) != 8 or len(rows) < 8:
        return ()
    source_bound = role in {"degree-recognition", "degree-cloze"}
    if source_bound and any(frame.answer_lemma not in by_lemma for frame in frames):
        return ()
    result: list[EvidenceCandidate] = []
    for index, frame in enumerate(frames, start=1):
        sentence, token, _ladder = by_lemma[frame.answer_lemma] if source_bound else rows[index - 1]
        start_offset = frame.rendering_surface.index(frame.answer)
        end_offset = start_offset + len(frame.answer)
        result.append(
            EvidenceCandidate(
                activity_type=activity_type,
                candidate_id=f"{activity_type}:{group_number}:{index}",
                sentence_id=sentence.sentence_id,
                token_id=token.token_id,
                literal_evidence=sentence.text,
                expected_key=frame.answer,
                semantic_target=f"{role}:{frame.answer_lemma}",
                focus_alignment=role,
                source_lemma=frame.answer_lemma,
                kit_rule_id=f"{DEGREE_CATALOG_VERSION}:{role}",
                rendering_surface=frame.rendering_surface,
                target_start_offset=start_offset,
                target_end_offset=end_offset,
                degree_class=frame.degree_class,
                morphology_class=frame.morphology_class,
                choice_bank=frame.choice_bank,
                frame_family=frame.frame_family,
                semantic_warrant=frame.semantic_warrant,
                exclusion_warrants=frame.exclusion_warrants,
            )
        )
    return tuple(result)


def _certified_choice_bank(
    token: AnchorToken, *, minimum_distractors: int = 2
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]] | None:
    """Return one closed VESUM bank with an exclusion proof per distractor.

    Alternative forms come from one attested content lemma/POS analysis of the
    source token and must realize a different safe tag bundle.  Spelling
    variants with the same analysis are not graded as distractors.
    """
    if any(parse.get("pos") in _CLOSED_CLASS_POS for parse in token.vesum_parses) or any(
        ":impr" in str(parse.get("raw", "")) for parse in token.vesum_parses
    ):
        return None
    identity = _unambiguous_content_lemma_pos(token)
    if identity is None:
        return None
    observed_pos = {
        str(parse["pos"])
        for parse in token.vesum_parses
        if isinstance(parse.get("pos"), str)
        and isinstance(parse.get("lemma"), str)
        and str(parse["lemma"]).strip()
    }
    # An isolated surface can hide a different major POS that is the actual
    # reading in context (``зараз`` adverb vs the noun ``зараза``; ``кілька``
    # numeral vs the fish noun). Never build a morphology bank from the one
    # surviving dictionary identity when VESUM exposes that cross-POS risk.
    if observed_pos != {identity[1]}:
        return None
    identities = (identity,)
    for lemma, pos in identities:
        source_tags = tuple(
            frozenset(str(parse["raw"]).split(":"))
            for parse in token.vesum_parses
            if str(parse.get("lemma", "")).casefold() == lemma
            and parse.get("pos") == pos
            and isinstance(parse.get("raw"), str)
        )
        if not source_tags:
            continue
        candidates: list[tuple[int, int, str, str]] = []
        for row in verify_lemma(lemma, db_path=data.active_bundle().vesum_db):
            form = row.get("word_form")
            tags = row.get("tags")
            if (
                not isinstance(form, str)
                or not form.strip()
                or not re.fullmatch(_TOKEN_RE, form.strip())
                or form.casefold() == token.surface.casefold()
                or row.get("pos") != pos
                or not isinstance(tags, str)
                or "v_kly" in tags.split(":")
                or any(marker in f":{tags}" for marker in _UNSAFE_TAG_MARKERS)
            ):
                continue
            feature_set = frozenset(tags.split(":"))
            if feature_set in source_tags:
                continue
            rendered = form[:1].upper() + form[1:] if token.surface[:1].isupper() else form
            distance = min(len(feature_set.symmetric_difference(source)) for source in source_tags)
            candidates.append((distance, abs(len(form) - len(token.surface)), rendered, tags))
        selected: list[tuple[str, str]] = []
        seen = {token.surface.casefold()}
        seen_feature_sets = set(source_tags)
        for _distance, _length_delta, form, tags in sorted(
            candidates, key=lambda row: (row[0], row[1], row[2].casefold(), row[3])
        ):
            feature_set = frozenset(tags.split(":"))
            if form.casefold() in seen or feature_set in seen_feature_sets:
                continue
            seen.add(form.casefold())
            seen_feature_sets.add(feature_set)
            selected.append((form, tags))
            if len(selected) == minimum_distractors:
                break
        if len(selected) != minimum_distractors:
            continue
        source_label = " / ".join(":".join(sorted(tags)) for tags in source_tags)
        bank = (token.surface, *(form for form, _tags in selected))
        warrants = tuple(
            (
                form,
                f"VESUM analysis {tags} differs from certified source analysis {source_label}",
            )
            for form, tags in selected
        )
        return bank, warrants
    return None


def _has_distractor_capacity(token: AnchorToken, *, minimum: int = 2) -> bool:
    """Return whether a closed, exclusion-warranted VESUM bank is available."""
    return _certified_choice_bank(token, minimum_distractors=minimum) is not None


def _cross_gap_choice_banks(
    tokens: Sequence[AnchorToken],
    *,
    sentences: Mapping[str, AnchorSentence] | None = None,
) -> dict[str, tuple[tuple[str, ...], tuple[tuple[str, str], ...]]] | None:
    """Build a complete gap bank for one cloze plan.

    Compact synthetic bundles retain their historical cross-gap lexical bank.
    Production prefers same-lemma forms whose visible sentence dependency
    proves them wrong.  A long-form reconstruction may also use two thematic
    lexical foils attested elsewhere in the same passage.  That second lane is
    what lets a story test meaning as well as morphology without demanding an
    artificial inflectional trap in every sentence.
    """
    identities = [_unambiguous_content_lemma_pos(token) for token in tokens]
    if len(tokens) < 3 or any(identity is None for identity in identities):
        return None
    typed = tuple(
        (token, identity[0], identity[1])
        for token, identity in zip(tokens, identities, strict=True)
        if identity is not None
    )
    if data.active_bundle().manifest.get("version") == "test" and len(tokens) < 18:
        lemmas = {lemma for _token, lemma, _pos in typed}
        atlas_lookup = build_atlas_lookup(lemmas, db_path=data.active_bundle().atlas_db)
        synonym_pairs = frozenset(
            frozenset((lemma, synonym.casefold()))
            for lemma, record in atlas_lookup.items()
            for synonym in record.get("synonyms", ())
            if isinstance(synonym, str) and synonym.casefold() in lemmas
        )

        def is_synonym(left: str, right: str) -> bool:
            return frozenset((left, right)) in synonym_pairs

        fixture_result: dict[str, tuple[tuple[str, ...], tuple[tuple[str, str], ...]]] = {}
        for index, (token, lemma, pos) in enumerate(typed):
            rotated = (*typed[index + 1 :], *typed[:index])
            same_pos = tuple(
                row
                for row in rotated
                if row[2] == pos
                and row[1] != lemma
                and not is_synonym(lemma, row[1])
                and row[0].surface.casefold() != token.surface.casefold()
            )
            if len(same_pos) < 2:
                return None
            distractors = same_pos[:2]
            fixture_result[token.token_id] = (
                (token.surface, *(row[0].surface for row in distractors)),
                tuple(
                    (
                        row[0].surface,
                        f"certified answer for another fixture gap ({row[0].token_id})",
                    )
                    for row in distractors
                ),
            )
        return fixture_result

    if sentences is None:
        return None
    contextual_banks = {
        token.token_id: (
            _contextual_choice_bank(sentence, token)
            if (sentence := sentences.get(token.sentence_id)) is not None
            else None
        )
        for token, _lemma, _pos in typed
    }
    if all(bank is not None for bank in contextual_banks.values()):
        return {token_id: bank for token_id, bank in contextual_banks.items() if bank is not None}
    lemmas = {lemma for _token, lemma, _pos in typed}
    atlas_lookup = build_atlas_lookup(lemmas, db_path=data.active_bundle().atlas_db)
    synonym_pairs = frozenset(
        frozenset((lemma, synonym.casefold()))
        for lemma, record in atlas_lookup.items()
        for synonym in record.get("synonyms", ())
        if isinstance(synonym, str) and synonym.casefold() in lemmas
    )
    result: dict[str, tuple[tuple[str, ...], tuple[tuple[str, str], ...]]] = {}
    for index, (token, lemma, pos) in enumerate(typed):
        bank = contextual_banks[token.token_id]
        if bank is None:
            rotated = (*typed[index + 1 :], *typed[:index])
            foils = tuple(
                row
                for row in rotated
                if row[2] == pos
                and row[1] != lemma
                and frozenset((lemma, row[1])) not in synonym_pairs
                and row[0].surface.casefold() != token.surface.casefold()
            )[:2]
            if len(foils) != 2:
                return None
            bank = (
                (token.surface, *(row[0].surface for row in foils)),
                tuple(
                    (
                        row[0].surface,
                        "the source passage attests this foil in a different proposition",
                    )
                    for row in foils
                ),
            )
        result[token.token_id] = bank
    return result


def _raw_tags(token: AnchorToken) -> tuple[str, ...]:
    return tuple(str(parse.get("raw", "")) for parse in token.vesum_parses)


def _finite_predicate(token: AnchorToken) -> bool:
    identity = _unambiguous_content_lemma_pos(token)
    if identity is None or identity[1] != "verb":
        return False
    lemma = identity[0]
    return any(
        parse.get("pos") == "verb"
        and str(parse.get("lemma", "")).casefold() == lemma
        and any(marker in raw for marker in (":pres:", ":futr:", ":past:"))
        and ":impr:" not in raw
        for parse in token.vesum_parses
        if (raw := str(parse.get("raw", "")))
    )


def _content_lemma(token: AnchorToken) -> str | None:
    if any(parse.get("pos") in _CLOSED_CLASS_POS | {"numr"} for parse in token.vesum_parses):
        return None
    rows = [
        parse
        for parse in token.vesum_parses
        if parse.get("pos") in _CONTENT_POS
        and isinstance(parse.get("lemma"), str)
        and not any(marker in str(parse.get("raw", "")) for marker in _UNSAFE_TAG_MARKERS)
    ]
    if not rows:
        return None
    ordinary = [parse for parse in rows if ":pron" not in str(parse.get("raw", ""))]
    if not ordinary:
        return None
    lemmas = {
        str(parse["lemma"]).casefold()
        for parse in ordinary
        if isinstance(parse.get("lemma"), str) and str(parse["lemma"]).strip()
    }
    return next(iter(lemmas)) if len(lemmas) == 1 else None


def _topic_lemma(token: AnchorToken) -> str | None:
    """Return one safe question-topic lemma, including a source proper name.

    Proper names remain excluded from generated answer banks and morphology
    tasks, but a name already present in the teacher's source is often the most
    natural subject of a comprehension question.
    """
    if any(parse.get("pos") in _CLOSED_CLASS_POS | {"numr"} for parse in token.vesum_parses):
        return None
    if any(":pron" in str(parse.get("raw", "")) for parse in token.vesum_parses):
        return None
    rows = [
        parse
        for parse in token.vesum_parses
        if parse.get("pos") in _CONTENT_POS
        and isinstance(parse.get("lemma"), str)
        and not any(marker in str(parse.get("raw", "")) for marker in _TOPIC_UNSAFE_TAG_MARKERS)
    ]
    lemmas = {
        str(parse["lemma"]).casefold()
        for parse in rows
        if isinstance(parse.get("lemma"), str) and str(parse["lemma"]).strip()
    }
    return next(iter(lemmas)) if len(lemmas) == 1 else None


def _topic_noun_lemma(token: AnchorToken) -> str | None:
    """Return the unambiguous safe noun lemma carried by ``token``.

    A surface can have an incidental noun homonym while its only safe topic
    reading is verbal (for example ``куплю``: future ``купити`` versus an
    archaic noun). Such a token must not outrank the concrete nouns that follow
    it merely because one discarded parse happened to be nominal.
    """
    rows = [
        parse
        for parse in token.vesum_parses
        if parse.get("pos") == "noun"
        and isinstance(parse.get("lemma"), str)
        and ":pron" not in str(parse.get("raw", ""))
        and not any(marker in str(parse.get("raw", "")) for marker in _TOPIC_UNSAFE_TAG_MARKERS)
    ]
    lemmas = {
        str(parse["lemma"]).casefold()
        for parse in rows
        if isinstance(parse.get("lemma"), str) and str(parse["lemma"]).strip()
    }
    return next(iter(lemmas)) if len(lemmas) == 1 else None


def _clause_bounds(sentence: AnchorSentence, token: AnchorToken) -> tuple[int, int]:
    delimiters = tuple(re.finditer(r"[,;—–]", sentence.text))
    start = max(
        (match.end() for match in delimiters if match.end() <= token.start_offset),
        default=0,
    )
    end = min(
        (match.start() for match in delimiters if match.start() >= token.end_offset),
        default=len(sentence.text),
    )
    while start < end and sentence.text[start].isspace():
        start += 1
    while end > start and sentence.text[end - 1].isspace():
        end -= 1
    return start, end


def _safe_asserted_predicates(sentence: AnchorSentence) -> tuple[_PredicateSpan, ...]:
    """Return only finite predicates that admit one unambiguous scoped negation."""
    if any(marker in sentence.text for marker in ("?", ":", "«", "»", '"')):
        return ()
    words = [token.surface.casefold() for token in sentence.tokens]
    finite = [token for token in sentence.tokens if _finite_predicate(token)]
    result: list[_PredicateSpan] = []
    for token in finite:
        index = sentence.tokens.index(token)
        if index and words[index - 1] in {"не", "ні"}:
            continue
        start, end = _clause_bounds(sentence, token)
        clause_tokens = tuple(
            item
            for item in sentence.tokens
            if item.start_offset >= start and item.end_offset <= end
        )
        clause_words = [item.surface.casefold() for item in clause_tokens]
        token_position = clause_tokens.index(token)
        if _NEGATION_SCOPE_EXCLUSIONS & set(clause_words):
            continue
        if _SUBORDINATE_MARKERS & set(clause_words[:token_position]):
            continue
        clause_finite = [item for item in clause_tokens if _finite_predicate(item)]
        if len(clause_finite) > 1 and any(word in {"і", "й", "та"} for word in clause_words):
            continue
        lemma = _content_lemma(token)
        if lemma in _EXISTENTIAL_LEMMAS:
            continue
        if lemma in _MODAL_LEMMAS and any(
            ":inf" in raw
            for later in clause_tokens[token_position + 1 :]
            for raw in _raw_tags(later)
        ):
            continue
        result.append(_PredicateSpan(token=token, clause_start=start, clause_end=end))
    return tuple(result)


def _negatable_predicate(sentence: AnchorSentence) -> AnchorToken | None:
    """Compatibility helper returning the first proof-grade predicate."""
    candidate = next(iter(_safe_asserted_predicates(sentence)), None)
    return None if candidate is None else candidate.token


def _topic_token(
    sentence: AnchorSentence,
    *,
    before_offset: int | None = None,
    prefer_nearest_verb: bool = False,
    excluded_lemmas: frozenset[str] = frozenset(),
) -> tuple[AnchorToken, str] | None:
    speaker_prefix = re.match(
        r"^\s*[А-ЯІЇЄҐ][А-Яа-яІіЇїЄєҐґ'’\- ]{0,39}:\s*",
        sentence.text,
    )
    content_start = speaker_prefix.end() if speaker_prefix is not None else 0
    tokens = tuple(
        token
        for token in sentence.tokens
        if token.start_offset >= content_start
        and (before_offset is None or token.end_offset <= before_offset)
    )
    if prefer_nearest_verb:
        ordered = (
            *reversed(
                tuple(
                    token
                    for token in tokens
                    if any(parse.get("pos") == "verb" for parse in token.vesum_parses)
                )
            ),
            *tokens,
        )
    else:
        has_predicative = any(
            ":predic" in str(parse.get("raw", ""))
            for token in tokens
            for parse in token.vesum_parses
        )
        direct_case_nouns = tuple(
            token
            for token in tokens
            if any(
                parse.get("pos") == "noun"
                and any(marker in str(parse.get("raw", "")) for marker in (":v_naz", ":v_zna"))
                and ":pron" not in str(parse.get("raw", ""))
                and _topic_noun_lemma(token) is not None
                and _topic_noun_lemma(token) == _topic_lemma(token)
                for parse in token.vesum_parses
            )
        )
        ordinary_nouns = tuple(
            token
            for token in tokens
            if any(parse.get("pos") == "noun" for parse in token.vesum_parses)
            and _topic_noun_lemma(token) is not None
            and _topic_noun_lemma(token) == _topic_lemma(token)
        )
        predicative_infinitives = tuple(
            token
            for token in tokens
            if has_predicative
            and any(
                parse.get("pos") == "verb" and ":inf" in str(parse.get("raw", ""))
                for parse in token.vesum_parses
            )
        )
        finite_verbs = tuple(token for token in tokens if _finite_predicate(token))
        content = tuple(token for token in tokens if _topic_lemma(token) is not None)
        ordered = (
            *direct_case_nouns,
            *predicative_infinitives,
            *ordinary_nouns,
            *finite_verbs,
            *content,
        )
    seen: set[str] = set()
    for token in ordered:
        if token.token_id in seen:
            continue
        seen.add(token.token_id)
        lemma = _topic_lemma(token)
        if lemma is not None and lemma not in excluded_lemmas:
            return token, lemma
    return None


def _relation_spans(sentence: AnchorSentence) -> tuple[_RelationSpan, ...]:
    """Extract only the closed, explicit source relations admitted by #424."""
    if not _safe_source_proposition_carrier(sentence):
        return ()
    tokens = sentence.tokens
    words = [token.surface.casefold() for token in tokens]
    rows: list[_RelationSpan] = []

    def append(
        rule_id: str,
        connector_index: int,
        *,
        answer_start: int | None = None,
        topic_before: int | None = None,
        topic_token: AnchorToken | None = None,
    ) -> None:
        start = tokens[connector_index].start_offset if answer_start is None else answer_start
        topic_row = (
            (topic_token, _content_lemma(topic_token))
            if topic_token is not None
            else _topic_token(
                sentence,
                before_offset=topic_before or tokens[connector_index].start_offset,
                prefer_nearest_verb=True,
            )
        )
        if (
            topic_row is None
            or topic_row[0] is None
            or not isinstance(topic_row[1], str)
            or not topic_row[1]
            or start < 0
            or start >= len(sentence.text)
        ):
            return
        rows.append(
            _RelationSpan(
                rule_id=rule_id,
                sentence=sentence,
                answer_start=start,
                answer_end=len(sentence.text),
                topic_token=topic_row[0],
                topic_lemma=topic_row[1],
            )
        )

    for index, word in enumerate(words):
        if sentence.text.rfind("(", 0, tokens[index].start_offset) > sentence.text.rfind(
            ")", 0, tokens[index].start_offset
        ):
            continue
        prior_punctuation = (
            sentence.text[tokens[index - 1].end_offset : tokens[index].start_offset]
            if index
            else ""
        )
        definition_predicate = next(
            (
                token
                for token in reversed(tokens[:index])
                if _finite_predicate(token) and _content_lemma(token) == "полягати"
            ),
            None,
        )
        definition_complement = (
            word in {"що", "щоб"}
            and index >= 2
            and words[index - 2 : index] == ["в", "тому"]
            and definition_predicate is not None
        )
        if definition_complement:
            topic = _topic_token(
                sentence,
                before_offset=definition_predicate.start_offset,
            )
            if topic is not None:
                append(
                    "definition-content.v1",
                    index,
                    topic_token=topic[0],
                )
            continue
        if word in {"бо", "оскільки"} or (word == "адже" and index > 0):
            if (index > 0 and _finite_predicate(tokens[index - 1])) or any(
                _finite_predicate(token) for token in tokens[:index]
            ):
                append("causal-clause.v1", index)
        if word == "тому" and index + 1 < len(words) and words[index + 1] == "що":
            if not (index > 0 and words[index - 1] == "в" and definition_predicate is not None):
                append("causal-clause.v1", index)
        if word == "через" and words[index : index + 3] == ["через", "те", "що"]:
            append("causal-clause.v1", index)
        if word in {"аби", "щоб"}:
            append("purpose-clause.v1", index)
        if word in {"поки", "доки", "коли"}:
            if word == "поки" and index + 1 < len(words) and words[index + 1] == "що":
                continue
            if not re.search(r"[,;—–]", prior_punctuation):
                continue
            if any(_finite_predicate(token) for token in tokens[index + 1 :]):
                append("temporal-clause.v1", index)
        if words[index : index + 3] in (["після", "того", "як"], ["до", "того", "як"]) and any(
            _finite_predicate(token) for token in tokens[index + 3 :]
        ):
            append("temporal-clause.v1", index)

    for predicate in (token for token in tokens if _finite_predicate(token)):
        lemma = _content_lemma(predicate)
        if lemma not in _LICENSED_VID_CAUSE_LEMMAS:
            continue
        for index, word in enumerate(words):
            if word != "від" or tokens[index].start_offset <= predicate.end_offset:
                continue
            if any(
                parse.get("pos") == "noun" and ":v_rod" in str(parse.get("raw", ""))
                for token in tokens[index + 1 :]
                for parse in token.vesum_parses
            ):
                append(
                    "licensed-vid-cause.v1",
                    index,
                    topic_before=tokens[index].start_offset,
                    topic_token=predicate,
                )
                break

    unique: dict[tuple[str, int, int], _RelationSpan] = {}
    for row in rows:
        unique.setdefault((row.rule_id, row.answer_start, row.answer_end), row)
    return tuple(unique.values())


def _source_question_candidate(
    sentence: AnchorSentence,
    *,
    candidate_number: int,
    category: str,
    intent: str,
    answer_start: int,
    answer_end: int,
    topic_token: AnchorToken,
    topic_lemma: str,
    focus_alignment: str | None = None,
) -> EvidenceCandidate:
    answer = sentence.text[answer_start:answer_end]
    return EvidenceCandidate(
        activity_type="text-questions",
        candidate_id=f"text-questions:1:{candidate_number}",
        sentence_id=sentence.sentence_id,
        token_id=topic_token.token_id,
        literal_evidence=sentence.text,
        expected_key=answer,
        semantic_target=(
            f"source-proposition:{sentence.sentence_id}:{category}:"
            f"{answer_start}:{answer_end}:{intent}"
        ),
        category=category,
        question_intent=intent,
        semantic_warrant=(
            "exact source proposition span states the recoverable fact"
            if category == "comprehension"
            else f"exact source proposition span realizes {intent}"
            if category == "explanation_inference"
            else "exact source proposition anchors one realistic application criterion"
        ),
        answer_start_offset=answer_start,
        answer_end_offset=answer_end,
        topic_token_id=topic_token.token_id,
        topic_lemma=topic_lemma,
        focus_alignment=focus_alignment,
    )


_REGENERATION_CHANGE_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:зараз|спочатку|потім|згодом|невдовзі|через|за\s+(?:кілька|\d+)|"
    r"третин\w*|половин\w*|відсот\w*|але|проте|однак)\b|\d",
    re.IGNORECASE,
)
_REGENERATION_LATE_DAYPART_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:звечора|увечері|вечір\w*|ніч|ночі|ніччю|нічн\w*)\b",
    re.IGNORECASE,
)
_REGENERATION_EARLY_DAYPART_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:вдосвіта|вранці|світан\w*|ранок\w*|ранков\w*)\b",
    re.IGNORECASE,
)
_REGENERATION_SOUND_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:звук\w*|шурх\w*|скрик\w*|плюск\w*|плеск\w*|стук\w*|"
    r"шум\w*|гул\w*|голос\w*|постріл\w*|чути|чутно|луна\w*|бах|лоп|фш)\b",
    re.IGNORECASE,
)
_REGENERATION_VISIBLE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:видн\w*|бач\w*|з'яв\w*|з’яв\w*|зник\w*|світл\w*|темн\w*|"
    r"яскрав\w*|блід\w*|тьмян\w*|сяй\w*|див\w*|сонц\w*|туман\w*|"
    r"колір\w*|форма\w*|розмір\w*|вигляд\w*)\b",
    re.IGNORECASE,
)


def _narrative_regeneration_profile(sentences: Sequence[AnchorSentence]) -> bool:
    """Whether the source independently supports all three narrative lenses."""
    text = " ".join(sentence.text for sentence in sentences)
    return (
        _REGENERATION_LATE_DAYPART_RE.search(text) is not None
        and _REGENERATION_EARLY_DAYPART_RE.search(text) is not None
        and _REGENERATION_SOUND_RE.search(text) is not None
        and _REGENERATION_CHANGE_MARKER_RE.search(text) is not None
        and _REGENERATION_VISIBLE_RE.search(text) is not None
    )


def _narrative_application_carriers(
    carriers: Sequence[tuple[AnchorSentence, tuple[str, ...]]],
) -> tuple[tuple[AnchorSentence, tuple[str, ...]], ...]:
    """Choose a human episode and a separate detail-rich proposition."""
    if len(carriers) < 2:
        return ()

    def personal_rank(row: tuple[AnchorSentence, tuple[str, ...]]) -> tuple[int, ...]:
        sentence = row[0]
        words = {token.surface.casefold() for token in sentence.tokens}
        has_person = bool(words & _APPLICATION_PERSONAL_FORMS) or any(
            ":anim" in str(parse.get("raw", ""))
            for token in sentence.tokens
            for parse in token.vesum_parses
        )
        return (
            int(not has_person),
            *_application_carrier_rank(sentence)[:3],
            int(sentence.sentence_id.removeprefix("s-")),
        )

    personal = min(
        carriers,
        key=personal_rank,
    )
    evidence = min(
        (row for row in carriers if row[0].sentence_id != personal[0].sentence_id),
        key=lambda row: (
            -len({token.surface.casefold() for token in row[0].tokens if len(token.surface) >= 4}),
            _application_carrier_rank(row[0]),
        ),
    )
    return (personal, evidence)


def _regeneration_span_supports(lens: str, context: Sequence[AnchorSentence]) -> bool:
    """Certify that a multi-sentence span can honestly support its B1 move."""
    predicate_count = sum(
        1 for sentence in context for token in sentence.tokens if _finite_predicate(token)
    )
    if predicate_count < 2:
        return False
    if lens == "change-comparison":
        return (
            _REGENERATION_CHANGE_MARKER_RE.search(" ".join(sentence.text for sentence in context))
            is not None
        )
    return lens in {"episode-synthesis", "evidence-details"}


def regeneration_text_question_inventory(
    inventory: CertificationInventory,
    *,
    original_unit_ids: Sequence[str],
    excluded_comprehension_sentence_ids: Collection[str] = (),
) -> CertificationInventory:
    """Build a fresh five-proposition board for one reviewed B1 question block.

    The original three comprehension carriers remain the evidence spine when
    they express complete propositions.  Elliptical fragments are replaced by
    the nearest unused declarative proposition, preferring a following
    sentence at equal distance.  The two application carriers are chosen
    across the complete anchor by proposition transferability instead of by
    the lexical token group that happened to fill the original slot.  Their
    contract is the proposition itself: no noun lemma is exposed as a topic
    the serializer must force into a learner question.
    """
    original_by_id = {
        candidate.candidate_id: candidate
        for candidate in inventory.candidates
        if candidate.activity_type == "text-questions"
    }
    original = tuple(
        original_by_id[unit_id] for unit_id in original_unit_ids if unit_id in original_by_id
    )
    minimum = floor_for("text-questions").minimum_units
    comprehension = tuple(
        candidate for candidate in original if candidate.category == "comprehension"
    )
    if (
        len(original) != minimum
        or len(comprehension) < TEXT_QUESTION_COMPREHENSION_FLOOR.comprehension
        or any(candidate.focus_alignment != "source-comprehension" for candidate in original)
    ):
        return replace(
            inventory,
            candidates=tuple(
                candidate
                for candidate in inventory.candidates
                if candidate.activity_type != "text-questions"
            ),
        )

    selected_comprehension = comprehension[: TEXT_QUESTION_COMPREHENSION_FLOOR.comprehension]
    sentence_positions = {
        sentence.sentence_id: position for position, sentence in enumerate(inventory.sentences)
    }
    excluded_carriers = frozenset(excluded_comprehension_sentence_ids)
    narrative_profile = _narrative_regeneration_profile(inventory.sentences)
    comprehension_lenses = (
        ("daypart-contrast", "sound-shift", "visible-change")
        if narrative_profile
        else ("episode-synthesis", "evidence-details", "change-comparison")
    )
    comprehension_carriers: dict[
        str, tuple[AnchorSentence, AnchorToken, str, tuple[AnchorSentence, ...]]
    ] = {}
    comprehension_sentence_ids: set[str] = set()
    comprehension_context_ids: set[str] = set()
    selection_order = (
        tuple(zip(comprehension_lenses, selected_comprehension, strict=True))
        if narrative_profile
        else (
            ("change-comparison", selected_comprehension[2]),
            ("episode-synthesis", selected_comprehension[0]),
            ("evidence-details", selected_comprehension[1]),
        )
    )
    for lens, candidate in selection_order:
        original_position = sentence_positions.get(candidate.sentence_id)
        if original_position is None:
            break
        ranked_sentences = sorted(
            inventory.sentences,
            key=lambda sentence: (
                abs(sentence_positions[sentence.sentence_id] - original_position),
                sentence_positions[sentence.sentence_id] < original_position,
                sentence_positions[sentence.sentence_id],
            ),
        )
        carrier = None
        for sentence in ranked_sentences:
            position = sentence_positions[sentence.sentence_id]
            offsets = (-1, 0, 1) if lens != "change-comparison" else (0, 1, 2)
            context = (
                tuple(inventory.sentences)
                if narrative_profile
                else tuple(
                    inventory.sentences[context_position]
                    for offset in offsets
                    if 0 <= (context_position := position + offset) < len(inventory.sentences)
                )
            )
            context_ids = {item.sentence_id for item in context}
            if (
                len(context) < 2
                or sentence.sentence_id in comprehension_sentence_ids
                or sentence.sentence_id in excluded_carriers
                or (not narrative_profile and not context_ids.isdisjoint(comprehension_context_ids))
                or (not narrative_profile and not _regeneration_span_supports(lens, context))
                or not _safe_source_proposition_carrier(sentence)
                or not any(_finite_predicate(token) for token in sentence.tokens)
                or (topic := _topic_token(sentence)) is None
            ):
                continue
            topic_token, topic_lemma = topic
            carrier = (sentence, topic_token, topic_lemma, context)
            break
        if carrier is None:
            break
        comprehension_carriers[lens] = carrier
        comprehension_sentence_ids.add(carrier[0].sentence_id)
        if not narrative_profile:
            comprehension_context_ids.update(item.sentence_id for item in carrier[3])
    if len(comprehension_carriers) != TEXT_QUESTION_COMPREHENSION_FLOOR.comprehension:
        return replace(
            inventory,
            candidates=tuple(
                candidate
                for candidate in inventory.candidates
                if candidate.activity_type != "text-questions"
            ),
        )
    eligible_application_carriers = tuple(
        (sentence, grounding_terms)
        for sentence in sorted(
            inventory.sentences,
            key=lambda item: (
                _application_carrier_rank(item),
                int(item.sentence_id.removeprefix("s-")),
            ),
        )
        if sentence.sentence_id not in comprehension_sentence_ids
        and sentence.tokens
        and _safe_item_carrier(sentence)
        and _safe_source_proposition_carrier(sentence)
        and (grounding_terms := _application_question_grounding_terms(sentence))
    )
    application_carriers = (
        _narrative_application_carriers(eligible_application_carriers)
        if narrative_profile
        else eligible_application_carriers[:2]
    )
    if len(application_carriers) != 2:
        return replace(
            inventory,
            candidates=tuple(
                candidate
                for candidate in inventory.candidates
                if candidate.activity_type != "text-questions"
            ),
        )

    replacements: list[EvidenceCandidate] = []
    for lens in comprehension_lenses:
        sentence, topic_token, _topic_lemma, context = comprehension_carriers[lens]
        position = len(replacements) + 1
        rendering_surface = " ".join(item.text for item in context)
        replacements.append(
            EvidenceCandidate(
                activity_type="text-questions",
                candidate_id=f"text-questions:regeneration:{position}",
                sentence_id=sentence.sentence_id,
                token_id=topic_token.token_id,
                literal_evidence=sentence.text,
                expected_key=rendering_surface,
                semantic_target=(
                    f"source-span-regeneration:{sentence.sentence_id}:comprehension:{position}"
                ),
                category="comprehension",
                question_intent="fact-recovery",
                semantic_warrant=(
                    "ordered exact source sentences support one fresh communicative "
                    "B1 comprehension move"
                ),
                answer_start_offset=0,
                answer_end_offset=len(rendering_surface),
                focus_alignment="source-comprehension",
                rendering_surface=rendering_surface,
                question_basis="source-span",
                question_lens=lens,
                question_context_sentence_ids=tuple(item.sentence_id for item in context),
            )
        )
    application_lenses = ("personal-example", "evidence-evaluation")
    for (sentence, grounding_terms), application_lens in zip(
        application_carriers, application_lenses, strict=True
    ):
        position = len(replacements) + 1
        replacements.append(
            EvidenceCandidate(
                activity_type="text-questions",
                candidate_id=f"text-questions:regeneration:{position}",
                sentence_id=sentence.sentence_id,
                token_id=sentence.tokens[0].token_id,
                literal_evidence=sentence.text,
                expected_key=sentence.text,
                semantic_target=(
                    f"source-proposition-regeneration:{sentence.sentence_id}:"
                    f"anchored-application:{position}"
                ),
                category="anchored_application",
                question_intent="anchored-application.v1",
                semantic_warrant=(
                    "complete source proposition supports a broadly relatable "
                    "experience or evidence-based interpretation"
                ),
                answer_start_offset=0,
                answer_end_offset=len(sentence.text),
                focus_alignment="source-comprehension",
                question_basis="source-proposition",
                question_lens=application_lens,
                question_grounding_terms=grounding_terms,
            )
        )
    return replace(
        inventory,
        candidates=(
            *(
                candidate
                for candidate in inventory.candidates
                if candidate.activity_type != "text-questions"
            ),
            *replacements,
        ),
    )


def _joint_source_comprehension(
    sentences: Sequence[AnchorSentence],
    *,
    sentence_group_uses: Counter[str],
    sentence_phase_uses: dict[str, set[int]],
) -> tuple[tuple[EvidenceCandidate, ...], tuple[TrueFalseFact, ...]] | None:
    """Allocate the complete 8+8 Phase-2 source-comprehension substrate.

    Selection is joint because independent greedy builders can consume the
    same scarce relation or safely-negatable sentence and discover the
    conflict only in exact cover.  Match-up citations do not consume source
    capacity and therefore are intentionally absent here.
    """
    eligible = tuple(
        sentence
        for sentence in sentences
        if sentence.tokens
        and sentence_group_uses[sentence.sentence_id] < 2
        and 2 not in sentence_phase_uses.get(sentence.sentence_id, set())
        and _safe_item_carrier(sentence)
    )
    if len(eligible) < 16:
        return None

    relations = {
        rule_id: tuple(
            row
            for sentence in eligible
            for row in _relation_spans(sentence)
            if row.rule_id == rule_id
        )
        for rule_id in _RELATION_PRIORITY
    }
    selected_relations: list[_RelationSpan] = []
    relation_sentence_ids: set[str] = set()
    for rule_id in _RELATION_PRIORITY:
        row = next(
            (
                candidate
                for candidate in relations[rule_id]
                if candidate.sentence.sentence_id not in relation_sentence_ids
            ),
            None,
        )
        if row is None:
            continue
        selected_relations.append(row)
        relation_sentence_ids.add(row.sentence.sentence_id)
        if len(selected_relations) == 3:
            break
    if len(selected_relations) < 3:
        for rule_id in _RELATION_PRIORITY:
            for row in relations[rule_id]:
                if row.sentence.sentence_id in relation_sentence_ids:
                    continue
                selected_relations.append(row)
                relation_sentence_ids.add(row.sentence.sentence_id)
                if len(selected_relations) == 3:
                    break
            if len(selected_relations) == 3:
                break
    if len(selected_relations) != 3:
        return None

    false_candidates: list[tuple[AnchorSentence, _PredicateSpan]] = []
    for sentence in eligible:
        if sentence.sentence_id in relation_sentence_ids:
            continue
        predicate = next(iter(_safe_asserted_predicates(sentence)), None)
        if predicate is not None:
            false_candidates.append((sentence, predicate))
    false_rows = sorted(
        false_candidates,
        key=lambda row: (
            _safe_source_proposition_carrier(row[0]),
            int(row[0].sentence_id.removeprefix("s-")),
        ),
    )[:4]
    if len(false_rows) != 4:
        return None
    false_sentence_ids = {sentence.sentence_id for sentence, _predicate in false_rows}

    remaining = tuple(
        sentence
        for sentence in eligible
        if sentence.sentence_id not in relation_sentence_ids | false_sentence_ids
        and _safe_source_proposition_carrier(sentence)
        and _topic_token(sentence) is not None
    )
    # Preserve remaining safely-negatable sentences for literal-true rows only
    # when no non-negatable fact/application carrier is available.
    ranked = sorted(
        remaining,
        key=lambda sentence: (
            _safe_true_fact_carrier(sentence),
            bool(_safe_asserted_predicates(sentence)),
            int(sentence.sentence_id.removeprefix("s-")),
        ),
    )
    comprehension_carriers: list[tuple[AnchorSentence, tuple[AnchorToken, str]]] = []
    used_comprehension_topics: set[str] = set()
    for sentence in ranked:
        topic = _topic_token(
            sentence,
            excluded_lemmas=frozenset(used_comprehension_topics),
        )
        if topic is None:
            continue
        comprehension_carriers.append((sentence, topic))
        used_comprehension_topics.add(topic[1])
        if len(comprehension_carriers) == 3:
            break
    comprehension_sentence_ids = {
        sentence.sentence_id for sentence, _topic in comprehension_carriers
    }
    application_carriers = [
        (sentence, topic)
        for sentence in sorted(
            ranked,
            key=lambda sentence: (
                _application_carrier_rank(sentence),
                int(sentence.sentence_id.removeprefix("s-")),
            ),
        )
        if sentence.sentence_id not in comprehension_sentence_ids
        if (topic := _application_topic(sentence)) is not None
    ][:2]
    if len(comprehension_carriers) != 3 or len(application_carriers) != 2:
        return None
    question_carriers = tuple(
        sentence for sentence, _topic in (*comprehension_carriers, *application_carriers)
    )
    question_sentence_ids = {sentence.sentence_id for sentence in question_carriers}
    true_sentences = [
        sentence
        for sentence in eligible
        if sentence.sentence_id
        not in relation_sentence_ids | false_sentence_ids | question_sentence_ids
        and _safe_true_fact_carrier(sentence)
    ][:4]
    if len(true_sentences) != 4:
        return None

    questions: list[EvidenceCandidate] = []
    for sentence, topic in comprehension_carriers:
        questions.append(
            _source_question_candidate(
                sentence,
                candidate_number=len(questions) + 1,
                category="comprehension",
                intent="fact-recovery",
                answer_start=0,
                answer_end=len(sentence.text),
                topic_token=topic[0],
                topic_lemma=topic[1],
            )
        )
    for relation in selected_relations:
        questions.append(
            _source_question_candidate(
                relation.sentence,
                candidate_number=len(questions) + 1,
                category="explanation_inference",
                intent=relation.rule_id,
                answer_start=relation.answer_start,
                answer_end=relation.answer_end,
                topic_token=relation.topic_token,
                topic_lemma=relation.topic_lemma,
            )
        )
    for sentence, topic in application_carriers:
        questions.append(
            _source_question_candidate(
                sentence,
                candidate_number=len(questions) + 1,
                category="anchored_application",
                intent="anchored-application.v1",
                answer_start=0,
                answer_end=len(sentence.text),
                topic_token=topic[0],
                topic_lemma=topic[1],
            )
        )

    false_facts = [
        TrueFalseFact(
            fact_id=f"true-false:1:false-{index}",
            sentence_id=sentence.sentence_id,
            literal_evidence=sentence.text,
            source_surface=predicate.token.surface,
            replacement_surface=f"не {predicate.token.surface}",
            truth_value=False,
            mutation_rule_id="negate-asserted-predicate.v1",
            source_start_offset=predicate.token.start_offset,
            source_end_offset=predicate.token.end_offset,
        )
        for index, (sentence, predicate) in enumerate(false_rows, start=1)
    ]
    true_facts = [
        TrueFalseFact(
            fact_id=f"true-false:1:true-{index}",
            sentence_id=sentence.sentence_id,
            literal_evidence=sentence.text,
            source_surface=sentence.tokens[0].surface,
            replacement_surface=sentence.tokens[0].surface,
            truth_value=True,
            mutation_rule_id="negate-asserted-predicate.v1",
        )
        for index, sentence in enumerate(true_sentences, start=1)
    ]
    order_seed = hashlib.sha256(
        "\n".join(sentence.text for sentence in eligible).encode("utf-8")
    ).hexdigest()
    facts = list(
        sorted(
            (*true_facts, *false_facts),
            key=lambda fact: hashlib.sha256(
                f"{order_seed}:{fact.sentence_id}:{fact.truth_value}".encode()
            ).hexdigest(),
        )
    )
    _break_trivial_truth_pattern(facts)
    if len(questions) != 8 or len(facts) != 8:
        return None
    return tuple(questions), tuple(facts)


def _balanced_true_false(
    sentences: Sequence[AnchorSentence],
    *,
    sentence_group_uses: Counter[str],
    sentence_phase_uses: dict[str, set[int]],
    phase: int = 2,
) -> tuple[TrueFalseFact, ...]:
    """Build a balanced source-bound board for the caller-selected phase.

    Phase 2 remains the legacy default. The qualified 45-minute profile passes
    phase 1 when true/false replaces the unavailable match-up board. Relation
    scarcity must not erase independently provable statements; every row still
    uses a distinct safe carrier and false rows use the closed mutation catalog.
    """
    eligible = tuple(
        sentence
        for sentence in sentences
        if sentence.tokens
        and sentence_group_uses[sentence.sentence_id] < 2
        and phase not in sentence_phase_uses.get(sentence.sentence_id, set())
        and _safe_item_carrier(sentence)
    )
    false_rows = [
        (sentence, predicate)
        for sentence in eligible
        if (predicate := next(iter(_safe_asserted_predicates(sentence)), None)) is not None
    ][:4]
    if len(false_rows) != 4:
        return ()
    false_sentence_ids = {sentence.sentence_id for sentence, _predicate in false_rows}
    true_sentences = [
        sentence
        for sentence in eligible
        if sentence.sentence_id not in false_sentence_ids and _safe_true_fact_carrier(sentence)
    ][:4]
    if len(true_sentences) != 4:
        return ()

    facts = [
        TrueFalseFact(
            fact_id=f"true-false:1:false-{index}",
            sentence_id=sentence.sentence_id,
            literal_evidence=sentence.text,
            source_surface=predicate.token.surface,
            replacement_surface=f"не {predicate.token.surface}",
            truth_value=False,
            mutation_rule_id="negate-asserted-predicate.v1",
            source_start_offset=predicate.token.start_offset,
            source_end_offset=predicate.token.end_offset,
        )
        for index, (sentence, predicate) in enumerate(false_rows, start=1)
    ]
    facts.extend(
        TrueFalseFact(
            fact_id=f"true-false:1:true-{index}",
            sentence_id=sentence.sentence_id,
            literal_evidence=sentence.text,
            source_surface=sentence.tokens[0].surface,
            replacement_surface=sentence.tokens[0].surface,
            truth_value=True,
            mutation_rule_id="negate-asserted-predicate.v1",
        )
        for index, sentence in enumerate(true_sentences, start=1)
    )
    order_seed = hashlib.sha256(
        "\n".join(sentence.text for sentence in eligible).encode("utf-8")
    ).hexdigest()
    facts.sort(
        key=lambda fact: hashlib.sha256(
            f"{order_seed}:{fact.sentence_id}:{fact.truth_value}".encode()
        ).hexdigest()
    )
    _break_trivial_truth_pattern(facts)
    return tuple(facts)


def _break_trivial_truth_pattern(facts: list[TrueFalseFact]) -> None:
    """Keep balanced answer keys from teaching a positional shortcut."""
    pattern = tuple(fact.truth_value for fact in facts)
    if pattern in {(True, False) * 4, (False, True) * 4}:
        facts[1], facts[2] = facts[2], facts[1]
    elif pattern in {
        (True,) * 4 + (False,) * 4,
        (False,) * 4 + (True,) * 4,
    }:
        facts[3], facts[4] = facts[4], facts[3]


def _eligible_tokens(
    activity_type: str,
    sentence: AnchorSentence,
    *,
    focus_mode: str | None = None,
    match_pairs: dict[str, tuple[str, str, str]] | None = None,
) -> tuple[AnchorToken, ...]:
    if activity_type != "mark-the-words" and not _safe_item_carrier(sentence):
        return ()
    tokens = tuple(token for token in sentence.tokens if token.vesum_parses)

    def focused(rows: tuple[AnchorToken, ...]) -> tuple[AnchorToken, ...]:
        if focus_mode == _FOCUS_PRIMARY or focus_mode in {
            "degree-recognition",
            "degree-cloze",
            "degree-context",
        }:
            return tuple(
                token
                for token in rows
                if (
                    _is_comparison_form(token)
                    if activity_type == "mark-the-words"
                    else _is_degree_token(token)
                )
            )
        if focus_mode == _FOCUS_REINFORCEMENT:
            return tuple(token for token in rows if not _is_predicative_adverb_token(token))
        return rows

    if activity_type == "cloze":
        if focus_mode != "long-form-reconstruction":
            interior = tokens[2:] if len(tokens) >= 3 else ()
            return focused(
                tuple(
                    token
                    for token in interior
                    if (identity := _unambiguous_content_lemma_pos(token)) is not None
                    and identity[1] in {"noun", "verb", "adj"}
                    and (
                        data.active_bundle().manifest.get("version") == "test"
                        or _contextual_choice_bank(sentence, token) is not None
                    )
                )
            )
        # The passage-level selector preserves visible context around every
        # marker. Keep every unambiguous content word available so a natural
        # story can carry one meaningful gap in every sentence; do not require
        # every sentence to contain an artificial morphology trap.
        return focused(
            tuple(
                token
                for token in tokens
                if (identity := _unambiguous_content_lemma_pos(token)) is not None
                and identity[1] in _CONTENT_POS
            )
        )
    if activity_type == "quiz" and focus_mode == "source-comprehension":
        topic = (
            _topic_token(
                sentence,
                excluded_lemmas=_UNSAFE_QUESTION_TOPIC_LEMMAS,
            )
            if _safe_source_proposition_carrier(sentence)
            else None
        )
        return () if topic is None else (topic[0],)
    if activity_type in {"quiz", "fill-in"}:
        return focused(
            tuple(
                token
                for token in tokens
                if any(parse.get("pos") in _CONTENT_POS for parse in token.vesum_parses)
                and _visible_carrier_choice_bank(sentence, token) is not None
            )
        )
    if activity_type == "match-up":
        return tuple(token for token in tokens if token.token_id in (match_pairs or {}))
    if activity_type == "error-correction":
        return focused(
            tuple(token for token in tokens if _error_replacement(sentence, token) is not None)
        )
    if activity_type == "text-questions" and focus_mode == "source-comprehension":
        topic = _topic_token(sentence) if _safe_source_proposition_carrier(sentence) else None
        return () if topic is None else (topic[0],)
    if activity_type == "short-writing":
        return focused(
            tuple(
                token
                for token in tokens
                if (
                    _is_degree_token(token)
                    if focus_mode in {_FOCUS_WRITING, "degree-writing"}
                    else _unambiguous_content_lemma_pos(token) is not None
                )
            )
        )
    if activity_type == "mark-the-words":
        if focus_mode is not None:
            return focused(tokens)
        return tuple(
            token
            for token in tokens
            if any(parse.get("pos") == "noun" for parse in token.vesum_parses)
        )
    if activity_type == "true-false":
        # Generic predicate negation produces formally different but
        # pedagogically trivial statements. Keep the closed mutation catalog
        # for explicit inventories, but do not schedule it in production.
        return ()
    return tokens


_CLOZE_WORD_RE: Final = re.compile(r"[А-Яа-яІіЇїЄєҐґ][А-Яа-яІіЇїЄєҐґ'’\-]*")
CLOZE_TOKENIZER_VERSION: Final = "ukrainian-word.v1"


@dataclass(frozen=True)
class ClozeGroup(Sequence[AnchorToken]):
    """One certified gap per sentence plus the exact contiguous passage."""

    tokens: tuple[AnchorToken, ...]
    sentences: tuple[AnchorSentence, ...]

    def __len__(self) -> int:
        return len(self.tokens)

    def __getitem__(self, index: int | slice) -> AnchorToken | tuple[AnchorToken, ...]:
        return self.tokens[index]


def _cloze_group(
    sentences: Sequence[AnchorSentence],
    used_token_ids: set[str],
) -> ClozeGroup | None:
    minimum_sentences, maximum_sentences = cloze_interaction_bounds()
    eligible_by_sentence = {
        sentence.sentence_id: tuple(
            token
            for token in _eligible_tokens("cloze", sentence, focus_mode="long-form-reconstruction")
            if token.token_id not in used_token_ids
        )
        for sentence in sentences
    }
    for i in range(len(sentences)):
        words = 0
        end_idx = i
        while end_idx < len(sentences):
            words += len(_CLOZE_WORD_RE.findall(sentences[end_idx].text))
            if words > 450:
                break
            if words >= 350:
                if end_idx - i < 2:
                    end_idx += 1
                    continue
                window = tuple(sentences[i : end_idx + 1])
                if not minimum_sentences <= len(window) <= maximum_sentences:
                    end_idx += 1
                    continue
                candidates_by_sentence: list[tuple[tuple[AnchorToken, str, str, int], ...]] = []
                for s_idx in range(i, end_idx + 1):
                    s_text = sentences[s_idx].text
                    s_words = _CLOZE_WORD_RE.findall(s_text)
                    rows: list[tuple[AnchorToken, str, str, int]] = []
                    for token in eligible_by_sentence[sentences[s_idx].sentence_id]:
                        identity = _unambiguous_content_lemma_pos(token)
                        if identity is None:
                            continue
                        words_before = len(_CLOZE_WORD_RE.findall(s_text[: token.start_offset]))
                        words_after = len(_CLOZE_WORD_RE.findall(s_text[token.end_offset :]))
                        boundary_context_missing = (s_idx == i and words_before < 2) or (
                            s_idx == end_idx and words_after < 2
                        )
                        if boundary_context_missing or words_before + words_after < 1:
                            continue
                        rows.append(
                            (
                                token,
                                identity[1],
                                identity[0],
                                abs(words_before - len(s_words) // 2),
                            )
                        )
                    if not rows:
                        candidates_by_sentence = []
                        break
                    candidates_by_sentence.append(tuple(rows))

                gaps: tuple[AnchorToken, ...] | None = None
                # Long-form option banks are certified independently for each
                # sentence. Let the passage use the natural POS mix of the
                # story instead of applying compact-fixture composition rules.
                lemmas_by_position: dict[str, set[str]] = defaultdict(set)
                for rows in candidates_by_sentence:
                    for _token, pos, lemma, _rank in rows:
                        lemmas_by_position[pos].add(lemma)
                position_sets = (
                    tuple(
                        pos
                        for pos, lemmas in sorted(lemmas_by_position.items())
                        if len(lemmas) >= 3
                    ),
                )
                for allowed_positions in position_sets:
                    selected: list[AnchorToken] = []
                    used_lemmas: set[str] = set()
                    pos_counts: Counter[str] = Counter()
                    for rows in candidates_by_sentence:
                        position_rows = [
                            row
                            for row in rows
                            if not allowed_positions or row[1] in allowed_positions
                        ]
                        available = [
                            row for row in position_rows if row[2] not in used_lemmas
                        ] or position_rows
                        if not available:
                            selected = []
                            break
                        token, pos, lemma, _rank = min(
                            available,
                            key=lambda row: (pos_counts[row[1]], row[3], row[0].token_id),
                        )
                        selected.append(token)
                        used_lemmas.add(lemma)
                        pos_counts[pos] += 1
                    if selected:
                        candidate_gaps = tuple(selected)
                        if (
                            _cross_gap_choice_banks(
                                candidate_gaps,
                                sentences={sentence.sentence_id: sentence for sentence in window},
                            )
                            is not None
                        ):
                            gaps = candidate_gaps
                            break

                if gaps is not None and len(gaps) == len(window):
                    return ClozeGroup(
                        tokens=gaps,
                        sentences=window,
                    )
            end_idx += 1
    return None


def _diverse_group(
    sentences: Sequence[AnchorSentence],
    *,
    activity_type: str,
    used_token_ids: set[str],
    sentence_group_uses: Counter[str],
    sentence_operation_uses: dict[str, set[str]],
    sentence_phase_uses: dict[str, set[int]],
    sentence_activity_uses: dict[str, set[str]],
    slot_id: str | None,
    phase: int | None,
    remaining_groups: int,
    focus: str | None,
    focus_mode: str | None = None,
    match_pairs: dict[str, tuple[str, str, str]] | None = None,
    prefer_reuse: bool = False,
    used_match_left: set[str] | None = None,
    used_match_right: set[str] | None = None,
) -> tuple[AnchorToken, ...] | None:
    """Choose one teacher-runnable block without repeating target lemmas.

    Recognition boards may carry two independent rows from one source sentence.
    Error correction uses one carrier per item. Cloze may carry two well-separated gaps,
    Atlas/degree match-up may
    take three independently certified kit pairs, and mark-the-words may retain
    four real targets from one excerpt. A source supports at most two blocks
    per lesson under different cognitive operations. The complete cloze
    passage consumes source capacity. Source comprehension is a semantic
    overlay: undrilled carriers remain literal recall, while any overlap that
    is needed for a fallback lane is relabelled as anchored application after
    all candidate lanes have been planned.
    """
    target_units = (
        planned_interactions(slot_id, activity_type) if slot_id is not None else None
    ) or floor_for(activity_type).minimum_units
    pools: list[tuple[AnchorSentence, tuple[AnchorToken, ...]]] = []
    operation = focus_mode or COGNITIVE_OPERATION.get(activity_type, activity_type)
    consumes_evidence_capacity = (
        activity_type != "match-up"
        and (
            activity_type,
            operation,
        )
        not in EVIDENCE_CAPACITY_OVERLAYS
    )

    def cloze_capacity_after(
        sentence: AnchorSentence, *, excluded_token_id: str | None = None
    ) -> int:
        """Preserve scarce, well-separated cloze targets during recognition."""
        targets = tuple(
            token
            for token in _eligible_tokens("cloze", sentence, focus_mode="degree-cloze")
            if token.token_id not in used_token_ids and token.token_id != excluded_token_id
        )
        if not targets:
            return 0
        indexes = {token.token_id: index for index, token in enumerate(sentence.tokens)}
        return (
            2
            if any(
                abs(indexes[left.token_id] - indexes[right.token_id]) >= 4
                for left_index, left in enumerate(targets)
                for right in targets[left_index + 1 :]
            )
            else 1
        )

    # Choice-based drills never replay the same proposition. Error correction
    # may use an earlier carrier because identifying a dependency error is a
    # distinct operation, but its own five items remain sentence-unique.
    structural_drill_types = {"quiz", "cloze", "fill-in"}
    for sentence in sentences:
        if consumes_evidence_capacity:
            if activity_type in structural_drill_types and any(
                sentence.sentence_id in sentence_activity_uses.get(other_type, set())
                for other_type in structural_drill_types - {activity_type}
            ):
                continue
            if sentence_group_uses[
                sentence.sentence_id
            ] >= 2 or operation in sentence_operation_uses.get(sentence.sentence_id, set()):
                continue
            if (
                phase is not None
                and phase in sentence_phase_uses.get(sentence.sentence_id, set())
                and not (
                    operation.startswith("degree-")
                    or operation == "anchor-comprehension"
                    or "source-comprehension"
                    in {
                        operation,
                        *sentence_operation_uses.get(sentence.sentence_id, set()),
                    }
                )
            ):
                continue
        available = tuple(
            sorted(
                (
                    token
                    for token in _eligible_tokens(
                        activity_type,
                        sentence,
                        focus_mode=focus_mode,
                        match_pairs=match_pairs,
                    )
                    if token.token_id not in used_token_ids
                ),
                key=lambda token: (
                    token.start_offset == 0 if activity_type == "error-correction" else False,
                    -cloze_capacity_after(sentence, excluded_token_id=token.token_id)
                    if focus_mode == "degree-recognition"
                    else 0,
                    -int(_is_comparison_form(token)) if activity_type == "text-questions" else 0,
                    _focus_rank(token, focus),
                ),
            )
        )
        if available:
            pools.append((sentence, available))
    if len(pools) < 4:
        return None
    if activity_type == "cloze":
        pos_capacity = Counter(
            identity[1]
            for _sentence, tokens in pools
            for token in tokens
            if (identity := _unambiguous_content_lemma_pos(token)) is not None
        )
        target_pos = next(
            (
                pos
                for pos, _count in sorted(pos_capacity.items(), key=lambda row: (-row[1], row[0]))
                if pos_capacity[pos] >= target_units
            ),
            None,
        )
        if target_pos is None:
            return None
        pools = [
            (
                sentence,
                tuple(
                    token
                    for token in tokens
                    if (identity := _unambiguous_content_lemma_pos(token)) is not None
                    and identity[1] == target_pos
                ),
            )
            for sentence, tokens in pools
        ]
        pools = [(sentence, tokens) for sentence, tokens in pools if tokens]
    # Prefer low-use sources, then sources able to supply both permitted units.
    # Python's sort remains stable for equal capacity, preserving document order.
    pools.sort(
        key=lambda row: (
            (
                int(
                    any(
                        _contextual_choice_bank(row[0], token) is not None
                        for token in row[0].tokens
                    )
                )
                if activity_type == "cloze"
                and data.active_bundle().manifest.get("version") != "test"
                else 0
            ),
            (
                min(
                    (
                        {
                            "agreement-number": 0,
                            "agreement-case": 1,
                            "agreement-gender": 2,
                            "government-case": 3,
                            "subject-verb-person": 4,
                            "subject-verb-number": 5,
                        }.get(replacement[1], 9)
                        for token in row[1]
                        if (replacement := _error_replacement(row[0], token)) is not None
                    ),
                    default=9,
                )
                if activity_type == "error-correction"
                else 0
            ),
            (
                0
                if activity_type == "text-questions"
                and focus_mode == "anchor-comprehension"
                and any(_is_comparison_form(token) for token in row[1])
                else 1
            ),
            (
                max(
                    (
                        cloze_capacity_after(row[0], excluded_token_id=token.token_id)
                        for token in row[1]
                    ),
                    default=0,
                )
                if focus_mode == "degree-recognition"
                else 0
            ),
            (
                1
                if activity_type == "fill-in"
                and row[0].sentence_id in sentence_activity_uses.get("quiz", set())
                else 0
            ),
            (
                1
                if activity_type == "fill-in"
                and row[0].sentence_id in sentence_activity_uses.get("text-questions", set())
                else 0
            ),
            (
                -sentence_group_uses[row[0].sentence_id]
                if prefer_reuse
                else sentence_group_uses[row[0].sentence_id]
            ),
            (
                0
                if focus_mode == _FOCUS_REINFORCEMENT
                and any(_is_degree_token(token) for token in row[1])
                else 1
                if focus_mode == _FOCUS_REINFORCEMENT
                else 0
            ),
            (
                sum(_is_degree_token(token) for token in row[1])
                if focus_mode == _FOCUS_REINFORCEMENT and activity_type == "error-correction"
                else 0
            ),
            min(_focus_rank(token, focus) for token in row[1]),
            -min(2, len(row[1])),
        )
    )

    if activity_type == "text-questions":
        if focus_mode == "source-comprehension":
            pools = [
                row
                for row in pools
                if _safe_source_proposition_carrier(row[0]) and _topic_token(row[0]) is not None
            ]
            pools.sort(
                key=lambda row: (
                    (
                        -sentence_group_uses[row[0].sentence_id]
                        if prefer_reuse
                        else sentence_group_uses[row[0].sentence_id]
                    ),
                    int(row[0].sentence_id.removeprefix("s-")),
                )
            )
        else:
            if focus_mode is None:
                pools = [
                    (sentence, (topic[0],))
                    for sentence, _tokens in pools
                    if _safe_source_proposition_carrier(sentence)
                    and (topic := _topic_token(sentence)) is not None
                ]
            comprehension_pools = (
                [row for row in pools if any(_is_comparison_form(token) for token in row[1])]
                if focus_mode == "anchor-comprehension"
                else pools
            )
            causal_pools = [
                row for row in pools if _EXPLICIT_CAUSAL_RE.search(row[0].text.casefold())
            ]
            chosen_question_pools: list[tuple[AnchorSentence, tuple[AnchorToken, ...]]] = []

            def append_distinct(
                candidates: Sequence[tuple[AnchorSentence, tuple[AnchorToken, ...]]],
                count: int,
            ) -> None:
                if len(chosen_question_pools) >= count:
                    return
                for row in candidates:
                    if row in chosen_question_pools:
                        continue
                    chosen_question_pools.append(row)
                    if len(chosen_question_pools) >= count:
                        return

            append_distinct(
                sorted(
                    comprehension_pools,
                    key=lambda row: (
                        bool(_EXPLICIT_CAUSAL_RE.search(row[0].text.casefold())),
                        int(row[0].sentence_id.removeprefix("s-")),
                    ),
                ),
                min(3, target_units),
            )
            if len(chosen_question_pools) != min(3, target_units):
                return None
            inference_target = min(6, target_units)
            append_distinct(causal_pools, inference_target)
            if len(chosen_question_pools) != inference_target:
                return None
            append_distinct(pools, target_units)
            if len(chosen_question_pools) != target_units:
                return None
            pools = chosen_question_pools

    per_source_limit = (
        4
        if activity_type == "mark-the-words"
        else 3
        if activity_type == "match-up"
        else 2
        if activity_type == "cloze"
        else 1
    )

    def source_capacity(sentence: AnchorSentence, tokens: tuple[AnchorToken, ...]) -> int:
        """Return the usable per-source capacity for this activity shape."""
        if activity_type != "cloze":
            return min(per_source_limit, len(tokens))
        token_indexes = {token.token_id: index for index, token in enumerate(sentence.tokens)}
        has_separated_pair = any(
            abs(token_indexes[left.token_id] - token_indexes[right.token_id]) >= 4
            for left_index, left in enumerate(tokens)
            for right in tokens[left_index + 1 :]
        )
        return 2 if has_separated_pair else 1

    if activity_type == "cloze":
        # Prefer four consecutive eligible source pools so the excerpt board
        # remains narratively compact without claiming unselected sentences.
        ordered_pools = sorted(pools, key=lambda row: int(row[0].sentence_id.removeprefix("s-")))
        compact_windows = []
        for start in range(max(0, len(ordered_pools) - 3)):
            window = ordered_pools[start : start + 4]
            first = int(window[0][0].sentence_id.removeprefix("s-"))
            last = int(window[-1][0].sentence_id.removeprefix("s-"))
            if (
                len(window) != 4
                or last - first != 3
                or sum(source_capacity(*row) for row in window) < target_units
            ):
                continue
            contextual_carriers = sum(
                any(
                    _contextual_choice_bank(sentence, token) is not None
                    for token in sentence.tokens
                )
                for sentence in sentences[first - 1 : last]
            )
            compact_windows.append(((contextual_carriers, last - first, first), window))
        if compact_windows:
            _score, best_window = min(compact_windows, key=lambda row: row[0])
            pools = [
                *best_window,
                *(row for row in ordered_pools if row not in best_window),
            ]

    distinct_target = (
        target_units
        if activity_type == "error-correction"
        else min(4, target_units)
        if activity_type in {"cloze", "match-up", "mark-the-words"}
        else target_units
    )
    # Match pairs additionally require unique left and right surfaces.  A
    # source pool can therefore become unusable after an earlier pair is
    # selected; keep later pools available instead of failing the whole group
    # merely because one of the first eight pools collides semantically.
    if activity_type == "quiz" and focus_mode == "source-comprehension":
        chosen_pools = []
        chosen_topic_lemmas: set[str] = set()
        for sentence, tokens in pools:
            unique_tokens = tuple(
                token
                for token in tokens
                if (_content_lemma(token) or token.surface.casefold()) not in chosen_topic_lemmas
            )
            if not unique_tokens:
                continue
            chosen_pools.append((sentence, unique_tokens))
            chosen_topic_lemmas.add(
                _content_lemma(unique_tokens[0]) or unique_tokens[0].surface.casefold()
            )
            if len(chosen_pools) == target_units:
                break
    elif (
        activity_type in {"cloze", "match-up", "error-correction"}
        or (activity_type == "fill-in" and focus_mode == _FOCUS_REINFORCEMENT)
        or (activity_type == "text-questions" and focus_mode == "source-comprehension")
    ):
        chosen_pools = pools
    else:
        chosen_rows: list[tuple[AnchorSentence, tuple[AnchorToken, ...]]] = []
        chosen_capacity = 0
        for pool in pools:
            chosen_rows.append(pool)
            # Source reuse counts slots, not constituent units: once this
            # source is eligible for another slot, that slot retains its own
            # per-source unit allowance.
            chosen_capacity += source_capacity(*pool)
            if len(chosen_rows) >= distinct_target and chosen_capacity >= target_units:
                break
        chosen_pools = chosen_rows
    selected: list[AnchorToken] = []
    selected_per_source: Counter[str] = Counter()
    selected_degree_count = 0
    selected_error_classes: set[str] = set()
    selected_initial_errors = 0
    match_left = set(used_match_left or ())
    match_right = set(used_match_right or ())

    def usable(token: AnchorToken) -> bool:
        if activity_type in {
            "quiz",
            "cloze",
            "fill-in",
            "text-questions",
        }:
            identity = _content_lemma(token) or token.surface.casefold()
            if any(
                (_content_lemma(selected_token) or selected_token.surface.casefold()) == identity
                for selected_token in selected
            ):
                return False
        if activity_type == "fill-in" and token.sentence_id in sentence_activity_uses.get(
            "quiz", set()
        ):
            overlap = sum(
                selected_token.sentence_id in sentence_activity_uses.get("quiz", set())
                for selected_token in selected
            )
            if overlap >= 2:
                return False
        if activity_type == "cloze":
            prior = next(
                (item for item in selected if item.sentence_id == token.sentence_id),
                None,
            )
            if prior is None:
                return True
            sentence = next(
                item for item, _tokens in chosen_pools if item.sentence_id == token.sentence_id
            )
            token_indexes = {item.token_id: index for index, item in enumerate(sentence.tokens)}
            # Preserve at least three visible words between cloze markers,
            # matching the deterministic learner-context gate.
            return abs(token_indexes[prior.token_id] - token_indexes[token.token_id]) >= 4
        if activity_type != "match-up":
            return True
        pair = (match_pairs or {}).get(token.token_id)
        return (
            pair is not None
            and pair[0].casefold() not in match_left
            and pair[1].casefold() not in match_right
        )

    rounds = 1 if activity_type == "true-false" else per_source_limit
    for _round in range(rounds):
        for sentence, tokens in chosen_pools:
            if selected_per_source[sentence.sentence_id] > _round:
                continue
            available_candidates = tuple(
                candidate for candidate in tokens if candidate not in selected and usable(candidate)
            )
            if focus_mode == _FOCUS_REINFORCEMENT:
                preferred_degree = selected_degree_count < 2
                token = next(
                    (
                        candidate
                        for candidate in available_candidates
                        if _is_comparison_form(candidate) == preferred_degree
                    ),
                    next(iter(available_candidates), None),
                )
            elif focus_mode == "degree-recognition":
                token = max(
                    available_candidates,
                    key=lambda candidate: (
                        cloze_capacity_after(
                            sentence,
                            excluded_token_id=candidate.token_id,
                        ),
                        _focus_rank(candidate, focus),
                    ),
                    default=None,
                )
            elif activity_type == "text-questions" and focus_mode == "anchor-comprehension":
                preferred_degree = selected_degree_count < 3
                token = next(
                    (
                        candidate
                        for candidate in available_candidates
                        if _is_degree_token(candidate) == preferred_degree
                    ),
                    next(iter(available_candidates), None),
                )
            elif activity_type == "cloze":
                token = next(iter(available_candidates), None)
            elif activity_type == "error-correction":
                new_class = next(
                    (
                        candidate
                        for candidate in available_candidates
                        if (candidate.start_offset > 0 or selected_initial_errors < 4)
                        and (row := _error_replacement(sentence, candidate)) is not None
                        and row[1] not in selected_error_classes
                    ),
                    None,
                )
                token = (
                    new_class
                    if new_class is not None or len(selected_error_classes) < 3
                    else next(
                        (
                            candidate
                            for candidate in available_candidates
                            if candidate.start_offset > 0 or selected_initial_errors < 4
                        ),
                        None,
                    )
                )
            else:
                token = next(iter(available_candidates), None)
            if token is None:
                continue
            selected.append(token)
            selected_degree_count += int(
                _is_comparison_form(token)
                if activity_type == "text-questions"
                else _is_degree_token(token)
            )
            selected_per_source[sentence.sentence_id] += 1
            if activity_type == "error-correction":
                replacement = _error_replacement(sentence, token)
                assert replacement is not None
                selected_error_classes.add(replacement[1])
                selected_initial_errors += int(token.start_offset == 0)
            if activity_type == "match-up":
                pair = (match_pairs or {}).get(token.token_id)
                assert pair is not None
                match_left.add(pair[0].casefold())
                match_right.add(pair[1].casefold())
            if len(selected) == target_units:
                break
        if len(selected) == target_units:
            break
    if activity_type == "cloze":
        # Solve the passage layout as a whole. Per-sentence spacing alone can
        # still put one gap at a sentence end and the next near the following
        # sentence start, leaving no learner context between them.
        candidate_tokens = tuple(
            sorted(
                (token for _sentence, tokens in chosen_pools for token in tokens),
                key=lambda token: (
                    int(token.sentence_id.removeprefix("s-")),
                    token.start_offset,
                ),
            )
        )
        passage_candidate: tuple[AnchorToken, ...] | None = None
        for combination in combinations(candidate_tokens, target_units):
            source_counts = Counter(token.sentence_id for token in combination)
            if (
                len(source_counts) < min(4, target_units)
                or max(source_counts.values()) > per_source_limit
            ):
                continue
            lemmas = [_content_lemma(token) or token.surface.casefold() for token in combination]
            if len(lemmas) != len(set(lemmas)):
                continue
            selected_sentence_numbers = sorted(
                int(sentence_id.removeprefix("s-")) for sentence_id in source_counts
            )
            # The renderer joins only the sentences that carry selected gaps.
            # Count learner-visible context against that exact passage; counting
            # omitted intervening source sentences can certify adjacent markers
            # that the rendered cloze later (correctly) rejects.
            passage_tokens = tuple(
                token
                for sentence_number in selected_sentence_numbers
                for sentence in (sentences[sentence_number - 1],)
                for token in sentence.tokens
            )
            positions = {token.token_id: index for index, token in enumerate(passage_tokens)}
            gap_positions = [positions[token.token_id] for token in combination]
            if (
                gap_positions[0] < 2
                or len(passage_tokens) - gap_positions[-1] - 1 < 2
                or any(
                    right - left - 1 < 3
                    for left, right in zip(gap_positions, gap_positions[1:], strict=False)
                )
                or _cross_gap_choice_banks(
                    combination,
                    sentences={sentence.sentence_id: sentence for sentence in sentences},
                )
                is None
            ):
                continue
            passage_candidate = combination
            break
        if passage_candidate is not None:
            selected = list(passage_candidate)
            selected_per_source = Counter(token.sentence_id for token in selected)
        else:
            selected = []
            selected_per_source = Counter()
    if focus_mode == _FOCUS_REINFORCEMENT and selected_degree_count < 2:
        for sentence, tokens in chosen_pools:
            if selected_per_source[sentence.sentence_id] >= per_source_limit:
                continue
            replacement = next(
                (
                    candidate
                    for candidate in tokens
                    if candidate not in selected
                    and usable(candidate)
                    and _is_degree_token(candidate)
                ),
                None,
            )
            if replacement is None:
                continue
            distinct_sources = {token.sentence_id for token in selected}
            removable = next(
                (
                    candidate
                    for candidate in reversed(selected)
                    if not _is_degree_token(candidate)
                    and (
                        selected_per_source[candidate.sentence_id] > 1 or len(distinct_sources) > 4
                    )
                ),
                None,
            )
            if removable is None:
                continue
            selected.remove(removable)
            selected_per_source[removable.sentence_id] -= 1
            selected.append(replacement)
            selected_per_source[replacement.sentence_id] += 1
            selected_degree_count += 1
            if selected_degree_count >= 2:
                break
    minimum_sources = (
        target_units
        if activity_type == "error-correction"
        else min(4, target_units)
        if activity_type in {"cloze", "match-up", "mark-the-words"}
        else target_units
    )
    if (
        len(selected) != target_units
        or len({token.sentence_id for token in selected}) < minimum_sources
    ):
        return None
    if activity_type in {"cloze", "mark-the-words"}:
        selected.sort(
            key=lambda token: (
                int(token.sentence_id.removeprefix("s-")),
                token.start_offset,
            )
        )
    if focus_mode == _FOCUS_REINFORCEMENT and sum(map(_is_degree_token, selected)) < 2:
        return None
    if activity_type == "error-correction" and (
        len(selected_error_classes) < 3 or selected_initial_errors > 4
    ):
        return None
    if (
        activity_type == "text-questions"
        and focus_mode == "anchor-comprehension"
        and sum(map(_is_comparison_form, selected)) < 3
    ):
        return None
    if activity_type == "match-up":
        if used_match_left is not None:
            used_match_left.update(match_left)
        if used_match_right is not None:
            used_match_right.update(match_right)
    used_token_ids.update(token.token_id for token in selected)
    selected_sentence_ids = {token.sentence_id for token in selected}
    # Atlas pairs are kit-anchored; their source sentence is citation
    # provenance, not evidence capacity. This mirrors `_source_evidence_ids`
    # in the exact-cover allocator instead of rejecting lessons that the
    # certified plans can safely allocate.
    if consumes_evidence_capacity:
        sentence_group_uses.update({sentence_id: 1 for sentence_id in selected_sentence_ids})
    sentence_activity_uses.setdefault(activity_type, set()).update(selected_sentence_ids)
    if consumes_evidence_capacity:
        for sentence_id in selected_sentence_ids:
            sentence_operation_uses.setdefault(sentence_id, set()).add(operation)
            if phase is not None:
                sentence_phase_uses.setdefault(sentence_id, set()).add(phase)
    return tuple(selected)


def _writing_group(
    sentences: Sequence[AnchorSentence],
    *,
    used_token_ids: set[str],
    sentence_group_uses: Counter[str],
    sentence_operation_uses: dict[str, set[str]],
    sentence_phase_uses: dict[str, set[int]],
    phase: int | None,
    focus: str | None,
) -> tuple[AnchorToken, ...] | None:
    """Reserve the single source unit that short-writing actually certifies."""
    operation = COGNITIVE_OPERATION.get("short-writing", "short-writing")
    if degree_focus_requested(focus):
        degree_candidates: list[tuple[AnchorSentence, tuple[AnchorToken, ...]]] = []
        for sentence in sentences:
            if sentence_group_uses[
                sentence.sentence_id
            ] >= 2 or operation in sentence_operation_uses.get(sentence.sentence_id, set()):
                continue
            tokens = tuple(
                token
                for token in _eligible_tokens(
                    "short-writing",
                    sentence,
                    focus_mode=_FOCUS_PRIMARY,
                )
                if token.token_id not in used_token_ids
            )
            unique_lemmas = {
                ladder.positive
                for token in tokens
                if (ladder := _degree_ladder_for_token(token)) is not None
            }
            if set(DEGREE_WRITING_LEMMAS) <= unique_lemmas:
                degree_candidates.append((sentence, tokens))
        if not degree_candidates:
            return None
        degree_candidates.sort(
            key=lambda row: (
                sentence_group_uses[row[0].sentence_id],
                int(row[0].sentence_id.removeprefix("s-")),
            )
        )
        sentence, tokens = degree_candidates[0]
        by_lemma = {
            ladder.positive: token
            for token in tokens
            if (ladder := _degree_ladder_for_token(token)) is not None
        }
        selected = [by_lemma[lemma] for lemma in DEGREE_WRITING_LEMMAS]
        used_token_ids.update(token.token_id for token in selected)
        sentence_group_uses[sentence.sentence_id] += 1
        sentence_operation_uses.setdefault(sentence.sentence_id, set()).add(operation)
        if phase is not None:
            sentence_phase_uses.setdefault(sentence.sentence_id, set()).add(phase)
        return tuple(selected)
    candidates: list[tuple[AnchorSentence, AnchorToken]] = []
    for sentence in sentences:
        if sentence_group_uses[
            sentence.sentence_id
        ] >= 2 or operation in sentence_operation_uses.get(sentence.sentence_id, set()):
            continue
        if (
            _ANAPHORIC_WRITING_OPENING_RE.search(sentence.text)
            or len(sentence.tokens) < 6
            or sentence.text.endswith("!")
        ):
            continue
        token = next(
            iter(
                sorted(
                    (
                        item
                        for item in _eligible_tokens(
                            "short-writing",
                            sentence,
                            focus_mode=(_FOCUS_PRIMARY if degree_focus_requested(focus) else None),
                        )
                        if item.token_id not in used_token_ids
                    ),
                    key=lambda item: _focus_rank(item, focus),
                )
            ),
            None,
        )
        if token is not None:
            candidates.append((sentence, token))
    if not candidates:
        return None
    candidates.sort(
        key=lambda row: (
            sentence_group_uses[row[0].sentence_id],
            _focus_rank(row[1], focus),
        )
    )
    sentence, token = candidates[0]
    used_token_ids.add(token.token_id)
    sentence_group_uses[sentence.sentence_id] += 1
    sentence_operation_uses.setdefault(sentence.sentence_id, set()).add(operation)
    if phase is not None:
        sentence_phase_uses.setdefault(sentence.sentence_id, set()).add(phase)
    return (token,)


def inventory_from_anchor(
    anchor: str,
    *,
    scheduled_types: Sequence[str],
    replacement_types: Sequence[str] = (),
    duration_minutes: int = 45,
    focus: str | None = None,
    receipt_context: BankReceiptContext | None = None,
    receipt_sink: ReceiptSink | None = None,
) -> CertificationInventory:
    """Build only literal-evidence candidates required by one v3 lesson shape.

    The result may be incomplete.  That is an expected preflight outcome: the
    allocator will return ``insufficient_anchor_capacity`` before any model
    call rather than lowering a type's floor or inventing source material.
    """
    if (receipt_context is None) != (receipt_sink is None):
        raise ValueError("Candidate-bank receipt capture requires context and sink together.")
    sentences = _sentences(anchor)
    writing_ranges = {45: (60, 80), 60: (80, 110), 90: (120, 160)}
    if duration_minutes not in writing_ranges:
        raise ValueError("v3 anchor inventory needs a supported lesson duration.")
    by_id = {sentence.sentence_id: sentence for sentence in sentences}
    match_pairs = _atlas_gloss_pairs(sentences)
    if not match_pairs:
        # Minimal synthetic/test bundles predate Atlas gloss payloads.  Keep
        # their already-certified semantic relations as a compatibility lane;
        # production bundles with any usable B1+ gloss never enter it.
        match_pairs = _atlas_semantic_pairs(sentences)
    required = Counter((*scheduled_types, *replacement_types))
    shape = phase_shape_for(duration_minutes)
    primary_slots = tuple(
        (phase, f"P{phase}-A{position}")
        for phase, count in sorted(shape.phase_slots.items())
        for position in range(1, count + 1)
    )
    scheduled_lane = (
        tuple(
            (activity_type, phase, slot_id)
            for activity_type, (phase, slot_id) in zip(scheduled_types, primary_slots, strict=True)
        )
        if len(primary_slots) == len(scheduled_types)
        else tuple((activity_type, None, None) for activity_type in scheduled_types)
    )
    source_comprehension_45 = (
        duration_minutes == 45 and tuple(scheduled_types) == _SOURCE_COMPREHENSION_45
    )
    groups_by_type: dict[str, list[Sequence[AnchorToken]]] = defaultdict(list)
    group_numbers_by_type: dict[str, list[int]] = defaultdict(list)
    group_focus_modes: dict[str, list[str | None]] = defaultdict(list)
    prebuilt_candidates: dict[tuple[str, int], tuple[EvidenceCandidate, ...]] = {}
    primary_occurrences = Counter(
        activity_type for activity_type, _phase, _slot_id in scheduled_lane
    )
    replacement_lane = (
        tuple(dict.fromkeys(replacement_types))
        if source_comprehension_45
        else tuple(replacement_types)
    )
    lanes = (
        scheduled_lane,
        # The narrative replacements are mutually exclusive alternatives for
        # two Phase-2 primaries. Certify one shared spare board and let the
        # slot adapter bind either occurrence to it; exact cover still forbids
        # both slots from selecting the same claims. Other lesson shapes keep
        # independent replacement occurrences.
        tuple((activity_type, 2, None) for activity_type in replacement_lane),
    )
    primary_state: (
        tuple[
            set[str],
            Counter[str],
            dict[str, set[str]],
            dict[str, set[int]],
            dict[str, set[str]],
        ]
        | None
    ) = None
    for lane_index, lane in enumerate(lanes):
        if lane_index == 0 or primary_state is None:
            used_token_ids: set[str] = set()
            sentence_group_uses: Counter[str] = Counter()
            sentence_operation_uses: dict[str, set[str]] = {}
            sentence_phase_uses: dict[str, set[int]] = {}
            sentence_activity_uses: dict[str, set[str]] = {}
        else:
            used_token_ids = set(primary_state[0])
            sentence_group_uses = Counter(primary_state[1])
            # A replacement is mutually exclusive only with its own primary;
            # it still has to coexist with every other scheduled slot.  Keep
            # the primary lane's operation reservations so inventory cannot
            # certify a fallback that exact cover must reject later.
            sentence_operation_uses = {key: set(value) for key, value in primary_state[2].items()}
            sentence_phase_uses = {key: set(value) for key, value in primary_state[3].items()}
            sentence_activity_uses = {key: set(value) for key, value in primary_state[4].items()}
        occurrences = Counter(activity_type for activity_type, _phase, _slot_id in lane)
        semantic_token_ids: set[str] = set()
        semantic_left: set[str] = set()
        semantic_right: set[str] = set()
        # Reserve repeated and source-scarce activity groups before broad
        # token-selection types consume their eligible sentences.  Group IDs
        # remain per-type and the lesson slot order is unchanged.
        annotated_lane: list[tuple[str, int | None, int, str | None]] = []
        type_occurrences: Counter[str] = Counter()
        for activity_type, phase, slot_id in lane:
            type_occurrences[activity_type] += 1
            annotated_lane.append((activity_type, phase, type_occurrences[activity_type], slot_id))

        def planning_priority(
            activity_type: str,
            occurrence: int,
            slot_id: str | None,
            _lane_index: int = lane_index,
        ) -> int:
            if source_comprehension_45 and _lane_index == 0 and activity_type == "text-questions":
                # Choose literal questions only after all language-drill
                # carriers have consumed capacity. This makes unused source
                # propositions the deterministic first choice.
                return 9
            if _lane_index == 0 and degree_focus_requested(focus):
                role = degree_role(slot_id, activity_type) if slot_id is not None else None
                if role in {
                    "degree-formation",
                    "degree-comparison-syntax",
                    "degree-context",
                    "degree-error-correction",
                    "degree-positive-comparative",
                    "degree-comparative-superlative",
                }:
                    return 0
                if role == "degree-recognition":
                    return 1
                if role == "degree-cloze":
                    return 2
                if activity_type == "short-writing":
                    return 4
                if occurrence == 1 and activity_type in {"quiz", "cloze"}:
                    return 1
                if activity_type == "fill-in" and occurrence > 1:
                    return 3
            return {
                # Cloze needs non-adjacent target positions within each source
                # sentence, so reserve its scarcer carrier capacity before the
                # broad token-selection activities.
                "cloze": 2,
                "mark-the-words": 3,
                # Preserve the scarce explicit-causal carriers before the
                # broadly eligible error-correction lane chooses sentences.
                "text-questions": 4,
                "quiz": 5,
                "fill-in": 6,
                "error-correction": 7,
                "match-up": 8,
                "true-false": 9,
            }.get(activity_type, 10)

        planning_lane = tuple(
            (activity_type, phase, occurrence, slot_id)
            for original_index, (activity_type, phase, occurrence, slot_id) in sorted(
                enumerate(annotated_lane),
                key=lambda row: (
                    planning_priority(row[1][0], row[1][2], row[1][3]),
                    sum(
                        bool(_eligible_tokens(row[1][0], sentence, match_pairs=match_pairs))
                        for sentence in sentences
                    ),
                    -occurrences[row[1][0]],
                    row[0],
                ),
            )
        )
        for index, (activity_type, phase, occurrence, slot_id) in enumerate(planning_lane):
            if activity_type == "short-writing" and groups_by_type[activity_type]:
                continue
            focus_mode = None
            if source_comprehension_45 and lane_index == 0 and activity_type == "quiz":
                focus_mode = "source-comprehension"
            elif source_comprehension_45 and lane_index == 0 and activity_type == "text-questions":
                focus_mode = "source-comprehension"
            elif lane_index == 0 and degree_focus_requested(focus):
                focus_mode = degree_role(slot_id, activity_type) if slot_id is not None else None
                if focus_mode is not None:
                    pass
                elif activity_type in {"quiz", "cloze", "mark-the-words"} and occurrence == 1:
                    focus_mode = _FOCUS_PRIMARY
                elif activity_type == "fill-in" and occurrence > 1:
                    focus_mode = _FOCUS_REINFORCEMENT
                elif activity_type == "short-writing":
                    focus_mode = _FOCUS_WRITING
            group_number = (
                occurrence if lane_index == 0 else primary_occurrences[activity_type] + occurrence
            )
            kit_candidates = (
                _degree_kit_candidates(
                    sentences,
                    activity_type=activity_type,
                    role=focus_mode,
                    group_number=group_number,
                )
                if focus_mode
                in {
                    "degree-recognition",
                    "degree-cloze",
                    "degree-formation",
                    "degree-comparison-syntax",
                    "degree-context",
                    "degree-error-correction",
                }
                else ()
            )
            selection_token_ids = (
                set()
                if activity_type
                in {
                    "mark-the-words",
                    "text-questions",
                    # A dependency-diagnosis item may intentionally revisit a
                    # form tested earlier, while sentence uniqueness prevents
                    # correction-board padding.
                    "error-correction",
                }
                or focus_mode in {"degree-positive-comparative", "degree-comparative-superlative"}
                else semantic_token_ids
                if activity_type == "match-up"
                else used_token_ids
            )
            active_match_pairs = (
                _degree_match_pairs(sentences, focus_mode)
                if focus_mode in {"degree-positive-comparative", "degree-comparative-superlative"}
                else match_pairs
            )
            match_left = (
                set()
                if focus_mode in {"degree-positive-comparative", "degree-comparative-superlative"}
                else semantic_left
            )
            match_right = (
                set()
                if focus_mode in {"degree-positive-comparative", "degree-comparative-superlative"}
                else semantic_right
            )
            group = (
                tuple(
                    next(
                        token
                        for sentence in sentences
                        for token in sentence.tokens
                        if token.token_id == candidate.token_id
                    )
                    for candidate in kit_candidates
                )
                if kit_candidates
                else _writing_group(
                    sentences,
                    used_token_ids=selection_token_ids,
                    sentence_group_uses=sentence_group_uses,
                    sentence_operation_uses=sentence_operation_uses,
                    sentence_phase_uses=sentence_phase_uses,
                    phase=phase,
                    focus=focus,
                )
                if activity_type == "short-writing"
                else _cloze_group(
                    sentences,
                    used_token_ids=selection_token_ids,
                )
                if activity_type == "cloze" and source_comprehension_45
                else _diverse_group(
                    sentences,
                    activity_type=activity_type,
                    used_token_ids=selection_token_ids,
                    sentence_group_uses=sentence_group_uses,
                    sentence_operation_uses=sentence_operation_uses,
                    sentence_phase_uses=sentence_phase_uses,
                    sentence_activity_uses=sentence_activity_uses,
                    slot_id=slot_id if source_comprehension_45 else None,
                    phase=phase,
                    remaining_groups=len(planning_lane) - index,
                    focus=focus,
                    focus_mode=focus_mode,
                    match_pairs=active_match_pairs,
                    prefer_reuse=focus_mode == _FOCUS_REINFORCEMENT,
                    used_match_left=match_left,
                    used_match_right=match_right,
                )
            )
            if group is not None:
                if isinstance(group, ClozeGroup):
                    # The final passage may reuse propositions from earlier
                    # phases, but never the exact token already reserved as a
                    # scored cloze gap.
                    selection_token_ids.update(token.token_id for token in group)
                groups_by_type[activity_type].append(group)
                group_numbers_by_type[activity_type].append(group_number)
                group_focus_modes[activity_type].append(focus_mode)
                if kit_candidates:
                    prebuilt_candidates[(activity_type, group_number)] = kit_candidates
        if lane_index == 0:
            primary_state = (
                set(used_token_ids),
                Counter(sentence_group_uses),
                {key: set(value) for key, value in sentence_operation_uses.items()},
                {key: set(value) for key, value in sentence_phase_uses.items()},
                {key: set(value) for key, value in sentence_activity_uses.items()},
            )
    candidates: list[EvidenceCandidate] = []
    true_false_facts: list[TrueFalseFact] = []
    atlas_pairs: list[AtlasPassPair] = []
    mark_requests: list[MarkTheWordsRequest] = []
    writing_tasks: list[ShortWritingTask] = []
    for activity_type, _count in sorted(required.items()):
        if activity_type not in _LIST_TYPES | {"short-writing"}:
            continue
        type_groups = groups_by_type.get(activity_type, ())
        group_rows = sorted(
            zip(
                group_numbers_by_type.get(activity_type, ()),
                type_groups,
                group_focus_modes.get(activity_type, ()),
                strict=True,
            ),
            key=lambda row: row[0],
        )
        for group_number, group, focus_mode in group_rows:
            if activity_type == "text-questions" and focus_mode == "source-comprehension":
                # Question semantics are assigned against the mandatory
                # primary lane. Exactly three fresh propositions are protected
                # as literal comprehension in exact cover; the remaining two
                # are interpretive/application overlays. A replacement may
                # overlap only those overlays, never the protected facts.
                assert primary_state is not None
                drilled_sentence_ids = set().union(
                    *(
                        primary_state[4].get(drill_type, set())
                        for drill_type in ("quiz", "cloze", "fill-in")
                    )
                )
                question_candidates: list[EvidenceCandidate] = []
                for candidate_number, token in enumerate(group, start=1):
                    sentence = by_id[token.sentence_id]
                    topic = _topic_token(
                        sentence,
                        excluded_lemmas=_UNSAFE_QUESTION_TOPIC_LEMMAS,
                    )
                    if topic is None:
                        question_candidates = []
                        break
                    already_drilled = sentence.sentence_id in drilled_sentence_ids
                    literal_recovery = (
                        not already_drilled
                        and sum(
                            candidate.category == "comprehension"
                            for candidate in question_candidates
                        )
                        < TEXT_QUESTION_COMPREHENSION_FLOOR.comprehension
                    )
                    question_candidates.append(
                        _source_question_candidate(
                            sentence,
                            candidate_number=candidate_number,
                            category=(
                                "comprehension" if literal_recovery else "anchored_application"
                            ),
                            intent=(
                                "fact-recovery" if literal_recovery else "anchored-application.v1"
                            ),
                            answer_start=0,
                            answer_end=len(sentence.text),
                            topic_token=topic[0],
                            topic_lemma=topic[1],
                            focus_alignment="source-comprehension",
                        )
                    )
                if (
                    len(question_candidates) != floor_for("text-questions").minimum_units
                    or sum(
                        candidate.category == "comprehension" for candidate in question_candidates
                    )
                    < TEXT_QUESTION_COMPREHENSION_FLOOR.comprehension
                ):
                    continue
                prebuilt_candidates[(activity_type, group_number)] = tuple(question_candidates)
            ready_candidates = prebuilt_candidates.get((activity_type, group_number))
            if ready_candidates is not None:
                candidates.extend(ready_candidates)
                continue
            cloze_passage = None
            cloze_sentence_starts: dict[str, int] = {}
            cloze_banks = None
            if activity_type == "cloze" and group:
                cloze_banks = _cross_gap_choice_banks(group, sentences=by_id)
                if cloze_banks is None:
                    continue
                if isinstance(group, ClozeGroup):
                    cloze_sentences = group.sentences
                else:
                    sentence_numbers = sorted(
                        {int(token.sentence_id.removeprefix("s-")) for token in group}
                    )
                    cloze_sentences = tuple(by_id[f"s-{index}"] for index in sentence_numbers)
                cursor = 0
                for passage_sentence in cloze_sentences:
                    cloze_sentence_starts[passage_sentence.sentence_id] = cursor
                    cursor += len(passage_sentence.text) + 1
                cloze_passage = " ".join(sentence.text for sentence in cloze_sentences)
                if (
                    cloze_passage.count("«") != cloze_passage.count("»")
                    or cloze_passage.count("(") != cloze_passage.count(")")
                    or cloze_passage.count("[") != cloze_passage.count("]")
                ):
                    continue
            source_quiz_sentences = (
                tuple(by_id[token.sentence_id] for token in group)
                if activity_type == "quiz" and focus_mode == "source-comprehension"
                else ()
            )
            for token_number, token in enumerate(group, start=1):
                sentence = by_id[token.sentence_id]
                candidate_id = f"{activity_type}:{group_number}:{token_number}"
                if activity_type == "quiz" and focus_mode == "source-comprehension":
                    topic = _topic_token(
                        sentence,
                        excluded_lemmas=_UNSAFE_QUESTION_TOPIC_LEMMAS,
                    )
                    distractors = tuple(
                        candidate.text
                        for candidate in (
                            *source_quiz_sentences[token_number:],
                            *source_quiz_sentences[: token_number - 1],
                        )
                        if candidate.sentence_id != sentence.sentence_id
                    )[:2]
                    if topic is None or len(distractors) != 2:
                        continue
                    candidates.append(
                        EvidenceCandidate(
                            activity_type="quiz",
                            candidate_id=candidate_id,
                            sentence_id=sentence.sentence_id,
                            token_id=topic[0].token_id,
                            literal_evidence=sentence.text,
                            expected_key=sentence.text,
                            semantic_target=f"source-quiz:{sentence.sentence_id}",
                            category="comprehension",
                            focus_alignment="source-comprehension",
                            source_lemma=topic[1],
                            choice_bank=(sentence.text, *distractors),
                            exclusion_warrants=tuple(
                                (
                                    distractor,
                                    "different certified source proposition",
                                )
                                for distractor in distractors
                            ),
                            question_intent="fact-recovery",
                            topic_token_id=topic[0].token_id,
                            topic_lemma=topic[1],
                            question_basis="source-proposition",
                            question_grounding_terms=(topic[0].surface,),
                        )
                    )
                elif activity_type in {"quiz", "cloze", "fill-in", "error-correction"}:
                    bank_row = (
                        cloze_banks.get(token.token_id)
                        if activity_type == "cloze" and cloze_banks is not None
                        else _visible_carrier_choice_bank(sentence, token)
                        if activity_type in {"quiz", "fill-in"}
                        else None
                    )
                    replacement_row = (
                        _error_replacement(sentence, token)
                        if activity_type == "error-correction"
                        else None
                    )
                    replacement = replacement_row[0] if replacement_row is not None else None
                    morphology_class = replacement_row[1] if replacement_row is not None else None
                    semantic_warrant = replacement_row[2] if replacement_row is not None else None
                    derived = (
                        sentence.text[: token.start_offset]
                        + replacement
                        + sentence.text[token.end_offset :]
                        if replacement is not None and replacement != token.surface
                        else None
                    )
                    cloze_lexical_bank = bool(
                        activity_type == "cloze"
                        and bank_row is not None
                        and set(bank_row[0])
                        <= {candidate_token.surface for candidate_token in group}
                    )
                    candidates.append(
                        EvidenceCandidate(
                            activity_type=activity_type,
                            candidate_id=candidate_id,
                            sentence_id=sentence.sentence_id,
                            token_id=token.token_id,
                            literal_evidence=sentence.text,
                            expected_key=token.surface,
                            semantic_target=f"{activity_type}:{token.token_id}",
                            certified_error_count=1 if derived is not None else 0,
                            derived_surface=derived,
                            focus_alignment=(
                                focus_mode
                                if focus_mode in _DEGREE_LIST_ROLES
                                or focus_mode == _FOCUS_PRIMARY
                                or (focus_mode == _FOCUS_REINFORCEMENT and _is_degree_token(token))
                                else None
                            ),
                            rendering_surface=cloze_passage,
                            target_start_offset=(
                                (cloze_sentence_starts[sentence.sentence_id] + token.start_offset)
                                if activity_type == "cloze" and cloze_passage is not None
                                else token.start_offset
                                if activity_type in {"quiz", "fill-in", "error-correction"}
                                else None
                            ),
                            target_end_offset=(
                                cloze_sentence_starts[sentence.sentence_id] + token.end_offset
                                if activity_type == "cloze" and cloze_passage is not None
                                else token.end_offset
                                if activity_type in {"quiz", "fill-in", "error-correction"}
                                else None
                            ),
                            morphology_class=morphology_class,
                            frame_family=(
                                "contextual-mismatch.v1"
                                if activity_type == "error-correction"
                                else (
                                    "cross-gap-lexical.v1"
                                    if data.active_bundle().manifest.get("version") == "test"
                                    and len(group) < 18
                                    else "source-context-lexical.v1"
                                    if cloze_lexical_bank
                                    else "contextual-morphology-cloze.v3"
                                )
                                if activity_type == "cloze"
                                else None
                            ),
                            semantic_warrant=semantic_warrant,
                            source_lemma=_content_lemma(token),
                            choice_bank=bank_row[0] if bank_row is not None else (),
                            exclusion_warrants=bank_row[1] if bank_row is not None else (),
                        )
                    )
                elif activity_type == "text-questions":
                    category = _TEXT_QUESTION_CATEGORIES[token_number - 1]
                    intent = _TEXT_QUESTION_INTENTS[token_number - 1]
                    candidates.append(
                        EvidenceCandidate(
                            activity_type=activity_type,
                            candidate_id=candidate_id,
                            sentence_id=sentence.sentence_id,
                            token_id=token.token_id,
                            literal_evidence=sentence.text,
                            expected_key=sentence.text,
                            semantic_target=(f"{activity_type}:{category}:{sentence.sentence_id}"),
                            category=category,
                            question_intent=intent,
                            semantic_warrant=(
                                "source carrier contains an explicit causal connective"
                                if intent == "explicit-causal"
                                else "source carrier states the fact to recover"
                                if intent == "fact-recovery"
                                else "source carrier anchors one realistic transfer prompt"
                            ),
                            focus_alignment=(
                                "anchor-comprehension"
                                if focus_mode == "anchor-comprehension"
                                else None
                            ),
                        )
                    )
                elif activity_type == "true-false":
                    # Generic predicate negation is not a meaningful comprehension
                    # task. Production true/false remains unavailable until a
                    # source-backed semantic mutation catalog exists.
                    continue
                elif activity_type == "match-up":
                    pair_source = (
                        _degree_match_pairs(sentences, focus_mode)
                        if focus_mode
                        in {"degree-positive-comparative", "degree-comparative-superlative"}
                        else match_pairs
                    )
                    pair = pair_source.get(token.token_id)
                    if pair is None:
                        continue
                    atlas_pairs.append(
                        AtlasPassPair(
                            pair_id=candidate_id,
                            sentence_id=sentence.sentence_id,
                            left=pair[0],
                            right=pair[1],
                            literal_evidence=sentence.text,
                            atlas_pass=True,
                            relation=pair[2],
                        )
                    )
            if activity_type == "short-writing":
                task_sentence = by_id[group[0].sentence_id]
                lemmas: list[str] = []
                for token in group:
                    ladder = _degree_ladder_for_token(token)
                    identity = _unambiguous_content_lemma_pos(token)
                    lemma = (
                        ladder.positive
                        if ladder is not None
                        else identity[0]
                        if identity is not None
                        else None
                    )
                    if lemma is not None and lemma not in lemmas:
                        lemmas.append(lemma)
                degree_writing = focus_mode in {_FOCUS_WRITING, "degree-writing"}
                if (degree_writing and lemmas) or (not degree_writing and task_sentence.tokens):
                    minimum, maximum = writing_ranges[duration_minutes]
                    writing_sample = group if degree_writing else task_sentence.tokens
                    writing_tasks.append(
                        ShortWritingTask(
                            task_id=f"short-writing:{group_number}",
                            sentence_id=task_sentence.sentence_id,
                            prompt=(
                                DEGREE_WRITING_SCENARIO if degree_writing else task_sentence.text
                            ),
                            constraints=(
                                (
                                    ConstraintSpec(
                                        "contains_lemma_set",
                                        {"lemmas": tuple(DEGREE_WRITING_LEMMAS)},
                                    ),
                                    ConstraintSpec(
                                        "word_count_range",
                                        {"minimum": minimum, "maximum": maximum},
                                    ),
                                )
                                if degree_writing
                                else (
                                    ConstraintSpec(
                                        "source_proposition", {"text": task_sentence.text}
                                    ),
                                    ConstraintSpec(
                                        "word_count_range",
                                        {"minimum": minimum, "maximum": maximum},
                                    ),
                                )
                            ),
                            sample_tokens=tuple(
                                VesumToken(
                                    surface=token.surface,
                                    start_offset=token.start_offset,
                                    end_offset=token.end_offset,
                                    parses=token.vesum_parses,
                                )
                                for token in writing_sample
                            ),
                            focus_alignment=(_FOCUS_WRITING if degree_writing else None),
                            attribute_warrants=(DEGREE_WRITING_WARRANTS if degree_writing else ()),
                        )
                    )
            elif activity_type == "mark-the-words":
                sentence_indexes = sorted(
                    int(token.sentence_id.removeprefix("s-")) for token in group
                )
                mark_requests.append(
                    MarkTheWordsRequest(
                        request_id=f"mark-the-words:{group_number}",
                        sentence_ids=tuple(
                            f"s-{index}"
                            for index in range(sentence_indexes[0], sentence_indexes[-1] + 1)
                        ),
                        criterion=("degree=comparison" if focus_mode is not None else "pos=noun"),
                        target_token_ids=tuple(token.token_id for token in group),
                    )
                )
    if source_comprehension_45 and "true-false" in replacement_lane and primary_state is not None:
        # The fallback is a complete, source-bound selected-response bank. It
        # is generated from carriers not already consumed by the phase-1 quiz
        # or phase-2 drills, so exact-cover can substitute it without sharing
        # an evidence operation with the remaining lesson.
        true_false_facts.extend(
            _balanced_true_false(
                sentences,
                sentence_group_uses=Counter(primary_state[1]),
                sentence_phase_uses={key: set(value) for key, value in primary_state[3].items()},
                phase=1,
            )
        )
    inventory = CertificationInventory(
        source_id=_source_id(anchor),
        sentences=sentences,
        candidates=tuple(candidates),
        true_false_facts=tuple(true_false_facts),
        atlas_pairs=tuple(atlas_pairs),
        mark_requests=tuple(mark_requests),
        writing_tasks=tuple(writing_tasks),
    )
    # Receipt issue is an explicit qualification-only creation hook.  It sees
    # the complete ordered inventory before callers can request a bank.
    if receipt_context is not None and receipt_sink is not None:
        receipt_sink(
            receipts_for_inventory(
                inventory,
                context=receipt_context,
                seal=seal_complete_inventory(inventory, context=receipt_context),
            )
        )
    return inventory


def inventory_for_group(
    inventory: CertificationInventory, *, activity_type: str, group_number: int
) -> CertificationInventory:
    """Return one slot's literal resource group without weakening its floor.

    Repeated lesson types receive different preselected source groups.  Feeding
    only that group to the existing pure builder keeps the exact-cover search
    bounded and makes every slot's resource claims explicit before generation.
    """
    prefix = f"{activity_type}:{group_number}:"
    task_id = f"{activity_type}:{group_number}"
    return replace(
        inventory,
        candidates=tuple(
            candidate
            for candidate in inventory.candidates
            if candidate.activity_type != activity_type or candidate.candidate_id.startswith(prefix)
        ),
        true_false_facts=tuple(
            fact
            for fact in inventory.true_false_facts
            if activity_type != "true-false" or fact.fact_id.startswith(prefix)
        ),
        atlas_pairs=tuple(
            pair
            for pair in inventory.atlas_pairs
            if activity_type != "match-up" or pair.pair_id.startswith(prefix)
        ),
        mark_requests=tuple(
            request
            for request in inventory.mark_requests
            if activity_type != "mark-the-words" or request.request_id == task_id
        ),
        writing_tasks=tuple(
            task
            for task in inventory.writing_tasks
            if activity_type != "short-writing" or task.task_id == task_id
        ),
    )
