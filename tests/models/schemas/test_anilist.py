"""Tests for AniList schema utility behavior."""

from datetime import UTC, datetime, timedelta

import msgspec

from anibridge.app.models.schemas.anilist import (
    AiringSchedule,
    AniListBaseModel,
    FuzzyDate,
    Media,
    MediaCoverImage,
    MediaFormat,
    MediaTitle,
    _resolve_model_type,
)


class _Nested(AniListBaseModel):
    value: str | None = None


class _Container(AniListBaseModel):
    nested: _Nested | None = None
    many: list[_Nested] | None = None
    title: str | None = None
    tags: list[str] = msgspec.field(default_factory=list)


def test_anilist_base_model_unset_fields_resets_defaults() -> None:
    """Unset fields should restore default values and default factories."""
    item = _Container(
        nested=_Nested(value="x"),
        title="Title",
        tags=["one"],
    )

    item.unset_fields({"nested", "title", "tags", "missing"})

    assert item.nested is None
    assert item.title is None
    assert item.tags == []


def test_anilist_base_model_graphql_dump_and_recursion_guard() -> None:
    """GraphQL dump should include nested models and guard repeated models."""
    _Container.model_dump_graphql.cache_clear()
    _Nested.model_dump_graphql.cache_clear()

    graphql = _Container.model_dump_graphql()

    assert "nested {" in graphql
    assert "many {" in graphql
    assert "title" in graphql
    _Container._processed_models.add("_Container")
    try:
        assert _Container.model_dump_graphql.__wrapped__(_Container) == ""
    finally:
        _Container._processed_models.remove("_Container")


def test_anilist_schema_repr_hash_titles_and_dates() -> None:
    """Model representation helpers should expose non-empty values."""
    title = MediaTitle(romaji="Romaji", english="English", user_preferred="Preferred")
    fuzzy = FuzzyDate(year=2026, month=6)
    media = Media(id=1, title=title, format=MediaFormat.TV)

    assert title.titles()[:3] == ["Romaji", "English", None]
    assert str(title) == "Preferred"
    assert str(MediaTitle(native="Native")) == "Native"
    assert str(FuzzyDate()) == "????-??-??"
    assert repr(fuzzy) == "2026-06-??"
    assert "id=1" in repr(media)
    assert hash(media) == hash(repr(media))


def test_resolve_model_type_and_airing_schedule_timezone() -> None:
    """Nested annotation resolution and airing time normalization should work."""
    assert _resolve_model_type(list[_Nested]) is _Nested
    assert _resolve_model_type(str) is None

    schedule = AiringSchedule(
        id=1,
        airing_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
        time_until_airing=timedelta(minutes=5),
        episode=1,
        media_id=1,
    )

    assert schedule.airing_at.tzinfo is UTC


def test_media_cover_image_simple_model() -> None:
    """Leaf schema models should serialize through msgspec normally."""
    assert msgspec.to_builtins(MediaCoverImage(medium="poster.jpg")) == {
        "medium": "poster.jpg"
    }
