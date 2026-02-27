"""Bridge Client Module."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import cast

from anibridge.library import (
    LibraryMovie,
    LibraryProvider,
    LibrarySection,
    LibraryShow,
    MediaKind,
)
from anibridge.list import ListProvider
from starlette.requests import Request

from anibridge.app import log
from anibridge.app.config.database import db
from anibridge.app.config.settings import AniBridgeConfig, AniBridgeProfileConfig
from anibridge.app.core.animap import AnimapClient
from anibridge.app.core.providers import build_library_provider, build_list_provider
from anibridge.app.core.sync import BaseSyncClient, MovieSyncClient, ShowSyncClient
from anibridge.app.core.sync.stats import SyncProgress, SyncStats
from anibridge.app.exceptions import MediaTypeError
from anibridge.app.models.db.housekeeping import Housekeeping
from anibridge.app.models.db.sync_history import SyncOutcome
from anibridge.app.utils.terminal import ARROW

__all__ = ["BridgeClient"]


class BridgeClient:
    """Single-profile bridge client that coordinates provider synchronization."""

    def __init__(
        self,
        profile_name: str,
        profile_config: AniBridgeProfileConfig,
        global_config: AniBridgeConfig,
        shared_animap_client: AnimapClient,
    ) -> None:
        """Initialize the bridge client for a single profile.

        Args:
            profile_name (str): The name of the profile.
            profile_config (AniBridgeProfileConfig): The profile-specific configuration.
            global_config (AniBridgeConfig): The global application configuration.
            shared_animap_client (AnimapClient): Shared Animap client instance.
        """
        self.profile_name = profile_name
        self.profile_config = profile_config
        self.global_config = global_config
        self.animap_client = shared_animap_client

        self.library_provider: LibraryProvider = build_library_provider(profile_config)
        self.list_provider: ListProvider = build_list_provider(profile_config)

        self.last_synced = self._get_last_synced()
        self.current_sync: SyncProgress | None = None

    async def initialize(self) -> None:
        """Initialize both providers and prepare for synchronization."""
        log.debug("[%s] Initializing bridge client", self.profile_name)

        try:
            await self.library_provider.initialize()
        except Exception:
            log.error(
                "[%s] Library provider '%s' initialization failed",
                self.profile_name,
                self.library_provider.NAMESPACE,
            )
            log.exception(
                "[%s] Library provider initialization error details",
                self.profile_name,
            )
            raise

        try:
            await self.list_provider.initialize()
        except Exception:
            log.error(
                "[%s] List provider '%s' initialization failed",
                self.profile_name,
                self.list_provider.NAMESPACE,
            )
            log.exception(
                "[%s] List provider initialization error details",
                self.profile_name,
            )
            raise

        await self._backup_list()

        library_user = self.library_provider.user()
        list_user = self.list_provider.user()
        library_label = library_user.title if library_user else "unknown"
        list_label = list_user.title if list_user else "unknown"

        log.info(
            "[%s] Bridge client initialized for %s library user $$'%s'$$ %s %s list "
            "user $$'%s'$$",
            self.profile_name,
            self.library_provider.NAMESPACE,
            library_label,
            ARROW,
            self.list_provider.NAMESPACE,
            list_label,
        )

    async def close(self) -> None:
        """Close all provider connections."""
        log.debug("[%s] Closing bridge client", self.profile_name)
        try:
            await self.list_provider.close()
        except Exception:
            log.error(
                "[%s] List provider '%s' close failed",
                self.profile_name,
                self.list_provider.NAMESPACE,
            )
            log.exception(
                "[%s] List provider close error details",
                self.profile_name,
            )

        try:
            await self.library_provider.close()
        except Exception:
            log.error(
                "[%s] Library provider '%s' close failed",
                self.profile_name,
                self.library_provider.NAMESPACE,
            )
            log.exception(
                "[%s] Library provider close error details",
                self.profile_name,
            )

    async def __aenter__(self) -> BridgeClient:
        """Enter async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit async context manager."""
        await self.close()

    def _get_last_synced_key(self) -> str:
        """Return the database key storing the last sync timestamp."""
        return f"last_synced_{self.profile_name}"

    def _get_last_synced(self) -> datetime | None:
        """Fetch the last successful sync timestamp from the database."""
        with db() as ctx:
            last_synced = ctx.session.get(Housekeeping, self._get_last_synced_key())
            if last_synced is None or last_synced.value is None:
                return None
            return datetime.fromisoformat(last_synced.value)

    def _set_last_synced(self, last_synced: datetime) -> None:
        """Persist the timestamp of the most recent successful sync."""
        self.last_synced = last_synced
        with db() as ctx:
            ctx.session.merge(
                Housekeeping(
                    key=self._get_last_synced_key(), value=last_synced.isoformat()
                )
            )
            ctx.session.commit()

    async def _backup_list(self) -> None:
        """Persist an initial list backup when supported by the provider."""
        if self.profile_config.backup_retention_days == -1:
            log.debug(
                "[%s] List backup creation is disabled by configuration; skipping",
                self.profile_name,
            )
            return
        try:
            payload = await self.list_provider.backup_list()
        except NotImplementedError:
            return
        except Exception:
            log.error("[%s] Failed to export list backup", self.profile_name)
            log.exception(
                "[%s] List backup export error details",
                self.profile_name,
            )
            return

        if not payload:
            log.debug(
                "[%s] List provider produced an empty backup; skipping write",
                self.profile_name,
            )
            return

        target_fname = (
            f"anibridge_{self.profile_name}_{self.list_provider.NAMESPACE}_"
            f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.json"
        )
        target_path = (
            self.global_config.data_path / "backups" / self.profile_name / target_fname
        )

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(payload, encoding="utf-8")
        except Exception:
            log.error(
                "[%s] Failed to write backup file to $$'%s'$$",
                self.profile_name,
                target_path,
            )
            log.exception(
                "[%s] Backup write error details",
                self.profile_name,
            )
            return

        log.info(
            "[%s] List provider backup written to $$'%s'$$",
            self.profile_name,
            target_path,
        )

    async def sync(
        self, poll: bool = False, library_keys: Sequence[str] | None = None
    ) -> None:
        """Run a synchronization cycle for the configured profile.

        Args:
            poll (bool): Whether to poll for updates.
            library_keys (Sequence[str] | None): Sequence of library media keys to
                restrict the sync scope.
        """
        library_user = self.library_provider.user()
        list_user = self.list_provider.user()
        library_label = library_user.title if library_user else "unknown"
        list_label = list_user.title if list_user else "unknown"

        log.info(
            "[%s] Starting %s%ssync for library user $$'%s'$$ %s list user $$'%s'$$",
            self.profile_name,
            "full " if self.profile_config.full_scan else "partial ",
            "and destructive " if self.profile_config.destructive_sync else "",
            library_label,
            ARROW,
            list_label,
        )

        sync_start_time = datetime.now(UTC)

        movie_sync = MovieSyncClient(
            library_provider=self.library_provider,
            list_provider=self.list_provider,
            animap_client=self.animap_client,
            sync_fields=self.profile_config.sync_fields,
            full_scan=self.profile_config.full_scan,
            destructive_sync=self.profile_config.destructive_sync,
            empty_sync=self.profile_config.empty_sync,
            search_fallback_threshold=self.profile_config.search_fallback_threshold,
            batch_requests=self.profile_config.batch_requests,
            dry_run=self.profile_config.dry_run,
            profile_name=self.profile_name,
        )
        show_sync = ShowSyncClient(
            library_provider=self.library_provider,
            list_provider=self.list_provider,
            animap_client=self.animap_client,
            sync_fields=self.profile_config.sync_fields,
            full_scan=self.profile_config.full_scan,
            destructive_sync=self.profile_config.destructive_sync,
            empty_sync=self.profile_config.empty_sync,
            search_fallback_threshold=self.profile_config.search_fallback_threshold,
            batch_requests=self.profile_config.batch_requests,
            dry_run=self.profile_config.dry_run,
            profile_name=self.profile_name,
        )

        await movie_sync.clear_cache()
        await show_sync.clear_cache()

        sections = list(await self.library_provider.get_sections())
        log.debug(
            "[%s] Retrieved %s library sections",
            self.profile_name,
            len(sections),
        )

        self.current_sync = SyncProgress(
            state="running",
            started_at=sync_start_time,
            section_index=0,
            section_count=len(sections),
            section_title=None,
            stage="initializing",
            section_items_total=0,
            section_items_processed=0,
        )
        sync_stats = SyncStats()

        try:
            for idx, section in enumerate(sections, start=1):
                if self.current_sync is not None:
                    self.current_sync = self.current_sync.model_copy(
                        update={
                            "section_index": idx,
                            "section_title": section.title,
                            "stage": "enumerating",
                            "section_items_total": 0,
                            "section_items_processed": 0,
                        }
                    )

                section_stats = await self._sync_section(
                    section,
                    poll,
                    movie_sync,
                    show_sync,
                    keys=library_keys,
                    section_index=idx,
                    section_count=len(sections),
                )
                sync_stats = sync_stats.combine(section_stats)

            sync_completion_time = datetime.now(UTC)
            duration = sync_completion_time - sync_start_time

            self._set_last_synced(sync_start_time)

            log.info(
                "[%s] Sync completed: %s synced, %s deleted, %s skipped, %s not found, "
                "%s failed. Coverage: %.2f%% (%s total) in %.2f seconds",
                self.profile_name,
                sync_stats.synced,
                sync_stats.deleted,
                sync_stats.skipped,
                sync_stats.not_found,
                sync_stats.failed,
                sync_stats.coverage * 100,
                len(sync_stats.get_grandchild_items_by_outcome()),
                duration.total_seconds(),
            )

            uncovered_items = sync_stats.get_grandchild_items_by_outcome(
                SyncOutcome.NOT_FOUND,
                SyncOutcome.FAILED,
                SyncOutcome.PENDING,
            )
            if uncovered_items:
                log.debug(
                    "[%s] Uncovered items: %s",
                    self.profile_name,
                    ", ".join([repr(item) for item in uncovered_items]),
                )

        except Exception as exc:
            end_time = datetime.now(UTC)
            duration = end_time - sync_start_time

            log.exception(
                "[%s] Sync failed after %.2f seconds: %s",
                self.profile_name,
                duration.total_seconds(),
                exc,
            )
            raise
        finally:
            if self.current_sync is not None:
                self.current_sync = self.current_sync.model_copy(
                    update={"stage": "completed", "state": "idle"}
                )

    async def parse_webhook(
        self, request: Request
    ) -> tuple[bool, Sequence[str] | None]:
        """Parse a webhook request and extract relevant library keys.

        Args:
            request (Request): The incoming webhook request.

        Returns:
            tuple[bool, Sequence[str] | None]: A tuple containing a boolean
                indicating whether the webhook is valid and targetting the profile,
                and a sequence of library media keys to sync, or None if not
                applicable.
        """
        try:
            return await self.library_provider.parse_webhook(request)
        except Exception:
            log.error(
                "[%s] Library provider '%s' webhook parsing failed",
                self.profile_name,
                self.library_provider.NAMESPACE,
            )
            log.exception(
                "[%s] Webhook parsing error details",
                self.profile_name,
            )
            return False, None

    async def _sync_section(
        self,
        section: LibrarySection,
        poll: bool,
        movie_sync: MovieSyncClient,
        show_sync: ShowSyncClient,
        keys: Sequence[str] | None = None,
        *,
        section_index: int,
        section_count: int,
    ) -> SyncStats:
        """Synchronize a single library section."""
        log.info(
            "[%s] Syncing section $$'%s'$$",
            self.profile_name,
            section.title,
        )

        min_last_modified = (
            (self.last_synced or datetime.now(UTC)) - timedelta(seconds=15)
            if poll
            else None
        )

        parts = []
        if min_last_modified:
            parts.append(f"min_last_modified={min_last_modified.isoformat()}")
        if self.profile_config.full_scan:
            parts.append("require_watched=False")
        if keys is not None:
            parts.append(f"keys={list(keys)}")
        debug_log_args = f" ({', '.join(parts)})" if parts else ""

        log.debug(
            "[%s] Fetching items in section $$'%s'$$%s",
            self.profile_name,
            section.title,
            debug_log_args,
        )
        items = list(
            await self.library_provider.list_items(
                section,
                min_last_modified=min_last_modified,
                require_watched=not self.profile_config.full_scan,
                keys=keys,
            )
        )
        log.debug(
            "[%s] Found %s items in section $$'%s'$$",
            self.profile_name,
            len(items),
            section.title,
        )

        if self.current_sync is not None:
            self.current_sync = self.current_sync.model_copy(
                update={
                    "section_index": section_index,
                    "section_count": section_count,
                    "section_title": section.title,
                    "section_items_total": len(items),
                    "section_items_processed": 0,
                    "stage": (
                        "prefetching"
                        if self.profile_config.batch_requests
                        else "processing"
                    ),
                }
            )

        sync_client: BaseSyncClient
        if section.media_kind == MediaKind.MOVIE:
            sync_client = movie_sync
        elif section.media_kind == MediaKind.SHOW:
            sync_client = show_sync
        else:
            log.warning(
                "[%s] Unsupported section kind '%s', skipping",
                self.profile_name,
                section.media_kind.value,
            )
            return SyncStats()

        if self.profile_config.batch_requests:
            log.info(
                "[%s] Prefetching list entries for $$'%s'$$ (%s items)",
                self.profile_name,
                section.title,
                len(items),
            )
            try:
                if section.media_kind == MediaKind.MOVIE:
                    await movie_sync.prefetch_entries(
                        cast(Sequence[LibraryMovie], items)
                    )
                elif section.media_kind == MediaKind.SHOW:
                    await show_sync.prefetch_entries(cast(Sequence[LibraryShow], items))
            except Exception:
                log.error(
                    "[%s] Failed to prefetch list entries",
                    self.profile_name,
                )
                log.exception(
                    "[%s] Prefetch error details",
                    self.profile_name,
                )
            if self.current_sync is not None:
                self.current_sync = self.current_sync.model_copy(
                    update={"stage": "processing"}
                )

        for item in items:
            try:
                if item.media_kind == MediaKind.MOVIE:
                    await movie_sync.process_media(cast(LibraryMovie, item))
                elif item.media_kind == MediaKind.SHOW:
                    await show_sync.process_media(cast(LibraryShow, item))
                else:
                    raise MediaTypeError(
                        f"Unsupported media type '{type(item).__name__}' "
                        "encountered during sync"
                    )

                if self.current_sync is not None:
                    self.current_sync = self.current_sync.model_copy(
                        update={
                            "stage": "processing",
                            "section_items_processed": (
                                self.current_sync.section_items_processed + 1
                            ),
                        }
                    )

            except Exception:
                log.error(
                    "[%s] Failed to sync item $$'%s'$$",
                    self.profile_name,
                    item.title,
                )
                log.exception(
                    "[%s] Item sync error details",
                    self.profile_name,
                )

        try:
            if self.profile_config.batch_requests:
                if self.current_sync is not None:
                    self.current_sync = self.current_sync.model_copy(
                        update={"stage": "finalizing"}
                    )
                await sync_client.batch_sync()
        finally:
            sync_client.flush_failure_history_cleanup()

        return sync_client.sync_stats
