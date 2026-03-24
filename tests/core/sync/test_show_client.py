"""Unit tests for `anibridge.app.core.sync.show`."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from typing import TYPE_CHECKING, Any, cast

import pytest
from anibridge.library import LibraryEpisode as LibraryEpisodeProtocol
from anibridge.library import LibrarySeason as LibrarySeasonProtocol
from anibridge.library import LibraryShow as LibraryShowProtocol
from anibridge.list import ListEntry as ListEntryProtocol
from anibridge.list import ListMediaType, ListStatus
from anibridge.utils.mappings import AnibridgeDescriptorMapping

import anibridge.app.core.sync.show as show_module
from anibridge.app.core.sync.show import ShowSyncClient
from anibridge.app.core.sync.targeting import ResolvedListTarget
from anibridge.app.models.db.sync_history import SyncHistory, SyncOutcome
from tests.core.sync.fakes import (
    FakeAnimapClient,
    FakeLibraryEpisode,
    FakeLibraryProvider,
    FakeLibrarySeason,
    FakeLibraryShow,
    FakeListEntry,
    FakeListProvider,
    make_history_entry,
)

if TYPE_CHECKING:
    from anibridge.app.config.database import AnibridgeDb


@dataclass(slots=True, frozen=True)
class MappingSpec:
    """Test helper that represents one source/target range segment."""

    start: int
    end: int | None
    ratio: int | None = None


def _format_mapping_segment(segment: MappingSpec) -> str:
    if segment.end is None:
        return f"{segment.start}-"
    if segment.start == segment.end:
        return str(segment.start)
    return f"{segment.start}-{segment.end}"


def make_descriptor_mapping(
    *,
    descriptor,
    source_ranges: Sequence[MappingSpec],
    target_ranges: Sequence[Sequence[MappingSpec]] | None = None,
) -> AnibridgeDescriptorMapping:
    """Build a descriptor mapping using schema strings."""
    mapping = AnibridgeDescriptorMapping(
        source=descriptor,
        target=("_test", "_target", None),
    )
    targets = tuple(target_ranges or ())

    for index, source_range in enumerate(source_ranges):
        if source_range.ratio == 0:
            source_text = _format_mapping_segment(source_range)
            mapping.add_mapping(source_text, f"{source_text}|0")
            continue

        source_text = _format_mapping_segment(
            MappingSpec(
                start=source_range.start,
                end=source_range.end,
                ratio=None,
            )
        )
        scoped_targets = targets[index] if index < len(targets) else ()

        ratio = None
        if source_range.ratio is not None:
            ratio = source_range.ratio * -1
        elif scoped_targets:
            explicit = [
                segment.ratio for segment in scoped_targets if segment.ratio is not None
            ]
            if (
                explicit
                and len(explicit) == len(scoped_targets)
                and len(set(explicit)) == 1
            ):
                ratio = explicit[0]

        target_text = (
            ",".join(_format_mapping_segment(segment) for segment in scoped_targets)
            if scoped_targets
            else source_text
        )

        if (
            ratio is not None
            and not scoped_targets
            and source_range.end is not None
            and ratio != 0
        ):
            source_length = source_range.end - source_range.start + 1
            if ratio > 0:
                expected_target_length = Fraction(source_length, ratio)
            else:
                expected_target_length = Fraction(source_length * abs(ratio), 1)
            if expected_target_length.denominator != 1:
                raise AssertionError(
                    "Test mapping has non-integral ratio alignment. "
                    "Provide explicit target_ranges for this case."
                )
            aligned_end = source_range.start + expected_target_length.numerator - 1
            target_text = _format_mapping_segment(
                MappingSpec(start=source_range.start, end=aligned_end)
            )

        if ratio is not None:
            target_text = f"{target_text}|{ratio}"

        mapping.add_mapping(source_text, target_text)

    return mapping


@pytest.fixture
def show_client() -> ShowSyncClient:
    """Provide a configured show sync client."""
    return ShowSyncClient(
        library_provider=cast(Any, FakeLibraryProvider()),
        list_provider=cast(Any, FakeListProvider()),
        animap_client=cast(Any, FakeAnimapClient()),
        full_scan=False,
        destructive_sync=False,
        search_fallback_threshold=80,
        batch_requests=False,
        dry_run=False,
        profile_name="tester",
    )


def build_show(
    *,
    view_counts: Sequence[int],
    show_kwargs: dict[str, Any] | None = None,
    season_kwargs: dict[str, Any] | None = None,
    episode_kwargs: dict[str, Any] | None = None,
) -> tuple[FakeLibraryShow, FakeLibrarySeason, list[FakeLibraryEpisode]]:
    """Construct a fake show with a single season and configurable episodes."""
    show = FakeLibraryShow(
        key="show-1",
        title="Show",
        **(show_kwargs or {}),
    )
    season = FakeLibrarySeason(
        key="season-1",
        title="S1",
        index=1,
        show=show,
        **(season_kwargs or {}),
    )
    episodes: list[FakeLibraryEpisode] = []
    for idx, count in enumerate(view_counts, start=1):
        episodes.append(
            FakeLibraryEpisode(
                key=f"ep-{idx}",
                title=f"Episode {idx}",
                index=idx,
                season_index=1,
                show=show,
                season=season,
                view_count=count,
                **(episode_kwargs or {}),
            )
        )
    season._episodes = list(episodes)
    show.attach_children(episodes=episodes, seasons=[season])
    return show, season, episodes


def build_multi_season_show() -> tuple[
    FakeLibraryShow, list[FakeLibrarySeason], list[FakeLibraryEpisode]
]:
    """Construct a show with two seasons and watched episodes."""
    show = FakeLibraryShow(key="show-2", title="Show Two")
    seasons: list[FakeLibrarySeason] = []
    episodes: list[FakeLibraryEpisode] = []

    for season_index in (1, 2):
        season = FakeLibrarySeason(
            key=f"season-{season_index}",
            title=f"S{season_index}",
            index=season_index,
            show=show,
            mapping_descriptors=[("tmdb", str(season_index), None)],
        )
        season_episodes = [
            FakeLibraryEpisode(
                key=f"ep-{season_index}-1",
                title="Episode 1",
                index=1,
                season_index=season_index,
                show=show,
                season=season,
                view_count=1,
            ),
            FakeLibraryEpisode(
                key=f"ep-{season_index}-2",
                title="Episode 2",
                index=2,
                season_index=season_index,
                show=show,
                season=season,
                view_count=1,
            ),
        ]
        season._episodes = list(season_episodes)
        seasons.append(season)
        episodes.extend(season_episodes)

    show.attach_children(episodes=episodes, seasons=seasons)
    return show, seasons, episodes


@pytest.mark.asyncio
async def test_process_media_syncs_show_and_writes_history(
    show_client: ShowSyncClient, sync_db: AnibridgeDb
) -> None:
    """Processing a show syncs its episodes and writes history entries."""
    provider = cast(FakeListProvider, show_client.list_provider)
    show, _, episodes = build_show(
        view_counts=[2, 2],
        season_kwargs={"mapping_descriptors": [("anilist", "400", "s1")]},
    )
    entry = FakeListEntry(
        provider=provider,
        key="400",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=len(episodes),
    )
    provider.entries["400"] = entry
    provider.derived_keys = ["400"]

    await show_client.process_media(cast(LibraryShowProtocol, show))

    assert provider.updated_entries and provider.updated_entries[0][0] == "400"
    assert show_client.sync_stats.synced >= 1
    with sync_db as ctx:
        records = ctx.session.query(SyncHistory).all()
        assert len(records) == 1
        assert records[0].library_media_key == show.key


@pytest.mark.asyncio
async def test_process_media_untracks_filtered_mapping_range_items(
    show_client: ShowSyncClient,
    monkeypatch: pytest.MonkeyPatch,
    sync_db: AnibridgeDb,
) -> None:
    """Excluded mapped episodes should be removed from sync tracking."""
    provider = cast(FakeListProvider, show_client.list_provider)
    show, _season, _episodes = build_show(
        view_counts=[1, 0],
        season_kwargs={"mapping_descriptors": [("tmdb", "10", "s1")]},
    )
    entry = FakeListEntry(
        provider=provider,
        key="909",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=2,
    )

    async def _resolve_batch(**_kwargs):
        return [
            [
                ResolvedListTarget(
                    "909",
                    (),
                    (
                        make_descriptor_mapping(
                            descriptor=("tmdb", "10", "s1"),
                            source_ranges=(MappingSpec(start=1, end=1, ratio=None),),
                        ),
                    ),
                )
            ]
        ]

    async def _get_entry(_key: str):
        return entry

    monkeypatch.setattr(show_module, "resolve_list_targets_batch", _resolve_batch)
    monkeypatch.setattr(show_client._cache, "get_entry", _get_entry)

    await show_client.process_media(cast(LibraryShowProtocol, show))

    tracked_episode_keys = {
        item.key for item in show_client.sync_stats.get_grandchild_items_by_outcome()
    }
    assert tracked_episode_keys == {"ep-1"}


@pytest.mark.asyncio
async def test_process_media_skips_when_all_mapped_items_are_filtered(
    show_client: ShowSyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shows whose mapped episodes are all filtered out should be skipped."""
    show, _season, _episodes = build_show(
        view_counts=[1, 0],
        season_kwargs={"mapping_descriptors": [("tmdb", "10", "s1")]},
    )

    async def _resolve_batch(**_kwargs):
        return [
            [
                ResolvedListTarget(
                    "910",
                    (),
                    (
                        make_descriptor_mapping(
                            descriptor=("tmdb", "10", "s1"),
                            source_ranges=(MappingSpec(start=2, end=2, ratio=None),),
                        ),
                    ),
                )
            ]
        ]

    monkeypatch.setattr(show_module, "resolve_list_targets_batch", _resolve_batch)

    await show_client.process_media(cast(LibraryShowProtocol, show))

    assert show_client.sync_stats.skipped == 1
    assert show_client.sync_stats.not_found == 0
    assert show_client.sync_stats.pending == 0
    assert show_client.sync_stats.get_grandchild_items_by_outcome() == []
    assert show_client.sync_stats.get_items_by_outcome(SyncOutcome.SKIPPED)


@pytest.mark.asyncio
async def test_map_media_uses_mapping_resolution(show_client: ShowSyncClient) -> None:
    """Mapping graphs resolve to list entries when provider supports it."""
    provider = cast(FakeListProvider, show_client.list_provider)
    show, season, episodes = build_show(
        view_counts=[1, 1],
        season_kwargs={"mapping_descriptors": [("tmdb", "10", "s1")]},
    )
    entry = FakeListEntry(
        provider=provider,
        key="401",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=len(episodes),
    )
    provider.entries["401"] = entry
    provider.derived_keys = ["401"]

    results = [
        result
        async for result in show_client.map_media(cast(LibraryShowProtocol, show))
    ]

    assert len(results) == 1
    mapped_season, mapped_eps, target = results[0]
    assert mapped_season is season
    assert list(mapped_eps) == episodes
    assert target.entry is entry
    assert target.list_media_key == "401"


@pytest.mark.asyncio
async def test_search_media_prefers_matching_unit_counts(
    show_client: ShowSyncClient,
) -> None:
    """Ensure search results prefer entries with matching episode counts."""
    provider = cast(FakeListProvider, show_client.list_provider)
    show, season, episodes = build_show(view_counts=[0, 0])
    provider.search_results = [
        FakeListEntry(
            provider=provider,
            key="500",
            title="Show",
            media_type=ListMediaType.TV,
            total_units=len(episodes),
        ),
        FakeListEntry(
            provider=provider,
            key="501",
            title="Show Variant",
            media_type=ListMediaType.TV,
            total_units=len(episodes) + 2,
        ),
    ]
    show_client.search_fallback_threshold = 0

    entry = await show_client.search_media(
        cast(LibraryShowProtocol, show),
        cast(LibrarySeasonProtocol, season),
    )

    assert entry is provider.search_results[0]


@pytest.mark.asyncio
async def test_search_media_returns_none_when_disabled(
    show_client: ShowSyncClient,
) -> None:
    """Search should be disabled when threshold is negative or season index is 0."""
    show_client.search_fallback_threshold = -1
    show, season, _episodes = build_show(view_counts=[0])

    result = await show_client.search_media(
        cast(LibraryShowProtocol, show),
        cast(LibrarySeasonProtocol, season),
    )

    assert result is None


def test_filter_episodes_by_ranges(show_client: ShowSyncClient) -> None:
    """Episode filtering should honor source range mappings."""
    _show, _season, episodes = build_show(view_counts=[0, 0, 0])
    mapping = make_descriptor_mapping(
        descriptor=("anilist", "1", None),
        source_ranges=(MappingSpec(start=2, end=2, ratio=None),),
    )

    filtered = show_client._filter_episodes_by_ranges(
        episodes,
        [mapping],
    )

    assert [ep.index for ep in filtered] == [2]


def test_filter_episodes_by_ranges_returns_empty_when_no_range_matches(
    show_client: ShowSyncClient,
) -> None:
    """Non-overlapping source ranges should not fall back to all episodes."""
    _show, _season, episodes = build_show(view_counts=[0] * 12)
    mapping = make_descriptor_mapping(
        descriptor=("anilist", "2", None),
        source_ranges=(MappingSpec(start=13, end=24, ratio=None),),
    )

    filtered = show_client._filter_episodes_by_ranges(
        episodes,
        [mapping],
    )

    assert filtered == []


def test_filter_episodes_by_ranges_treats_zero_source_ratio_as_empty(
    show_client: ShowSyncClient,
) -> None:
    """Source ranges with ratio=0 should not include any episodes."""
    _show, _season, episodes = build_show(view_counts=[0, 0, 0])
    mapping = make_descriptor_mapping(
        descriptor=("anilist", "3", None),
        source_ranges=(MappingSpec(start=1, end=3, ratio=0),),
    )

    filtered = show_client._filter_episodes_by_ranges(
        episodes,
        [mapping],
    )

    assert filtered == []


def test_filter_episodes_by_ranges_ignores_zero_ratio_when_mixed(
    show_client: ShowSyncClient,
) -> None:
    """Zero-ratio source ranges should be ignored when other ranges are present."""
    _show, _season, episodes = build_show(view_counts=[0, 0, 0])
    mapping = make_descriptor_mapping(
        descriptor=("anilist", "4", None),
        source_ranges=(
            MappingSpec(start=1, end=1, ratio=0),
            MappingSpec(start=2, end=3, ratio=None),
        ),
    )

    filtered = show_client._filter_episodes_by_ranges(
        episodes,
        [mapping],
    )

    assert [ep.index for ep in filtered] == [2, 3]


@pytest.mark.asyncio
async def test_collect_prefetch_keys_sorted(
    show_client: ShowSyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prefetch keys should be sorted and unique."""
    show_client.full_scan = True
    show, _season, _episodes = build_show(view_counts=[0])

    async def _resolve_batch(**_kwargs):
        return [
            [
                ResolvedListTarget("3", (), ()),
                ResolvedListTarget("1", (), ()),
            ],
        ]

    monkeypatch.setattr(show_module, "resolve_list_targets_batch", _resolve_batch)

    keys = await show_client._collect_prefetch_keys(cast(LibraryShowProtocol, show))

    assert keys == ("1", "3")


@pytest.mark.asyncio
async def test_calculate_status_variants(show_client: ShowSyncClient) -> None:
    """Exercise the status calculator across watch states."""
    show, season, episodes = build_show(
        view_counts=[2, 2],
        show_kwargs={"on_watching": True},
        episode_kwargs={"on_watching": True},
    )
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=len(episodes),
    )
    status = await show_client._calculate_status(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
    )
    assert status == ListStatus.REPEATING

    entry.total_units = 10
    status = await show_client._calculate_status(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
    )
    assert status == ListStatus.CURRENT

    planning_show, planning_season, planning_eps = build_show(
        view_counts=[0],
        show_kwargs={"on_watchlist": True},
        season_kwargs={"on_watchlist": True},
    )
    entry.total_units = 1
    status = await show_client._calculate_status(
        item=cast(LibraryShowProtocol, planning_show),
        child_item=cast(LibrarySeasonProtocol, planning_season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(planning_eps)),
        entry=cast(ListEntryProtocol, entry),
    )
    assert status == ListStatus.PLANNING


@pytest.mark.asyncio
async def test_calculate_status_paused_and_dropped(
    show_client: ShowSyncClient,
) -> None:
    """Paused and dropped statuses should be inferred from watchlist flags."""
    show, season, episodes = build_show(
        view_counts=[1, 0],
        show_kwargs={"on_watchlist": True},
    )
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=len(episodes) + 1,
    )
    status = await show_client._calculate_status(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
    )
    assert status == ListStatus.PAUSED

    show, season, episodes = build_show(view_counts=[1, 0])
    status = await show_client._calculate_status(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
    )
    assert status == ListStatus.DROPPED


@pytest.mark.asyncio
async def test_calculate_status_empty_sync_marks_idle_as_planning(
    show_client: ShowSyncClient,
) -> None:
    """empty_sync should classify idle shows as planning."""
    show_client.empty_sync = True
    show, season, episodes = build_show(view_counts=[0, 0])
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=len(episodes),
    )

    status = await show_client._calculate_status(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
    )

    assert status == ListStatus.PLANNING


@pytest.mark.asyncio
async def test_calculate_status_empty_sync_skips_existing_entry(
    show_client: ShowSyncClient,
) -> None:
    """empty_sync should not force planning for already tracked show entries."""
    show_client.empty_sync = True
    show, season, episodes = build_show(view_counts=[0, 0])
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=len(episodes),
    )
    entry.status = ListStatus.COMPLETED

    status = await show_client._calculate_status(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
    )

    assert status is None


@pytest.mark.asyncio
async def test_calculate_review_prefers_episode_for_singletons(
    show_client: ShowSyncClient,
) -> None:
    """Prioritize specific episode reviews when only one episode exists."""
    episode_review = "Episode note"
    show, season, episodes = build_show(
        view_counts=[1], episode_kwargs={"review": episode_review}
    )
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=1,
    )

    review = await show_client._calculate_review(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
    )

    assert review == episode_review


@pytest.mark.asyncio
async def test_filter_history_by_episodes_sorts_records(
    show_client: ShowSyncClient,
) -> None:
    """Filter and order history entries to match watched episodes."""
    history = [
        make_history_entry("ep-1", ts=datetime(2025, 1, 5, tzinfo=UTC)),
        make_history_entry("ep-2", ts=datetime(2025, 1, 3, tzinfo=UTC)),
        make_history_entry("ignored", ts=datetime(2025, 1, 1, tzinfo=UTC)),
    ]
    show, _, episodes = build_show(
        view_counts=[1, 1],
        show_kwargs={"history": history},
    )

    filtered = await show_client._filter_history_by_episodes(
        cast(LibraryShowProtocol, show),
        cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
    )

    assert [record.library_key for record in filtered] == ["ep-2", "ep-1"]


@pytest.mark.asyncio
async def test_map_media_returns_empty_when_no_seasons(
    show_client: ShowSyncClient,
) -> None:
    """Shows without seasons should yield no mappings."""
    show = FakeLibraryShow(key="empty", title="Empty")

    results = [
        result
        async for result in show_client.map_media(cast(LibraryShowProtocol, show))
    ]

    assert results == []


@pytest.mark.asyncio
async def test_map_media_skips_missing_entries(
    show_client: ShowSyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing cached entries should fall through without results."""
    show, _season, _episodes = build_show(view_counts=[1])

    async def _resolve_batch(**_kwargs):
        return [[ResolvedListTarget("missing", (), ())]]

    async def _get_entry(_key: str):
        return None

    async def _search_media(*_args, **_kwargs):
        return None

    monkeypatch.setattr(show_module, "resolve_list_targets_batch", _resolve_batch)
    monkeypatch.setattr(show_client._cache, "get_entry", _get_entry)
    show_client.search_media = _search_media  # type: ignore[method-assign]

    results = [
        result
        async for result in show_client.map_media(cast(LibraryShowProtocol, show))
    ]

    assert results == []


@pytest.mark.asyncio
async def test_map_media_uses_search_fallback(
    show_client: ShowSyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search fallback should cache and emit a target."""
    provider = cast(FakeListProvider, show_client.list_provider)
    show, _season, episodes = build_show(view_counts=[1, 1])
    entry = FakeListEntry(
        provider=provider,
        key="901",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=len(episodes),
    )
    provider.entries["901"] = entry

    cached = {"hit": False}

    async def _resolve_batch(**_kwargs):
        return [[]]

    async def _search_media(*_args, **_kwargs):
        return entry

    def _cache_entry(_entry: ListEntryProtocol) -> None:
        cached["hit"] = True

    monkeypatch.setattr(show_module, "resolve_list_targets_batch", _resolve_batch)
    show_client.search_media = _search_media  # type: ignore[method-assign]
    monkeypatch.setattr(show_client._cache, "cache_entry", _cache_entry)

    results = [
        result
        async for result in show_client.map_media(cast(LibraryShowProtocol, show))
    ]

    assert results
    assert cached["hit"] is True
    assert results[0][2].entry is entry


@pytest.mark.asyncio
async def test_map_media_filters_out_empty_ranges(
    show_client: ShowSyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty filtered episode sets should skip targets."""
    provider = cast(FakeListProvider, show_client.list_provider)
    show, _season, _episodes = build_show(view_counts=[1])
    entry = FakeListEntry(
        provider=provider,
        key="902",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=1,
    )

    async def _resolve_batch(**_kwargs):
        return [[ResolvedListTarget("902", (), ())]]

    async def _get_entry(_key: str):
        return entry

    def _filter_episodes_by_ranges(*_args, **_kwargs):
        return []

    monkeypatch.setattr(show_module, "resolve_list_targets_batch", _resolve_batch)
    monkeypatch.setattr(show_client._cache, "get_entry", _get_entry)
    show_client._filter_episodes_by_ranges = _filter_episodes_by_ranges  # type: ignore[method-assign]

    results = [
        result
        async for result in show_client.map_media(cast(LibraryShowProtocol, show))
    ]

    assert results == []


@pytest.mark.asyncio
async def test_map_media_skips_inactive_mapping_ranges_before_lookup(
    show_client: ShowSyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inactive mapped ranges should not trigger list lookups or fallback search."""
    show, _season, _episodes = build_show(
        view_counts=[1, 0],
        season_kwargs={"mapping_descriptors": [("tmdb", "10", "s1")]},
    )
    lookup_called = False
    search_called = False

    async def _resolve_batch(**_kwargs):
        return [
            [
                ResolvedListTarget(
                    "904",
                    (),
                    (
                        make_descriptor_mapping(
                            descriptor=("tmdb", "10", "s1"),
                            source_ranges=(MappingSpec(start=2, end=2, ratio=None),),
                        ),
                    ),
                )
            ]
        ]

    async def _get_entry(_key: str):
        nonlocal lookup_called
        lookup_called = True
        return None

    async def _search_media(*_args, **_kwargs):
        nonlocal search_called
        search_called = True
        return None

    monkeypatch.setattr(show_module, "resolve_list_targets_batch", _resolve_batch)
    monkeypatch.setattr(show_client._cache, "get_entry", _get_entry)
    show_client.search_media = _search_media  # type: ignore[method-assign]

    results = [
        result
        async for result in show_client.map_media(cast(LibraryShowProtocol, show))
    ]

    assert results == []
    assert lookup_called is False
    assert search_called is False


@pytest.mark.asyncio
async def test_map_media_uses_search_fallback_for_active_mapping_range(
    show_client: ShowSyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active mapped ranges should still fall back to search on cache miss."""
    provider = cast(FakeListProvider, show_client.list_provider)
    show, _season, episodes = build_show(
        view_counts=[1, 0],
        season_kwargs={"mapping_descriptors": [("tmdb", "10", "s1")]},
    )
    entry = FakeListEntry(
        provider=provider,
        key="906",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=len(episodes),
    )
    cached = {"hit": False}

    async def _resolve_batch(**_kwargs):
        return [
            [
                ResolvedListTarget(
                    "906",
                    (),
                    (
                        make_descriptor_mapping(
                            descriptor=("tmdb", "10", "s1"),
                            source_ranges=(MappingSpec(start=1, end=1, ratio=None),),
                        ),
                    ),
                )
            ]
        ]

    async def _get_entry(_key: str):
        return None

    async def _search_media(*_args, **_kwargs):
        return entry

    def _cache_entry(_entry: ListEntryProtocol) -> None:
        cached["hit"] = True

    monkeypatch.setattr(show_module, "resolve_list_targets_batch", _resolve_batch)
    monkeypatch.setattr(show_client._cache, "get_entry", _get_entry)
    monkeypatch.setattr(show_client._cache, "cache_entry", _cache_entry)
    show_client.search_media = _search_media  # type: ignore[method-assign]

    results = [
        result
        async for result in show_client.map_media(cast(LibraryShowProtocol, show))
    ]

    assert len(results) == 1
    assert list(results[0][1]) == [episodes[0], episodes[1]]
    assert results[0][2].entry is entry
    assert cached["hit"] is True


@pytest.mark.asyncio
async def test_map_media_keeps_inactive_mapping_range_when_watchlisted(
    show_client: ShowSyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Watchlist state should keep mapped ranges eligible even without activity."""
    provider = cast(FakeListProvider, show_client.list_provider)
    show, season, episodes = build_show(
        view_counts=[1, 0],
        season_kwargs={
            "mapping_descriptors": [("tmdb", "10", "s1")],
            "on_watchlist": True,
        },
    )
    entry = FakeListEntry(
        provider=provider,
        key="907",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=len(episodes),
    )

    async def _resolve_batch(**_kwargs):
        return [
            [
                ResolvedListTarget(
                    "907",
                    (),
                    (
                        make_descriptor_mapping(
                            descriptor=("tmdb", "10", "s1"),
                            source_ranges=(MappingSpec(start=2, end=2, ratio=None),),
                        ),
                    ),
                )
            ]
        ]

    async def _get_entry(_key: str):
        return entry

    monkeypatch.setattr(show_module, "resolve_list_targets_batch", _resolve_batch)
    monkeypatch.setattr(show_client._cache, "get_entry", _get_entry)

    results = [
        result
        async for result in show_client.map_media(cast(LibraryShowProtocol, show))
    ]

    assert len(results) == 1
    assert results[0][0] is season
    assert list(results[0][1]) == [episodes[1]]
    assert results[0][2].entry is entry


@pytest.mark.asyncio
async def test_map_media_merges_groups_across_seasons(
    show_client: ShowSyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Targets for multiple seasons should merge into one group."""
    provider = cast(FakeListProvider, show_client.list_provider)
    show, seasons, episodes = build_multi_season_show()
    entry = FakeListEntry(
        provider=provider,
        key="903",
        title="Show Two",
        media_type=ListMediaType.TV,
        total_units=len(episodes),
    )
    provider.entries["903"] = entry

    async def _resolve_batch(**_kwargs):
        return [
            [ResolvedListTarget("903", (("tmdb", "1", None),), ())],
            [ResolvedListTarget("903", (("tmdb", "2", None),), ())],
        ]

    async def _get_entry(_key: str):
        return entry

    monkeypatch.setattr(show_module, "resolve_list_targets_batch", _resolve_batch)
    monkeypatch.setattr(show_client._cache, "get_entry", _get_entry)

    results = [
        result
        async for result in show_client.map_media(cast(LibraryShowProtocol, show))
    ]

    assert len(results) == 1
    mapped_season, mapped_eps, target = results[0]
    assert mapped_season in seasons
    assert len(mapped_eps) == len(episodes)
    assert len(target.mapping_descriptors) == 2


def test_filter_episodes_by_ranges_deduplicates(
    show_client: ShowSyncClient,
) -> None:
    """Duplicate episode matches should only be included once."""
    _show, _season, episodes = build_show(view_counts=[0, 0])
    mapping = make_descriptor_mapping(
        descriptor=("anilist", "1", None),
        source_ranges=(
            MappingSpec(start=1, end=2, ratio=None),
            MappingSpec(start=1, end=1, ratio=None),
        ),
    )

    filtered = show_client._filter_episodes_by_ranges(
        episodes,
        [mapping],
    )

    assert [ep.index for ep in filtered] == [1, 2]


@pytest.mark.asyncio
async def test_get_all_trackable_items_empty(show_client: ShowSyncClient) -> None:
    """Shows without episodes should return no trackable items."""
    show = FakeLibraryShow(key="empty", title="Empty")

    items = await show_client._get_all_trackable_items(cast(LibraryShowProtocol, show))

    assert items == []


@pytest.mark.asyncio
async def test_collect_prefetch_keys_empty(show_client: ShowSyncClient) -> None:
    """Shows without seasons should yield no prefetch keys."""
    show = FakeLibraryShow(key="empty", title="Empty")

    keys = await show_client._collect_prefetch_keys(cast(LibraryShowProtocol, show))

    assert keys == []


@pytest.mark.asyncio
async def test_collect_prefetch_keys_skips_inactive_mapping_ranges(
    show_client: ShowSyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prefetch should omit mapped targets whose ranges have no activity."""
    show, _season, _episodes = build_show(
        view_counts=[1, 0],
        season_kwargs={"mapping_descriptors": [("tmdb", "10", "s1")]},
    )

    async def _resolve_batch(**_kwargs):
        return [
            [
                ResolvedListTarget(
                    "905",
                    (),
                    (
                        make_descriptor_mapping(
                            descriptor=("tmdb", "10", "s1"),
                            source_ranges=(MappingSpec(start=2, end=2, ratio=None),),
                        ),
                    ),
                )
            ]
        ]

    monkeypatch.setattr(show_module, "resolve_list_targets_batch", _resolve_batch)

    keys = await show_client._collect_prefetch_keys(cast(LibraryShowProtocol, show))

    assert keys == ()


@pytest.mark.asyncio
async def test_collect_prefetch_keys_keeps_inactive_mapping_ranges_in_full_scan(
    show_client: ShowSyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full scans should continue prefetching inactive mapped ranges."""
    show_client.full_scan = True
    show, _season, _episodes = build_show(
        view_counts=[1, 0],
        season_kwargs={"mapping_descriptors": [("tmdb", "10", "s1")]},
    )

    async def _resolve_batch(**_kwargs):
        return [
            [
                ResolvedListTarget(
                    "908",
                    (),
                    (
                        make_descriptor_mapping(
                            descriptor=("tmdb", "10", "s1"),
                            source_ranges=(MappingSpec(start=2, end=2, ratio=None),),
                        ),
                    ),
                )
            ]
        ]

    monkeypatch.setattr(show_module, "resolve_list_targets_batch", _resolve_batch)

    keys = await show_client._collect_prefetch_keys(cast(LibraryShowProtocol, show))

    assert keys == ("908",)


@pytest.mark.asyncio
async def test_calculate_status_finished_no_total_units(
    show_client: ShowSyncClient,
) -> None:
    """Finished shows without total units should still be current."""
    show, season, episodes = build_show(view_counts=[1, 1])
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=None,
    )

    status = await show_client._calculate_status(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
    )

    assert status == ListStatus.CURRENT


@pytest.mark.asyncio
async def test_calculate_status_finished_no_total_units_with_ratio_expansion(
    show_client: ShowSyncClient,
) -> None:
    """Finished episode coverage should be current even when weighted units expand."""
    show, season, episodes = build_show(view_counts=[1, 1])
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=None,
    )
    mappings = (
        make_descriptor_mapping(
            descriptor=("anilist", "1", None),
            source_ranges=(MappingSpec(start=1, end=2, ratio=2),),
        ),
    )

    status = await show_client._calculate_status(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
        mappings=mappings,
    )

    assert status == ListStatus.CURRENT


@pytest.mark.asyncio
async def test_calculate_user_rating_prefers_sources(
    show_client: ShowSyncClient,
) -> None:
    """User rating should prefer episode, then season, then show."""
    show, season, episodes = build_show(
        view_counts=[1],
        show_kwargs={"user_rating": 4},
        season_kwargs={"user_rating": 6},
        episode_kwargs={"user_rating": 8},
    )
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=1,
    )

    rating = await show_client._calculate_user_rating(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
    )

    assert rating == 8


@pytest.mark.asyncio
async def test_calculate_progress_and_repeats(
    show_client: ShowSyncClient,
) -> None:
    """Progress and repeat counts should reflect view counts."""
    show, season, episodes = build_show(view_counts=[2, 3])
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=None,
    )

    progress = await show_client._calculate_progress(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
    )
    repeats = await show_client._calculate_repeats(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
    )

    assert progress == 2
    assert repeats == 1


@pytest.mark.asyncio
async def test_calculate_progress_applies_positive_ratio(
    show_client: ShowSyncClient,
) -> None:
    """Positive source ratios should scale watched progress upward."""
    show, season, episodes = build_show(view_counts=[1, 1])
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=4,
    )
    mappings = (
        make_descriptor_mapping(
            descriptor=("anilist", "1", None),
            source_ranges=(MappingSpec(start=1, end=2, ratio=2),),
        ),
    )

    progress = await show_client._calculate_progress(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
        mappings=mappings,
    )
    status = await show_client._calculate_status(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
        mappings=mappings,
    )

    assert progress == 4
    assert status == ListStatus.COMPLETED


@pytest.mark.asyncio
async def test_calculate_progress_applies_negative_ratio(
    show_client: ShowSyncClient,
) -> None:
    """Negative source ratios should scale watched progress downward."""
    show, season, episodes = build_show(view_counts=[1, 1])
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=3,
    )
    mappings = (
        make_descriptor_mapping(
            descriptor=("anilist", "1", None),
            source_ranges=(MappingSpec(start=1, end=2, ratio=-2),),
        ),
    )

    progress = await show_client._calculate_progress(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
        mappings=mappings,
    )

    assert progress == 1


@pytest.mark.asyncio
async def test_calculate_progress_applies_destination_ratio(
    show_client: ShowSyncClient,
) -> None:
    """Target-side positive ratios should compress watched progress units."""
    show, season, episodes = build_show(view_counts=[1, 1])
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=4,
    )
    mappings = (
        make_descriptor_mapping(
            descriptor=("anilist", "1", None),
            source_ranges=(MappingSpec(start=1, end=2, ratio=None),),
            target_ranges=((MappingSpec(start=10, end=None, ratio=2),),),
        ),
    )

    progress = await show_client._calculate_progress(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
        mappings=mappings,
    )

    assert progress == 1


@pytest.mark.asyncio
async def test_calculate_progress_prefers_source_ratio_over_destination_ratio(
    show_client: ShowSyncClient,
) -> None:
    """Explicit source ratio should take precedence when both sides provide one."""
    show, season, episodes = build_show(view_counts=[1, 1])
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=4,
    )
    mappings = (
        make_descriptor_mapping(
            descriptor=("anilist", "1", None),
            source_ranges=(MappingSpec(start=1, end=2, ratio=-2),),
            target_ranges=((MappingSpec(start=1, end=1, ratio=2),),),
        ),
    )

    progress = await show_client._calculate_progress(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
        mappings=mappings,
    )

    assert progress == 1


@pytest.mark.asyncio
async def test_calculate_progress_defaults_to_unit_weight_without_ratio(
    show_client: ShowSyncClient,
) -> None:
    """Destination segment lengths should not affect weight when ratio is absent."""
    show, season, episodes = build_show(view_counts=[1, 1])
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=4,
    )
    mappings = (
        make_descriptor_mapping(
            descriptor=("anilist", "1", None),
            source_ranges=(MappingSpec(start=1, end=2, ratio=None),),
            target_ranges=(
                (
                    MappingSpec(start=1, end=1, ratio=None),
                    MappingSpec(start=3, end=6, ratio=None),
                ),
            ),
        ),
    )

    progress = await show_client._calculate_progress(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
        mappings=mappings,
    )

    assert progress == 2


@pytest.mark.asyncio
async def test_calculate_progress_applies_mixed_target_ratio_segments(
    show_client: ShowSyncClient,
) -> None:
    """Mixed target segments should honor per-segment ratio semantics."""
    show, season, episodes = build_show(view_counts=[1] * 23)
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=100,
    )
    mappings = (
        make_descriptor_mapping(
            descriptor=("anilist", "17074", None),
            source_ranges=(MappingSpec(start=1, end=23, ratio=None),),
            target_ranges=(
                (
                    MappingSpec(start=1, end=22, ratio=None),
                    MappingSpec(start=23, end=26, ratio=-4),
                ),
            ),
        ),
    )

    progress = await show_client._calculate_progress(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
        mappings=mappings,
    )

    assert progress == 23


@pytest.mark.asyncio
async def test_calculate_progress_uses_piecewise_target_segments_for_steps(
    show_client: ShowSyncClient,
) -> None:
    """Per-episode progress should follow segment boundaries, not a global average."""
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=100,
    )
    mappings = (
        make_descriptor_mapping(
            descriptor=("anilist", "17074", None),
            source_ranges=(MappingSpec(start=1, end=23, ratio=None),),
            target_ranges=(
                (
                    MappingSpec(start=1, end=4, ratio=None),
                    MappingSpec(start=5, end=6, ratio=-2),
                    MappingSpec(start=7, end=9, ratio=None),
                    MappingSpec(start=10, end=11, ratio=-2),
                    MappingSpec(start=12, end=14, ratio=None),
                    MappingSpec(start=15, end=16, ratio=-2),
                    MappingSpec(start=17, end=26, ratio=None),
                ),
            ),
        ),
    )

    checkpoints = {
        4: 4,
        5: 5,
        8: 8,
        9: 9,
        12: 12,
        13: 13,
        23: 23,
    }

    for watched_count, expected in checkpoints.items():
        show, season, episodes = build_show(view_counts=[1] * watched_count)
        progress = await show_client._calculate_progress(
            item=cast(LibraryShowProtocol, show),
            child_item=cast(LibrarySeasonProtocol, season),
            grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
            entry=cast(ListEntryProtocol, entry),
            mappings=mappings,
        )

        assert progress == expected


@pytest.mark.asyncio
async def test_calculate_progress_applies_target_positive_ratio_compression(
    show_client: ShowSyncClient,
) -> None:
    """Target-side positive ratios should compress mapped progress units."""
    show, season, episodes = build_show(view_counts=[1] * 22)
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=100,
    )
    mappings = (
        make_descriptor_mapping(
            descriptor=("anilist", "17074", None),
            source_ranges=(MappingSpec(start=1, end=22, ratio=None),),
            target_ranges=((MappingSpec(start=1, end=11, ratio=2),),),
        ),
    )

    progress = await show_client._calculate_progress(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
        mappings=mappings,
    )

    assert progress == 11


@pytest.mark.asyncio
async def test_calculate_progress_caps_after_ratio_expansion(
    show_client: ShowSyncClient,
) -> None:
    """Expanded ratio progress should still cap to the entry total units."""
    show, season, episodes = build_show(view_counts=[1] * 23)
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=23,
    )
    mappings = (
        make_descriptor_mapping(
            descriptor=("anilist", "17074", None),
            source_ranges=(MappingSpec(start=1, end=23, ratio=None),),
            target_ranges=(
                (
                    MappingSpec(start=1, end=22, ratio=None),
                    MappingSpec(start=23, end=26, ratio=-4),
                ),
            ),
        ),
    )

    progress = await show_client._calculate_progress(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
        mappings=mappings,
    )

    assert progress == 23


@pytest.mark.asyncio
async def test_calculate_started_finished_at(show_client: ShowSyncClient) -> None:
    """Start and finish timestamps should use min/max history values."""
    history = [
        make_history_entry("ep-1", ts=datetime(2025, 1, 5, tzinfo=UTC)),
        make_history_entry("ep-2", ts=datetime(2025, 1, 3, tzinfo=UTC)),
    ]
    show, season, episodes = build_show(
        view_counts=[1, 1],
        show_kwargs={"history": history},
    )
    entry = FakeListEntry(
        provider=FakeListProvider(),
        key="entry",
        title="Show",
        media_type=ListMediaType.TV,
        total_units=2,
    )

    started_at = await show_client._calculate_started_at(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
    )
    finished_at = await show_client._calculate_finished_at(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        grandchild_items=cast(Sequence[LibraryEpisodeProtocol], tuple(episodes)),
        entry=cast(ListEntryProtocol, entry),
    )

    assert started_at == datetime(2025, 1, 3, tzinfo=UTC)
    assert finished_at == datetime(2025, 1, 5, tzinfo=UTC)


def test_debug_log_helpers(show_client: ShowSyncClient) -> None:
    """Debug helpers should format titles and descriptors."""
    show, season, _episodes = build_show(view_counts=[0])

    title = show_client._debug_log_title(cast(LibraryShowProtocol, show))
    season_title = show_client._debug_log_title(
        cast(LibraryShowProtocol, show), cast(LibrarySeasonProtocol, season)
    )
    ids = show_client._debug_log_ids(
        item=cast(LibraryShowProtocol, show),
        child_item=cast(LibrarySeasonProtocol, season),
        entry=None,
        media_key="123",
    )

    assert "Show" in title
    assert "S1" in season_title
    assert "123" in ids
