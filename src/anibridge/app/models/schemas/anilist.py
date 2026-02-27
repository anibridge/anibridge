"""AniList Models Module."""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, ClassVar, get_args, get_origin

from anibridge.utils.cache import cache
from pydantic import AfterValidator, BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

UTCDateTime = Annotated[datetime, AfterValidator(lambda dt: dt.astimezone(UTC))]


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


class AniListBaseModel(BaseModel):
    """Base, abstract class for all AniList models to represent GraphQL objects.

    Provides serialization, aliasing, and GraphQL query generation utilities.
    """

    _processed_models: ClassVar[set] = set()

    def model_dump(self, **kwargs) -> dict:
        """Convert the model to a dictionary, converting all keys to camelCase.

        Returns:
            dict: Dictionary representation of the model.
        """
        return super().model_dump(by_alias=True, **kwargs)

    def model_dump_json(self, **kwargs) -> str:
        """Serialize the model to JSON, converting all keys to camelCase.

        Returns:
            str: JSON serialized string of the model.
        """
        return super().model_dump_json(by_alias=True, **kwargs)

    def unset_fields(self, fields: Iterable[str]) -> None:
        """Unset specified fields to their default values.

        Args:
            fields (Iterable[str]): Field names to unset.
        """
        for field, field_info in self.__class__.model_fields.items():
            if field in fields:
                setattr(self, field, field_info.default)

    @classmethod
    @cache
    def model_dump_graphql(cls) -> str:
        """Generate GraphQL query fields for this model.

        Returns:
            str: The GraphQL query fields.
        """
        if cls.__name__ in cls._processed_models:
            return ""

        cls._processed_models.add(cls.__name__)
        fields = cls.model_fields
        graphql_fields = []

        for field_name, field in fields.items():
            field_type = (
                get_args(field.annotation)[0]
                if get_origin(field.annotation)
                else field.annotation
            )

            camel_field_name = to_camel(field_name)

            if isinstance(field_type, type) and issubclass(
                field_type, AniListBaseModel
            ):
                nested_fields = field_type.model_dump_graphql()
                if nested_fields:
                    graphql_fields.append(f"{camel_field_name} {{\n{nested_fields}\n}}")
            else:
                graphql_fields.append(f"{camel_field_name}")

        cls._processed_models.remove(cls.__name__)
        return "\n".join(graphql_fields)

    def __hash__(self) -> int:
        """Return hash of the model representation."""
        return hash(self.__repr__())

    def __repr__(self) -> str:
        """Return string representation of the model."""
        return f"<{
            ' : '.join(
                [f'{k}={v}' for k, v in self.model_dump().items() if v is not None]
            )
        }>"

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


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
        """Return a list of all the available titles.

        Returns:
            list[str]: All the available titles.
        """
        return [getattr(self, t) for t in self.__class__.model_fields if t]

    def __str__(self) -> str:
        """Return the first available title or an empty string.

        Returns:
            str: A title or an empty string.
        """
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

    extra_large: str | None = None
    large: str | None = None
    medium: str | None = None
    color: str | None = None


class AiringSchedule(AniListBaseModel):
    """Model representing an airing schedule for a media entry."""

    id: int
    airing_at: UTCDateTime
    time_until_airing: timedelta
    episode: int
    media_id: int


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
