"""Animap client for v3 provider-range mappings."""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import md5
from itertools import batched
from pathlib import Path

from anibridge.list import MappingDescriptor, MappingEdge, MappingGraph
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import delete, select, tuple_

from src import log
from src.config.database import db
from src.core.mappings import MappingsClient
from src.models.db.animap import AnimapEntry, AnimapMapping, AnimapProvenance
from src.models.db.housekeeping import Housekeeping
from src.utils.mapping_ranges import is_valid_source_range, is_valid_target_range

__all__ = [
    "AnimapClient",
    "AnimapEdge",
    "AnimapGraph",
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
class AnimapEdge(MappingEdge):
    """Directed mapping between two provider entries with episode ranges."""

    source: MappingDescriptor
    destination: MappingDescriptor
    source_range: str
    destination_range: str | None


@dataclass(frozen=True, slots=True)
class AnimapGraph(MappingGraph):
    """Subset of the mapping graph relevant to a lookup request."""

    edges: tuple[MappingEdge, ...]

    def descriptors(self) -> tuple[MappingDescriptor, ...]:
        """Return unique descriptors referenced by the edges."""
        seen: set[str] = set()
        ordered: list[MappingDescriptor] = []
        for edge in self.edges:
            for descriptor in (edge.source, edge.destination):
                key = descriptor_key(descriptor)
                if key in seen:
                    continue
                seen.add(key)
                ordered.append(descriptor)
        return tuple(ordered)


class AnimapClient:
    """Client for managing Animap data using the v3 range-based schema."""

    _SQLITE_SAFE_VARIABLES = 900

    def __init__(self, data_path: Path, upstream_url: str | None) -> None:
        """Create a new Animap client."""
        self.data_path = data_path
        self.upstream_url = upstream_url
        self.mappings_client = MappingsClient(data_path, upstream_url)
        self._edge_cache: tuple[AnimapEdge, ...] = tuple()
        self._adjacency: dict[tuple[str, str, str | None], tuple[int, ...]] = {}
        self._lookup_cache: dict[
            frozenset[tuple[str, str, str | None]], MappingGraph
        ] = {}
        self._cache_version: str | None = None

    async def initialize(self) -> None:
        """Initialize and immediately sync the local database."""
        try:
            await self.sync_db()
        except Exception as exc:
            log.error(f"Failed to sync database: {exc}", exc_info=True)
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
                log.warning(f"Invalid mapping descriptor $$'{raw_source}'$$; skipped")
                invalid_count += 1
                continue

            descriptors[descriptor_key(source_desc)] = source_desc

            if not isinstance(targets, dict):
                log.warning(
                    f"Descriptor $$'{raw_source}'$$ has non-object target payload; "
                    "skipped",
                )
                invalid_count += 1
                continue

            for raw_target, ranges in targets.items():
                try:
                    target_desc = parse_mapping_descriptor(raw_target)
                except ValueError:
                    log.warning(
                        f"Invalid target descriptor $$'{raw_target}'$$ under "
                        f"$$'{raw_source}'$$; skipped",
                    )
                    invalid_count += 1
                    continue

                descriptors[descriptor_key(target_desc)] = target_desc

                if ranges is None:
                    continue
                if not isinstance(ranges, dict):
                    log.warning(
                        f"Descriptor $$'{raw_source}'$$ → $$'{raw_target}'$$ has "
                        "non-object ranges; skipped",
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
                            f"Invalid source range $$'{source_range}'$$ under "
                            f"$$'{raw_source}'$$ → $$'{raw_target}'$$; skipped"
                        )
                        invalid_count += 1
                        continue
                    if destination_range is not None and not is_valid_target_range(
                        destination_range
                    ):
                        log.warning(
                            f"Invalid destination range $$'{destination_range}'$$ under"
                            f" $$'{raw_source}'$$ → $$'{raw_target}'$$; skipped"
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

    def _build_cache_from_edges(
        self, edges: tuple[AnimapEdge, ...], version: str
    ) -> None:
        """Populate in-memory adjacency for fast lookups."""
        adjacency: dict[tuple[str, str, str | None], list[int]] = {}
        for idx, edge in enumerate(edges):
            for descriptor in (edge.source, edge.destination):
                provider, entry_id, scope = descriptor
                adjacency.setdefault((provider, entry_id, scope), []).append(idx)

        self._edge_cache = edges
        self._adjacency = {key: tuple(ids) for key, ids in adjacency.items()}
        self._lookup_cache.clear()
        self._cache_version = version

    def _build_cache_from_db(self) -> None:
        """Rebuild the in-memory graph cache from the SQLite tables."""
        with db() as ctx:
            entries = {
                entry.id: entry
                for entry in ctx.session.execute(select(AnimapEntry)).scalars().all()
            }
            mappings = ctx.session.execute(select(AnimapMapping)).scalars().all()

        edges: list[AnimapEdge] = []

        for mapping in mappings:
            src_entry = entries.get(mapping.source_entry_id)
            dst_entry = entries.get(mapping.destination_entry_id)
            if not src_entry or not dst_entry:
                continue

            edges.append(
                AnimapEdge(
                    source=(
                        src_entry.provider,
                        src_entry.entry_id,
                        src_entry.entry_scope,
                    ),
                    destination=(
                        dst_entry.provider,
                        dst_entry.entry_id,
                        dst_entry.entry_scope,
                    ),
                    source_range=mapping.source_range,
                    destination_range=mapping.destination_range,
                )
            )

        self._build_cache_from_edges(tuple(edges), self._cache_version or "db")

    def _ensure_cache(self) -> None:
        if not self._edge_cache:
            self._build_cache_from_db()

    def get_graph_for_descriptors(
        self, descriptors: Sequence[MappingDescriptor]
    ) -> MappingGraph:
        """Return all edges touching any of the supplied mapping descriptors."""
        self._ensure_cache()

        if not descriptors:
            return AnimapGraph(edges=tuple())

        cache_key = frozenset(descriptors)
        if self._cache_version is not None and cache_key in self._lookup_cache:
            return self._lookup_cache[cache_key]

        edge_indexes: set[int] = set()
        for provider, entry_id, scope in cache_key:
            edge_indexes.update(self._adjacency.get((provider, entry_id, scope), ()))

        graph = (
            AnimapGraph(edges=tuple(self._edge_cache[i] for i in edge_indexes))
            if edge_indexes
            else AnimapGraph(edges=tuple())
        )

        if self._cache_version is not None:
            self._lookup_cache[cache_key] = graph

        return graph

    def get_graph_for_ids(self, external_ids: dict[str, str]) -> MappingGraph:
        """Return all edges touching the supplied external identifiers."""
        descriptor_tuples = [
            (provider, entry_id, None) for provider, entry_id in external_ids.items()
        ]
        return self.get_graph_for_descriptors(descriptor_tuples)

    async def sync_db(self) -> None:
        """Synchronize the local database with the upstream mappings."""
        self._edge_cache = tuple()
        self._adjacency = {}
        self._lookup_cache.clear()
        self._cache_version = None

        mappings = await self.mappings_client.load_mappings()
        provenance_by_descriptor = self.mappings_client.get_provenance()

        descriptors, edges, provenance, _invalid_count = self._build_edges(
            mappings, provenance_by_descriptor
        )
        edge_list = tuple(edges.values())

        curr_mappings_hash = md5(
            json.dumps(mappings, sort_keys=True).encode()
        ).hexdigest()

        with db() as ctx:
            existing_entries = {
                descriptor_key(
                    (entry.provider, entry.entry_id, entry.entry_scope)
                ): entry
                for entry in ctx.session.execute(select(AnimapEntry)).scalars().all()
            }

            new_entry_keys = set(descriptors.keys())
            existing_entry_keys = set(existing_entries.keys())

            to_delete_entries = existing_entry_keys - new_entry_keys
            to_insert_entries = new_entry_keys - existing_entry_keys

            if to_delete_entries:
                for chunk in batched(
                    [existing_entries[k].id for k in to_delete_entries],
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
                descriptor_key(
                    (entry.provider, entry.entry_id, entry.entry_scope)
                ): entry
                for entry in ctx.session.execute(select(AnimapEntry)).scalars().all()
            }

            # Translate edge keys to entry-id keyed tuples
            edge_key_to_ids: dict[tuple[int, int, str, str | None], AnimapEdge] = {}
            provenance_by_id_key: dict[tuple[int, int, str, str | None], list[str]] = {}
            for key, edge in edges.items():
                src_key, dst_key, source_range, destination_range = key
                src_entry = existing_entries.get(src_key)
                dst_entry = existing_entries.get(dst_key)
                if not src_entry or not dst_entry:
                    continue
                id_key = (src_entry.id, dst_entry.id, source_range, destination_range)
                edge_key_to_ids[id_key] = edge
                provenance_by_id_key[id_key] = provenance.get(key, [])

            existing_mappings: dict[tuple[int, int, str, str | None], AnimapMapping] = {
                (
                    mapping.source_entry_id,
                    mapping.destination_entry_id,
                    mapping.source_range,
                    mapping.destination_range,
                ): mapping
                for mapping in ctx.session.execute(select(AnimapMapping))
                .scalars()
                .all()
            }

            new_keys = set(edge_key_to_ids.keys())
            existing_keys = set(existing_mappings.keys())

            to_delete_mappings = existing_keys - new_keys
            to_insert_mappings = new_keys - existing_keys

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
                    mapping.source_entry_id,
                    mapping.destination_entry_id,
                    mapping.source_range,
                    mapping.destination_range,
                ): mapping
                for mapping in ctx.session.execute(select(AnimapMapping))
                .scalars()
                .all()
            }

            desired_provenance: dict[int, list[str]] = {}
            for key, sources in provenance_by_id_key.items():
                mapping = existing_mappings.get(key)
                if mapping:
                    desired_provenance[mapping.id] = sources

            self._sync_provenance_rows(ctx.session, desired_provenance)

            ctx.session.merge(
                Housekeeping(key="animap_mappings_hash", value=curr_mappings_hash)
            )

            ctx.session.commit()

            self._build_cache_from_edges(edge_list, curr_mappings_hash)

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
                "Mappings database sync complete: "
                f"{len(removed_pairs)} removed, "
                f"{len(updated_pairs)} updated, "
                f"{len(created_pairs)} created"
            )
