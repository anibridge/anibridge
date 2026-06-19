"""Synchronization primitives."""

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import cast

import msgspec
from anibridge.provider.base import Ref, Step

__all__ = [
    "RefKey",
    "RefPayload",
    "RefStepPayload",
    "ScanPlan",
    "SyncRequest",
    "SyncTrigger",
    "dedupe_refs",
    "ref_from_payload",
    "ref_payload_from_json",
    "ref_to_json",
    "ref_to_key",
]


class SyncTrigger(StrEnum):
    """Reason a sync was started."""

    MANUAL = "manual"
    PERIODIC = "periodic"
    POLL = "poll"
    WEBHOOK = "webhook"


class SyncRequest(msgspec.Struct, frozen=True):
    """A sync request before it is narrowed to a provider scan."""

    trigger: SyncTrigger = SyncTrigger.MANUAL
    source_refs: tuple[Ref, ...] | None = None


class ScanPlan(msgspec.Struct, frozen=True):
    """The source scan AniBridge will ask the provider to run."""

    trigger: SyncTrigger
    source_refs: tuple[Ref, ...] | None
    require_user_data: bool
    from_change_feed: bool = False


class RefStepPayload(msgspec.Struct, frozen=True):
    """JSON payload for one ref path coordinate."""

    axis: str
    value: int | str


class RefPayload(msgspec.Struct, frozen=True):
    """JSON payload for a provider ref."""

    key: str
    path: tuple[RefStepPayload, ...] = ()


class RefKey(msgspec.Struct, frozen=True):
    """Stable key for comparing provider refs."""

    key: str
    path: tuple[RefStepPayload, ...] = ()

    @classmethod
    def from_ref(cls, ref: Ref) -> RefKey:
        """Build a key from a provider ref."""
        return cls(
            key=ref.key,
            path=tuple(RefStepPayload(step.axis, step.value) for step in ref.path),
        )

    @classmethod
    def from_payload(cls, payload: RefPayload) -> RefKey:
        """Build a key from a serialized ref payload."""
        return cls(key=payload.key, path=payload.path)

    @property
    def is_anchor(self) -> bool:
        """Return whether this key points at an anchor ref."""
        return not self.path

    def covers(self, other: RefKey) -> bool:
        """Return whether this key matches another key directly or by anchor."""
        return self == other or (self.key == other.key and self.is_anchor)

    def to_json(self) -> dict[str, object]:
        """Serialize this key to the ref JSON shape."""
        return {
            "key": self.key,
            "path": [{"axis": step.axis, "value": step.value} for step in self.path],
        }


def ref_to_key(ref: Ref) -> RefKey:
    """Return a stable, hashable key for a provider ref."""
    return RefKey.from_ref(ref)


def dedupe_refs(refs: Sequence[Ref] | None) -> tuple[Ref, ...]:
    """Return refs in first-seen order."""
    if not refs:
        return ()

    seen: dict[RefKey, Ref] = {}
    for ref in refs:
        seen.setdefault(ref_to_key(ref), ref)
    return tuple(seen.values())


def ref_to_json(ref: Ref) -> dict[str, object]:
    """Serialize a ref into the database JSON shape."""
    return ref_to_key(ref).to_json()


def ref_payload_from_json(payload: Mapping[str, object] | None) -> RefPayload | None:
    """Deserialize a database JSON ref payload."""
    if not payload or "key" not in payload:
        return None

    raw_path = payload.get("path", ())
    steps: list[RefStepPayload] = []
    if isinstance(raw_path, list | tuple):
        for raw_step in raw_path:
            if not isinstance(raw_step, Mapping):
                continue

            raw_step_map = cast(Mapping[str, object], raw_step)
            axis = raw_step_map.get("axis")
            value = raw_step_map.get("value")
            if isinstance(axis, str) and isinstance(value, str | int):
                steps.append(RefStepPayload(axis, value))

    return RefPayload(key=str(payload["key"]), path=tuple(steps))


def ref_from_payload(payload: RefPayload | Mapping[str, object] | None) -> Ref | None:
    """Deserialize a typed or raw JSON ref payload."""
    if payload is None:
        return None
    if isinstance(payload, RefPayload):
        typed_payload = payload
    elif isinstance(payload, Mapping):
        typed_payload = ref_payload_from_json(payload)
        if typed_payload is None:
            return None
    else:
        return None

    return Ref(
        key=typed_payload.key,
        path=tuple(Step(step.axis, step.value) for step in typed_payload.path),
    )
