"""Frozen pilot activity registry versus the landed public contract."""

from __future__ import annotations

from jsonschema import Draft7Validator

from hramatka.contracts import PILOT_ACTIVITY_TYPES
from hramatka.engine import registry, vendoring


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
