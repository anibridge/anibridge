"""AniList Models Module."""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import ClassVar, get_args, get_origin

import msgspec
from anibridge.utils.cache import cache


class AniListBaseEnum(StrEnum):
    """Base enum for AniList models."""

    pass


class MediaType(AniListBaseEnum):
    """Enum representing media types (ANIME, MANGA)."""

    ANIME = "ANIME"
    MANGA = "MANGA"


class MediaFormat(AniListBaseEnum):
    """Enum representing media formats (TV, MOVIE, etc)."""

    TV = "TV"
    TV_SHORT = "TV_SHORT"
    MOVIE = "MOVIE"
    SPECIAL = "SPECIAL"
    OVA = "OVA"
    ONA = "ONA"
    MUSIC = "MUSIC"
    MANGA = "MANGA"
    NOVEL = "NOVEL"
    ONE_SHOT = "ONE_SHOT"


class MediaStatus(AniListBaseEnum):
    """Enum representing media status (FINISHED, RELEASING, etc)."""

    FINISHED = "FINISHED"
    RELEASING = "RELEASING"
    NOT_YET_RELEASED = "NOT_YET_RELEASED"
    CANCELLED = "CANCELLED"
    HIATUS = "HIATUS"


class MediaSeason(AniListBaseEnum):
    """Enum representing media seasons (WINTER, SPRING, etc)."""

    WINTER = "WINTER"
    SPRING = "SPRING"
    SUMMER = "SUMMER"
    FALL = "FALL"


class MediaSort(AniListBaseEnum):
    """Enum representing sort options for media queries."""

    ID = "ID"
    ID_DESC = "ID_DESC"
    TITLE_ROMAJI = "TITLE_ROMAJI"
    TITLE_ROMAJI_DESC = "TITLE_ROMAJI_DESC"
    TITLE_ENGLISH = "TITLE_ENGLISH"
    TITLE_ENGLISH_DESC = "TITLE_ENGLISH_DESC"
    TITLE_NATIVE = "TITLE_NATIVE"
    TITLE_NATIVE_DESC = "TITLE_NATIVE_DESC"
    TYPE = "TYPE"
    TYPE_DESC = "TYPE_DESC"
    FORMAT = "FORMAT"
    FORMAT_DESC = "FORMAT_DESC"
    START_DATE = "START_DATE"
    START_DATE_DESC = "START_DATE_DESC"
    END_DATE = "END_DATE"
    END_DATE_DESC = "END_DATE_DESC"
    SCORE = "SCORE"
    SCORE_DESC = "SCORE_DESC"
    POPULARITY = "POPULARITY"
    POPULARITY_DESC = "POPULARITY_DESC"
    TRENDING = "TRENDING"
    TRENDING_DESC = "TRENDING_DESC"
    EPISODES = "EPISODES"
    EPISODES_DESC = "EPISODES_DESC"
    DURATION = "DURATION"
    DURATION_DESC = "DURATION_DESC"
    STATUS = "STATUS"
    STATUS_DESC = "STATUS_DESC"
    CHAPTERS = "CHAPTERS"
    CHAPTERS_DESC = "CHAPTERS_DESC"
    VOLUMES = "VOLUMES"
    VOLUMES_DESC = "VOLUMES_DESC"
    UPDATED_AT = "UPDATED_AT"
    UPDATED_AT_DESC = "UPDATED_AT_DESC"
    SEARCH_MATCH = "SEARCH_MATCH"
    FAVOURITES = "FAVOURITES"
    FAVOURITES_DESC = "FAVOURITES_DESC"


class MediaListStatus(AniListBaseEnum):
    """Enum representing status of a media list entry (CURRENT, COMPLETED, etc)."""

    CURRENT = "CURRENT"
    PLANNING = "PLANNING"
    COMPLETED = "COMPLETED"
    DROPPED = "DROPPED"
    PAUSED = "PAUSED"
    REPEATING = "REPEATING"


class AniListBaseModel(msgspec.Struct, rename="camel", kw_only=True):
    """Base, abstract class for all AniList models to represent GraphQL objects.

    Provides serialization, aliasing, and GraphQL query generation utilities.
    """

    _processed_models: ClassVar[set[str]] = set()

    def unset_fields(self, fields: Iterable[str]) -> None:
        """Unset specified fields to their default values."""
        field_set = set(fields)
        for field_info in msgspec.structs.fields(type(self)):
            if field_info.name not in field_set:
                continue
            if field_info.default_factory is not msgspec.NODEFAULT:
                setattr(self, field_info.name, field_info.default_factory())
            elif field_info.default is not msgspec.NODEFAULT:
                setattr(self, field_info.name, field_info.default)

    @classmethod
    @cache
    def model_dump_graphql(cls) -> str:
        """Generate GraphQL query fields for this model."""
        if cls.__name__ in cls._processed_models:
            return ""

        cls._processed_models.add(cls.__name__)
        graphql_fields = []

        for field_info in msgspec.structs.fields(cls):
            field_type = _resolve_model_type(field_info.type)
            if field_type is not None:
                nested_fields = field_type.model_dump_graphql()
                if nested_fields:
                    graphql_fields.append(
                        f"{field_info.encode_name} {{\n{nested_fields}\n}}"
                    )
            else:
                graphql_fields.append(f"{field_info.encode_name}")

        cls._processed_models.remove(cls.__name__)
        return "\n".join(graphql_fields)

    def __hash__(self) -> int:
        """Return hash of the model representation."""
        return hash(self.__repr__())

    def __repr__(self) -> str:
        """Return string representation of the model."""
        values = [
            f"{k}={v}" for k, v in msgspec.to_builtins(self).items() if v is not None
        ]
        return f"<{' : '.join(values)}>"


def _resolve_model_type(annotation: object) -> type[AniListBaseModel] | None:
    """Recursively resolve a type annotation to find an AniListBaseModel subclass."""
    if isinstance(annotation, type) and issubclass(annotation, AniListBaseModel):
        return annotation

    origin = get_origin(annotation)
    if origin is None:
        return None

    for arg in get_args(annotation):
        resolved = _resolve_model_type(arg)
        if resolved is not None:
            return resolved
    return None


class PageInfo(AniListBaseModel):
    """Model representing pagination info for AniList queries."""

    total: int | None = None
    per_page: int | None = None
    current_page: int | None = None
    last_page: int | None = None
    has_next_page: bool | None = None


class MediaTitle(AniListBaseModel):
    """Model representing media titles in various languages."""

    romaji: str | None = None
    english: str | None = None
    native: str | None = None
    user_preferred: str | None = None

    def titles(self) -> list[str]:
        """Return a list of all the available titles."""
        return [getattr(self, field) for field in self.__struct_fields__ if field]

    def __str__(self) -> str:
        """Return the first available title or an empty string."""
        return self.user_preferred or self.english or self.romaji or self.native or ""


class FuzzyDate(AniListBaseModel):
    """Model representing a fuzzy date (year, month, day may be missing)."""

    year: int | None = None
    month: int | None = None
    day: int | None = None

    def __str__(self) -> str:
        """Return string representation of the FuzzyDate."""
        return self.__repr__()

    def __repr__(self) -> str:
        """Return formatted string representation of the FuzzyDate."""
        return (
            f"{self.year or '????'}-"
            f"{str(self.month).zfill(2) if self.month else '??'}-"
            f"{str(self.day).zfill(2) if self.day else '??'}"
        )


class MediaCoverImage(AniListBaseModel):
    """Model representing a media cover image."""

    # extra_large: str | None = None
    # large: str | None = None
    medium: str | None = None
    # color: str | None = None


class AiringSchedule(AniListBaseModel):
    """Model representing an airing schedule for a media entry."""

    id: int
    airing_at: datetime
    time_until_airing: timedelta
    episode: int
    media_id: int

    def __post_init__(self) -> None:
        """Normalize AniList datetimes to UTC."""
        self.airing_at = self.airing_at.astimezone(UTC)


class Media(AniListBaseModel):
    """Model representing a media entry."""

    id: int
    # id_mal: int | None = None
    # type: MediaType | None = None
    format: MediaFormat | None = None
    status: MediaStatus | None = None
    season: MediaSeason | None = None
    season_year: int | None = None
    episodes: int | None = None
    duration: int | None = None
    cover_image: MediaCoverImage | None = None
    # banner_image: str | None = None
    # synonyms: list[str] | None = None
    # is_locked: bool | None = None
    is_adult: bool | None = None
    title: MediaTitle | None = None
    # start_date: FuzzyDate | None = None
    # end_date: FuzzyDate | None = None
    # next_airing_episode: AiringSchedule | None = None
