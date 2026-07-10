"""Animap client for v3 authority-range mappings."""

from collections.abc import Sequence, Set
from hashlib import md5
from itertools import batched
from pathlib import Path

import msgspec
import msgspec.json
from anibridge.utils.mappings import (
    AnibridgeMapping,
    descriptor_key,
    parse_mapping_descriptor,
)
from anibridge.utils.types import MappingDescriptor
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql import delete, select, tuple_
from sqlalchemy.sql.functions import func

from anibridge.app.config.database import db
from anibridge.app.core.mappings import MappingsClient
from anibridge.app.logging import get_logger
from anibridge.app.models.db.animap import AnimapEntry, AnimapMapping, AnimapProvenance
from anibridge.app.models.db.housekeeping import Housekeeping
from anibridge.app.utils.memory import release_memory

__all__ = [
    "AnimapClient",
    "AnimapEdge",
]

log = get_logger(__name__)


class AnimapEdge(msgspec.Struct, frozen=True):
    """Directed mapping between two authority entries with episode ranges."""

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
        target_authorities: Set[str] | None = None,
    ) -> tuple[AnimapEdge, ...]:
        """Resolve mapping edges for the provided source descriptors."""
        if not descriptors:
            return ()

        descriptor_list = list(set(descriptors))
        source_ids: list[int] = []
        with db() as ctx:
            source_ids.extend(self._select_entry_ids(ctx.session, descriptor_list))

            if not source_ids:
                return ()

            edges: list[AnimapEdge] = []
            for chunk in batched(source_ids, self._SQLITE_SAFE_VARIABLES, strict=False):
                source_entry = AnimapEntry
                dest_entry = aliased(AnimapEntry)
                query = (
                    select(
                        source_entry.authority,
                        source_entry.value,
                        source_entry.scope,
                        dest_entry.authority,
                        dest_entry.value,
                        dest_entry.scope,
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
                if target_authorities:
                    query = query.where(dest_entry.authority.in_(target_authorities))

                rows = ctx.session.execute(query).all()
                for row in rows:
                    (
                        src_authority,
                        src_value,
                        src_scope,
                        dst_authority,
                        dst_value,
                        dst_scope,
                        src_range,
                        dst_range,
                    ) = row
                    edges.append(
                        AnimapEdge(
                            source=(src_authority, src_value, src_scope),
                            destination=(dst_authority, dst_value, dst_scope),
                            source_range=src_range,
                            destination_range=dst_range,
                        )
                    )

        return tuple(edges)

    def resolve_edges_grouped(
        self,
        descriptors: Sequence[MappingDescriptor],
        *,
        target_authorities: Set[str] | None = None,
    ) -> dict[MappingDescriptor, dict[MappingDescriptor, list[tuple[str, str | None]]]]:
        """Resolve mapping edges into a grouped target->source mapping."""
        grouped: dict[
            MappingDescriptor,
            dict[MappingDescriptor, list[tuple[str, str | None]]],
        ] = {}
        for edge in self.resolve_edges(
            descriptors,
            target_authorities=target_authorities,
        ):
            target_ranges = grouped.setdefault(edge.destination, {})
            source_ranges = target_ranges.setdefault(edge.source, [])
            source_ranges.append((edge.source_range, edge.destination_range))
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
                (authority, value, scope or "") for authority, value, scope in chunk
            ]
            if not normalized:
                continue
            scope_key = func.coalesce(AnimapEntry.scope, "")
            rows = session.execute(
                select(AnimapEntry).where(
                    tuple_(
                        AnimapEntry.authority,
                        AnimapEntry.value,
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
        mapping_ids = tuple(desired)
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
                    to_delete_pairs.extend(
                        (mapping_id, i)
                        for i in range(len(sources), len(existing_sources))
                    )

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
        set[tuple[str, str, str, str | None]],
        dict[tuple[str, str, str, str | None], list[str]],
        int,
    ]:
        """Convert raw mappings into descriptor pairs and directed edge keys."""
        descriptors: dict[str, MappingDescriptor] = {}
        edge_keys: set[tuple[str, str, str, str | None]] = set()
        provenance: dict[tuple[str, str, str, str | None], list[str]] = {}
        invalid_count = 0

        # Pop items to release inner dicts as they are processed.
        while mappings:
            raw_source, targets = mappings.popitem()
            try:
                source_desc = parse_mapping_descriptor(raw_source)
            except ValueError:
                log.warning(
                    "Invalid mapping descriptor $$'%s'$$; skipped",
                    raw_source,
                )
                invalid_count += 1
                continue

            src_dkey = descriptor_key(source_desc)
            descriptors[src_dkey] = source_desc

            if not isinstance(targets, dict):
                log.warning(
                    "Descriptor $$'%s'$$ has non-object target payload; skipped",
                    raw_source,
                )
                invalid_count += 1
                continue

            src_prov = provenance_by_descriptor.get(raw_source, [])

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

                dst_dkey = descriptor_key(target_desc)
                descriptors[dst_dkey] = target_desc

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
                    source_range = str(source_range)
                    if not source_range:
                        invalid_count += 1
                        continue
                    if destination_range is None:
                        continue
                    destination_range = str(destination_range)
                    if not destination_range:
                        invalid_count += 1
                        continue
                    try:
                        AnibridgeMapping.parse(source_range, destination_range)
                    except ValueError as e:
                        log.warning(
                            "Invalid mapping $$'%s'$$ under $$'%s'$$ → $$'%s'$$: %s",
                            source_range,
                            raw_source,
                            raw_target,
                            e,
                        )
                        invalid_count += 1
                        continue

                    key = (src_dkey, dst_dkey, source_range, destination_range)
                    edge_keys.add(key)
                    if src_prov:
                        prov_list = provenance.get(key)
                        if prov_list is None:
                            provenance[key] = list(src_prov)
                        else:
                            prov_list.extend(src_prov)

        # Deduplicate provenance lists while preserving order
        for key, values in provenance.items():
            seen: set[str] = set()
            deduped: list[str] = []
            for value in values:
                if value not in seen:
                    seen.add(value)
                    deduped.append(value)
            provenance[key] = deduped

        return descriptors, edge_keys, provenance, invalid_count

    async def sync_db(self) -> None:
        """Synchronize the local database with the upstream mappings."""
        mappings = await self.mappings_client.load_mappings()
        provenance_by_descriptor = self.mappings_client.get_provenance()

        curr_mappings_hash = self.mappings_client.get_content_hash()
        curr_provenance_hash = md5(
            msgspec.json.encode(dict(sorted(provenance_by_descriptor.items())))
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
                del mappings, provenance_by_descriptor
                self.mappings_client.clear_cache()
                await self.mappings_client.close()
                release_memory()
                return

        descriptors, edge_keys, provenance, invalid_count = self._build_edges(
            mappings, provenance_by_descriptor
        )
        del mappings, provenance_by_descriptor

        log.debug(
            "Parsed mappings into %s descriptors, %s edges",
            len(descriptors),
            len(edge_keys),
        )
        if invalid_count:
            log.warning(
                "Skipped %s invalid mapping entries during parse",
                invalid_count,
            )
        with db() as ctx:
            existing_entries = {
                descriptor_key((authority, value, scope)): entry_id_db
                for entry_id_db, authority, value, scope in ctx.session.execute(
                    select(
                        AnimapEntry.id,
                        AnimapEntry.authority,
                        AnimapEntry.value,
                        AnimapEntry.scope,
                    )
                )
            }

            new_entry_keys = set(descriptors)
            existing_entry_keys = set(existing_entries)

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
                    authority, value, scope = d
                    rows.append(
                        {
                            "authority": authority,
                            "value": value,
                            "scope": scope,
                        }
                    )
                for chunk in batched(rows, self._SQLITE_SAFE_VARIABLES, strict=False):
                    ctx.session.execute(
                        insert(AnimapEntry)
                        .values(chunk)
                        .on_conflict_do_nothing(
                            index_elements=["authority", "value", "scope"]
                        )
                    )
                ctx.session.flush()

            del descriptors, new_entry_keys, existing_entry_keys
            del to_delete_entries, to_insert_entries

            # Refresh entry map after inserts
            existing_entries = {
                descriptor_key((authority, value, scope)): entry_id_db
                for entry_id_db, authority, value, scope in ctx.session.execute(
                    select(
                        AnimapEntry.id,
                        AnimapEntry.authority,
                        AnimapEntry.value,
                        AnimapEntry.scope,
                    )
                )
            }

            # Translate edge keys to entry-id keyed tuples and release the
            # descriptor-keyed dicts immediately afterwards.
            edge_id_keys: set[tuple[int, int, str, str | None]] = set()
            provenance_by_id_key: dict[tuple[int, int, str, str | None], list[str]] = {}
            for key in edge_keys:
                src_key, dst_key, source_range, destination_range = key
                src_entry_id = existing_entries.get(src_key)
                dst_entry_id = existing_entries.get(dst_key)
                if src_entry_id is None or dst_entry_id is None:
                    continue
                id_key = (src_entry_id, dst_entry_id, source_range, destination_range)
                edge_id_keys.add(id_key)
                provenance_by_id_key[id_key] = provenance.get(key, [])

            del edge_keys, provenance, existing_entries

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
            existing_keys = set(existing_mappings)

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

            n_removed = len(to_delete_mappings)
            n_created = len(to_insert_mappings)
            del to_delete_mappings, to_insert_mappings, existing_keys, new_keys

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

            del edge_id_keys, provenance_by_id_key, existing_mappings

            self._sync_provenance_rows(ctx.session, desired_provenance)
            del desired_provenance

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

            log.success(
                "Mappings database sync complete: %s removed, %s created",
                n_removed,
                n_created,
            )

        self.mappings_client.clear_cache()
        await self.mappings_client.close()
        release_memory()
