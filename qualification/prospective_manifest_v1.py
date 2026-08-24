"""Frozen, source-text-only manifest for the Hramatka #552 prospective sample.

This module deliberately knows nothing about production proof certificates or
the engine data release.  It reads the source SQLite database read-only and
turns its frozen Wikipedia rows into an immutable manifest before inventory
construction can begin.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Final

VERSION: Final = "HramatkaProspectiveManifest.v1"
SELECTION_SEED: Final = "hramatka-552-wikipedia-v1"
DATABASE_DIGEST: Final = "6c400c911fa5930175e64a1d33468112edd87ed73d610812fcf4a3c7aab3361e"
RIGHTS_RECEIPT_DIGEST: Final = "1359e2d2067795c4246be93cb4187d708b22a9d7af406e089d5ac087095c09d4"
SELECTED_ID_DIGEST: Final = "ab7c4e3f863b0bef6584a4008dabff4ba0721b372180e7cba331d9259a66faca"
POPULATION_PACKET_DIGEST: Final = "9ead4c4814aee4c01dc349c5016a7856b1512945925c51d0886f744efbf1bb76"
ELIGIBLE_COUNT: Final = 917
SELECTED_COUNT: Final = 94
UNSELECTED_ELIGIBLE_COUNT: Final = 823
EXPECTED_SELECTED_IDS: Final = (
    372,
    994,
    982,
    51,
    427,
    231,
    140,
    820,
    103,
    265,
    15,
    742,
    887,
    872,
    70,
    349,
    137,
    153,
    531,
    389,
    715,
    76,
    160,
    568,
    424,
    490,
    950,
    224,
    77,
    627,
    192,
    945,
    790,
    799,
    893,
    908,
    465,
    916,
    670,
    858,
    630,
    704,
    942,
    401,
    829,
    259,
    289,
    607,
    314,
    291,
    841,
    714,
    61,
    769,
    46,
    373,
    757,
    561,
    831,
    759,
    100,
    88,
    262,
    633,
    610,
    1006,
    560,
    793,
    606,
    702,
    254,
    423,
    536,
    948,
    648,
    508,
    16,
    595,
    691,
    619,
    696,
    304,
    727,
    208,
    815,
    57,
    861,
    600,
    798,
    232,
    114,
    228,
    133,
    642,
)
_ASCII_WHITESPACE: Final = re.compile(r"[ \t\n\r\f\v]+")
_SHA256: Final = re.compile(r"^[a-f0-9]{64}$")
_BURNED_TEXT_DIGESTS: Final = frozenset(
    {
        "f985bf674c8904acaf15dc157ab9b49e84092b385b5b5ab2c240a844f7b8d800",
        "b5df2b0ec36e42537da8f007e135855fe33d7ea33cccf4fd4cef4a0c46485d4d",
        "4a5c83aa4515d7914a60814916df8d3aa84ec1b1923997cc934f76ab2faed47a",
    }
)


class ProspectiveManifestError(ValueError):
    """The frozen source authority does not reproduce the preregistration."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_guard() -> None:
    raw = (
        files(__package__).joinpath("assets", "prospective-wikipedia-v1.manifest.json").read_bytes()
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProspectiveManifestError("Prospective asset is not JSON.") from exc
    if not isinstance(value, dict):
        raise ProspectiveManifestError("Prospective asset is not an object.")
    expected = {
        "version": "HramatkaProspectiveManifestAsset.v1",
        "database_sha256": DATABASE_DIGEST,
        "rights_receipt_sha256": RIGHTS_RECEIPT_DIGEST,
        "eligible_count": ELIGIBLE_COUNT,
        "selected_count": SELECTED_COUNT,
        "selected_id_sha256": SELECTED_ID_DIGEST,
        "selected_sqlite_ids": list(EXPECTED_SELECTED_IDS),
        "population_packet_sha256": POPULATION_PACKET_DIGEST,
        "selection": "smallest-sha256(utf8(seed)||nul||utf8(exact-stored-url));tie=sqlite-id",
        "seed": SELECTION_SEED,
    }
    if value != expected:
        raise ProspectiveManifestError("Prospective asset authority has drifted.")


def extract_source_text(text: str) -> str:
    """Apply the packet's ASCII-whitespace-only, 600-token extraction rule."""
    if not isinstance(text, str):
        raise ProspectiveManifestError("Wikipedia text must be a string.")
    return _extract(text)


def _extract(text: str) -> str:
    # Kept separate to make the exact split/join rule visually auditable.
    return " ".join(tuple(token for token in _ASCII_WHITESPACE.split(text) if token)[:600])


@dataclass(frozen=True)
class ProspectiveSource:
    sqlite_id: int
    title: str
    url: str
    fetched_at: str
    database_text_sha256: str
    extracted_text_sha256: str
    extracted_token_count: int
    selection_rank: int

    def __post_init__(self) -> None:
        if type(self.sqlite_id) is not int or self.sqlite_id < 1:
            raise ProspectiveManifestError("Prospective source SQLite ID is invalid.")
        if type(self.selection_rank) is not int or not 1 <= self.selection_rank <= SELECTED_COUNT:
            raise ProspectiveManifestError("Prospective source selection rank is invalid.")
        if not all(
            isinstance(value, str) and value
            for value in (self.title, self.url, self.fetched_at)
        ):
            raise ProspectiveManifestError("Prospective source metadata is invalid.")
        if any(
            _SHA256.fullmatch(value) is None
            for value in (self.database_text_sha256, self.extracted_text_sha256)
        ):
            raise ProspectiveManifestError("Prospective source digest is invalid.")
        if (
            type(self.extracted_token_count) is not int
            or not 1 <= self.extracted_token_count <= 600
        ):
            raise ProspectiveManifestError("Prospective source token count is invalid.")

    def to_dict(self) -> dict[str, object]:
        return {
            "sqlite_id": self.sqlite_id,
            "title": self.title,
            "url": self.url,
            "fetched_at": self.fetched_at,
            "database_text_sha256": self.database_text_sha256,
            "extracted_text_sha256": self.extracted_text_sha256,
            "extracted_token_count": self.extracted_token_count,
            "selection_rank": self.selection_rank,
        }


@dataclass(frozen=True)
class ProspectiveManifest:
    sources: tuple[ProspectiveSource, ...]
    db_digest: str
    rights_receipt_digest: str
    selected_id_digest: str
    population_packet_digest: str = POPULATION_PACKET_DIGEST
    version: str = VERSION

    def __post_init__(self) -> None:
        if self.version != VERSION or len(self.sources) != SELECTED_COUNT:
            raise ProspectiveManifestError("Prospective manifest must contain exactly 94 sources.")
        if tuple(row.selection_rank for row in self.sources) != tuple(range(1, SELECTED_COUNT + 1)):
            raise ProspectiveManifestError(
                "Prospective manifest selection ranks are not canonical."
            )
        if (
            len({row.sqlite_id for row in self.sources}) != SELECTED_COUNT
            or len({row.url for row in self.sources}) != SELECTED_COUNT
        ):
            raise ProspectiveManifestError("Prospective manifest source identities must be unique.")
        if (
            self.db_digest != DATABASE_DIGEST
            or self.rights_receipt_digest != RIGHTS_RECEIPT_DIGEST
            or self.selected_id_digest != SELECTED_ID_DIGEST
            or self.population_packet_digest != POPULATION_PACKET_DIGEST
        ):
            raise ProspectiveManifestError("Prospective manifest packet commitments have drifted.")
        if tuple(row.sqlite_id for row in self.sources) != EXPECTED_SELECTED_IDS:
            raise ProspectiveManifestError("Prospective manifest ordered source IDs have drifted.")
        counts = tuple(row.extracted_token_count for row in self.sources)
        if min(counts) != 370 or max(counts) != 600 or sum(count < 600 for count in counts) != 8:
            raise ProspectiveManifestError("Prospective manifest token geometry has drifted.")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "db_digest": self.db_digest,
            "rights_receipt_digest": self.rights_receipt_digest,
            "selected_id_digest": self.selected_id_digest,
            "population_packet_digest": self.population_packet_digest,
            "sources": [source.to_dict() for source in self.sources],
        }

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_bytes(self.to_dict()))


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise ProspectiveManifestError("Source database must be a regular file.")
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def _schema_guard(connection: sqlite3.Connection) -> None:
    columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(wikipedia)"))
    expected = ("id", "title", "url", "text", "char_count", "fetched_at")
    if columns != expected:
        raise ProspectiveManifestError(
            "Wikipedia source schema does not match the frozen authority."
        )


def build_manifest(
    database: Path,
    rights_receipt: Path,
    *,
    expected_database_digest: str = DATABASE_DIGEST,
    expected_rights_digest: str = RIGHTS_RECEIPT_DIGEST,
    expected_eligible_count: int = ELIGIBLE_COUNT,
    expected_selected_count: int = SELECTED_COUNT,
    expected_selected_id_digest: str = SELECTED_ID_DIGEST,
) -> tuple[ProspectiveManifest, dict[int, str]]:
    """Reproduce the packet's frozen source sample before any engine inventory exists."""
    database, rights_receipt = Path(database), Path(rights_receipt)
    _asset_guard()
    if (
        _file_digest(database) != expected_database_digest
        or _file_digest(rights_receipt) != expected_rights_digest
    ):
        raise ProspectiveManifestError("Frozen database or rights receipt digest does not match.")
    connection = _open_read_only(database)
    try:
        _schema_guard(connection)
        raw_rows = tuple(
            connection.execute(
                "SELECT id, title, url, text, char_count, fetched_at FROM wikipedia ORDER BY id"
            )
        )
    finally:
        connection.close()
    eligible = []
    for row_id, title, url, text, char_count, fetched_at in raw_rows:
        if not all(
            isinstance(value, str) for value in (title, url, text, fetched_at)
        ) or not isinstance(row_id, int):
            raise ProspectiveManifestError("Wikipedia source row has invalid types.")
        text_digest = sha256_bytes(text.encode("utf-8"))
        if (
            text.strip()
            and isinstance(char_count, int)
            and char_count >= 2600
            and text_digest not in _BURNED_TEXT_DIGESTS
        ):
            eligible.append(
                (
                    sha256_bytes(SELECTION_SEED.encode("utf-8") + b"\0" + url.encode("utf-8")),
                    row_id,
                    title,
                    url,
                    text,
                    fetched_at,
                    text_digest,
                )
            )
    if len(eligible) != expected_eligible_count:
        raise ProspectiveManifestError(
            "Eligible Wikipedia population does not match preregistration."
        )
    eligible.sort(key=lambda row: (row[0], row[1]))
    selected = eligible[:expected_selected_count]
    ids = [row[1] for row in selected]
    # The frozen list digest binds exactly this ordered SQLite-ID set, not a count.
    selected_id_digest = sha256_bytes(canonical_bytes(ids))
    if selected_id_digest != expected_selected_id_digest:
        raise ProspectiveManifestError("Selected Wikipedia ID set does not match preregistration.")
    if expected_selected_id_digest == SELECTED_ID_DIGEST and tuple(ids) != EXPECTED_SELECTED_IDS:
        raise ProspectiveManifestError(
            "Selected Wikipedia ordered ID set does not match preregistration."
        )
    sources, texts = [], {}
    for rank, (_score, row_id, title, url, text, fetched_at, text_digest) in enumerate(selected, 1):
        extracted = _extract(text)
        sources.append(
            ProspectiveSource(
                row_id,
                title,
                url,
                fetched_at,
                text_digest,
                sha256_bytes(extracted.encode("utf-8")),
                len(extracted.split(" ")) if extracted else 0,
                rank,
            )
        )
        texts[row_id] = extracted
    counts = [source.extracted_token_count for source in sources]
    if expected_selected_id_digest == SELECTED_ID_DIGEST and (
        min(counts) != 370 or max(counts) != 600 or sum(count < 600 for count in counts) != 8
    ):
        raise ProspectiveManifestError("Prospective extracted token range has drifted.")
    return ProspectiveManifest(
        tuple(sources), expected_database_digest, expected_rights_digest, selected_id_digest
    ), texts


def validate_manifest_source(
    manifest: ProspectiveManifest, source: ProspectiveSource, text: str
) -> None:
    if (
        source not in manifest.sources
        or sha256_bytes(text.encode("utf-8")) != source.extracted_text_sha256
    ):
        raise ProspectiveManifestError(
            "Prospective source text does not match its frozen manifest row."
        )
