"""Unit tests for declarative sync rule evaluation."""

from datetime import UTC, date, datetime

from anibridge.provider.base import (
    Node,
    Progress,
    Rating,
    Record,
    RecordField,
    Ref,
    State,
    Status,
)

from anibridge.app.config.sync_rules import SyncRulesConfig
from anibridge.app.core.sync.rules import SyncRuleDecision, SyncRuleEngine


def _decision(
    engine: SyncRuleEngine,
    field: RecordField,
    *,
    current: object = None,
    source: object = None,
) -> SyncRuleDecision:
    return engine.evaluate_record_field(
        field=field,
        current_values={field: current},
        source_values={field: source},
        planned_values={field: source},
        source_record=Record(ref=Ref.anchor("source"), surface="list"),
        target_record=None,
        target_ref=Ref.anchor("target"),
    )


def test_sync_rules_empty_list_uses_source_value() -> None:
    engine = SyncRuleEngine(SyncRulesConfig.model_validate([]))

    decision = _decision(
        engine,
        RecordField.STATUS,
        current=Status.PLANNED,
        source=Status.ACTIVE,
    )

    assert decision == SyncRuleDecision(True, Status.ACTIVE, "default")


def test_sync_rules_default_template_prevents_regression() -> None:
    engine = SyncRuleEngine(SyncRulesConfig())

    assert engine.is_disabled(RecordField.NOTES) is False
    assert engine.is_disabled(RecordField.RATING) is False
    assert engine.allows_event(action="upsert", kind="watch", destructive_sync=False)
    assert not engine.allows_event(
        action="delete",
        kind="watch",
        destructive_sync=False,
    )

    status_decision = _decision(
        engine,
        RecordField.STATUS,
        current=State(status=Status.COMPLETED),
        source=State(status=Status.ACTIVE),
    )
    progress_decision = _decision(
        engine,
        RecordField.PROGRESS,
        current=Progress(current=10),
        source=Progress(current=4),
    )

    assert status_decision == SyncRuleDecision(
        True,
        Status.COMPLETED,
        "record.status_1",
    )
    assert progress_decision.value == Progress(current=10)


def test_sync_rules_status_regression_uses_explicit_transitions() -> None:
    engine = SyncRuleEngine(SyncRulesConfig())

    cases = (
        (Status.ACTIVE, Status.PLANNED, Status.ACTIVE),
        (Status.COMPLETED, Status.ACTIVE, Status.COMPLETED),
        (Status.REPEATING, Status.COMPLETED, Status.REPEATING),
        (Status.PLANNED, Status.DROPPED, Status.DROPPED),
        (Status.PAUSED, Status.ACTIVE, Status.ACTIVE),
        (Status.COMPLETED, Status.REPEATING, Status.REPEATING),
    )

    for current, source, expected in cases:
        decision = _decision(
            engine,
            RecordField.STATUS,
            current=State(status=current),
            source=State(status=source),
        )
        value = (
            decision.value.status
            if isinstance(decision.value, State)
            else decision.value
        )

        assert value == expected


def test_sync_rules_date_regression_handles_mixed_date_and_datetime() -> None:
    engine = SyncRuleEngine(SyncRulesConfig())

    decision = _decision(
        engine,
        RecordField.STARTED_AT,
        current=date(2026, 1, 2),
        source=datetime(2026, 1, 1, 23, 30, tzinfo=UTC),
    )

    assert decision == SyncRuleDecision(True, date(2026, 1, 2), "record.started_at_3")


def test_sync_rules_later_rules_override_templates() -> None:
    engine = SyncRuleEngine(
        SyncRulesConfig.model_validate(
            [
                {"template": "prevent-regression"},
                {"selector": "record.progress", "value": "src.progress"},
                {"selector": "event.upsert", "skip": True},
            ]
        )
    )

    assert not engine.allows_event(action="upsert", kind="watch", destructive_sync=True)
    assert _decision(
        engine,
        RecordField.PROGRESS,
        current=Progress(current=10),
        source=Progress(current=4),
    ) == SyncRuleDecision(True, Progress(current=4), "record.progress_7")


def test_sync_rules_promote_rewatch_template_is_opt_in() -> None:
    default_engine = SyncRuleEngine(SyncRulesConfig())
    promoted_engine = SyncRuleEngine(
        SyncRulesConfig.model_validate(
            [{"template": "prevent-regression"}, {"template": "promote-rewatch"}]
        )
    )

    assert (
        _decision(
            default_engine,
            RecordField.STATUS,
            current=State(status=Status.COMPLETED),
            source=State(status=Status.ACTIVE),
        ).value
        == Status.COMPLETED
    )
    assert _decision(
        promoted_engine,
        RecordField.STATUS,
        current=State(status=Status.COMPLETED),
        source=State(status=Status.ACTIVE),
    ) == SyncRuleDecision(True, Status.REPEATING, "promote-rewatch")


def test_sync_rules_require_completed_for_rating_template_is_opt_in() -> None:
    default_engine = SyncRuleEngine(SyncRulesConfig())
    gated_engine = SyncRuleEngine(
        SyncRulesConfig.model_validate(
            [
                {"template": "prevent-regression"},
                {"template": "require-completed-for-rating"},
            ]
        )
    )
    rating = Rating(8, (0, 10, 1))

    assert _decision(
        default_engine,
        RecordField.RATING,
        source=rating,
    ) == SyncRuleDecision(True, rating, "default")
    blocked = gated_engine.evaluate_record_field(
        field=RecordField.RATING,
        current_values={},
        source_values={
            RecordField.STATUS: State(status=Status.ACTIVE),
            RecordField.RATING: rating,
        },
        planned_values={
            RecordField.STATUS: State(status=Status.ACTIVE),
            RecordField.RATING: rating,
        },
        source_record=Record(ref=Ref.anchor("source"), surface="list"),
        target_record=None,
        target_ref=Ref.anchor("target"),
    )
    allowed = gated_engine.evaluate_record_field(
        field=RecordField.RATING,
        current_values={},
        source_values={
            RecordField.STATUS: State(status=Status.COMPLETED),
            RecordField.RATING: rating,
        },
        planned_values={
            RecordField.STATUS: State(status=Status.COMPLETED),
            RecordField.RATING: rating,
        },
        source_record=Record(ref=Ref.anchor("source"), surface="list"),
        target_record=None,
        target_ref=Ref.anchor("target"),
    )

    assert blocked == SyncRuleDecision(
        False,
        rating,
        "require-completed-for-rating",
    )
    assert allowed == SyncRuleDecision(True, rating, "default")


def test_sync_rules_can_allow_event_deletes_explicitly() -> None:
    engine = SyncRuleEngine(
        SyncRulesConfig.model_validate([{"selector": "event.delete", "value": "True"}])
    )

    assert engine.allows_event(
        action="delete",
        kind="watch",
        destructive_sync=False,
    )


def test_sync_rules_can_skip_nodes() -> None:
    engine = SyncRuleEngine(
        SyncRulesConfig.model_validate(
            [{"selector": "node.*", "if": "node.kind == 'movie'", "skip": True}]
        )
    )

    assert not engine.allows_node(
        node=Node(ref=Ref.anchor("movie"), kind="movie", title="Movie")
    )
    assert engine.allows_node(node=Node(ref=Ref.anchor("show"), kind="show"))


def test_sync_rules_skip_blocks_record_fields() -> None:
    engine = SyncRuleEngine(
        SyncRulesConfig.model_validate([{"selector": "record.notes", "skip": True}])
    )

    decision = _decision(
        engine,
        RecordField.NOTES,
        current="keep",
        source="replace",
    )

    assert engine.is_disabled(RecordField.NOTES) is True
    assert decision.allowed is False
    assert decision.reason == "record.notes_1"


def test_sync_rules_evaluate_if_and_value_expressions() -> None:
    engine = SyncRuleEngine(
        SyncRulesConfig.model_validate(
            [
                {
                    "name": "Promote rewatch",
                    "selector": "record.status",
                    "if": (
                        "dst.status in (Status.COMPLETED, Status.REPEATING) "
                        "and src.status == Status.ACTIVE"
                    ),
                    "value": "Status.REPEATING",
                }
            ]
        )
    )

    decision = _decision(
        engine,
        RecordField.STATUS,
        current=State(status=Status.COMPLETED),
        source=State(status=Status.ACTIVE),
    )

    assert decision == SyncRuleDecision(True, Status.REPEATING, "Promote rewatch")


def test_sync_rules_can_clear_with_none_expression() -> None:
    engine = SyncRuleEngine(
        SyncRulesConfig.model_validate(
            [{"name": "Clear notes", "selector": "record.notes", "value": "None"}]
        )
    )

    decision = _decision(
        engine,
        RecordField.NOTES,
        current="keep",
        source="replace",
    )

    assert decision == SyncRuleDecision(True, None, "Clear notes")
