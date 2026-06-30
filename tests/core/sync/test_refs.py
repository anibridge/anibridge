"""Tests for provider ref serialization helpers."""

from anibridge.provider.base import Ref

from anibridge.app.core.sync import (
    RefKey,
    RefPayload,
    RefStepPayload,
    ref_from_payload,
    ref_payload_from_json,
    ref_to_json,
    ref_to_key,
)


def test_ref_to_key_includes_path_coordinates() -> None:
    """Ref keys should distinguish path coordinates."""
    ref = Ref.anchor("show").child("season", 2).child("episode", 5)

    assert ref_to_key(ref) == RefKey(
        key="show",
        path=(RefStepPayload("season", 2), RefStepPayload("episode", 5)),
    )


def test_ref_json_round_trip() -> None:
    """Refs should serialize to and from database JSON payloads."""
    ref = Ref.anchor("show").child("season", 2)
    payload = ref_to_json(ref)

    assert payload == {
        "key": "show",
        "path": [{"axis": "season", "value": 2}],
    }
    assert ref_payload_from_json(payload) == RefPayload(
        key="show",
        path=(RefStepPayload("season", 2),),
    )
    assert ref_from_payload(payload) == ref


def test_ref_payload_ignores_invalid_path_steps() -> None:
    """Malformed path steps should not prevent reading the ref key."""
    payload = {
        "key": "show",
        "path": [
            {"axis": "season", "value": 2},
            {"axis": None, "value": 3},
            "bad",
        ],
    }

    assert ref_payload_from_json(payload) == RefPayload(
        key="show",
        path=(RefStepPayload("season", 2),),
    )
    assert ref_from_payload(None) is None
    assert ref_payload_from_json({}) is None


def test_ref_payload_accepts_integral_float_path_steps() -> None:
    """JSON path coordinates may arrive as floats from external serializers."""
    payload = {
        "key": "show",
        "path": [
            {"axis": "season", "value": 1.0},
            {"axis": "episode", "value": 2.5},
        ],
    }

    assert ref_payload_from_json(payload) == RefPayload(
        key="show",
        path=(RefStepPayload("season", 1),),
    )
