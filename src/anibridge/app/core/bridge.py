"""Bridge client orchestration providers."""

import asyncio
import contextlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from anibridge.provider.base import (
    Change,
    ChangeQuery,
    InboundRequest,
    Page,
    Provider,
    Ref,
    Role,
    ScanItem,
    SupportsBackupExports,
    SupportsChangeFeed,
    SupportsInboundChanges,
)
from litestar.connection.request import Request

from anibridge.app.config.database import db
from anibridge.app.config.settings import AnibridgeConfig, AnibridgeProfileConfig
from anibridge.app.core.animap import AnimapClient
from anibridge.app.core.providers import build_profile_providers
from anibridge.app.core.sync import (
    ScanPlan,
    SyncRequest,
    SyncTrigger,
    dedupe_refs,
    ref_to_key,
)
from anibridge.app.core.sync.base import SyncClient
from anibridge.app.core.sync.stats import SyncProgress
from anibridge.app.logging import get_logger
from anibridge.app.models.db.housekeeping import Housekeeping
from anibridge.app.models.db.sync_history import SyncOutcome
from anibridge.app.utils.memory import release_memory
from anibridge.app.utils.terminal import ARROW
from anibridge.app.web.state import get_app_state

__all__ = ["BridgeClient"]

log = get_logger(__name__)
SYNC_SCAN_PAGE_SIZE = 10
SYNC_SCAN_QUEUE_SIZE = 100


class BridgeClient:
    """Single-profile bridge client that coordinates provider synchronization."""

    def __init__(
        self,
        profile_name: str,
        profile_config: AnibridgeProfileConfig,
        global_config: AnibridgeConfig,
        shared_animap_client: AnimapClient,
    ) -> None:
        """Initialize the bridge client for one profile."""
        self.profile_name = profile_name
        self.profile_config = profile_config
        self.global_config = global_config
        self.animap_client = shared_animap_client

        providers = build_profile_providers(profile_config, global_config)
        self.source_provider: Provider = providers[Role.SOURCE]
        self.target_provider: Provider = providers[Role.TARGET]

        self.last_synced = self._get_last_synced()
        self.current_sync: SyncProgress | None = None

    async def initialize(self) -> None:
        """Initialize both providers and prepare for synchronization."""
        log.debug("[%s] Initializing bridge client", self.profile_name)

        try:
            await self.source_provider.initialize()
        except Exception:
            log.exception(
                "[%s] Source provider '%s' initialization failed",
                self.profile_name,
                self.source_provider.NAMESPACE,
            )
            raise

        try:
            await self.target_provider.initialize()
        except Exception:
            log.exception(
                "[%s] Target provider '%s' initialization failed",
                self.profile_name,
                self.target_provider.NAMESPACE,
            )
            raise

        await self._backup_target()

        source_account = self.source_provider.account()
        target_account = self.target_provider.account()
        source_label = source_account.title if source_account else "unknown"
        target_label = target_account.title if target_account else "unknown"

        log.info(
            "[%s] Bridge client initialized for %s source account $$'%s'$$ %s "
            "%s target account $$'%s'$$",
            self.profile_name,
            self.source_provider.NAMESPACE,
            source_label,
            ARROW,
            self.target_provider.NAMESPACE,
            target_label,
        )

    async def close(self) -> None:
        """Close all provider connections."""
        log.debug("[%s] Closing bridge client", self.profile_name)
        providers = (
            (Role.TARGET, self.target_provider),
            (Role.SOURCE, self.source_provider),
        )
        for role, provider in providers:
            try:
                await provider.close()
            except Exception:
                log.exception(
                    "[%s] %s provider '%s' close failed",
                    self.profile_name,
                    role.value,
                    provider.NAMESPACE,
                )

    async def __aenter__(self) -> BridgeClient:
        """Enter async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit async context manager."""
        await self.close()

    def _get_last_synced(self) -> datetime | None:
        """Fetch the last successful sync timestamp from the database."""
        with db() as ctx:
            last_synced = ctx.session.get(
                Housekeeping,
                f"last_synced_{self.profile_name}",
            )
            if last_synced is None or last_synced.value is None:
                return None
            return datetime.fromisoformat(last_synced.value)

    def _set_last_synced(self, last_synced: datetime) -> None:
        """Persist the timestamp of the most recent successful sync."""
        self.last_synced = last_synced
        with db() as ctx:
            ctx.session.merge(
                Housekeeping(
                    key=f"last_synced_{self.profile_name}",
                    value=last_synced.isoformat(),
                )
            )
            ctx.session.commit()

    def _get_change_cursor(self) -> str | None:
        """Fetch the last source change-feed cursor from the database."""
        with db() as ctx:
            key = f"change_cursor_{self.profile_name}_{self.source_provider.NAMESPACE}"
            cursor = ctx.session.get(Housekeeping, key)
            return cursor.value if cursor is not None else None

    def _set_change_cursor(self, cursor: str) -> None:
        """Persist the latest source change-feed cursor."""
        with db() as ctx:
            ctx.session.merge(
                Housekeeping(
                    key=f"change_cursor_{self.profile_name}_{self.source_provider.NAMESPACE}",
                    value=cursor,
                )
            )
            ctx.session.commit()

    async def _backup_target(self) -> None:
        """Persist a target-provider backup snapshot when supported."""
        if self.profile_config.backup_retention_days == -1:
            log.debug(
                "[%s] Target backup creation is disabled by configuration; skipping",
                self.profile_name,
            )
            return

        backup_root = self.global_config.data_path / "backups" / self.profile_name
        try:
            if not isinstance(self.target_provider, SupportsBackupExports):
                return

            artifact = await self.target_provider.export_backup()
            if artifact is None or not artifact.content:
                log.debug(
                    "[%s] Target provider produced an empty backup; skipping write",
                    self.profile_name,
                )
                return

            target_fname = (
                f"anibridge_{self.profile_name}_{self.target_provider.NAMESPACE}_"
                f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
                f"{artifact.file_extension}"
            )
            target_path = backup_root / target_fname
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(artifact.content)
            log.info(
                "[%s] Target provider backup written to $$'%s'$$",
                self.profile_name,
                target_path,
            )
        except Exception:
            log.exception(
                "[%s] Failed to export or write target backup",
                self.profile_name,
            )
        finally:
            self._cleanup_old_backups(backup_root)

    def _cleanup_old_backups(self, backup_root: Path) -> None:
        """Delete stale backup files based on retention policy."""
        retention_days = self.profile_config.backup_retention_days
        if retention_days <= 0 or not backup_root.exists():
            return

        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        pattern = f"anibridge_{self.profile_name}_{self.target_provider.NAMESPACE}_*"
        deleted_count = 0

        for path in backup_root.glob(pattern):
            try:
                modified_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            except OSError:
                continue
            if modified_at >= cutoff:
                continue
            try:
                path.unlink()
                deleted_count += 1
            except OSError:
                log.warning(
                    "[%s] Failed to remove expired backup $$'%s'$$",
                    self.profile_name,
                    path,
                )

        if deleted_count:
            log.info(
                "[%s] Removed %s expired backups older than %s days",
                self.profile_name,
                deleted_count,
                retention_days,
            )

    async def sync(
        self,
        request: SyncRequest | None = None,
    ) -> None:
        """Run a synchronization cycle for the configured profile."""
        request = request or SyncRequest()
        source_account = self.source_provider.account()
        target_account = self.target_provider.account()
        source_label = source_account.title if source_account else "unknown"
        target_label = target_account.title if target_account else "unknown"

        log.info(
            "[%s] Starting %s%ssync (%s) for source account $$'%s'$$ %s "
            "target account $$'%s'$$",
            self.profile_name,
            "full " if self.profile_config.full_scan else "partial ",
            "and destructive " if self.profile_config.destructive_sync else "",
            request.trigger.value,
            source_label,
            ARROW,
            target_label,
        )

        sync_start_time = datetime.now(UTC)
        sync_client = SyncClient(
            source_provider=self.source_provider,
            target_provider=self.target_provider,
            animap_client=self.animap_client,
            sync_rules=self.profile_config.sync_rules,
            destructive_sync=self.profile_config.destructive_sync,
            dry_run=self.profile_config.dry_run,
            profile_name=self.profile_name,
        )

        self.current_sync = SyncProgress(
            state="running",
            started_at=sync_start_time,
            stage="enumerating",
            source_namespace=self.source_provider.NAMESPACE,
            target_namespace=self.target_provider.NAMESPACE,
            trigger=request.trigger.value,
            scanned_items=0,
            processed_items=0,
            total_items=None,
        )
        get_app_state().notify_status_change()

        try:
            scan = await self._scan_plan_for(request)
            if scan is None and not request.record_undos:
                log.info(
                    "[%s] No source changes found for %s sync; skipping",
                    self.profile_name,
                    request.trigger.value,
                )
                return

            if scan is not None:
                log.debug(
                    "[%s] Source scan prepared: trigger=%s refs=%s "
                    "require_user_data=%s change_feed=%s",
                    self.profile_name,
                    scan.trigger.value,
                    len(scan.source_refs) if scan.source_refs else "all",
                    scan.require_user_data,
                    scan.from_change_feed,
                )
                await self._process_scan_stream(sync_client, scan)

            if request.record_undos:
                self.current_sync.stage = "undoing"
                await sync_client.undo_records(request.record_undos)

            sync_client.flush_failure_history_cleanup()

            sync_completion_time = datetime.now(UTC)
            duration = sync_completion_time - sync_start_time
            self._set_last_synced(sync_start_time)

            sync_stats = sync_client.sync_stats
            log.info(
                "[%s] Sync completed: %s synced, %s deleted, %s skipped, %s not "
                "found, %s failed. Coverage: %.2f%% (%s total) in %.2f seconds",
                self.profile_name,
                sync_stats.synced,
                sync_stats.deleted,
                sync_stats.skipped,
                sync_stats.not_found,
                sync_stats.failed,
                sync_stats.coverage * 100,
                sync_stats.count(),
                duration.total_seconds(),
            )

            uncovered_items = sync_stats.items(
                SyncOutcome.NOT_FOUND,
                SyncOutcome.FAILED,
                SyncOutcome.PENDING,
            )
            if uncovered_items:
                log.debug(
                    "[%s] Uncovered items: %s",
                    self.profile_name,
                    ", ".join(repr(item) for item in uncovered_items),
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
            self.current_sync = None
            get_app_state().notify_status_change()
            await sync_client.clear_cache()
            release_memory()

    async def _process_scan_stream(
        self,
        sync_client: SyncClient,
        scan: ScanPlan,
    ) -> None:
        """Process source items while scan pages are being produced."""
        queue: asyncio.Queue[Page[ScanItem] | None] = asyncio.Queue(
            maxsize=SYNC_SCAN_QUEUE_SIZE
        )
        scanned_total = 0
        declared_total: int | None = None

        async def produce() -> None:
            nonlocal scanned_total, declared_total
            try:
                async for page in sync_client.scan_source_pages(
                    scan=scan,
                    page_size=SYNC_SCAN_PAGE_SIZE,
                ):
                    scanned_total += len(page.items)
                    if page.total is not None:
                        declared_total = max(0, page.total)
                    if self.current_sync is not None:
                        self.current_sync.stage = "enumerating"
                        self.current_sync.scanned_items = scanned_total
                        self.current_sync.total_items = declared_total
                        await asyncio.sleep(0)
                    await queue.put(page)
            except asyncio.CancelledError:
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(None)
                raise
            except Exception:
                await queue.put(None)
                raise
            else:
                if self.current_sync is not None and declared_total is None:
                    self.current_sync.total_items = scanned_total
                    await asyncio.sleep(0)
                await queue.put(None)

        producer = asyncio.create_task(produce())
        try:
            while True:
                page = await queue.get()
                if page is None:
                    break
                try:
                    await sync_client.process_page(page.items)
                except Exception:
                    log.exception(
                        "[%s] Failed to sync source item batch", self.profile_name
                    )
                    raise
                finally:
                    if self.current_sync is not None:
                        self.current_sync.stage = "processing"
                        self.current_sync.processed_items += len(page.items)
                        await asyncio.sleep(0)
            await producer
        finally:
            if not producer.done():
                producer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await producer

    async def _scan_plan_for(
        self,
        request: SyncRequest,
    ) -> ScanPlan | None:
        """Translate a sync request into an explicit provider scan projection."""
        if request.record_undos and request.source_refs == ():
            return None

        if request.trigger == SyncTrigger.POLL:
            changed_refs = await self._poll_source_refs()
            if changed_refs is None:
                source_refs = (
                    None
                    if request.full_scan_on_poll_fallback
                    else dedupe_refs(request.source_refs)
                    if request.source_refs is not None
                    else None
                )
                return ScanPlan(
                    trigger=request.trigger,
                    source_refs=source_refs,
                    require_user_data=(
                        False
                        if request.source_refs is not None
                        else not self.profile_config.full_scan
                    ),
                    from_change_feed=False,
                )

            source_refs = dedupe_refs(
                (
                    *changed_refs,
                    *(request.source_refs or ()),
                )
            )
            if not source_refs:
                return None
            return ScanPlan(
                trigger=request.trigger,
                source_refs=source_refs,
                require_user_data=False,
                from_change_feed=changed_refs is not None,
            )

        if request.trigger == SyncTrigger.WEBHOOK:
            source_refs = dedupe_refs(request.source_refs)
            if not source_refs:
                return None
            return ScanPlan(
                trigger=request.trigger,
                source_refs=source_refs,
                require_user_data=False,
            )

        if request.source_refs is not None:
            return ScanPlan(
                trigger=request.trigger,
                source_refs=dedupe_refs(request.source_refs),
                require_user_data=False,
            )

        return ScanPlan(
            trigger=request.trigger,
            source_refs=None,
            require_user_data=not self.profile_config.full_scan,
        )

    async def _poll_source_refs(self) -> tuple[Ref, ...] | None:
        """Poll the source change feed and return changed refs when possible."""
        if not isinstance(self.source_provider, SupportsChangeFeed):
            log.warning(
                "[%s] Poll sync requested, but source provider '%s' does not "
                "support it. Falling back to a periodic scan",
                self.profile_name,
                self.source_provider.NAMESPACE,
            )
            return None

        changes: list[Change] = []
        cursor = self._get_change_cursor()
        latest_cursor: str | None = cursor
        while True:
            page = await self.source_provider.poll_changes(ChangeQuery(cursor=cursor))
            changes.extend(page.items)
            if page.cursor is None:
                break
            latest_cursor = page.cursor
            if not page.items:
                break
            if page.cursor == cursor:
                log.warning(
                    "[%s] Source provider '%s' returned an unchanged change-feed "
                    "cursor; stopping poll pagination",
                    self.profile_name,
                    self.source_provider.NAMESPACE,
                )
                break
            cursor = page.cursor

        if latest_cursor:
            self._set_change_cursor(latest_cursor)

        return self._change_refs(changes)

    async def parse_webhook(
        self,
        request: Request,
    ) -> tuple[bool, Sequence[Ref] | None]:
        """Parse a provider webhook request into source refs."""
        if not isinstance(self.source_provider, SupportsInboundChanges):
            return False, None

        try:
            inbound = await self._to_inbound_request(request)
            result = await self.source_provider.parse_inbound(inbound)
        except Exception:
            log.exception(
                "[%s] Source provider '%s' webhook parsing failed",
                self.profile_name,
                self.source_provider.NAMESPACE,
            )
            return False, None

        if not result.matched:
            return False, None
        refs = self._change_refs(result.changes)
        return True, refs or None

    @staticmethod
    async def _to_inbound_request(request: Request) -> InboundRequest:
        """Convert a Litestar request into the provider-base inbound shape."""
        body = await request.body()
        query = {
            str(key): (
                tuple(value) if isinstance(value, list | tuple) else (str(value),)
            )
            for key, value in dict(request.query_params).items()
        }
        return InboundRequest(
            method=request.method,
            path=request.url.path,
            headers={str(key): str(value) for key, value in request.headers.items()},
            query=query,
            body=body,
        )

    @staticmethod
    def _change_refs(changes: Sequence[Change]) -> tuple[Ref, ...]:
        """Extract source refs from normalized change payloads."""
        refs: list[Ref] = []
        seen = set()
        for change in changes:
            ref = change.ref
            if ref is None:
                key = change.key
                ref = Ref.anchor(str(key)) if key else None
            if ref is None:
                continue
            marker = ref_to_key(ref)
            if marker in seen:
                continue
            seen.add(marker)
            refs.append(ref)
        return tuple(refs)
