from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hramatka.engine.tests.fixtures._build_fixtures import _surface_closed_vesum_rows


def test_surface_closed_vesum_rows_retains_all_same_form_analyses(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "vesum.db")
    try:
        connection.execute(
            "CREATE TABLE forms (word_form TEXT, lemma TEXT, tags TEXT, pos TEXT)"
        )
        connection.executemany(
            "INSERT INTO forms (word_form, lemma, tags, pos) VALUES (?, ?, ?, ?)",
            (
                ("мила", "мити", "verb:past:f", "verb"),
                ("мила", "милий", "adj:f", "adj"),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    rows = _surface_closed_vesum_rows(
        tmp_path,
        [{"word_form": "мила", "lemma": "мити", "tags": "verb:past:f", "pos": "verb"}],
    )

    assert rows == [
        {"word_form": "мила", "lemma": "милий", "tags": "adj:f", "pos": "adj"},
        {"word_form": "мила", "lemma": "мити", "tags": "verb:past:f", "pos": "verb"},
    ]


def test_surface_closed_vesum_rows_rejects_non_authoritative_capture(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "vesum.db")
    try:
        connection.execute(
            "CREATE TABLE forms (word_form TEXT, lemma TEXT, tags TEXT, pos TEXT)"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="absent from the authoritative surface"):
        _surface_closed_vesum_rows(
            tmp_path,
            [{"word_form": "мила", "lemma": "мити", "tags": "verb:past:f", "pos": "verb"}],
        )
