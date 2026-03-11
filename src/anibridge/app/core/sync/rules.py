"""Declarative sync rules for transforming computed field values."""

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

from anibridge.list import ListStatus

__all__ = [
    "SyncRuleDecision",
    "SyncRuleEngine",
    "validate_sync_rule_expression",
]


_SAFE_FUNCTIONS: dict[str, Any] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "date": date,
    "datetime": datetime,
    "float": float,
    "int": int,
    "len": len,
    "max": max,
    "min": min,
    "round": round,
    "str": str,
    "sum": sum,
    "timedelta": timedelta,
}

_SAFE_METHODS = {
    "astimezone",
    "capitalize",
    "casefold",
    "date",
    "endswith",
    "format",
    "hour",
    "isoformat",
    "join",
    "lower",
    "lstrip",
    "replace",
    "rstrip",
    "split",
    "startswith",
    "strip",
    "strftime",
    "time",
    "title",
    "upper",
}

_ALIASES = {
    "false": False,
    "none": None,
    "null": None,
    "true": True,
    **{status.value: status.value for status in ListStatus},
}

_ALLOWED_NAMES = frozenset(
    {"computed", "current", "ctx", "vars", *_SAFE_FUNCTIONS, *_ALIASES}
)
_ALLOWED_NODES = (
    ast.Add,
    ast.And,
    ast.Attribute,
    ast.BinOp,
    ast.BoolOp,
    ast.Call,
    ast.Compare,
    ast.Constant,
    ast.Dict,
    ast.Div,
    ast.Eq,
    ast.Expression,
    ast.FloorDiv,
    ast.Gt,
    ast.GtE,
    ast.IfExp,
    ast.In,
    ast.Is,
    ast.IsNot,
    ast.keyword,
    ast.List,
    ast.Load,
    ast.Lt,
    ast.LtE,
    ast.Mod,
    ast.Mult,
    ast.Name,
    ast.Not,
    ast.NotEq,
    ast.NotIn,
    ast.Or,
    ast.Slice,
    ast.Sub,
    ast.Subscript,
    ast.Tuple,
    ast.UAdd,
    ast.UnaryOp,
    ast.USub,
)


class _ContextNamespace(Mapping[str, Any]):
    """Mapping wrapper that exposes sync context values via attribute access."""

    def __init__(
        self,
        values: Mapping[str, Any],
        *,
        missing_value: Any = ...,
    ) -> None:
        """Store raw context values for attribute and key access."""
        self._values = values
        self._missing_value = missing_value

    def __getitem__(self, key: str) -> Any:
        """Return a normalized context value for the provided key."""
        if key not in self._values:
            if self._missing_value is ...:
                raise KeyError(key)
            return self._missing_value
        return self._normalize(self._values[key])

    def __iter__(self):
        """Iterate over available context keys."""
        return iter(self._values)

    def __len__(self) -> int:
        """Return the number of stored context values."""
        return len(self._values)

    def __getattr__(self, key: str) -> Any:
        """Expose mapping keys as attributes for expression evaluation."""
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def _normalize(self, value: Any) -> Any:
        """Normalize values before exposing them to expressions."""
        if isinstance(value, ListStatus):
            return value.value
        if isinstance(value, Mapping):
            return _ContextNamespace(value, missing_value=self._missing_value)
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return tuple(self._normalize(item) for item in value)
        return value


@dataclass(frozen=True, slots=True)
class SyncRuleDecision:
    """Outcome of evaluating declarative rules for a single field."""

    allowed: bool
    value: Any
    reason: str | None = None


def _validate_expression_ast(expression: str) -> ast.Expression:
    """Validate and return the AST for a rule expression."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid sync rule expression: {expression!r}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(
                "sync rule expression contains unsupported syntax: "
                f"{type(node).__name__}"
            )
        if isinstance(node, ast.Name) and node.id not in _ALLOWED_NAMES:
            raise ValueError(
                f"sync rule expression references unknown name: {node.id!r}"
            )
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("sync rule expressions cannot access private attributes")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in _SAFE_FUNCTIONS:
                    raise ValueError(
                        "sync rule expression calls unsupported function: "
                        f"{node.func.id!r}"
                    )
            elif isinstance(node.func, ast.Attribute):
                if (
                    node.func.attr.startswith("_")
                    or node.func.attr not in _SAFE_METHODS
                ):
                    raise ValueError(
                        "sync rule expression calls an unsupported method: "
                        f"{node.func.attr!r}"
                    )
            else:
                raise ValueError(
                    "sync rule expression contains an unsupported call target"
                )
            for keyword in node.keywords:
                if keyword.arg is None:
                    raise ValueError(
                        "sync rule expressions do not support unpacked "
                        "keyword arguments"
                    )

    return tree


@lru_cache(maxsize=256)
def _compile_expression(expression: str):
    """Compile a validated rule expression for reuse."""
    tree = _validate_expression_ast(expression)
    return compile(tree, "<sync-rule>", "eval")


def validate_sync_rule_expression(expression: str) -> None:
    """Validate that a sync rule expression uses the supported subset.

    Args:
        expression (str): Expression string to validate.

    Returns:
        None: This function raises if the expression is unsupported.
    """
    _compile_expression(expression)


def _evaluate_expression(expression: str, environment: Mapping[str, Any]) -> Any:
    """Evaluate a previously validated rule expression."""
    return eval(
        _compile_expression(expression), {"__builtins__": {}}, dict(environment)
    )


class SyncRuleEngine:
    """Apply declarative sync rules to computed field values."""

    def __init__(
        self,
        *,
        variables: Mapping[str, str] | None = None,
        field_rules: Mapping[str, bool | Sequence[Mapping[str, Any]]] | None = None,
    ) -> None:
        """Store reusable expressions and per-field decision lists.

        Args:
            variables (Mapping[str, str] | None): Named expressions exposed under
                ``vars`` in rule evaluation.
            field_rules (Mapping[str, bool | Sequence[Mapping[str, Any]]] | None):
                Runtime rule payload keyed by sync field name.

        Returns:
            None: This initializer stores rule state for later evaluation.
        """
        self._variables = dict(variables or {})
        self._field_rules = dict(field_rules or {})

    def has_field_rules(self, field_name: str) -> bool:
        """Return whether a field has an ordered decision list configured.

        Args:
            field_name (str): Sync field name to inspect.

        Returns:
            bool: True when the field has a list of declarative rules.
        """
        return isinstance(
            self._field_rules.get(field_name), Sequence
        ) and not isinstance(self._field_rules.get(field_name), (str, bytes))

    def is_disabled(self, field_name: str) -> bool:
        """Return whether a field is explicitly disabled by declarative rules.

        Args:
            field_name (str): Sync field name to inspect.

        Returns:
            bool: True when the field is disabled by a ``false`` rule value.
        """
        return self._field_rules.get(field_name) is False

    def evaluate_field(
        self,
        *,
        field_name: str,
        current_values: Mapping[str, Any],
        computed_values: Mapping[str, Any],
        rule_context: Mapping[str, Any] | None = None,
    ) -> SyncRuleDecision:
        """Resolve the effective value for one field against the sync context.

        Args:
            field_name (str): Sync field name being evaluated.
            current_values (Mapping[str, Any]): Current list-entry field values.
            computed_values (Mapping[str, Any]): Newly computed field values before
                declarative overrides.
            rule_context (Mapping[str, Any] | None): Shimmed sync metadata exposed
                under ``ctx`` for rule expressions.

        Returns:
            SyncRuleDecision: Decision describing whether the field may sync and
                which value should be applied.
        """
        rules = self._field_rules.get(field_name)
        computed_value = computed_values.get(field_name)
        current_value = current_values.get(field_name)
        if rules is None:
            return SyncRuleDecision(allowed=True, value=computed_value)
        if rules is True:
            return SyncRuleDecision(allowed=True, value=computed_value)
        if rules is False:
            return SyncRuleDecision(
                allowed=False, value=current_value, reason="disabled"
            )

        environment = self._build_environment(
            current_values=current_values,
            computed_values=computed_values,
            rule_context=rule_context,
        )
        for index, rule in enumerate(rules, start=1):
            condition = rule.get("if")
            if condition is not None and not bool(
                _evaluate_expression(str(condition), environment)
            ):
                continue

            if "set" in rule:
                value = self._resolve_set_value(field_name, rule["set"], environment)
            else:
                value = computed_value

            rule_name = str(rule.get("name") or f"rule_{index}")
            return SyncRuleDecision(allowed=True, value=value, reason=rule_name)

        return SyncRuleDecision(allowed=False, value=current_value, reason="no_match")

    def _build_environment(
        self,
        *,
        current_values: Mapping[str, Any],
        computed_values: Mapping[str, Any],
        rule_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the evaluation environment for expressions."""
        current = _ContextNamespace(current_values, missing_value=None)
        computed = _ContextNamespace(computed_values, missing_value=None)
        ctx = _ContextNamespace(rule_context or {}, missing_value=None)
        variables: dict[str, Any] = {}
        base_environment: dict[str, Any] = {
            **_SAFE_FUNCTIONS,
            **_ALIASES,
            "current": current,
            "computed": computed,
            "ctx": ctx,
        }

        for name, expression in self._variables.items():
            variables[name] = _evaluate_expression(
                expression,
                {
                    **base_environment,
                    "vars": _ContextNamespace(variables),
                },
            )

        return {
            **base_environment,
            "vars": _ContextNamespace(variables),
        }

    def _resolve_set_value(
        self,
        field_name: str,
        raw_value: Any,
        environment: Mapping[str, Any],
    ) -> Any:
        """Resolve a rule's set payload into a final field value."""
        value = (
            _evaluate_expression(raw_value, environment)
            if isinstance(raw_value, str)
            else raw_value
        )
        if field_name != "status" or value is None or isinstance(value, ListStatus):
            return value
        if isinstance(value, str):
            try:
                return ListStatus(value)
            except ValueError as exc:
                raise ValueError(
                    f"invalid status value produced by sync rule: {value!r}"
                ) from exc
        raise ValueError(
            "sync rule for status must return a string or null, got "
            f"{type(value).__name__}"
        )
