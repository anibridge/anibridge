"""Declarative sync rule configuration models and built-in templates."""

import keyword
from enum import StrEnum
from typing import Any, Final, cast

from anibridge.provider.base import RecordField
from pydantic import BaseModel, Field, field_validator

from anibridge.app.core.sync.rules import validate_sync_rule_expression

__all__ = [
    "SYNC_RULE_TEMPLATES",
    "SyncRuleDefinition",
    "SyncRuleTemplateId",
    "SyncRulesConfig",
]

_RECORD_FIELD_NAMES: Final[tuple[str, ...]] = tuple(
    field.value for field in RecordField
)


class BaseStrEnum(StrEnum):
    """Base class for string-based enumerations with a custom __repr__ method.

    Provides case-insensitive lookup functionality and consistent string
    representation for enumeration values.
    """

    @classmethod
    def _missing_(cls, value: object) -> BaseStrEnum | None:
        """Handle case-insensitive lookup for enum values."""
        value = value.lower() if isinstance(value, str) else value
        for member in cls:
            if member.lower() == value:
                return member
        return None

    def __repr__(self) -> str:
        """Return the string value of the enum member."""
        return self.value

    def __str__(self) -> str:
        """Return the string representation of the enum member."""
        return repr(self)


class SyncRuleDefinition(BaseModel):
    """Single declarative sync rule for a field."""

    name: str | None = Field(default=None, description="Human-readable rule label")
    if_expr: str | None = Field(
        default=None,
        alias="if",
        description="Condition expression evaluated against the sync context",
    )
    set_expr: Any = Field(
        alias="set",
        description="Expression or literal value returned when the rule matches",
    )

    @field_validator("if_expr")
    @classmethod
    def validate_if_expr(cls, value: str | None) -> str | None:
        """Validate the optional condition expression."""
        if value is None:
            return value
        if not value.strip():
            raise ValueError("sync rule conditions cannot be blank")
        validate_sync_rule_expression(value)
        return value

    @field_validator("set_expr")
    @classmethod
    def validate_set_expr(cls, value: Any) -> Any:
        """Validate the required set expression when it is string-based."""
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("sync rule set expressions cannot be blank")
            validate_sync_rule_expression(value)
        return value

    model_config = {"populate_by_name": True}


class SyncRuleTemplate(BaseModel):
    """Constant built-in sync-rule template."""

    description: str = Field(description="Summary of the template behavior")
    vars: dict[str, str] = Field(
        default_factory=dict,
        description="Reusable expressions contributed by the template",
    )
    status: bool | list[SyncRuleDefinition] | None = None
    progress: bool | list[SyncRuleDefinition] | None = None
    rating: bool | list[SyncRuleDefinition] | None = None
    started_at: bool | list[SyncRuleDefinition] | None = None
    finished_at: bool | list[SyncRuleDefinition] | None = None
    last_activity_at: bool | list[SyncRuleDefinition] | None = None
    repeat_count: bool | list[SyncRuleDefinition] | None = None
    notes: bool | list[SyncRuleDefinition] | None = None

    @field_validator("vars")
    @classmethod
    def validate_vars(cls, value: dict[str, str]) -> dict[str, str]:
        """Validate reusable variable names and expressions."""
        reserved = {"computed", "current", "ctx", "vars"}
        for name, expression in value.items():
            if not name.isidentifier() or keyword.iskeyword(name):
                raise ValueError(
                    f"sync_rules.vars contains invalid variable name: {name!r}"
                )
            if name in reserved:
                raise ValueError(
                    f"sync_rules.vars cannot redefine reserved name: {name!r}"
                )
            if not expression.strip():
                raise ValueError(
                    f"sync_rules.vars.{name} must be a non-empty expression"
                )
            validate_sync_rule_expression(expression)
        return value

    @field_validator(*_RECORD_FIELD_NAMES)
    @classmethod
    def validate_field_rules(
        cls,
        value: bool | list[SyncRuleDefinition] | None,
    ) -> bool | list[SyncRuleDefinition] | None:
        """Reject empty rule lists for field-specific rule sets."""
        if isinstance(value, list) and not value:
            raise ValueError("sync_rules field rule lists cannot be empty")
        return value

    def get_field_value(
        self,
        field_name: str,
    ) -> bool | list[SyncRuleDefinition] | None:
        """Return the template contribution for one sync field."""
        return cast(bool | list[SyncRuleDefinition] | None, getattr(self, field_name))


class SyncRuleTemplateId(BaseStrEnum):
    """Built-in sync-rule template identifiers."""

    DISABLE_DROPPED_AND_PAUSED = "disable_dropped_and_paused"
    DISABLE_RATING_AND_NOTES = "disable_rating_and_notes"
    PREVENT_REGRESSIONS = "prevent_regressions"
    PROMOTE_REWATCH = "promote_rewatch"
    RATING_REQUIRES_COMPLETED = "rating_requires_completed"


SYNC_RULE_TEMPLATES: Final[dict[SyncRuleTemplateId, SyncRuleTemplate]] = {
    SyncRuleTemplateId.DISABLE_DROPPED_AND_PAUSED: SyncRuleTemplate(
        description=(
            "Prevent dropped and paused computed statuses from replacing the "
            "current target status."
        ),
        status=[
            SyncRuleDefinition.model_validate(
                {
                    "name": "Don't sync dropped or paused status changes",
                    "if": "computed.status in (Status.DROPPED, Status.PAUSED)",
                    "set": "current.status",
                }
            ),
        ],
    ),
    SyncRuleTemplateId.DISABLE_RATING_AND_NOTES: SyncRuleTemplate(
        description="Disable syncing for notes and rating fields.",
        notes=False,
        rating=False,
    ),
    SyncRuleTemplateId.PREVENT_REGRESSIONS: SyncRuleTemplate(
        description=(
            "Prevent status, progress, repeat count, and watch dates from moving "
            "backward by keeping the current target value instead."
        ),
        status=[
            SyncRuleDefinition.model_validate(
                {
                    "name": "Prevent regressing status",
                    "if": (
                        "current.status is not None and "
                        "status_rank(computed.status) < status_rank(current.status)"
                    ),
                    "set": "current.status",
                }
            )
        ],
        progress=[
            SyncRuleDefinition.model_validate(
                {
                    "name": "Prevent regressing progress",
                    "if": (
                        "current.progress is not None and "
                        "current.progress.current is not None and "
                        "(computed.progress is None or "
                        "computed.progress.current is None or "
                        "computed.progress.current < current.progress.current)"
                    ),
                    "set": "current.progress",
                }
            )
        ],
        repeat_count=[
            SyncRuleDefinition.model_validate(
                {
                    "name": "Prevent regressing repeat count",
                    "if": (
                        "current.repeat_count is not None and "
                        "(computed.repeat_count is None or "
                        "computed.repeat_count < current.repeat_count)"
                    ),
                    "set": "current.repeat_count",
                }
            )
        ],
        started_at=[
            SyncRuleDefinition.model_validate(
                {
                    "name": "Prevent regressing started_at",
                    "if": (
                        "current.started_at is not None and "
                        "(computed.started_at is None or "
                        "computed.started_at > current.started_at)"
                    ),
                    "set": "current.started_at",
                }
            )
        ],
        finished_at=[
            SyncRuleDefinition.model_validate(
                {
                    "name": "Prevent regressing finished_at",
                    "if": (
                        "current.finished_at is not None and "
                        "(computed.finished_at is None or "
                        "computed.finished_at > current.finished_at)"
                    ),
                    "set": "current.finished_at",
                }
            )
        ],
    ),
    SyncRuleTemplateId.PROMOTE_REWATCH: SyncRuleTemplate(
        description=(
            "Promote completed or repeating entries to repeating when new "
            "activity computes a current status."
        ),
        status=[
            SyncRuleDefinition.model_validate(
                {
                    "name": "Promote rewatch to repeating",
                    "if": (
                        "current.status in (Status.COMPLETED, "
                        "Status.REPEATING) and "
                        "computed.status == Status.ACTIVE"
                    ),
                    "set": "Status.REPEATING",
                }
            )
        ],
    ),
    SyncRuleTemplateId.RATING_REQUIRES_COMPLETED: SyncRuleTemplate(
        description=("Keep the current rating until the resolved status is completed."),
        rating=[
            SyncRuleDefinition.model_validate(
                {
                    "name": "Require completed status for rating sync",
                    "if": (
                        "max(status_rank(computed.status), "
                        "status_rank(current.status)) < "
                        "status_rank(Status.COMPLETED)"
                    ),
                    "set": "current.rating",
                }
            )
        ],
    ),
}


class SyncRulesConfig(BaseModel):
    """Declarative per-field sync rules, templates, and reusable variables."""

    templates: list[SyncRuleTemplateId] = Field(
        default_factory=lambda: [
            SyncRuleTemplateId.RATING_REQUIRES_COMPLETED,
            SyncRuleTemplateId.DISABLE_RATING_AND_NOTES,
            SyncRuleTemplateId.PREVENT_REGRESSIONS,
        ],
        description="Built-in templates to apply in order after the user-defined rules",
    )
    vars: dict[str, str] = Field(
        default_factory=dict,
        description="Reusable expressions available under vars.<name>",
    )
    status: bool | list[SyncRuleDefinition] = True
    progress: bool | list[SyncRuleDefinition] = True
    rating: bool | list[SyncRuleDefinition] = True
    started_at: bool | list[SyncRuleDefinition] = True
    finished_at: bool | list[SyncRuleDefinition] = True
    last_activity_at: bool | list[SyncRuleDefinition] = True
    repeat_count: bool | list[SyncRuleDefinition] = True
    notes: bool | list[SyncRuleDefinition] = True

    @field_validator("vars")
    @classmethod
    def validate_vars(cls, value: dict[str, str]) -> dict[str, str]:
        """Validate reusable variable names and expressions."""
        return SyncRuleTemplate.validate_vars(value)

    @field_validator(*_RECORD_FIELD_NAMES)
    @classmethod
    def validate_field_rules(
        cls,
        value: bool | list[SyncRuleDefinition],
    ) -> bool | list[SyncRuleDefinition]:
        """Reject empty rule lists for field-specific rule sets."""
        return cast(
            bool | list[SyncRuleDefinition],
            SyncRuleTemplate.validate_field_rules(value),
        )

    def resolved_vars(self) -> dict[str, str]:
        """Return template and user variables with user-defined names winning."""
        merged: dict[str, str] = {}
        for template_id in self.templates:
            merged.update(SYNC_RULE_TEMPLATES[template_id].vars)
        merged.update(self.vars)
        return merged

    def field_rules(self) -> dict[str, bool | list[dict[str, Any]]]:
        """Return configured field rules as plain runtime mappings."""
        payload: dict[str, bool | list[dict[str, Any]]] = {}
        for field_name in _RECORD_FIELD_NAMES:
            value = self._resolve_field_rules(field_name)
            if value is True:
                continue
            payload[field_name] = (
                value
                if isinstance(value, bool)
                else [
                    rule.model_dump(by_alias=True, exclude_unset=True) for rule in value
                ]
            )
        return payload

    def _resolve_field_rules(
        self,
        field_name: str,
    ) -> bool | list[SyncRuleDefinition]:
        """Resolve one field's effective rule payload with user-first priority."""
        template_value = self._template_field_value(field_name)
        user_value = cast(bool | list[SyncRuleDefinition], getattr(self, field_name))
        user_explicit = field_name in self.model_fields_set

        if not user_explicit:
            return template_value if template_value is not None else user_value
        if user_value is False:
            return False
        if user_value is True:
            return template_value if template_value is not None else True
        if isinstance(template_value, list):
            return [*user_value, *template_value]
        return user_value

    def _template_field_value(
        self,
        field_name: str,
    ) -> bool | list[SyncRuleDefinition] | None:
        """Resolve the aggregate template contribution for one field."""
        value: bool | list[SyncRuleDefinition] | None = None
        for template_id in self.templates:
            incoming = SYNC_RULE_TEMPLATES[template_id].get_field_value(field_name)
            value = self._merge_template_field_value(value, incoming)
        return value

    @staticmethod
    def _merge_template_field_value(
        current: bool | list[SyncRuleDefinition] | None,
        incoming: bool | list[SyncRuleDefinition] | None,
    ) -> bool | list[SyncRuleDefinition] | None:
        """Merge one template field contribution into the accumulated state."""
        if incoming is None:
            return current
        if incoming is True:
            return current if current is not None else True
        if isinstance(current, list) and isinstance(incoming, list):
            return [*current, *incoming]
        return incoming
