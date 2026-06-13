"""Unit tests for declarative sync rules."""

from datetime import UTC, date, datetime

import pytest
from anibridge.provider.base import Node, Record, RecordField, Ref, State, Status

from anibridge.app.core.sync.rules import (
    SyncRuleDecision,
    SyncRuleEngine,
    _Namespace,
    _validate_expression_ast,
    build_rule_context,
)


def test_namespace_normalizes_nested_values_and_missing_access() -> None:
    namespace = _Namespace(
        {
            "status": Status.ACTIVE,
            "nested": {"status": Status.PLANNED},
            "items": [Status.COMPLETED, {"status": Status.DROPPED}],
        },
        missing=None,
    )

    assert namespace.status == Status.ACTIVE
    assert namespace.nested.status == Status.PLANNED
    assert namespace.items[0] == Status.COMPLETED
    assert namespace.items[1].status == Status.DROPPED
    assert list(namespace) == ["status", "nested", "items"]
    assert len(namespace) == 3

    strict = _Namespace({}, missing=...)
    with pytest.raises(AttributeError):
        _ = strict.missing


@pytest.mark.parametrize(
    "expression",
    [
        pytest.param("([len][0])([1])", id="unsupported-call-target"),
        pytest.param("len(**ctx)", id="unpacked-keywords"),
        pytest.param("ctx.__class__", id="private-attribute"),
    ],
)
def test_validate_expression_ast_rejects_unsafe_expressions(expression: str) -> None:
    with pytest.raises(ValueError):
        _validate_expression_ast(expression)


@pytest.mark.parametrize(
    "expression",
    [
        "[item for item in ctx.source.values if item is not None]",
        "max(item for item in (1, 2, 3))",
        "sum(1 for value in ctx.source.values if value)",
    ],
)
def test_validate_expression_ast_accepts_comprehensions(expression: str) -> None:
    _validate_expression_ast(expression)


def test_validate_expression_ast_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="unknown name"):
        _validate_expression_ast("missing + 1")

    with pytest.raises(ValueError, match="unknown name"):
        _validate_expression_ast("[value for value in (1, 2)] and value")


def test_build_rule_context_exposes_provider_contract_objects() -> None:
    node = Node(
        ref=Ref.at("series-1", ("episode", 3)),
        kind="episode",
        title="Episode 3",
    )
    source = Record(
        ref=node.ref,
        kind="progress",
        values={RecordField.STATUS: State(status=Status.ACTIVE)},
    )
    target_ref = Ref.anchor("target-1")

    ctx = build_rule_context(
        node=node,
        source_record=source,
        target_record=None,
        target_ref=target_ref,
    )

    assert ctx.node.title == "Episode 3"
    assert ctx.node.ref.path[0].axis == "episode"
    assert ctx.source.values.status.status == Status.ACTIVE
    assert ctx.target.key is None
    assert ctx.target_ref.key == "target-1"


def test_sync_rule_engine_defaults_and_disables_fields() -> None:
    engine = SyncRuleEngine(field_rules={"notes": False})

    assert engine.is_disabled(RecordField.NOTES) is True
    assert engine.evaluate_field(
        field=RecordField.PROGRESS,
        current_values={RecordField.PROGRESS: 1},
        computed_values={RecordField.PROGRESS: 2},
    ) == SyncRuleDecision(allowed=True, value=2)
    assert engine.evaluate_field(
        field=RecordField.NOTES,
        current_values={RecordField.NOTES: "keep"},
        computed_values={RecordField.NOTES: "replace"},
    ) == SyncRuleDecision(allowed=False, value="keep", reason="disabled")


def test_sync_rule_engine_evaluates_first_matching_rule_with_variables() -> None:
    engine = SyncRuleEngine(
        variables={"same_title": "ctx.node.title == 'Movie'"},
        field_rules={
            "status": [
                {
                    "name": "promote",
                    "if": "vars.same_title and computed.status == Status.ACTIVE",
                    "set": "Status.COMPLETED",
                },
                {"name": "fallback", "set": "Status.PLANNED"},
            ]
        },
    )

    decision = engine.evaluate_field(
        field=RecordField.STATUS,
        current_values={RecordField.STATUS: Status.PLANNED},
        computed_values={RecordField.STATUS: Status.ACTIVE},
        rule_context={"node": {"title": "Movie"}},
    )

    assert decision == SyncRuleDecision(
        allowed=True,
        value=Status.COMPLETED,
        reason="promote",
    )


def test_sync_rule_engine_requires_status_rules_to_return_status_values() -> None:
    engine = SyncRuleEngine(field_rules={"status": [{"set": "'bad'"}]})

    with pytest.raises(ValueError, match="must return a Status"):
        engine.evaluate_field(
            field=RecordField.STATUS,
            current_values={RecordField.STATUS: Status.PLANNED},
            computed_values={RecordField.STATUS: Status.ACTIVE},
        )


def test_sync_rule_engine_accepts_state_from_status_rules() -> None:
    state = State(native="completed", status=Status.COMPLETED)
    engine = SyncRuleEngine(field_rules={"status": [{"set": state}]})

    decision = engine.evaluate_field(
        field=RecordField.STATUS,
        current_values={RecordField.STATUS: Status.ACTIVE},
        computed_values={RecordField.STATUS: Status.ACTIVE},
    )

    assert decision.value == state


def test_sync_rule_engine_compares_mixed_date_and_datetime_values() -> None:
    engine = SyncRuleEngine(
        field_rules={
            "started_at": [
                {
                    "name": "keep-current",
                    "if": (
                        "current.started_at is not None and "
                        "computed.started_at > current.started_at"
                    ),
                    "set": "current.started_at",
                }
            ]
        },
    )

    result = engine.evaluate_field(
        field=RecordField.STARTED_AT,
        current_values={RecordField.STARTED_AT: date(2026, 1, 2)},
        computed_values={RecordField.STARTED_AT: datetime(2026, 1, 3, 12, tzinfo=UTC)},
    )

    assert result == SyncRuleDecision(
        allowed=True,
        value=date(2026, 1, 2),
        reason="keep-current",
    )


def test_sync_rule_engine_falls_through_to_computed_value() -> None:
    engine = SyncRuleEngine(
        field_rules={
            "repeat_count": [
                {
                    "name": "never-matches",
                    "if": "computed.repeat_count < current.repeat_count",
                    "set": "current.repeat_count",
                }
            ]
        },
    )

    result = engine.evaluate_field(
        field=RecordField.REPEAT_COUNT,
        current_values={RecordField.REPEAT_COUNT: 1},
        computed_values={RecordField.REPEAT_COUNT: 2},
    )

    assert result == SyncRuleDecision(allowed=True, value=2, reason="default")
