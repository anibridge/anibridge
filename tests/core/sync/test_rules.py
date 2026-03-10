"""Tests for declarative sync-rule expression helpers."""

from anibridge.app.core.sync.rules import SyncRuleEngine, validate_sync_rule_expression


def test_validate_sync_rule_expression_allows_null_safe_comparison_helpers() -> None:
    """Progress comparison helpers should be available in expressions."""
    validate_sync_rule_expression("progressed(current.progress, computed.progress)")
    validate_sync_rule_expression("regressed(current.progress, computed.progress)")


def test_evaluate_field_progressed_treats_none_to_value_as_progress() -> None:
    """Missing values should not raise and should count as progression."""
    engine = SyncRuleEngine(
        field_rules={
            "progress": [
                {
                    "name": "Only sync progress gains",
                    "if": "progressed(current.progress, computed.progress)",
                }
            ]
        }
    )

    decision = engine.evaluate_field(
        field_name="progress",
        current_values={"progress": None},
        computed_values={"progress": 3},
    )

    assert decision.allowed is True
    assert decision.value == 3
    assert decision.reason == "Only sync progress gains"


def test_evaluate_field_regressed_treats_value_to_none_as_regression() -> None:
    """Dropping an existing value should count as regression."""
    engine = SyncRuleEngine(
        field_rules={
            "progress": [
                {
                    "name": "Block regressions",
                    "if": "regressed(current.progress, computed.progress)",
                    "set": "current.progress",
                }
            ]
        }
    )

    decision = engine.evaluate_field(
        field_name="progress",
        current_values={"progress": 7},
        computed_values={"progress": None},
    )

    assert decision.allowed is True
    assert decision.value == 7
    assert decision.reason == "Block regressions"


def test_evaluate_field_progressed_returns_no_match_for_regression() -> None:
    """A regression should fail a progressed guard instead of raising."""
    engine = SyncRuleEngine(
        field_rules={
            "progress": [
                {
                    "name": "Only sync progress gains",
                    "if": "progressed(current.progress, computed.progress)",
                }
            ]
        }
    )

    decision = engine.evaluate_field(
        field_name="progress",
        current_values={"progress": 9},
        computed_values={"progress": 6},
    )

    assert decision.allowed is False
    assert decision.value == 9
    assert decision.reason == "no_match"
