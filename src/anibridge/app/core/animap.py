"""Animap client for v3 provider-range mappings."""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import md5
from itertools import batched
from pathlib import Path

from anibridge.utils.types import MappingDescriptor
from sqlalchemy import func
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql import delete, select, tuple_

from anibridge.app import log
from anibridge.app.config.database import db
from anibridge.app.core.mappings import MappingsClient
from anibridge.app.models.db.animap import AnimapEntry, AnimapMapping, AnimapProvenance
from anibridge.app.models.db.housekeeping import Housekeeping
from anibridge.app.utils.mapping_ranges import (
    is_valid_source_range,
    is_valid_target_range,
)

__all__ = [
    "AnimapClient",
    "AnimapEdge",
    "descriptor_key",
    "parse_mapping_descriptor",
]


_DESCRIPTOR_PATTERN = re.compile(
    r"^(?P<provider>\w+):(?P<entry>\w+)(?::(?P<scope>\w+))?$"
)


def descriptor_key(descriptor: MappingDescriptor) -> str:
    """Return a stable string key for a descriptor tuple."""
    provider, entry_id, scope = descriptor
    return f"{provider}:{entry_id}:{scope}" if scope else f"{provider}:{entry_id}"


def parse_mapping_descriptor(raw: str) -> MappingDescriptor:
    """Parse a descriptor string into its tuple representation."""
    match = _DESCRIPTOR_PATTERN.match(raw)
    if not match:
        raise ValueError("Invalid mapping descriptor")
    scope = match.group("scope") or None
    return (match.group("provider"), match.group("entry"), scope)


@dataclass(frozen=True, slots=True)
class AnimapEdge:
    """Directed mapping between two provider entries with episode ranges."""

    source: MappingDescriptor
    destination: MappingDescriptor
    source_range: str
    destination_range: str | None


class AnimapClient:
    """Client for managing Animap data using the v3 range-based schema."""

    _SQLITE_SAFE_VARIABLES = 900
    _MAPPINGS_HASH_KEY = "animap_mappings_hash"
    _PROVENANCE_HASH_KEY = "animap_provenance_hash"

    def __init__(self, data_path: Path, upstream_url: str | None) -> None:
        """Create a new Animap client."""
        self.data_path = data_path
        self.upstream_url = upstream_url
        self.mappings_client = MappingsClient(data_path, upstream_url)

    async def initialize(self) -> None:
        """Initialize and immediately sync the local database."""
        try:
            await self.sync_db()
        except Exception as exc:
            log.exception("Failed to sync database: %s", exc)
            raise

    async def close(self) -> None:
        """Close the underlying mappings client session."""
        await self.mappings_client.close()

    async def __aenter__(self):
        """Enter async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context manager and close resources."""
        await self.close()

    def resolve_edges(
        self,
        descriptors: Sequence[MappingDescriptor],
        *,
        target_providers: set[str] | frozenset[str] | None = None,
    ) -> tuple[AnimapEdge, ...]:
        """Resolve mapping edges for the provided source descriptors."""
        if not descriptors:
            return tuple()

        descriptor_list = list({descriptor for descriptor in descriptors})
        source_ids: list[int] = []
        with db() as ctx:
            source_ids.extend(self._select_entry_ids(ctx.session, descriptor_list))

            if not source_ids:
                return tuple()

            edges: list[AnimapEdge] = []
            for chunk in batched(source_ids, self._SQLITE_SAFE_VARIABLES, strict=False):
                source_entry = AnimapEntry
                dest_entry = aliased(AnimapEntry)
                query = (
                    select(
                        source_entry.provider,
                        source_entry.entry_id,
                        source_entry.entry_scope,
                        dest_entry.provider,
                        dest_entry.entry_id,
                        dest_entry.entry_scope,
                        AnimapMapping.source_range,
                        AnimapMapping.destination_range,
                    )
                    .select_from(AnimapMapping)
                    .join(
                        source_entry,
                        AnimapMapping.source_entry_id == source_entry.id,
                    )
                    .join(
                        dest_entry,
                        AnimapMapping.destination_entry_id == dest_entry.id,
                    )
                    .where(AnimapMapping.source_entry_id.in_(chunk))
                )
                if target_providers:
                    query = query.where(dest_entry.provider.in_(target_providers))

                rows = ctx.session.execute(query).all()
                for row in rows:
                    (
                        src_provider,
                        src_entry_id,
                        src_scope,
                        dst_provider,
                        dst_entry_id,
                        dst_scope,
                        src_range,
                        dst_range,
                    ) = row
                    edges.append(
                        AnimapEdge(
                            source=(src_provider, src_entry_id, src_scope),
                            destination=(dst_provider, dst_entry_id, dst_scope),
                            source_range=src_range,
                            destination_range=dst_range,
                        )
                    )

        return tuple(edges)

    def resolve_edges_grouped(
        self,
        descriptors: Sequence[MappingDescriptor],
        *,
        target_providers: set[str] | frozenset[str] | None = None,
    ) -> dict[MappingDescriptor, dict[MappingDescriptor, list[str]]]:
        """Resolve mapping edges into a grouped target->source mapping."""
        grouped: dict[MappingDescriptor, dict[MappingDescriptor, list[str]]] = {}
        for edge in self.resolve_edges(descriptors, target_providers=target_providers):
            target_ranges = grouped.setdefault(edge.destination, {})
            source_ranges = target_ranges.setdefault(edge.source, [])
            source_ranges.append(edge.source_range)
        return grouped

    @classmethod
    def _select_entry_ids(
        cls,
        session: Session,
        descriptors: Sequence[MappingDescriptor],
    ) -> list[int]:
        """Return entry IDs matching the provided descriptors."""
        entry_ids: list[int] = []
        for chunk in batched(descriptors, cls._SQLITE_SAFE_VARIABLES, strict=False):
            normalized = [
                (provider, entry_id, scope or "") for provider, entry_id, scope in chunk
            ]
            if not normalized:
                continue
            scope_key = func.coalesce(AnimapEntry.entry_scope, "")
            rows = session.execute(
                select(AnimapEntry).where(
                    tuple_(
                        AnimapEntry.provider,
                        AnimapEntry.entry_id,
                        scope_key,
                    ).in_(normalized)
                )
            ).scalars()
            entry_ids.extend(entry.id for entry in rows)
        return entry_ids

    def _sync_provenance_rows(
        self, session: Session, desired: dict[int, list[str]]
    ) -> None:
        """Ensure provenance rows align with the desired mapping sources."""
        mapping_ids = list(desired.keys())
        if not mapping_ids:
            return

        existing: dict[int, list[str]] = {}
        for chunk in batched(mapping_ids, self._SQLITE_SAFE_VARIABLES, strict=False):
            rows = (
                session.execute(
                    select(AnimapProvenance)
                    .where(AnimapProvenance.mapping_id.in_(chunk))
                    .order_by(AnimapProvenance.mapping_id, AnimapProvenance.n)
                )
                .scalars()
                .all()
            )
            for row in rows:
                existing.setdefault(row.mapping_id, []).append(row.source)

        ids_to_refresh: list[int] = []
        rows_to_upsert: list[dict[str, object]] = []
        to_delete_pairs: list[tuple[int, int]] = []

        for mapping_id, sources in desired.items():
            existing_sources = existing.get(mapping_id, [])
            if sources != existing_sources:
                ids_to_refresh.append(mapping_id)

                # Prepare upsert rows (mapping_id, n, source)
                for i, source in enumerate(sources):
                    rows_to_upsert.append(
                        {"mapping_id": mapping_id, "n": i, "source": source}
                    )

                # If existing had extra indices, schedule them for deletion
                if len(existing_sources) > len(sources):
                    for i in range(len(sources), len(existing_sources)):
                        to_delete_pairs.append((mapping_id, i))

        if not ids_to_refresh:
            return

        # Perform upsert for desired rows using SQLite ON CONFLICT DO UPDATE
        if rows_to_upsert:
            for chunk in batched(
                rows_to_upsert, self._SQLITE_SAFE_VARIABLES, strict=False
            ):
                session.execute(
                    insert(AnimapProvenance)
                    .values(chunk)
                    .on_conflict_do_update(
                        index_elements=["mapping_id", "n"],
                        set_={"source": insert(AnimapProvenance).excluded.source},
                    )
                )

        # Delete any extra provenance rows that are no longer desired
        if to_delete_pairs:
            # Delete in chunks to avoid SQLite variable limits
            for chunk in batched(
                to_delete_pairs, self._SQLITE_SAFE_VARIABLES, strict=False
            ):
                session.execute(
                    delete(AnimapProvenance).where(
                        tuple_(AnimapProvenance.mapping_id, AnimapProvenance.n).in_(
                            chunk
                        )
                    )
                )

    def _build_edges(
        self,
        mappings: dict,
        provenance_by_descriptor: dict[str, list[str]],
    ) -> tuple[
        dict[str, MappingDescriptor],
        dict[tuple[str, str, str, str | None], AnimapEdge],
        dict[tuple[str, str, str, str | None], list[str]],
        int,
    ]:
        """Convert raw mappings into descriptor pairs and directed edges."""
        descriptors: dict[str, MappingDescriptor] = {}
        edges: dict[tuple[str, str, str, str | None], AnimapEdge] = {}
        provenance: dict[tuple[str, str, str, str | None], list[str]] = {}
        invalid_count = 0

        for raw_source, targets in mappings.items():
            try:
                source_desc = parse_mapping_descriptor(raw_source)
            except ValueError:
                log.warning(
                    "Invalid mapping descriptor $$'%s'$$; skipped",
                    raw_source,
                )
                invalid_count += 1
                continue

            descriptors[descriptor_key(source_desc)] = source_desc

            if not isinstance(targets, dict):
                log.warning(
                    "Descriptor $$'%s'$$ has non-object target payload; skipped",
                    raw_source,
                )
                invalid_count += 1
                continue

            for raw_target, ranges in targets.items():
                try:
                    target_desc = parse_mapping_descriptor(raw_target)
                except ValueError:
                    log.warning(
                        "Invalid target descriptor $$'%s'$$ under $$'%s'$$; skipped",
                        raw_target,
                        raw_source,
                    )
                    invalid_count += 1
                    continue

                descriptors[descriptor_key(target_desc)] = target_desc

                if ranges is None:
                    continue
                if not isinstance(ranges, dict):
                    log.warning(
                        "Descriptor $$'%s'$$ → $$'%s'$$ has non-object ranges; skipped",
                        raw_source,
                        raw_target,
                    )
                    invalid_count += 1
                    continue

                for source_range, destination_range in ranges.items():
                    if not isinstance(source_range, str) or not source_range:
                        invalid_count += 1
                        continue
                    if destination_range is not None and not isinstance(
                        destination_range, str
                    ):
                        invalid_count += 1
                        continue
                    if not is_valid_source_range(source_range):
                        log.warning(
                            "Invalid source range $$'%s'$$ under $$'%s'$$ → "
                            "$$'%s'$$; skipped",
                            source_range,
                            raw_source,
                            raw_target,
                        )
                        invalid_count += 1
                        continue
                    if destination_range is not None and not is_valid_target_range(
                        destination_range
                    ):
                        log.warning(
                            "Invalid destination range $$'%s'$$ under $$'%s'$$ → "
                            "$$'%s'$$; skipped",
                            destination_range,
                            raw_source,
                            raw_target,
                        )
                        invalid_count += 1
                        continue

                    key = (
                        descriptor_key(source_desc),
                        descriptor_key(target_desc),
                        source_range,
                        destination_range,
                    )
                    if key not in edges:
                        edges[key] = AnimapEdge(
                            source=source_desc,
                            destination=target_desc,
                            source_range=source_range,
                            destination_range=destination_range,
                        )
                    provenance.setdefault(key, []).extend(
                        provenance_by_descriptor.get(raw_source, [])
                    )

        # Deduplicate provenance lists while preserving order
        for key, values in provenance.items():
            seen: set[str] = set()
            deduped: list[str] = []
            for value in values:
                if value in seen:
                    continue
                seen.add(value)
                deduped.append(value)
            provenance[key] = deduped

        return descriptors, edges, provenance, invalid_count

    async def sync_db(self) -> None:
        """Synchronize the local database with the upstream mappings."""
        mappings = await self.mappings_client.load_mappings()
        provenance_by_descriptor = self.mappings_client.get_provenance()

        curr_mappings_hash = md5(
            json.dumps(mappings, sort_keys=True).encode()
        ).hexdigest()
        curr_provenance_hash = md5(
            json.dumps(provenance_by_descriptor, sort_keys=True).encode()
        ).hexdigest()

        with db() as ctx:
            stored_mappings_hash = ctx.session.get(
                Housekeeping, self._MAPPINGS_HASH_KEY
            )
            stored_provenance_hash = ctx.session.get(
                Housekeeping, self._PROVENANCE_HASH_KEY
            )
            if (
                stored_mappings_hash is not None
                and stored_mappings_hash.value == curr_mappings_hash
                and stored_provenance_hash is not None
                and stored_provenance_hash.value == curr_provenance_hash
            ):
                log.debug("Mappings and provenance unchanged; skipping sync")
                return

        descriptors, edges, provenance, invalid_count = self._build_edges(
            mappings, provenance_by_descriptor
        )
        log.debug(
            "Parsed mappings into %s descriptors, %s edges",
            len(descriptors),
            len(edges),
        )
        if invalid_count:
            log.warning(
                "Skipped %s invalid mapping entries during parse",
                invalid_count,
            )
        with db() as ctx:
            existing_entries = {
                descriptor_key((provider, entry_id, entry_scope)): entry_id_db
                for entry_id_db, provider, entry_id, entry_scope in ctx.session.execute(
                    select(
                        AnimapEntry.id,
                        AnimapEntry.provider,
                        AnimapEntry.entry_id,
                        AnimapEntry.entry_scope,
                    )
                )
            }

            new_entry_keys = set(descriptors.keys())
            existing_entry_keys = set(existing_entries.keys())

            to_delete_entries = existing_entry_keys - new_entry_keys
            to_insert_entries = new_entry_keys - existing_entry_keys

            log.debug(
                "Deleted %s and inserted %s animap entries",
                len(to_delete_entries),
                len(to_insert_entries),
            )

            if to_delete_entries:
                for chunk in batched(
                    [existing_entries[k] for k in to_delete_entries],
                    self._SQLITE_SAFE_VARIABLES,
                    strict=False,
                ):
                    ctx.session.execute(
                        delete(AnimapEntry).where(AnimapEntry.id.in_(chunk))
                    )

            # Upsert new entries using SQLite INSERT ... ON CONFLICT DO NOTHING
            if to_insert_entries:
                rows: list[dict[str, object]] = []
                for key in to_insert_entries:
                    d = descriptors[key]
                    provider, entry_id, scope = d
                    rows.append(
                        {
                            "provider": provider,
                            "entry_id": entry_id,
                            "entry_scope": scope,
                        }
                    )
                for chunk in batched(rows, self._SQLITE_SAFE_VARIABLES, strict=False):
                    ctx.session.execute(
                        insert(AnimapEntry)
                        .values(chunk)
                        .on_conflict_do_nothing(
                            index_elements=["provider", "entry_id", "entry_scope"]
                        )
                    )
                ctx.session.flush()

            # Refresh entry map after inserts
            existing_entries = {
                descriptor_key((provider, entry_id, entry_scope)): entry_id_db
                for entry_id_db, provider, entry_id, entry_scope in ctx.session.execute(
                    select(
                        AnimapEntry.id,
                        AnimapEntry.provider,
                        AnimapEntry.entry_id,
                        AnimapEntry.entry_scope,
                    )
                )
            }

            # Translate edge keys to entry-id keyed tuples
            edge_id_keys: set[tuple[int, int, str, str | None]] = set()
            provenance_by_id_key: dict[tuple[int, int, str, str | None], list[str]] = {}
            for key in edges:
                src_key, dst_key, source_range, destination_range = key
                src_entry_id = existing_entries.get(src_key)
                dst_entry_id = existing_entries.get(dst_key)
                if src_entry_id is None or dst_entry_id is None:
                    continue
                id_key = (src_entry_id, dst_entry_id, source_range, destination_range)
                edge_id_keys.add(id_key)
                provenance_by_id_key[id_key] = provenance.get(key, [])

            existing_mappings: dict[tuple[int, int, str, str | None], int] = {
                (
                    source_entry_id,
                    destination_entry_id,
                    source_range,
                    destination_range,
                ): mapping_id
                for (
                    mapping_id,
                    source_entry_id,
                    destination_entry_id,
                    source_range,
                    destination_range,
                ) in ctx.session.execute(
                    select(
                        AnimapMapping.id,
                        AnimapMapping.source_entry_id,
                        AnimapMapping.destination_entry_id,
                        AnimapMapping.source_range,
                        AnimapMapping.destination_range,
                    )
                )
            }

            new_keys = edge_id_keys
            existing_keys = set(existing_mappings.keys())

            to_delete_mappings = existing_keys - new_keys
            to_insert_mappings = new_keys - existing_keys

            log.debug(
                "Deleted %s and inserted %s animap mappings",
                len(to_delete_mappings),
                len(to_insert_mappings),
            )

            if to_delete_mappings:
                for chunk in batched(
                    list(to_delete_mappings),
                    self._SQLITE_SAFE_VARIABLES,
                    strict=False,
                ):
                    ctx.session.execute(
                        delete(AnimapMapping).where(
                            tuple_(
                                AnimapMapping.source_entry_id,
                                AnimapMapping.destination_entry_id,
                                AnimapMapping.source_range,
                                AnimapMapping.destination_range,
                            ).in_(chunk)
                        )
                    )

            # Upsert new mappings using SQLite INSERT ... ON CONFLICT DO NOTHING
            if to_insert_mappings:
                mapping_rows: list[dict[str, object]] = []
                for key in to_insert_mappings:
                    s_eid, d_eid, s_range, d_range = key
                    mapping_rows.append(
                        {
                            "source_entry_id": s_eid,
                            "destination_entry_id": d_eid,
                            "source_range": s_range,
                            "destination_range": d_range,
                        }
                    )
                for chunk in batched(
                    mapping_rows, self._SQLITE_SAFE_VARIABLES, strict=False
                ):
                    ctx.session.execute(
                        insert(AnimapMapping)
                        .values(chunk)
                        .on_conflict_do_nothing(
                            index_elements=[
                                "source_entry_id",
                                "destination_entry_id",
                                "source_range",
                                "destination_range",
                            ]
                        )
                    )
                ctx.session.flush()

            # Refresh mapping map to include newly inserted rows (ids now populated)
            existing_mappings = {
                (
                    source_entry_id,
                    destination_entry_id,
                    source_range,
                    destination_range,
                ): mapping_id
                for (
                    mapping_id,
                    source_entry_id,
                    destination_entry_id,
                    source_range,
                    destination_range,
                ) in ctx.session.execute(
                    select(
                        AnimapMapping.id,
                        AnimapMapping.source_entry_id,
                        AnimapMapping.destination_entry_id,
                        AnimapMapping.source_range,
                        AnimapMapping.destination_range,
                    )
                )
            }

            desired_provenance: dict[int, list[str]] = {}
            for key, sources in provenance_by_id_key.items():
                mapping_id = existing_mappings.get(key)
                if mapping_id is not None:
                    desired_provenance[mapping_id] = sources

            self._sync_provenance_rows(ctx.session, desired_provenance)

            ctx.session.merge(
                Housekeeping(key=self._MAPPINGS_HASH_KEY, value=curr_mappings_hash)
            )
            ctx.session.merge(
                Housekeeping(
                    key=self._PROVENANCE_HASH_KEY,
                    value=curr_provenance_hash,
                )
            )

            ctx.session.commit()

            existing_pairs = {
                (src_id, dst_id) for src_id, dst_id, _, _ in existing_keys
            }
            new_pairs = {(src_id, dst_id) for src_id, dst_id, _, _ in new_keys}
            removed_pairs = existing_pairs - new_pairs
            created_pairs = new_pairs - existing_pairs
            changed_pairs = {
                (src_id, dst_id)
                for src_id, dst_id, _, _ in (to_delete_mappings | to_insert_mappings)
            }
            updated_pairs = changed_pairs - removed_pairs - created_pairs

            log.success(
                "Mappings database sync complete: %s removed, %s updated, %s created",
                len(removed_pairs),
                len(updated_pairs),
                len(created_pairs),
            )
