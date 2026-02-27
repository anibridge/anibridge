"""Tests for SQL utility helpers."""

from typing import Any, cast

from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import Mapped
from sqlalchemy.sql import column, false

from anibridge.app.utils.sql import (
    _to_like_pattern,
    json_array_between,
    json_array_compare,
    json_array_contains,
    json_array_exists,
    json_array_like,
    json_dict_has_key,
    json_dict_has_value,
    json_dict_key_like,
    json_dict_value_like,
)

_SQLITE_DIALECT = sqlite.dialect()


def _compile(expr) -> str:
    """Compile a SQLAlchemy expression to a SQL string for SQLite."""
    return str(
        expr.compile(dialect=_SQLITE_DIALECT, compile_kwargs={"literal_binds": True})
    )


def _mapped_column(name: str = "data") -> Mapped[Any]:
    """Create a mapped column for testing."""
    return cast("Mapped[Any]", column(name))


def test_to_like_pattern_handles_wildcards_and_escapes():
    """Test that _to_like_pattern correctly converts wildcards and escapes."""
    assert _to_like_pattern("foo*bar") == "foo%bar"
    assert _to_like_pattern(r"foo\*bar") == r"foo\*bar"
    assert _to_like_pattern("foo?bar") == "foo_bar"
    assert _to_like_pattern(r"foo\?bar") == r"foo\?bar"
    assert _to_like_pattern("100%") == r"100\%"
    assert _to_like_pattern("value_with_underscore") == r"value\_with\_underscore"


def test_json_array_contains_returns_false_for_empty_values():
    """Test that json_array_contains returns false for empty values."""
    field = _mapped_column()

    assert json_array_contains(field, []).compare(false())


def test_json_array_contains_generates_exists_clause():
    """Test that json_array_contains generates the correct SQL clause."""
    field = _mapped_column()
    sql = _compile(json_array_contains(field, ["a", "b"]))

    assert "json_each" in sql
    assert "value IN ('a', 'b')" in sql


def test_json_array_like_case_insensitive():
    """Test that json_array_like generates a case-insensitive LIKE clause."""
    field = _mapped_column()
    sql = _compile(json_array_like(field, "pattern*"))

    assert "LIKE" in sql
    assert 'COLLATE "NOCASE"' in sql
    assert "pattern%" in sql


def test_json_dict_key_like_case_sensitive():
    """Test that json_dict_key_like generates a case-sensitive LIKE clause."""
    field = _mapped_column()
    sql = _compile(json_dict_key_like(field, "key?", case_insensitive=False))

    assert "COLLATE" not in sql
    assert "key_" in sql


def test_json_dict_value_like_generates_clause():
    """Test that json_dict_value_like generates the correct SQL clause."""
    field = _mapped_column()
    sql = _compile(json_dict_value_like(field, "value"))

    assert "json_each" in sql
    assert "LIKE" in sql


def test_json_dict_has_key_and_value():
    """Test that json_dict_has_key and json_dict_has_value generate correct clauses."""
    field = _mapped_column()

    key_sql = _compile(json_dict_has_key(field, "item"))
    value_sql = _compile(json_dict_has_value(field, "foo"))

    assert "json_type" in key_sql
    assert "$.item" in key_sql
    assert "json_each" in value_sql
    assert "= 'foo'" in value_sql


def test_json_array_between_and_compare():
    """Test that json_array_between and json_array_compare generate correct clauses."""
    field = _mapped_column()

    between_sql = _compile(json_array_between(field, 1, 10))
    compare_sql = _compile(json_array_compare(field, ">=", 5))

    assert ">= 1" in between_sql and "<= 10" in between_sql
    assert ">= 5" in compare_sql
    assert json_array_compare(field, "!=", 5).compare(false())


def test_json_array_exists_generates_length_check():
    """Test that json_array_exists generates a length check clause."""
    field = _mapped_column()
    sql = _compile(json_array_exists(field))

    assert "json_array_length" in sql
    assert "> 0" in sql
