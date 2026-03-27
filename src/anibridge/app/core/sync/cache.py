"""Caching helpers for sync clients."""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from anibridge.list import ListEntry, ListProvider

from anibridge.app import log
from anibridge.app.models.db.pin import Pin

__all__ = ["SyncCacheManager"]


class SyncCacheManager:
    """Manage prefetched list entries and pinned field lookups."""

    def __init__(
        self,
        *,
        list_provider: ListProvider,
        profile_name: str,
        db_factory: Callable[[], Any],
    ) -> None:
        """Initialize cache state for a sync client.

        Args:
            list_provider (ListProvider): Provider used to load list entries.
            profile_name (str): Sync profile name for pin lookups.
            db_factory (Callable[[], Any]): Factory returning a database context
                manager.
        """
        self.list_provider = list_provider
        self.profile_name = profile_name
        self._db_factory = db_factory
        self._pin_cache: dict[tuple[str, str], list[str]] = {}
        self._prefetched_entries: dict[str, ListEntry] = {}

    def clear(self) -> None:
        """Clear all cached pins and prefetched entries.

        Returns:
            None: This method mutates in-memory cache state only.
        """
        self._pin_cache.clear()
        self._prefetched_entries.clear()

    def cache_entry(self, entry: ListEntry) -> None:
        """Store a list entry in the local cache.

        Args:
            entry (ListEntry): Entry to cache by list media key.

        Returns:
            None: This method mutates in-memory cache state only.
        """
        media_key = str(entry.media().key)
        self._prefetched_entries[media_key] = entry

    def remove_entry(self, media_key: str) -> None:
        """Remove a list entry from cache by list media key."""
        self._prefetched_entries.pop(str(media_key), None)

    @staticmethod
    def apply_planned_update(
        *,
        source_entry: ListEntry | None,
        planned_entry: ListEntry,
        fields: Sequence[str],
    ) -> None:
        """Apply planned field values onto an in-memory cached source entry."""
        if source_entry is None:
            return

        for field_name in fields:
            setattr(source_entry, field_name, getattr(planned_entry, field_name))

    async def get_entry(self, media_key: str) -> ListEntry | None:
        """Return a list entry from cache or the provider.

        Args:
            media_key (str): List media key to resolve.

        Returns:
            ListEntry | None: Cached or fetched entry, if available.
        """
        cache_key = str(media_key)
        cached = self._prefetched_entries.get(cache_key)
        if cached is not None:
            return cached
        entry = await self.list_provider.get_entry(media_key)
        if entry is not None:
            self.cache_entry(entry)
        return entry

    async def prefetch_entries(
        self,
        *,
        items: Sequence[Any],
        collect_keys: Callable[[Any], Awaitable[Sequence[str]]],
    ) -> None:
        """Prefetch list entries for the supplied items.

        Args:
            items (Sequence[Any]): Source items whose keys should be prefetched.
            collect_keys (Callable[[Any], Awaitable[Sequence[str]]]): Async
                callback that extracts provider keys.

        Returns:
            None: This method warms the local cache and logs failures.
        """
        if not items:
            return

        collected: set[str] = set()
        for item in items:
            try:
                keys = await collect_keys(item)
            except Exception:
                log.error(
                    "[%s] Failed to collect prefetch keys",
                    self.profile_name,
                )
                log.exception(
                    "[%s] Prefetch key collection error details",
                    self.profile_name,
                )
                continue
            for key in keys:
                if key is not None:
                    collected.add(str(key))

        if not collected:
            return

        log.debug(
            "[%s] Prefetching %s list entries",
            self.profile_name,
            len(collected),
        )

        try:
            entries = await self.list_provider.get_entries_batch(tuple(collected))
        except Exception:
            log.error(
                "[%s] Failed to prefetch list entries",
                self.profile_name,
            )
            log.exception(
                "[%s] Prefetch batch error details",
                self.profile_name,
            )
            return

        for entry in entries:
            if entry is not None:
                self.cache_entry(entry)

        log.debug(
            "[%s] Prefetched %s list entries",
            self.profile_name,
            len(entries),
        )

    def get_pinned_fields(self, namespace: str, media_key: str | None) -> list[str]:
        """Return pinned fields for a list media identifier.

        Args:
            namespace (str): List provider namespace.
            media_key (str | None): List media identifier.

        Returns:
            list[str]: Pinned field names for the resolved media item.
        """
        if not media_key:
            return []

        cache_key = (namespace, media_key)
        cached = self._pin_cache.get(cache_key)
        if cached is not None:
            return cached

        with self._db_factory() as ctx:
            pin: Pin | None = (
                ctx.session.query(Pin)
                .filter(
                    Pin.profile_name == self.profile_name,
                    Pin.list_namespace == namespace,
                    Pin.list_media_key == media_key,
                )
                .first()
            )

        fields = list(pin.fields) if pin and pin.fields else []
        self._pin_cache[cache_key] = fields
        return fields
