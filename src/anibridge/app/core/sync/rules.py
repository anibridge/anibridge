"""Runtime evaluator for sync_rules."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import msgspec
from anibridge.provider.base import (
    Node,
    Progress,
    Record,
    RecordField,
    Ref,
    State,
    Status,
)

from anibridge.app.config.sync_rules import (
    SyncRuleDefinition,
    SyncRulesConfig,
    SyncRuleSelector,
    SyncRuleTemplateId,
    SyncRuleTemplateItem,
)

__all__ = ["SyncRuleDecision", "SyncRuleEngine"]


class SyncRuleDecision(msgspec.Struct, frozen=True):
    """Decision for one proposed sync operation."""

    allowed: bool
    value: Any = None
    reason: str | None = None


@dataclass(frozen=True)
class SyncRuleTemplate:
    """Built-in sync_rules entries."""

    rules: tuple[SyncRuleDefinition, ...] = field(default_factory=tuple)


def _rule(
    selector: SyncRuleSelector,
    *,
    if_expr: str = "True",
    skip: bool = False,
    value: Any = None,
    name: str | None = None,
) -> SyncRuleDefinition:
    payload: dict[str, Any] = {"selector": selector}
    if name is not None:
        payload["name"] = name
    if if_expr != "True":
        payload["if"] = if_expr
    if skip:
        payload["skip"] = True
    else:
        payload["value"] = value
    return SyncRuleDefinition.model_validate(payload)


SYNC_RULE_TEMPLATES: Mapping[SyncRuleTemplateId, SyncRuleTemplate] = {
    SyncRuleTemplateId.PREVENT_REGRESSION: SyncRuleTemplate(
        rules=(
            _rule(
                SyncRuleSelector.RECORD_STATUS,
                if_expr="regresses(src.status, dst.status)",
                value="dst.status",
            ),
            _rule(
                SyncRuleSelector.RECORD_PROGRESS,
                if_expr="regresses(src.progress, dst.progress)",
                value="dst.progress",
            ),
            _rule(
                SyncRuleSelector.RECORD_STARTED_AT,
                if_expr="regresses(src.started_at, dst.started_at)",
                value="dst.started_at",
            ),
            _rule(
                SyncRuleSelector.RECORD_FINISHED_AT,
                if_expr="regresses(src.finished_at, dst.finished_at)",
                value="dst.finished_at",
            ),
            _rule(
                SyncRuleSelector.RECORD_LAST_ACTIVITY_AT,
                if_expr="regresses(src.last_activity_at, dst.last_activity_at)",
                value="dst.last_activity_at",
            ),
            _rule(
                SyncRuleSelector.RECORD_REPEAT_COUNT,
                if_expr="regresses(src.repeat_count, dst.repeat_count)",
                value="dst.repeat_count",
            ),
        ),
    ),
    SyncRuleTemplateId.PROMOTE_REWATCH: SyncRuleTemplate(
        rules=(
            _rule(
                SyncRuleSelector.RECORD_STATUS,
                if_expr=(
                    "dst.status in (Status.COMPLETED, Status.REPEATING) "
                    "and src.status == Status.ACTIVE"
                ),
                value="Status.REPEATING",
                name="promote-rewatch",
            ),
        ),
    ),
    SyncRuleTemplateId.REQUIRE_COMPLETED_FOR_RATING: SyncRuleTemplate(
        rules=(
            _rule(
                SyncRuleSelector.RECORD_RATING,
                if_expr="not plan.completed",
                skip=True,
                name="require-completed-for-rating",
            ),
        ),
    ),
}


class _Namespace(Mapping[str, Any]):
    """Mapping with attribute access for rule expressions."""

    def __init__(self, values: Mapping[str, Any], *, missing: Any = None) -> None:
        self._values = values
        self._missing = missing

    @classmethod
    def wrap(cls, value: Any, *, missing: Any = None) -> Any:
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(value, missing=missing)
        if isinstance(value, tuple | list):
            return tuple(cls.wrap(item, missing=missing) for item in value)
        return value

    def __getitem__(self, key: str) -> Any:
        if key not in self._values:
            return self._missing
        return self.wrap(self._values[key], missing=self._missing)

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattribute__(self, key: str) -> Any:
        if not key.startswith("_"):
            values = object.__getattribute__(self, "_values")
            if key in values:
                return self[key]
        return object.__getattribute__(self, key)

    def __getattr__(self, key: str) -> Any:
        return self[key]


class SyncRuleEngine:
    """Evaluate sync_rules for record fields and events."""

    def __init__(self, config: SyncRulesConfig | None = None) -> None:
        """Initialize the evaluator."""
        self.config = config or SyncRulesConfig()
        self._rules = self._expand_rules()

    def is_disabled(self, field: RecordField) -> bool:
        """Return whether an unconditional rule blocks a record field."""
        for rule in reversed(self._rules):
            if not self._matches_record(rule.selector, field) or rule.if_expr != "True":
                continue
            return rule.skip is True
        return False

    def allows_event(self, *, action: str, kind: str, destructive_sync: bool) -> bool:
        """Return whether rules allow a candidate event write."""
        default_allowed = action == "upsert"
        for index, rule in reversed(list(enumerate(self._rules, start=1))):
            if not self._matches_event(rule.selector, action=action, kind=kind):
                continue
            environment = self._event_environment(
                action=action,
                kind=kind,
                destructive_sync=destructive_sync,
                default_allowed=default_allowed,
            )
            if rule.if_expr and not bool(eval(rule.if_expr, {}, environment)):
                continue
            name = rule.name or f"{rule.selector}_{index}"
            decision = self._rule_decision(rule, default_allowed, environment, name)
            return decision.allowed and bool(decision.value)
        return default_allowed

    def allows_node(self, *, node: Node) -> bool:
        """Return whether rules allow a scanned node to enter sync planning."""
        default_allowed = True
        for index, rule in reversed(list(enumerate(self._rules, start=1))):
            if not self._matches_node(rule.selector):
                continue
            environment = self._node_environment(
                node=node,
                default_allowed=default_allowed,
            )
            if rule.if_expr and not bool(eval(rule.if_expr, {}, environment)):
                continue
            name = rule.name or f"{rule.selector}_{index}"
            decision = self._rule_decision(rule, default_allowed, environment, name)
            return decision.allowed and bool(decision.value)
        return default_allowed

    def evaluate_record_field(
        self,
        *,
        field: RecordField,
        current_values: Mapping[RecordField, Any],
        source_values: Mapping[RecordField, Any],
        planned_values: Mapping[RecordField, Any],
        source_record: Record,
        target_record: Record | None,
        target_ref: Ref,
    ) -> SyncRuleDecision:
        """Evaluate one record field write."""
        proposed_value = source_values.get(field)

        decision = self._evaluate_rules(
            field=field,
            current_values=current_values,
            source_values=source_values,
            planned_values=planned_values,
            source_record=source_record,
            target_record=target_record,
            target_ref=target_ref,
            default_value=proposed_value,
        )
        if decision is not None:
            return decision
        return SyncRuleDecision(True, proposed_value, "default")

    def _expand_rules(self) -> list[SyncRuleDefinition]:
        rules: list[SyncRuleDefinition] = []
        for item in self.config.root:
            if isinstance(item, SyncRuleTemplateItem):
                rules.extend(SYNC_RULE_TEMPLATES[item.template].rules)
            else:
                rules.append(item)
        return rules

    def _evaluate_rules(
        self,
        *,
        field: RecordField,
        current_values: Mapping[RecordField, Any],
        source_values: Mapping[RecordField, Any],
        planned_values: Mapping[RecordField, Any],
        source_record: Record,
        target_record: Record | None,
        target_ref: Ref,
        default_value: Any,
    ) -> SyncRuleDecision | None:
        for index, rule in reversed(list(enumerate(self._rules, start=1))):
            if not self._matches_record(rule.selector, field):
                continue
            environment = self._environment(
                field=field,
                current_values=current_values,
                source_values=source_values,
                planned_values=planned_values,
                source_record=source_record,
                target_record=target_record,
                target_ref=target_ref,
                proposal_value=default_value,
            )
            if rule.if_expr and not bool(eval(rule.if_expr, {}, environment)):
                continue
            name = rule.name or f"{rule.selector}_{index}"
            return self._rule_decision(rule, default_value, environment, name)
        return None

    def _environment(
        self,
        *,
        field: RecordField,
        current_values: Mapping[RecordField, Any],
        source_values: Mapping[RecordField, Any],
        planned_values: Mapping[RecordField, Any],
        source_record: Record,
        target_record: Record | None,
        target_ref: Ref,
        proposal_value: Any,
    ) -> dict[str, Any]:
        base = {
            "Status": Status,
            "dst": _field_namespace(current_values),
            "src": _field_namespace(source_values),
            "plan": _plan_namespace(planned_values),
            "proposal": _Namespace(
                {
                    "resource": "record",
                    "action": "field",
                    "field": field.value,
                    "value": proposal_value,
                }
            ),
            "env": _Namespace({}),
            "node": _Namespace({}),
            "caps": _Namespace({}),
            "target_ref": target_ref,
            "source_record": source_record,
            "target_record": target_record,
            "regresses": _regresses,
        }

        return {**base, "vars": _Namespace({})}

    @staticmethod
    def _event_environment(
        *,
        action: str,
        kind: str,
        destructive_sync: bool,
        default_allowed: bool,
    ) -> dict[str, Any]:
        return {
            "Status": Status,
            "dst": _Namespace({}),
            "src": _Namespace({}),
            "plan": _Namespace({}),
            "proposal": _Namespace(
                {
                    "resource": "event",
                    "action": action,
                    "kind": kind,
                    "value": default_allowed,
                }
            ),
            "env": _Namespace({"destructive_sync": destructive_sync}),
            "node": _Namespace({}),
            "caps": _Namespace({}),
            "regresses": _regresses,
        }

    @staticmethod
    def _node_environment(*, node: Node, default_allowed: bool) -> dict[str, Any]:
        return {
            "Status": Status,
            "dst": _Namespace({}),
            "src": _Namespace({}),
            "plan": _Namespace({}),
            "proposal": _Namespace(
                {
                    "resource": "node",
                    "action": "scan",
                    "kind": node.kind,
                    "value": default_allowed,
                }
            ),
            "env": _Namespace({}),
            "node": _Namespace(
                {
                    "ref": node.ref,
                    "kind": node.kind,
                    "title": node.title,
                    "url": node.url,
                    "labels": node.labels,
                    "flags": node.flags,
                    "facets": node.facets,
                }
            ),
            "caps": _Namespace({}),
            "regresses": _regresses,
        }

    @staticmethod
    def _rule_decision(
        rule: SyncRuleDefinition,
        default_value: Any,
        environment: Mapping[str, Any],
        name: str,
    ) -> SyncRuleDecision:
        if rule.skip is not None:
            return SyncRuleDecision(False, default_value, name)
        value = eval(str(rule.value), {}, dict(environment))
        return SyncRuleDecision(True, value, name)

    @staticmethod
    def _matches_record(selector: SyncRuleSelector, field: RecordField) -> bool:
        return selector in {f"record.{field.value}", SyncRuleSelector.RECORD_ANY}

    @staticmethod
    def _matches_event(selector: SyncRuleSelector, *, action: str, kind: str) -> bool:
        return selector in {
            SyncRuleSelector.EVENT_ANY,
            f"event.{action}",
        }

    @staticmethod
    def _matches_node(selector: SyncRuleSelector) -> bool:
        return selector == SyncRuleSelector.NODE_ANY


def _field_namespace(values: Mapping[RecordField, Any]) -> _Namespace:
    return _Namespace(
        {field.value: _status_value(field, value) for field, value in values.items()}
    )


def _plan_namespace(values: Mapping[RecordField, Any]) -> _Namespace:
    payload = {
        field.value: _status_value(field, value) for field, value in values.items()
    }
    status = payload.get(RecordField.STATUS.value)
    payload["completed"] = status in {Status.COMPLETED, Status.REPEATING}
    payload["rewatching"] = status == Status.REPEATING
    payload["changed"] = bool(values)
    return _Namespace(payload)


def _status_value(field: RecordField, value: Any) -> Any:
    if field == RecordField.STATUS and isinstance(value, State):
        return value.status
    return value


def _regresses(source_value: Any, target_value: Any) -> bool:
    if target_value is None:
        return False
    if isinstance(target_value, Status):
        stalled = {Status.PLANNED, Status.ACTIVE, Status.PAUSED, Status.DROPPED}
        regressions = {
            Status.PLANNED: set(),
            Status.ACTIVE: {Status.PLANNED},
            Status.PAUSED: {Status.PLANNED},
            Status.DROPPED: {Status.PLANNED},
            Status.COMPLETED: stalled,
            Status.REPEATING: stalled | {Status.COMPLETED},
        }
        return (
            isinstance(source_value, Status)
            and source_value in regressions[target_value]
        )
    if isinstance(target_value, Progress):
        target_current = target_value.current
        source_current = getattr(source_value, "current", None)
        return target_current is not None and (
            source_current is None or source_current < target_current
        )
    if isinstance(source_value, date) and isinstance(target_value, date):
        if isinstance(source_value, datetime) is not isinstance(target_value, datetime):
            source_value = (
                source_value.date()
                if isinstance(source_value, datetime)
                else source_value
            )
            target_value = (
                target_value.date()
                if isinstance(target_value, datetime)
                else target_value
            )
        return source_value < target_value
    return source_value is None or source_value < target_value
