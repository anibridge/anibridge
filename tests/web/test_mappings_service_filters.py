"""Lightweight tests for mappings query specs (v3)."""

from anibridge.app.web.services.mappings_query_spec import get_query_field_map


def test_query_field_map_contains_core_fields() -> None:
    """The mappings query field map contains core fields."""
    field_map = get_query_field_map()
    core_keys = {"source.provider", "source.id", "source.scope"}
    assert core_keys.issubset(set(field_map.keys()))
    assert field_map["source.provider"].desc == "Source provider"
