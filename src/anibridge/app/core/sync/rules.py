"""Declarative sync rules over provider-contract records."""

import ast
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from functools import lru_cache, total_ordering
from typing import Any

import msgspec
from anibridge.provider.base import Node, Record, RecordField, Ref, State, Status

__all__ = [
    "SyncRuleDecision",
    "SyncRuleEngine",
    "build_rule_context",
    "validate_sync_rule_expression",
]


def status_rank(value: Any) -> int:
    """Return a stable order for normalized provider status values."""
    if isinstance(value, State):
        value = value.status
    return {
        None: 0,
        Status.PLANNED: 1,
        Status.DROPPED: 2,
        Status.PAUSED: 3,
        Status.ACTIVE: 4,
        Status.COMPLETED: 5,
        Status.REPEATING: 6,
    }.get(value, 0)


_FUNCTIONS: dict[str, Any] = {
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
    "status_rank": status_rank,
}
_GLOBALS: dict[str, Any] = {
    "Status": Status,
    "false": False,
    "none": None,
    "null": None,
    "true": True,
}
_METHODS = {
    "astimezone",
    "capitalize",
    "casefold",
    "date",
    "endswith",
    "format",
    "isoformat",
    "join",
    "lower",
    "lstrip",
    "replace",
    "rstrip",
    "split",
    "startswith",
    "strftime",
    "strip",
    "title",
    "upper",
}
_NAMES = frozenset({"computed", "current", "ctx", "vars", *_FUNCTIONS, *_GLOBALS})
_NODES = (
    ast.Add,
    ast.And,
    ast.Attribute,
    ast.BinOp,
    ast.BoolOp,
    ast.Call,
    ast.Compare,
    ast.comprehension,
    ast.Constant,
    ast.Dict,
    ast.Div,
    ast.Eq,
    ast.Expression,
    ast.FloorDiv,
    ast.GeneratorExp,
    ast.Gt,
    ast.GtE,
    ast.IfExp,
    ast.In,
    ast.Is,
    ast.IsNot,
    ast.keyword,
    ast.List,
    ast.ListComp,
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
    ast.Store,
    ast.Sub,
    ast.Subscript,
    ast.Tuple,
    ast.UAdd,
    ast.UnaryOp,
    ast.USub,
)


@total_ordering
class _Temporal:
    """Rule-facing temporal value with date/datetime comparison semantics."""

    def __init__(self, value: date | datetime) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        other_value = self._coerce_other(other)
        if other_value is NotImplemented:
            return False
        return self._pair(other_value)[0] == self._pair(other_value)[1]

    def __lt__(self, other: object) -> bool:
        other_value = self._coerce_other(other)
        if other_value is NotImplemented:
            return NotImplemented
        lhs, rhs = self._pair(other_value)
        return lhs < rhs

    def __bool__(self) -> bool:
        return bool(self.value)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.value, name)

    def __repr__(self) -> str:
        return repr(self.value)

    def __str__(self) -> str:
        return str(self.value)

    def _pair(self, other: date | datetime) -> tuple[date | datetime, date | datetime]:
        if self._date_only(self.value) or self._date_only(other):
            return self._as_date(self.value), self._as_date(other)
        return self.value, other

    @staticmethod
    def _coerce_other(other: object) -> date | datetime | Any:
        if isinstance(other, _Temporal):
            return other.value
        if isinstance(other, date | datetime):
            return other
        return NotImplemented

    @staticmethod
    def _date_only(value: date | datetime) -> bool:
        return isinstance(value, date) and not isinstance(value, datetime)

    @staticmethod
    def _as_date(value: date | datetime) -> date:
        return value.date() if isinstance(value, datetime) else value


class _Namespace(Mapping[str, Any]):
    """Small mapping wrapper used as the rule expression object model."""

    def __init__(self, values: Mapping[str, Any], *, missing: Any = ...) -> None:
        self._values = values
        self._missing = missing

    @classmethod
    def wrap(cls, value: Any, *, missing: Any = ...) -> Any:
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(value, missing=missing)
        if isinstance(value, Sequence) and not isinstance(
            value, str | bytes | bytearray
        ):
            return tuple(cls.wrap(item, missing=missing) for item in value)
        if isinstance(value, date | datetime):
            return _Temporal(value)
        return value

    def __getitem__(self, key: str) -> Any:
        if key not in self._values:
            if self._missing is ...:
                raise KeyError(key)
            return self._missing
        return self.wrap(self._values[key], missing=self._missing)

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattribute__(self, key: str) -> Any:
        if not key.startswith("_"):
            try:
                values = object.__getattribute__(self, "_values")
            except AttributeError:
                pass
            else:
                if key in values:
                    return self[key]
        return object.__getattribute__(self, key)

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


def _unwrap(value: Any) -> Any:
    return value.value if isinstance(value, _Temporal) else value


def _field_values(values: Mapping[RecordField, Any]) -> _Namespace:
    return _Namespace(
        {field.value: value for field, value in values.items()},
        missing=None,
    )


def _ref_context(ref: Ref) -> _Namespace:
    return _Namespace(
        {
            "key": ref.key,
            "path": tuple(
                _Namespace({"axis": step.axis, "value": step.value}, missing=None)
                for step in ref.path
            ),
            "is_anchor": ref.is_anchor,
        },
        missing=None,
    )


def _record_context(record: Record | None) -> _Namespace:
    if record is None:
        return _Namespace({}, missing=None)
    return _Namespace(
        {
            "ref": _ref_context(record.ref),
            "surface": record.surface,
            "key": record.key,
            "url": record.url,
            "updated_at": record.updated_at,
            "revision": record.revision,
            "ids": tuple(external_id.descriptor for external_id in record.ids),
            "values": {field.value: value for field, value in record.values.items()},
            "metadata": record.metadata,
        },
        missing=None,
    )


def _node_context(node: Node) -> _Namespace:
    return _Namespace(
        {
            "ref": _ref_context(node.ref),
            "kind": node.kind,
            "title": node.title,
            "url": node.url,
            "labels": node.labels,
            "flags": tuple(flag.value for flag in node.flags),
        },
        missing=None,
    )


def build_rule_context(
    *,
    node: Node,
    source_record: Record,
    target_record: Record | None,
    target_ref: Ref,
) -> _Namespace:
    """Build the `ctx` namespace exposed to sync rule expressions."""
    return _Namespace(
        {
            "node": _node_context(node),
            "source": _record_context(source_record),
            "target": _record_context(target_record),
            "target_ref": _ref_context(target_ref),
        },
        missing=None,
    )


class SyncRuleDecision(msgspec.Struct, frozen=True):
    """Decision for one synced record field."""

    allowed: bool
    value: Any
    reason: str | None = None


class _Validator(ast.NodeVisitor):
    def __init__(self) -> None:
        self.bound_names: set[str] = set()

    def generic_visit(self, node: ast.AST) -> None:
        if not isinstance(node, _NODES):
            raise ValueError(
                "sync rule expression contains unsupported syntax: "
                f"{type(node).__name__}"
            )
        super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if (
            isinstance(node.ctx, ast.Load)
            and node.id not in _NAMES
            and node.id not in self.bound_names
        ):
            raise ValueError(
                f"sync rule expression references unknown name: {node.id!r}"
            )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_"):
            raise ValueError("sync rule expressions cannot access private attributes")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            if node.func.id not in _FUNCTIONS:
                raise ValueError(
                    f"sync rule expression calls unsupported function: {node.func.id!r}"
                )
        elif isinstance(node.func, ast.Attribute):
            if node.func.attr.startswith("_") or node.func.attr not in _METHODS:
                raise ValueError(
                    "sync rule expression calls an unsupported method: "
                    f"{node.func.attr!r}"
                )
        else:
            raise ValueError("sync rule expression contains an unsupported call target")
        if any(keyword.arg is None for keyword in node.keywords):
            raise ValueError(
                "sync rule expressions do not support unpacked keyword arguments"
            )
        self.generic_visit(node)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension_expression(node.elt, node.generators)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension_expression(node.elt, node.generators)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        names = set(_target_names(node.target))
        self.visit(node.iter)
        self.bound_names.update(names)
        self.visit(node.target)
        for condition in node.ifs:
            self.visit(condition)

    def _visit_comprehension_expression(
        self,
        element: ast.AST,
        generators: list[ast.comprehension],
    ) -> None:
        previous = set(self.bound_names)
        for generator in generators:
            self.visit(generator)
        self.visit(element)
        self.bound_names = previous


def _target_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Tuple | ast.List):
        return tuple(name for item in node.elts for name in _target_names(item))
    return ()


def _validate_expression_ast(expression: str) -> ast.Expression:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid sync rule expression: {expression!r}") from exc
    _Validator().visit(tree)
    return tree


@lru_cache(maxsize=512)
def _compile(expression: str):
    return compile(_validate_expression_ast(expression), "<sync-rule>", "eval")


def validate_sync_rule_expression(expression: str) -> None:
    """Raise ValueError when a sync rule expression is not supported."""
    _compile(expression)


def _eval(expression: str, environment: Mapping[str, Any]) -> Any:
    return eval(_compile(expression), {"__builtins__": {}}, dict(environment))


class SyncRuleEngine:
    """Evaluate declarative sync rules for provider-contract record fields."""

    def __init__(
        self,
        *,
        variables: Mapping[str, str] | None = None,
        field_rules: Mapping[str, bool | Sequence[Mapping[str, Any]]] | None = None,
    ) -> None:
        """Store reusable variables and per-field rule decisions."""
        self._variables = dict(variables or {})
        self._field_rules = {
            RecordField(field): rules
            for field, rules in dict(field_rules or {}).items()
        }

    def is_disabled(self, field: RecordField) -> bool:
        """Return whether a field is explicitly blocked by configuration."""
        return self._field_rules.get(field) is False

    def evaluate_field(
        self,
        *,
        field: RecordField,
        current_values: Mapping[RecordField, Any],
        computed_values: Mapping[RecordField, Any],
        rule_context: Mapping[str, Any] | _Namespace | None = None,
    ) -> SyncRuleDecision:
        """Return the first matching decision for a field."""
        rules = self._field_rules.get(field, True)
        current_value = current_values.get(field)
        computed_value = computed_values.get(field)

        if rules is True:
            return SyncRuleDecision(True, computed_value)
        if rules is False:
            return SyncRuleDecision(False, current_value, "disabled")

        environment = self._environment(
            current_values=current_values,
            computed_values=computed_values,
            rule_context=rule_context,
        )
        for index, rule in enumerate(rules, start=1):
            condition = rule.get("if")
            if condition is not None and not bool(_eval(str(condition), environment)):
                continue

            value = self._resolve(field, rule["set"], environment)
            name = str(rule.get("name") or f"rule_{index}")
            return SyncRuleDecision(True, value, name)

        return SyncRuleDecision(True, computed_value, "default")

    def _environment(
        self,
        *,
        current_values: Mapping[RecordField, Any],
        computed_values: Mapping[RecordField, Any],
        rule_context: Mapping[str, Any] | _Namespace | None,
    ) -> dict[str, Any]:
        base = {
            **_FUNCTIONS,
            **_GLOBALS,
            "current": _field_values(current_values),
            "computed": _field_values(computed_values),
            "ctx": _Namespace.wrap(rule_context or {}, missing=None),
        }
        variables: dict[str, Any] = {}
        for name, expression in self._variables.items():
            variables[name] = _eval(
                expression,
                {**base, "vars": _Namespace(variables, missing=None)},
            )
        return {**base, "vars": _Namespace(variables, missing=None)}

    def _resolve(
        self,
        field: RecordField,
        raw_value: Any,
        environment: Mapping[str, Any],
    ) -> Any:
        value = (
            _eval(raw_value, environment) if isinstance(raw_value, str) else raw_value
        )
        value = _unwrap(value)
        if (
            field != RecordField.STATUS
            or value is None
            or isinstance(value, Status | State)
        ):
            return value
        raise ValueError(
            "sync rule for status must return a Status, State, or null; "
            f"got {type(value).__name__}"
        )
