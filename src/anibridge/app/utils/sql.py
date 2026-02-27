"""Reusable SQL utility functions."""

from typing import Any

from sqlalchemy.orm.base import Mapped
from sqlalchemy.sql import and_, cast, column, exists, false, func, select
from sqlalchemy.sql.elements import BinaryExpression, ColumnElement, UnaryExpression
from sqlalchemy.sql.sqltypes import Integer, String

__all__ = [
    "json_array_between",
    "json_array_compare",
    "json_array_contains",
    "json_array_like",
    "json_dict_has_key",
    "json_dict_has_value",
    "json_dict_key_like",
    "json_dict_value_like",
]


def json_array_contains(field: Mapped, values: list[Any]) -> ColumnElement[bool]:
    """Generates a JSON_CONTAINS function for the given field.

    Creates SQL conditions to check if any of the provided values exist
    within a JSON array field using SQLite's json_each function.

    Args:
        field (Mapped): SQLAlchemy mapped field representing a JSON array column
        values (list[Any]): List of values to search for within the JSON array

    Returns:
        ColumnElement[bool]: SQL condition that evaluates to True if any value
                                is found
    """
    if not values:
        return false()

    return exists(
        select(1).select_from(func.json_each(field)).where(column("value").in_(values))
    )


def json_array_exists(field: Mapped) -> ColumnElement[bool]:
    """Check if a JSON array field exists and is non-empty.

    Args:
        field (Mapped): SQLAlchemy mapped field representing a JSON array column.

    Returns:
        ColumnElement[bool]: SQL condition that evaluates to True if the array
                             exists and is non-empty.
    """
    return func.json_array_length(field) > 0


def _to_like_pattern(value: str) -> str:
    r"""Convert a user-provided pattern with '*' and '?' into a SQL LIKE pattern.

    - '*' -> '%' (multi-char wildcard)
    - '?' -> '_' (single-char wildcard)
    - '\*' -> literal '*'
    - '\?' -> literal '?'

    Escapes existing SQL LIKE meta characters so they cannot be injected.
    """
    # Escape SQL LIKE meta characters first
    pat = value.replace("\\", "\\\\")
    pat = pat.replace("%", "\\%").replace("_", "\\_")

    # Temporarily replace escaped wildcards to avoid double-replacing
    pat = pat.replace(r"\*", "[TMP_STAR]")
    pat = pat.replace(r"\?", "[TMP_QMARK]")

    pat = pat.replace("*", "%").replace("?", "_")

    pat = pat.replace("[TMP_STAR]", "*").replace("[TMP_QMARK]", "?")

    return pat


def json_array_like(
    field: Mapped, pattern: str, *, case_insensitive: bool = True
) -> ColumnElement[bool]:
    """Check if any element of a JSON array matches a LIKE pattern.

    Supports wildcard '*' and '?' in the given pattern.

    Args:
        field (Mapped): SQLAlchemy mapped field representing a JSON array column.
        pattern (str): LIKE pattern to match against (supports '*' and '?').
        case_insensitive (bool): Whether the match should be case-insensitive.

    Returns:
        ColumnElement[bool]: SQL condition that evaluates to True if any matches.
    """
    like_pat = _to_like_pattern(pattern)
    v = cast(column("value"), String)
    if case_insensitive:
        v = v.collate("NOCASE")
    cond = v.like(like_pat, escape="\\")
    return exists(select(1).select_from(func.json_each(field)).where(cond))


def json_dict_has_key(field: Mapped, key: str) -> BinaryExpression:
    """Generate a SQL expression for checking if a JSON field contains a key.

    Uses SQLite's json_type function to check if a specific key exists
    in a JSON object field.

    Args:
        field (Mapped): SQLAlchemy mapped field representing a JSON column
        key (str): JSON object key to search for (e.g., "s1" for season 1)

    Returns:
        BinaryExpression: SQL condition that evaluates to True if key exists
    """
    return func.json_type(field, f"$.{key}").is_not(None)


def json_dict_has_value(field: Mapped, value: Any) -> UnaryExpression:
    """Generate a SQL expression for checking if a JSON field contains a value.

    Uses SQLite's json_each function to check if a specific value exists
    in a JSON object field.

    Args:
        field (Mapped): SQLAlchemy mapped field representing a JSON column
        value (Any): Value to search for within the JSON object

    Returns:
        UnaryExpression: SQL condition that evaluates to True if value exists
    """
    return exists(
        select(1).select_from(func.json_each(field)).where(column("value") == value)
    )


def json_dict_key_like(
    field: Mapped, pattern: str, *, case_insensitive: bool = True
) -> UnaryExpression:
    """Check if any key in a JSON object matches a LIKE pattern.

    Supports wildcard '*' and '?'.

    Args:
        field (Mapped): SQLAlchemy mapped field representing a JSON object column.
        pattern (str): LIKE pattern to match against (supports '*' and '?').
        case_insensitive (bool): Whether the match should be case-insensitive.

    Returns:
        ColumnElement[bool]: SQL condition that evaluates to True if any matches.
    """
    like_pat = _to_like_pattern(pattern)
    k = cast(column("key"), String)
    if case_insensitive:
        k = k.collate("NOCASE")
    cond = k.like(like_pat, escape="\\")
    return exists(select(1).select_from(func.json_each(field)).where(cond))


def json_dict_value_like(
    field: Mapped, pattern: str, *, case_insensitive: bool = True
) -> UnaryExpression:
    """Check if any value in a JSON object matches a LIKE pattern.

    Supports wildcard '*' and '?'.

    Args:
        field (Mapped): SQLAlchemy mapped field representing a JSON object column.
        pattern (str): LIKE pattern to match against (supports '*' and '?').
        case_insensitive (bool): Whether the match should be case-insensitive.

    Returns:
        ColumnElement[bool]: SQL condition that evaluates to True if any matches.
    """
    like_pat = _to_like_pattern(pattern)
    v = cast(column("value"), String)
    if case_insensitive:
        v = v.collate("NOCASE")
    cond = v.like(like_pat, escape="\\")
    return exists(select(1).select_from(func.json_each(field)).where(cond))


def json_array_between(field: Mapped, lo: int, hi: int):
    """Check if any element of a JSON numeric array is within [lo, hi].

    Args:
        field (Mapped): SQLAlchemy mapped field representing a JSON array column
        lo (int): Lower bound of the range (inclusive)
        hi (int): Upper bound of the range (inclusive)

    Returns:
        ColumnElement[bool]: SQL condition that evaluates to True if any element is
            within the range
    """
    v = cast(column("value"), Integer)
    return exists(
        select(1).select_from(func.json_each(field)).where(and_(v >= lo, v <= hi))
    )


def json_array_compare(field: Mapped, op: str, num: int) -> ColumnElement[bool]:
    """Compare any element of a JSON numeric array to a number.

    Supported operators: ">", ">=", "<", "<=".

    Args:
        field (Mapped): SQLAlchemy mapped field representing a JSON array column
        op (str): Comparison operator (">", ">=", "<", "<=")
        num (int): Number to compare against

    Returns:
        ColumnElement[bool]: SQL condition that evaluates to True if any element
            satisfies the comparison
    """
    v = cast(column("value"), Integer)
    if op == ">":
        comp = v > num
    elif op == ">=":
        comp = v >= num
    elif op == "<":
        comp = v < num
    elif op == "<=":
        comp = v <= num
    else:
        # Fallback to false for unsupported operators
        return false()
    return exists(select(1).select_from(func.json_each(field)).where(comp))
