"""Service helpers for managing custom mapping overrides (v3 graph)."""

import asyncio
import copy
import json
from pathlib import Path
from typing import Any, ClassVar

import yaml
from anibridge.utils.cache import cache

from anibridge.app import config
from anibridge.app.config.settings import get_config
from anibridge.app.core.animap import descriptor_key, parse_mapping_descriptor
from anibridge.app.core.mappings import MappingsClient
from anibridge.app.exceptions import (
    MappingError,
    MissingDescriptorError,
    SchedulerNotInitializedError,
)
from anibridge.app.utils.mapping_ranges import (
    is_valid_source_range,
    is_valid_target_range,
)
from anibridge.app.web.state import get_app_state

__all__ = ["MappingOverridesService", "get_mapping_overrides_service"]


class MappingOverridesService:
    """Manage CRUD operations for custom mapping overrides (descriptor graph)."""

    _SUPPORTED_FORMATS: ClassVar[tuple[str, ...]] = ("json", "yaml")

    def __init__(self) -> None:
        """Initialise synchronization primitives for override operations."""
        self._lock = asyncio.Lock()

    def _ensure_scheduler(self):
        """Ensure the scheduler is available and return it."""
        scheduler = get_app_state().scheduler
        if not scheduler:
            raise SchedulerNotInitializedError("Scheduler not initialized")
        return scheduler

    def _resolve_custom_file(self) -> tuple[Path, str]:
        """Determine the path and format of the custom mappings override file."""
        candidates = [config.data_path / name for name in MappingsClient.MAPPING_FILES]
        if not candidates or not candidates[0].exists():
            return config.data_path / "mappings.json", "json"
        if candidates[0].suffix.lower() == ".json":
            return candidates[0], "json"
        return candidates[0], "yaml"

    def _load_raw(self) -> tuple[dict[str, Any], Path, str]:
        """Load raw override data from the custom mappings file."""
        path, fmt = self._resolve_custom_file()
        if not path.exists():
            return {}, path, fmt

        try:
            if fmt == "json":
                with path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
            else:
                with path.open("r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
        except Exception as exc:  # pragma: no cover - defensive
            raise MappingError("Failed to read custom mappings file") from exc

        if not data:
            data = {}
        if not isinstance(data, dict):
            raise MappingError("Custom mappings file must contain an object")

        return data, path, fmt

    def _write_raw(self, raw: dict[str, Any], path: Path, fmt: str) -> None:
        """Persist raw override data to the custom mappings file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            with path.open("w", encoding="utf-8") as fh:
                json.dump(raw, fh, indent=2, sort_keys=True)
                fh.write("\n")
        else:
            with path.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=False)

    async def _load_upstream(self) -> dict[str, Any]:
        """Load the upstream mappings payload (without merging custom)."""
        upstream_url = get_config().mappings_url
        if not upstream_url:
            return {}

        async with MappingsClient(config.data_path, upstream_url) as client:
            return await client.load_source(str(upstream_url)) or {}

    def _normalize_targets(self, raw: Any) -> dict[str, dict[str, str | None] | None]:
        """Normalize a raw descriptor payload into target-range maps.

        A value of None for a target means the target is explicitly disabled.
        """
        if not isinstance(raw, dict):
            return {}

        cleaned: dict[str, dict[str, str | None] | None] = {}
        for target_key, ranges in raw.items():
            if target_key is None:
                continue
            target_str = str(target_key)
            if target_str.startswith("$"):
                continue
            if ranges is None:
                cleaned[target_str] = None
                continue
            if not isinstance(ranges, dict):
                continue
            normalized_ranges: dict[str, str | None] = {}
            for src_range, dst_range in ranges.items():
                if not isinstance(src_range, (str, int)):
                    continue
                if dst_range is not None and not isinstance(dst_range, str):
                    continue
                source_range = str(src_range)
                if not is_valid_source_range(source_range):
                    continue
                if dst_range is not None and not is_valid_target_range(dst_range):
                    continue
                normalized_ranges[source_range] = dst_range
            cleaned[target_str] = normalized_ranges
        return cleaned

    def _merge_targets(
        self,
        upstream: dict[str, dict[str, str | None] | None],
        custom: dict[str, dict[str, str | None] | None],
    ) -> dict[str, dict[str, str | None] | None]:
        """Overlay custom targets onto upstream targets, honoring deletions."""
        merged: dict[str, dict[str, str | None] | None] = copy.deepcopy(upstream)
        for target, ranges in custom.items():
            if ranges is None:
                merged[target] = None
                continue
            bucket = merged.setdefault(target, {}) or {}
            merged[target] = bucket
            for src_range, dst_range in ranges.items():
                bucket[src_range] = dst_range
        return merged

    def _build_range_views(
        self,
        upstream_ranges: dict[str, str | None],
        custom_ranges: dict[str, str | None] | None,
        effective_ranges: dict[str, str | None] | None,
        *,
        custom_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        """Build a list of range view objects for a target descriptor."""
        ranges: list[dict[str, Any]] = []
        keys = (
            set(upstream_ranges.keys())
            | (set(custom_ranges.keys()) if custom_ranges else set())
            | (set(effective_ranges.keys()) if effective_ranges else set())
        )

        for source_range in sorted(keys):
            upstream_val = upstream_ranges.get(source_range)
            custom_val = (custom_ranges or {}).get(source_range)
            effective_val = (effective_ranges or {}).get(source_range)
            explicit_remove = (
                custom_ranges is not None
                and source_range in custom_ranges
                and custom_val is None
            )
            origin = "upstream"
            if custom_deleted:
                origin = "deleted"
            elif custom_ranges is not None and (
                custom_val is not None or explicit_remove
            ):
                origin = "custom"
            ranges.append(
                {
                    "source_range": source_range,
                    "upstream": upstream_val,
                    "custom": custom_val,
                    "effective": effective_val,
                    "origin": origin,
                    "inherited": (
                        not custom_deleted
                        and not explicit_remove
                        and custom_val is None
                        and upstream_val is not None
                    ),
                }
            )

        return ranges

    def _build_target_views(
        self,
        upstream: dict[str, dict[str, str | None] | None],
        custom: dict[str, dict[str, str | None] | None],
        effective: dict[str, dict[str, str | None] | None],
    ) -> list[dict[str, Any]]:
        """Construct per-target mapping views with origin metadata."""
        keys = set(upstream.keys()) | set(custom.keys()) | set(effective.keys())
        entries: list[dict[str, Any]] = []

        for target_key in sorted(keys):
            try:
                parsed = parse_mapping_descriptor(target_key)
            except ValueError:
                continue

            provider, entry_id, scope = parsed
            descriptor_str = descriptor_key(parsed)

            upstream_raw = upstream.get(target_key)
            upstream_ranges: dict[str, str | None] = upstream_raw or {}
            custom_raw = custom.get(target_key)
            custom_ranges: dict[str, str | None] = custom_raw or {}
            effective_ranges: dict[str, str | None] = effective.get(target_key) or {}

            custom_deleted = target_key in custom and custom_raw is None

            origin = (
                "deleted"
                if custom_deleted
                else ("custom" if custom_raw else "upstream")
            )
            if upstream_ranges and custom_raw and not custom_deleted:
                origin = "mixed"

            entries.append(
                {
                    "descriptor": descriptor_str,
                    "provider": provider,
                    "entry_id": entry_id,
                    "scope": scope,
                    "origin": origin,
                    "deleted": custom_deleted,
                    "ranges": self._build_range_views(
                        upstream_ranges,
                        custom_ranges,
                        effective_ranges,
                        custom_deleted=custom_deleted,
                    ),
                }
            )

        return entries

    async def get_mapping_detail(self, descriptor: str) -> dict[str, Any]:
        """Fetch mapping detail with layered upstream/custom targets.

        Args:
            descriptor (str): The mapping descriptor to retrieve.

        Returns:
            dict[str, Any]: The mapping detail data structure.
        """
        parsed = parse_mapping_descriptor(descriptor)
        provider, entry_id, scope = parsed
        descriptor_str = descriptor_key(parsed)

        async with self._lock:
            custom_raw, _, _ = self._load_raw()
        upstream_raw = await self._load_upstream()

        upstream_targets = self._normalize_targets(
            (upstream_raw or {}).get(descriptor_str, {})
        )
        custom_targets = self._normalize_targets(
            (custom_raw or {}).get(descriptor_str, {})
        )
        effective_targets = self._merge_targets(upstream_targets, custom_targets)

        return {
            "descriptor": descriptor_str,
            "provider": provider,
            "entry_id": entry_id,
            "scope": scope,
            "layers": {
                "upstream": upstream_targets,
                "custom": custom_targets,
                "effective": effective_targets,
            },
            "targets": self._build_target_views(
                upstream_targets, custom_targets, effective_targets
            ),
        }

    def _validate_ranges(
        self, ranges: list[dict[str, Any]] | None
    ) -> dict[str, str | None]:
        """Validate and normalize range inputs."""
        if not ranges:
            return {}

        normalized: dict[str, str | None] = {}
        for raw in ranges:
            source_range = str(raw.get("source_range", "")).strip()
            dest = raw.get("destination_range")
            if not source_range:
                raise MappingError("source_range is required for each range")
            if dest is not None and not isinstance(dest, str):
                raise MappingError("destination_range must be a string or null")
            if not is_valid_source_range(source_range):
                raise MappingError(
                    "source_range must match the mapping schema (no commas)"
                )
            if dest is not None and not is_valid_target_range(dest):
                raise MappingError(
                    "destination_range must match the mapping schema "
                    "(comma-separated target ranges only)"
                )
            normalized[source_range] = dest

        return normalized

    def _validate_targets(
        self, targets: list[dict[str, Any]] | None
    ) -> dict[str, dict[str, str | None] | None]:
        """Validate target payloads and return a mapping dict."""
        if not targets:
            return {}

        normalized: dict[str, dict[str, str | None] | None] = {}
        for entry in targets:
            provider = str(entry.get("provider", "")).strip()
            entry_id = str(entry.get("entry_id", "")).strip()
            raw_scope = str(entry.get("scope", "") or "").strip()
            if not provider or not entry_id:
                raise MappingError("provider and entry_id are required")
            scope = raw_scope or None
            target = (provider, entry_id, scope)
            target_key = descriptor_key(target)
            if entry.get("deleted") is True:
                normalized[target_key] = None
                continue
            ranges = self._validate_ranges(entry.get("ranges"))
            if ranges:
                normalized[target_key] = ranges

        return normalized

    async def _sync_database(self) -> None:
        """Trigger a synchronization of the AniMap database."""
        scheduler = self._ensure_scheduler()
        await scheduler.trigger_database_sync(source="service:mapping_overrides")

    async def save_override(
        self,
        *,
        descriptor: str | None,
        targets: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Persist overrides for a descriptor and trigger DB sync.

        Args:
            descriptor (str | None): The mapping descriptor to override.
            targets (list[dict[str, Any]] | None): Target mapping override payloads.

        Returns:
            dict[str, Any]: The updated mapping detail after saving.
        """
        if descriptor is None:
            raise MissingDescriptorError("descriptor is required")

        parsed = parse_mapping_descriptor(descriptor)
        descriptor_str = descriptor_key(parsed)
        target_map = self._validate_targets(targets)

        async with self._lock:
            raw, path, fmt = self._load_raw()
            if target_map:
                raw[descriptor_str] = target_map
            else:
                raw.pop(descriptor_str, None)
            self._write_raw(raw, path, fmt)

        await self._sync_database()
        return await self.get_mapping_detail(descriptor_str)


@cache
def get_mapping_overrides_service() -> MappingOverridesService:
    """Return a singleton mapping overrides service instance.

    Returns:
        MappingOverridesService: The singleton service instance.
    """
    return MappingOverridesService()
