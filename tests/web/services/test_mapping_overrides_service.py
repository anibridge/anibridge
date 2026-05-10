"""Tests for the mapping overrides service (v3)."""

import json
from pathlib import Path
from typing import Any, cast

import pytest

from anibridge.app.config.settings import get_config
from anibridge.app.exceptions import (
    MappingError,
    MissingDescriptorError,
    SchedulerNotInitializedError,
)
from anibridge.app.web.services.mapping_overrides_service import (
    MappingOverridesService,
    get_mapping_overrides_service,
)
from anibridge.app.web.state import get_app_state


class DummyScheduler:
    """Scheduler double exposing only the sync_db hook."""

    def __init__(self) -> None:
        """Initialize the dummy scheduler."""
        self.synced = False
        self.sync_sources: list[str] = []

    async def trigger_database_sync(self, source: str = "manual:database") -> None:
        self.sync_sources.append(source)
        self.synced = True


@pytest.fixture()
def overrides_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up environment for mapping overrides tests."""
    app_config = get_config()
    monkeypatch.setattr(app_config, "data_path", tmp_path)
    monkeypatch.setattr(
        MappingOverridesService,
        "_load_upstream",
        lambda self: _empty_upstream(),
    )
    scheduler = DummyScheduler()
    state = get_app_state()
    state.scheduler = cast(Any, scheduler)
    yield tmp_path, scheduler
    state.scheduler = None


async def _empty_upstream() -> dict[str, Any]:
    """Default upstream stub used to keep local override tests fast."""
    return {}


@pytest.mark.asyncio
async def test_save_override_writes_file_and_syncs_db(
    overrides_env: tuple[Path, DummyScheduler],
) -> None:
    """Saving an override persists to file and triggers DB sync."""
    tmp_path, scheduler = overrides_env
    service = MappingOverridesService()

    result = await service.save_override(
        descriptor="anilist:101",
        targets=[
            {
                "provider": "tmdb",
                "entry_id": "202",
                "ranges": [
                    {
                        "source_range": "1",
                        "destination_range": None,
                    }
                ],
            }
        ],
    )
    assert result["descriptor"] == "anilist:101"
    assert result["layers"]["effective"]["tmdb:202"]["1"] is None

    data = json.loads((tmp_path / "mappings.json").read_text(encoding="utf-8"))
    assert data["anilist:101"] == {"tmdb:202": {"1": None}}
    assert scheduler.synced is True
    assert scheduler.sync_sources == ["service:mapping_overrides"]


@pytest.mark.asyncio
async def test_get_mapping_detail_layers_upstream_and_custom(
    overrides_env: tuple[Path, DummyScheduler], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detail view includes upstream placeholders and custom overrides."""
    service = MappingOverridesService()

    async def fake_load_upstream(self):
        return {"anilist:909": {"tmdb:777": {"1": "1-3"}}}

    monkeypatch.setattr(MappingOverridesService, "_load_upstream", fake_load_upstream)

    await service.save_override(
        descriptor="anilist:909",
        targets=[
            {
                "provider": "tmdb",
                "entry_id": "777",
                "ranges": [
                    {
                        "source_range": "1",
                        "destination_range": None,
                    }
                ],
            }
        ],
    )

    detail = await service.get_mapping_detail("anilist:909")
    assert detail["layers"]["upstream"]["tmdb:777"]["1"] == "1-3"
    assert detail["layers"]["custom"]["tmdb:777"]["1"] is None
    assert detail["layers"]["effective"]["tmdb:777"]["1"] is None

    assert detail["targets"]
    entry = detail["targets"][0]
    assert entry["origin"] == "mixed"
    assert entry["ranges"] == [
        {
            "source_range": "1",
            "upstream": "1-3",
            "custom": None,
            "effective": None,
            "origin": "custom",
            "inherited": False,
        }
    ]


@pytest.mark.asyncio
async def test_save_override_rejects_invalid_source_range(
    overrides_env: tuple[Path, DummyScheduler],
) -> None:
    """Invalid source ranges with commas are rejected."""
    service = MappingOverridesService()

    with pytest.raises(MappingError):
        await service.save_override(
            descriptor="anilist:500",
            targets=[
                {
                    "provider": "tmdb",
                    "entry_id": "900",
                    "ranges": [
                        {
                            "source_range": "1,2",
                            "destination_range": "1-2",
                        }
                    ],
                }
            ],
        )


@pytest.mark.asyncio
async def test_save_override_allows_ratio_ranges(
    overrides_env: tuple[Path, DummyScheduler],
) -> None:
    """Ratio-form ranges are accepted for source and destination."""
    tmp_path, _scheduler = overrides_env
    service = MappingOverridesService()

    result = await service.save_override(
        descriptor="anilist:501",
        targets=[
            {
                "provider": "tmdb",
                "entry_id": "901",
                "ranges": [
                    {
                        "source_range": "1-6",
                        "destination_range": "1-3|2",
                    }
                ],
            }
        ],
    )

    assert result["layers"]["effective"]["tmdb:901"]["1-6"] == "1-3|2"
    data = json.loads((tmp_path / "mappings.json").read_text(encoding="utf-8"))
    assert data["anilist:501"]["tmdb:901"]["1-6"] == "1-3|2"


def test_mapping_overrides_service_scheduler_and_singleton(overrides_env) -> None:
    """Scheduler access should fail cleanly when the app state is uninitialized."""
    _tmp_path, _scheduler = overrides_env
    service = MappingOverridesService()
    state = get_app_state()
    original = state.scheduler
    state.scheduler = None
    try:
        with pytest.raises(SchedulerNotInitializedError):
            service._ensure_scheduler()
    finally:
        state.scheduler = original

    assert get_mapping_overrides_service() is get_mapping_overrides_service()


def test_mapping_overrides_service_yaml_loading_and_writing(
    overrides_env: tuple[Path, DummyScheduler],
) -> None:
    """YAML custom mapping files should round-trip through the raw helpers."""
    tmp_path, _scheduler = overrides_env
    yaml_path = tmp_path / "mappings.yaml"
    yaml_path.write_text("anilist:10:\n  tmdb:20:\n    '1': '1-3'\n", encoding="utf-8")
    service = MappingOverridesService()

    raw, path, fmt = service._load_raw()
    assert path == yaml_path
    assert fmt == "yaml"
    assert raw == {"anilist:10": {"tmdb:20": {"1": "1-3"}}}

    service._write_raw({"anilist:11": {"tmdb:21": {"1": None}}}, yaml_path, "yaml")

    assert "anilist:11" in yaml_path.read_text(encoding="utf-8")


def test_mapping_overrides_service_load_raw_handles_empty_and_invalid_yaml(
    overrides_env: tuple[Path, DummyScheduler],
) -> None:
    """Raw loading should normalize null payloads and reject non-object files."""
    tmp_path, _scheduler = overrides_env
    yaml_path = tmp_path / "mappings.yaml"
    service = MappingOverridesService()

    yaml_path.write_text("null\n", encoding="utf-8")
    raw, path, fmt = service._load_raw()
    assert raw == {}
    assert path == yaml_path
    assert fmt == "yaml"

    yaml_path.write_text("- just\n- a-list\n", encoding="utf-8")
    with pytest.raises(MappingError, match="must contain an object"):
        service._load_raw()


def test_mapping_overrides_service_normalizes_and_builds_views() -> None:
    """Normalization and view-building should ignore invalid payload fragments."""
    service = MappingOverridesService()

    normalized = service._normalize_targets(
        {
            None: {"1": "1"},
            "$meta": {"1": "1"},
            "tmdb:1": {
                "1": "1-2",
                2: None,
                object(): "1",
                "bad": "1",
                "4": 5,
            },
            "tvdb:2": None,
            "broken": ["nope"],
        }
    )
    assert normalized == {"tmdb:1": {"1": "1-2", "2": None}, "tvdb:2": None}

    merged = service._merge_targets(
        {"tmdb:1": {"1": "1-3"}, "tvdb:2": {"2": "2"}},
        {"tmdb:1": {"1": None, "4": "5"}, "tvdb:2": None},
    )
    assert merged == {"tmdb:1": {"1": None, "4": "5"}, "tvdb:2": None}

    views = service._build_target_views(
        {"tmdb:1": {"1": "1-3"}},
        {"tmdb:1": {"1": None}, "bad descriptor": {"1": "1"}, "tvdb:2": None},
        {"tmdb:1": {"1": None}, "tvdb:2": None},
    )

    assert len(views) == 2
    assert views[0]["origin"] in {"mixed", "deleted"}
    assert {view["descriptor"] for view in views} == {"tmdb:1", "tvdb:2"}
    assert service._normalize_includes([" ./a.yaml ", "", 123, None]) == [
        "./a.yaml",
        "123",
    ]
    assert service._normalize_includes("bad") == []


def test_mapping_overrides_service_validation_errors() -> None:
    """Validation helpers should reject malformed targets and ranges."""
    service = MappingOverridesService()

    with pytest.raises(MappingError, match="source_range is required"):
        service._validate_ranges([{"destination_range": "1"}])
    with pytest.raises(
        MappingError,
        match="destination_range must be a string or null",
    ):
        service._validate_ranges([{"source_range": "1", "destination_range": 1}])
    with pytest.raises(MappingError, match="source_range must match"):
        service._validate_ranges([{"source_range": "1,2", "destination_range": "1"}])
    with pytest.raises(MappingError, match="provider and entry_id are required"):
        service._validate_targets([{"provider": "", "entry_id": ""}])


@pytest.mark.asyncio
async def test_save_override_requires_descriptor_and_removes_entry(
    overrides_env: tuple[Path, DummyScheduler],
) -> None:
    """Saving without a descriptor should fail, and empty targets should remove data."""
    tmp_path, _scheduler = overrides_env
    service = MappingOverridesService()

    with pytest.raises(MissingDescriptorError):
        await service.save_override(descriptor=None, targets=[])

    (tmp_path / "mappings.json").write_text(
        json.dumps({"anilist:123": {"tmdb:456": {"1": "1"}}}),
        encoding="utf-8",
    )
    result = await service.save_override(descriptor="anilist:123", targets=[])

    assert result["layers"]["custom"] == {}
    raw = json.loads((tmp_path / "mappings.json").read_text(encoding="utf-8"))
    assert raw == {}


@pytest.mark.asyncio
async def test_mapping_config_round_trips_includes_and_preserves_overrides(
    overrides_env: tuple[Path, DummyScheduler],
) -> None:
    """Saving mapping config should only touch the top-level include list."""
    tmp_path, scheduler = overrides_env
    (tmp_path / "mappings.json").write_text(
        json.dumps(
            {
                "$includes": ["./old.yaml"],
                "anilist:123": {"tmdb:456": {"1": "1"}},
            }
        ),
        encoding="utf-8",
    )

    service = MappingOverridesService()

    loaded = await service.get_mapping_config()
    assert loaded["mappings_url"] == get_config().mappings_url
    assert loaded["includes"] == ["./old.yaml"]
    assert loaded["format"] == "json"

    saved = await service.save_mapping_config(
        includes=[" ./community.yaml ", "https://example.com/base.json", ""]
    )

    assert saved["includes"] == [
        "./community.yaml",
        "https://example.com/base.json",
    ]
    assert saved["mappings_url"] == get_config().mappings_url
    assert scheduler.sync_sources == ["service:mapping_overrides"]

    raw = json.loads((tmp_path / "mappings.json").read_text(encoding="utf-8"))
    assert raw == {
        "$includes": ["./community.yaml", "https://example.com/base.json"],
        "anilist:123": {"tmdb:456": {"1": "1"}},
    }


@pytest.mark.asyncio
async def test_mapping_config_removes_empty_includes_key(
    overrides_env: tuple[Path, DummyScheduler],
) -> None:
    """Saving an empty include list should drop the top-level key."""
    tmp_path, _scheduler = overrides_env
    (tmp_path / "mappings.json").write_text(
        json.dumps(
            {
                "$includes": ["./old.yaml"],
                "anilist:123": {"tmdb:456": {"1": "1"}},
            }
        ),
        encoding="utf-8",
    )

    service = MappingOverridesService()
    saved = await service.save_mapping_config(includes=[])

    assert saved["mappings_url"] == get_config().mappings_url
    assert saved["includes"] == []
    raw = json.loads((tmp_path / "mappings.json").read_text(encoding="utf-8"))
    assert raw == {"anilist:123": {"tmdb:456": {"1": "1"}}}
