"""AniBridge exception classes."""


class AniBridgeError(Exception):
    """Base class for all AniBridge exceptions."""

    # Default HTTP status for API responses
    status_code: int = 500


# Configuration errors
class ConfigError(AniBridgeError):
    """Base class for configuration-related errors."""

    status_code = 500


class ProfileConfigError(ConfigError, ValueError):
    """Invalid or incomplete configuration for a specific profile."""

    status_code = 400


class InvalidMappingsURLError(ConfigError, ValueError):
    """The mappings_url configuration is not a supported HTTP(S) URL or file type."""

    status_code = 400


class NoProfilesConfiguredError(ConfigError, ValueError):
    """No usable profiles were found in the user's configuration."""

    status_code = 400


class ProfileNotFoundError(ConfigError, KeyError):
    """Requested profile does not exist."""

    status_code = 404


class DataPathError(ConfigError, ValueError):
    """The configured data directory path is invalid for the requested operation."""

    status_code = 400


# Database errors
class DatabaseError(AniBridgeError):
    """Base class for database-related errors."""

    status_code = 500


class UnsupportedModeError(DatabaseError, ValueError):
    """Unsupported mode value was provided when dumping a database model."""

    status_code = 400


# Media/model errors
class MediaTypeError(AniBridgeError):
    """Base class for media type related errors."""

    status_code = 400


class UnsupportedMediaTypeError(MediaTypeError, ValueError):
    """A media object or enum is not one of the supported library media types."""

    status_code = 400


# AniList client errors
class AniListError(AniBridgeError):
    """Base class for AniList-related failures."""

    status_code = 500


class AniListQueryError(AniListError):
    """Base class for AniList query and search issues."""

    status_code = 502


class AniListFilterError(AniListQueryError, ValueError):
    """AniList filter arguments supplied by the user are invalid."""

    status_code = 400


class AniListSearchError(AniListQueryError):
    """AniList search failed or returned an unexpected response."""

    status_code = 502


# Scheduler errors
class SchedulerError(AniBridgeError):
    """Base class for scheduler-related failures."""

    status_code = 500


class SchedulerNotInitializedError(SchedulerError, RuntimeError):
    """A scheduler instance is required but not available/initialized."""

    status_code = 503


class SchedulerUnavailableError(SchedulerError):
    """The scheduler exists but is temporarily unavailable (e.g., shutting down)."""

    status_code = 503


# Backup/restore errors
class BackupError(AniBridgeError):
    """Base class for backup and restore failures."""

    status_code = 500


class InvalidBackupFilenameError(BackupError, ValueError):
    """Provided backup filename is invalid or not allowed."""

    status_code = 400


class BackupFileNotFoundError(BackupError, FileNotFoundError):
    """Expected backup file was not found on disk."""

    status_code = 404


class BackupParseError(BackupError, ValueError):
    """Backup file content could not be parsed for the provider."""

    status_code = 400


# History and actions errors
class HistoryError(AniBridgeError):
    """Base class for history-related failures."""

    status_code = 500


class HistoryItemNotFoundError(HistoryError, KeyError):
    """A requested history entry could not be located."""

    status_code = 404


class HistoryPermissionError(HistoryError, PermissionError):
    """The user does not have permission to perform the requested history operation."""

    status_code = 403


# Mappings errors
class MappingError(AniBridgeError):
    """Base class for mapping data source or parsing errors."""

    status_code = 500


class UnsupportedMappingFileExtensionError(MappingError, ValueError):
    """Provided mappings file path or upload uses an unsupported extension."""

    status_code = 400


class MissingAnilistIdError(MappingError, ValueError):
    """Operation requires an AniList ID but none was provided."""

    status_code = 422


class MissingDescriptorError(MappingError, ValueError):
    """Operation requires a mapping descriptor but none was provided."""

    status_code = 422


class MappingIdMismatchError(MappingError, ValueError):
    """The AniList ID in the URL does not match the request body."""

    status_code = 400


class MappingNotFoundError(MappingError, KeyError):
    """Requested mapping entry could not be located."""

    status_code = 404


class BooruQueryError(MappingError):
    """Base class for booru-like query failures."""

    status_code = 400


class BooruQuerySyntaxError(BooruQueryError, ValueError):
    """Query string could not be parsed due to invalid syntax."""

    status_code = 400


class BooruQueryEvaluationError(BooruQueryError, RuntimeError):
    """Evaluation of a booru query AST failed unexpectedly."""

    status_code = 400


# Logs errors
class LogsError(AniBridgeError):
    """Base class for logs-related failures."""

    status_code = 500


class InvalidLogFileNameError(LogsError, ValueError):
    """Provided log file name is invalid or attempts path traversal."""

    status_code = 400


class LogFileNotFoundError(LogsError, FileNotFoundError):
    """Requested log file was not found within the logs directory."""

    status_code = 404


# Webhook errors
class WebhookError(AniBridgeError):
    """Base class for webhook-related errors."""

    status_code = 400


class InvalidWebhookPayloadError(WebhookError, ValueError):
    """Webhook payload is missing required fields or malformed."""

    status_code = 400


class WebhookModeDisabledError(WebhookError):
    """Webhook sync mode is not enabled for any matching profile."""

    status_code = 400
