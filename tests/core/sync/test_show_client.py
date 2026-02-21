"""Unit tests for `src.core.sync.show`."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from anibridge.library import (
    LibraryEpisode as LibraryEpisodeProtocol,
)
from anibridge.library import (
    LibrarySeason as LibrarySeasonProtocol,
)
from anibridge.library import (
    LibraryShow as LibraryShowProtocol,
)
from anibridge.list import ListEntry as ListEntryProtocol
from anibridge.list import ListMediaType, ListStatus

from src.core.sync.base import ResolvedListTarget, SourceRangeMapping
from src.core.sync.show import ShowSyncClient
from src.models.db.sync_history import SyncHistory
from src.utils.mapping_ranges import SourceRange
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
    from src.config.database import AniBridgeDB


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
    show_client: ShowSyncClient, sync_db: AniBridgeDB
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
        assert ctx.session.query(SyncHistory).count() == 1


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
    _show, season, episodes = build_show(view_counts=[0, 0, 0])
    mapping = SourceRangeMapping(
        descriptor=("anilist", "1", None),
        ranges=(SourceRange(start=2, end=2, ratio=None),),
    )

    filtered = show_client._filter_episodes_by_ranges(
        episodes,
        season.index,
        [mapping],
    )

    assert [ep.index for ep in filtered] == [2]


@pytest.mark.asyncio
async def test_collect_prefetch_keys_sorted(show_client: ShowSyncClient) -> None:
    """Prefetch keys should be sorted and unique."""
    show_client.full_scan = True
    show, _season, _episodes = build_show(view_counts=[0])

    async def _resolve_batch(_payloads):
        return [
            [ResolvedListTarget("3", (), ())],
            [ResolvedListTarget("1", (), ())],
        ]

    show_client._resolve_list_targets_batch = _resolve_batch  # type: ignore[method-assign]

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
async def test_map_media_skips_missing_entries(show_client: ShowSyncClient) -> None:
    """Missing cached entries should fall through without results."""
    show, _season, _episodes = build_show(view_counts=[1])

    async def _resolve_batch(_payloads):
        return [[ResolvedListTarget("missing", (), ())]]

    async def _get_entry_cached(_key: str):
        return None

    async def _search_media(*_args, **_kwargs):
        return None

    show_client._resolve_list_targets_batch = _resolve_batch  # type: ignore[method-assign]
    show_client._get_entry_cached = _get_entry_cached  # type: ignore[method-assign]
    show_client.search_media = _search_media  # type: ignore[method-assign]

    results = [
        result
        async for result in show_client.map_media(cast(LibraryShowProtocol, show))
    ]

    assert results == []


@pytest.mark.asyncio
async def test_map_media_uses_search_fallback(show_client: ShowSyncClient) -> None:
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

    async def _resolve_batch(_payloads):
        return [[]]

    async def _search_media(*_args, **_kwargs):
        return entry

    def _cache_list_entry(_entry: ListEntryProtocol) -> None:
        cached["hit"] = True

    show_client._resolve_list_targets_batch = _resolve_batch  # type: ignore[method-assign]
    show_client.search_media = _search_media  # type: ignore[method-assign]
    show_client._cache_list_entry = _cache_list_entry  # type: ignore[method-assign]

    results = [
        result
        async for result in show_client.map_media(cast(LibraryShowProtocol, show))
    ]

    assert results
    assert cached["hit"] is True
    assert results[0][2].entry is entry


@pytest.mark.asyncio
async def test_map_media_filters_out_empty_ranges(show_client: ShowSyncClient) -> None:
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

    async def _resolve_batch(_payloads):
        return [[ResolvedListTarget("902", (), ())]]

    async def _get_entry_cached(_key: str):
        return entry

    def _filter_episodes_by_ranges(*_args, **_kwargs):
        return []

    show_client._resolve_list_targets_batch = _resolve_batch  # type: ignore[method-assign]
    show_client._get_entry_cached = _get_entry_cached  # type: ignore[method-assign]
    show_client._filter_episodes_by_ranges = _filter_episodes_by_ranges  # type: ignore[method-assign]

    results = [
        result
        async for result in show_client.map_media(cast(LibraryShowProtocol, show))
    ]

    assert results == []


@pytest.mark.asyncio
async def test_map_media_merges_groups_across_seasons(
    show_client: ShowSyncClient,
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

    async def _resolve_batch(_payloads):
        return [
            [ResolvedListTarget("903", (("tmdb", "1", None),), ())],
            [ResolvedListTarget("903", (("tmdb", "2", None),), ())],
        ]

    async def _get_entry_cached(_key: str):
        return entry

    show_client._resolve_list_targets_batch = _resolve_batch  # type: ignore[method-assign]
    show_client._get_entry_cached = _get_entry_cached  # type: ignore[method-assign]

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
    _show, season, episodes = build_show(view_counts=[0, 0])
    mapping = SourceRangeMapping(
        descriptor=("anilist", "1", None),
        ranges=(
            SourceRange(start=1, end=2, ratio=None),
            SourceRange(start=1, end=1, ratio=None),
        ),
    )

    filtered = show_client._filter_episodes_by_ranges(
        episodes,
        season.index,
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
