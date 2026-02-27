"""Backup listing and restore service."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anibridge.utils.cache import cache
from pydantic import BaseModel

from anibridge.app import log
from anibridge.app.exceptions import (
    BackupFileNotFoundError,
    BackupParseError,
    InvalidBackupFilenameError,
    ProfileNotFoundError,
    SchedulerNotInitializedError,
)
from anibridge.app.web.state import get_app_state

if TYPE_CHECKING:
    from anibridge.app.core.bridge import BridgeClient

__all__ = ["BackupService", "get_backup_service"]


class BackupMeta(BaseModel):
    """Metadata about a backup file used for listing in the UI."""

    filename: str
    created_at: datetime
    size_bytes: int
    entries: int | None = None
    user: str | None = None
    age_seconds: float


class BackupService:
    """Service for listing and restoring provider-managed backups."""

    def _get_profile_bridge(self, profile: str) -> BridgeClient:
        """Get the scheduler bridge client for a profile."""
        scheduler = get_app_state().scheduler
        if not scheduler:
            raise SchedulerNotInitializedError("Scheduler not available")
        bridge = scheduler.bridge_clients.get(profile)
        if not bridge:
            raise ProfileNotFoundError(f"Unknown profile: {profile}")
        return bridge

    def _backup_dir(self, profile: str) -> Path:
        """Get the backup directory for a profile."""
        bridge = self._get_profile_bridge(profile)
        return bridge.global_config.data_path / "backups"

    def list_backups(self, profile: str) -> list[BackupMeta]:
        """Enumerate available backups for a profile.

        Args:
            profile: Profile name.

        Returns:
            list[BackupMeta]: List of backup metadata, newest first.

        Raises:
            SchedulerNotInitializedError: If the scheduler is not running.
            ProfileNotFoundError: If the profile is unknown.
        """
        log.debug("Listing backups for profile $$'%s'$$", profile)
        bdir = self._backup_dir(profile) / profile
        if not bdir.exists():
            log.debug("Backup directory $$'%s'$$ does not exist", bdir)
            return []
        metas: list[BackupMeta] = []
        now = datetime.now(UTC)

        bridge = self._get_profile_bridge(profile)
        list_provider = bridge.list_provider
        provider_user = list_provider.user()

        count = 0
        pattern = f"anibridge_{profile}_{list_provider.NAMESPACE}_*.json"
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

    def read_backup_raw(self, profile: str, filename: str) -> dict[str, Any]:
        """Return the raw JSON content of a backup file.

        Args:
            profile: Profile name
            filename: Backup filename (basename only)

        Returns:
            dict[str, Any]: Parsed JSON content.

        Raises:
            SchedulerNotInitializedError: If the scheduler is not running.
            ProfileNotFoundError: If the profile is unknown.
            InvalidBackupFilenameError: If the filename is invalid.
            BackupFileNotFoundError: If the file does not exist.
        """
        log.debug(
            "Reading raw backup $$'%s'$$ for profile $$'%s'$$",
            filename,
            profile,
        )
        path = self._resolve_backup_path(profile, filename)

        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _resolve_backup_path(self, profile: str, filename: str) -> Path:
        """Resolve and validate a backup filename for a profile."""
        bdir = self._backup_dir(profile) / profile
        path = (bdir / filename).resolve()

        if path.parent != bdir.resolve():
            raise InvalidBackupFilenameError("Invalid backup filename")
        if not path.exists():
            raise BackupFileNotFoundError("Backup file not found")

        return path

    async def restore_backup(self, profile: str, filename: str) -> None:
        """Restore a backup file for a profile.

        Args:
            profile: Profile name
            filename: Backup filename (basename only)

        Raises:
            SchedulerNotInitializedError: If the scheduler is not running.
            ProfileNotFoundError: If the profile is unknown.
            InvalidBackupFilenameError: If the filename is invalid.
            BackupFileNotFoundError: If the file does not exist.
            BackupParseError: If there was an error parsing or restoring the backup.
        """
        log.info(
            "Restoring backup $$'%s'$$ for profile $$'%s'$$",
            filename,
            profile,
        )
        bridge = self._get_profile_bridge(profile)
        path = self._resolve_backup_path(profile, filename)

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
    """Get the singleton BackupService instance.

    Returns:
        BackupService: The singleton BackupService instance.
    """
    return BackupService()
