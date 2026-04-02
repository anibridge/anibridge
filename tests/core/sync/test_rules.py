"""Unit tests for declarative sync rule helpers."""

import pytest
from anibridge.list import ListStatus

from anibridge.app.core.sync.rules import (
    SyncRuleDecision,
    SyncRuleEngine,
    _ContextNamespace,
    _ctx_field_refs_for_expression,
    _validate_expression_ast,
)


def test_context_namespace_normalizes_values_and_missing_access() -> None:
    """Context namespaces should normalize nested values and handle missing attrs."""
    namespace = _ContextNamespace(
        {
            "status": ListStatus.CURRENT,
            "nested": {"status": ListStatus.PLANNING},
            "items": [ListStatus.COMPLETED, {"status": ListStatus.DROPPED}],
        },
        missing_value=None,
    )

    assert namespace["status"] == ListStatus.CURRENT
    assert namespace.nested.status == ListStatus.PLANNING
    assert namespace["items"][0] == ListStatus.COMPLETED
    assert namespace["items"][1].status == ListStatus.DROPPED
    assert list(iter(namespace)) == ["status", "nested", "items"]
    assert len(namespace) == 3

    strict = _ContextNamespace({}, missing_value=...)
    with pytest.raises(AttributeError):
        _ = strict.missing


@pytest.mark.parametrize(
    "expression",
    [
        pytest.param("([len][0])([1])", id="unsupported-call-target"),
        pytest.param("len(**ctx)", id="unpacked-keywords"),
    ],
)
def test_validate_expression_ast_rejects_unsafe_calls(expression: str) -> None:
    """Unsupported call targets and unpacked kwargs should be rejected."""
    with pytest.raises(ValueError):
        _validate_expression_ast(expression)


def test_ctx_field_refs_collects_supported_ctx_paths() -> None:
    """ctx field extraction should include item/child/grandchildren references only."""
    refs = _ctx_field_refs_for_expression(
        "ctx.item.title and ctx.child.season and "
        "ctx.grandchildren[0].index and current.status"
    )

    assert refs == {"title", "season", "index"}


def test_sync_rule_engine_reports_rule_state_and_invalid_status_outputs() -> None:
    """Rule engines should expose rule state and validate status outputs."""
    engine = SyncRuleEngine(
        variables={"same_title": "ctx.item.title == 'Movie'"},
        field_rules={
            "status": [{"name": "set status", "if": "vars.same_title", "set": "'bad'"}],
            "review": False,
            "progress": True,
        },
    )

    assert engine.has_field_rules("status") is True
    assert engine.is_disabled("review") is True
    assert engine.context_media_fields("status") == frozenset({"title"})
    assert engine.evaluate_field(
        field_name="progress",
        current_values={"progress": 1},
        computed_values={"progress": 2},
    ) == SyncRuleDecision(allowed=True, value=2)

    with pytest.raises(ValueError, match="must return a ListStatus or null"):
        engine.evaluate_field(
            field_name="status",
            current_values={"status": ListStatus.PLANNING},
            computed_values={"status": ListStatus.CURRENT},
            rule_context={"item": {"title": "Movie"}},
        )


def test_sync_rule_engine_rejects_non_enum_status_values() -> None:
    """Status rules must resolve to ListStatus values or null."""
    engine = SyncRuleEngine(
        field_rules={"status": [{"set": 123}]},
    )

    with pytest.raises(ValueError, match="must return a ListStatus or null"):
        engine.evaluate_field(
            field_name="status",
            current_values={"status": None},
            computed_values={"status": None},
        )


def test_sync_rule_engine_supports_liststatus_class_in_status_rules() -> None:
    """Status rules should evaluate ListStatus enum members directly."""
    engine = SyncRuleEngine(
        field_rules={
            "status": [
                {
                    "name": "prevent regression",
                    "if": (
                        "current.status == ListStatus.COMPLETED and "
                        "computed.status == ListStatus.CURRENT"
                    ),
                    "set": "ListStatus.COMPLETED",
                }
            ]
        }
    )

    result = engine.evaluate_field(
        field_name="status",
        current_values={"status": ListStatus.COMPLETED},
        computed_values={"status": ListStatus.CURRENT},
    )

    assert result.value == ListStatus.COMPLETED
    assert result.reason == "prevent regression"
