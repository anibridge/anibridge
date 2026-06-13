"""Backup listing and restore service."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import msgspec
import msgspec.json
from anibridge.utils.cache import cache

from anibridge.app.exceptions import (
    BackupFileNotFoundError,
    BackupParseError,
    InvalidBackupFilenameError,
    ProfileNotFoundError,
    SchedulerNotInitializedError,
    SchedulerUnavailableError,
)
from anibridge.app.logging import get_logger
from anibridge.app.web.state import get_app_state

__all__ = ["BackupService", "get_backup_service"]

log = get_logger(__name__)


class BackupMeta(msgspec.Struct):
    """Metadata about a backup file used for listing in the UI."""

    filename: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Backup file name stored on disk.",
            examples=["anibridge_default_anilist_20260508120000.json"],
        ),
    ]
    created_at: Annotated[
        datetime,
        msgspec.Meta(
            description="UTC timestamp when the backup file was created.",
            examples=["2026-01-01T00:00:00Z"],
        ),
    ]
    size_bytes: Annotated[
        int,
        msgspec.Meta(
            ge=0,
            description="Backup file size in bytes.",
            examples=[2048],
        ),
    ]
    age_seconds: Annotated[
        float,
        msgspec.Meta(
            ge=0,
            description="Age of the backup relative to the current time, in seconds.",
            examples=[3600.5],
        ),
    ]
    entries: (
        Annotated[
            int,
            msgspec.Meta(
                ge=0,
                description="Number of list entries contained in the backup if known.",
                examples=[142],
            ),
        ]
        | None
    ) = None
    user: (
        Annotated[
            str,
            msgspec.Meta(
                description="Provider user associated with the backup when available.",
                examples=["DemoUser"],
            ),
        ]
        | None
    ) = None


class BackupService:
    """Service for listing and restoring provider-managed backups."""

    def list_backups(self, profile: str) -> list[BackupMeta]:
        """Enumerate available backups for a profile."""
        scheduler = get_app_state().scheduler
        if not scheduler:
            raise SchedulerNotInitializedError("Scheduler not available")
        profile_config = scheduler.global_config.profiles.get(profile)
        if profile_config is None:
            raise ProfileNotFoundError(f"Unknown profile: {profile}")

        log.debug("Listing backups for profile $$'%s'$$", profile)
        bdir = scheduler.global_config.data_path / "backups" / profile
        if not bdir.exists():
            log.debug("Backup directory $$'%s'$$ does not exist", bdir)
            return []
        metas: list[BackupMeta] = []
        now = datetime.now(UTC)

        bridge = scheduler.bridge_clients.get(profile)
        list_provider = bridge.list_provider if bridge is not None else None
        provider_user = list_provider.user() if list_provider is not None else None

        count = 0
        provider_namespace = (
            list_provider.NAMESPACE if list_provider is not None else None
        )
        pattern = (
            f"anibridge_{profile}_{provider_namespace}_*.json"
            if provider_namespace
            else f"anibridge_{profile}_{profile_config.list_provider}_*.json"
        )
        if scheduler.failed_profile_errors.get(profile):
            pattern = f"anibridge_{profile}_*.json"
        for f in sorted(bdir.glob(pattern)):
            try:
                parts = f.name.split(".")
                ts_raw = parts[-2] if len(parts) >= 2 else None
                dt: datetime | None = None
                if ts_raw and ts_raw.isdigit():
                    try:
                        dt = datetime.strptime(ts_raw, "%Y%m%d%H%M%S").replace(
                            tzinfo=UTC
                        )
                    except ValueError:
                        dt = datetime.fromtimestamp(f.stat().st_mtime, UTC)
                else:
                    dt = datetime.fromtimestamp(f.stat().st_mtime, UTC)
                metas.append(
                    BackupMeta(
                        filename=f.name,
                        created_at=dt,
                        size_bytes=f.stat().st_size,
                        entries=None,  # Can be populated on demand
                        user=provider_user.title if provider_user else None,
                        age_seconds=(now - dt).total_seconds(),
                    )
                )
                count += 1
            except Exception:
                continue
        log.debug(
            "Found %s backups for profile $$'%s'$$",
            count,
            profile,
        )
        return list(reversed(metas))  # Newest first

    def read_backup_raw(self, profile: str, filename: str) -> Any:
        """Return the raw JSON content of a backup file."""
        scheduler = get_app_state().scheduler
        if not scheduler:
            raise SchedulerNotInitializedError("Scheduler not available")
        if profile not in scheduler.global_config.profiles:
            raise ProfileNotFoundError(f"Unknown profile: {profile}")

        log.debug(
            "Reading raw backup $$'%s'$$ for profile $$'%s'$$",
            filename,
            profile,
        )
        bdir = scheduler.global_config.data_path / "backups" / profile
        path = self._resolve_backup_path(bdir, filename)

        return msgspec.json.decode(path.read_text(encoding="utf-8"))

    def _resolve_backup_path(self, bdir: Path, filename: str) -> Path:
        """Resolve and validate a backup filename for a profile."""
        path = (bdir / filename).resolve()

        if path.parent != bdir.resolve():
            raise InvalidBackupFilenameError("Invalid backup filename")
        if not path.exists():
            raise BackupFileNotFoundError("Backup file not found")

        return path

    async def restore_backup(self, profile: str, filename: str) -> None:
        """Restore a backup file for a profile."""
        scheduler = get_app_state().scheduler
        if not scheduler:
            raise SchedulerNotInitializedError("Scheduler not available")
        if profile not in scheduler.global_config.profiles:
            raise ProfileNotFoundError(f"Unknown profile: {profile}")
        init_error = scheduler.failed_profile_errors.get(profile)
        if init_error:
            raise SchedulerUnavailableError(
                f"Profile '{profile}' is unavailable: {init_error}"
            )

        log.info(
            "Restoring backup $$'%s'$$ for profile $$'%s'$$",
            filename,
            profile,
        )
        bridge = scheduler.bridge_clients.get(profile)
        if bridge is None:
            raise SchedulerUnavailableError(
                f"Profile '{profile}' is unavailable for restore"
            )
        bdir = scheduler.global_config.data_path / "backups" / profile
        path = self._resolve_backup_path(bdir, filename)

        raw_backup = path.read_text(encoding="utf-8")
        try:
            await bridge.list_provider.restore_list(raw_backup)
        except NotImplementedError as exc:
            raise BackupParseError(
                "List provider does not support backup restoration"
            ) from exc
        except Exception as exc:
            raise BackupParseError(f"Error during backup restoration: {exc}") from exc
        log.info(
            "Successfully restored backup $$'%s'$$ for profile $$'%s'$$",
            filename,
            profile,
        )


@cache
def get_backup_service() -> BackupService:
    """Get the singleton BackupService instance."""
    return BackupService()
