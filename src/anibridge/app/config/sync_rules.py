"""Configuration schema for sync_rules."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, RootModel, field_validator, model_validator

__all__ = [
    "BaseStrEnum",
    "SyncRuleDefinition",
    "SyncRuleSelector",
    "SyncRuleTemplateId",
    "SyncRuleTemplateItem",
    "SyncRulesConfig",
]


class BaseStrEnum(StrEnum):
    """Base class for case-insensitive string enumerations."""

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


class SyncRuleTemplateId(BaseStrEnum):
    """Built-in sync rule templates."""

    PREVENT_REGRESSION = "prevent-regression"
    PROMOTE_REWATCH = "promote-rewatch"
    REQUIRE_COMPLETED_FOR_RATING = "require-completed-for-rating"


class SyncRuleSelector(BaseStrEnum):
    """Supported sync rule targets."""

    EVENT_ANY = "event.*"
    EVENT_DELETE = "event.delete"
    EVENT_UPSERT = "event.upsert"
    NODE_ANY = "node.*"
    RECORD_ANY = "record.*"
    RECORD_FINISHED_AT = "record.finished_at"
    RECORD_LAST_ACTIVITY_AT = "record.last_activity_at"
    RECORD_NOTES = "record.notes"
    RECORD_PROGRESS = "record.progress"
    RECORD_RATING = "record.rating"
    RECORD_REPEAT_COUNT = "record.repeat_count"
    RECORD_STARTED_AT = "record.started_at"
    RECORD_STATUS = "record.status"


class SyncRuleTemplateItem(BaseModel):
    """Reference to a built-in sync rule template."""

    template: SyncRuleTemplateId = Field(description="Built-in template name")


class SyncRuleDefinition(BaseModel):
    """One field or event rule in sync_rules."""

    name: str | None = Field(default=None, description="Optional diagnostics label")
    selector: SyncRuleSelector = Field(description="Record field or event selector")
    if_expr: str = Field(
        default="True",
        alias="if",
        description="Python condition expression",
    )
    skip: Literal[True] | None = Field(
        default=None, description="Block the matched field or event"
    )
    value: Any = Field(default=None, description="Python value expression")

    @field_validator("if_expr")
    @classmethod
    def validate_if_expr(cls, value: str) -> str:
        """Validate the condition expression is not blank."""
        if not value.strip():
            raise ValueError("sync_rules rule conditions cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_single_action(self) -> SyncRuleDefinition:
        """Require exactly one action key."""
        configured = []
        if self.skip is not None:
            configured.append("skip")
        if "value" in self.model_fields_set:
            configured.append("value")
        if len(configured) != 1:
            raise ValueError(
                "sync_rules rules must define exactly one action: skip or value"
            )
        return self

    model_config = {"populate_by_name": True}


type SyncRuleItem = SyncRuleTemplateItem | SyncRuleDefinition


def default_sync_rules() -> list[SyncRuleItem]:
    """Return the default sync_rules list."""
    return [SyncRuleTemplateItem(template=SyncRuleTemplateId.PREVENT_REGRESSION)]


class SyncRulesConfig(RootModel[list[SyncRuleItem]]):
    """Ordered sync_rules list."""

    root: list[SyncRuleItem] = Field(default_factory=default_sync_rules)
