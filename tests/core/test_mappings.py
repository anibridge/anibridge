"""Tests for the core mappings client (descriptor graph)."""

import json
from compression import zstd
from pathlib import Path
from typing import Any, cast

import aiohttp
import pytest

import anibridge.app.core.mappings as mappings_module
from anibridge.app.core.mappings import MappingsClient


def _make_client(tmp_path: Path) -> MappingsClient:
    return MappingsClient(data_path=tmp_path, upstream_url=None)


def test_deep_merge_merges_descriptor_targets(tmp_path: Path) -> None:
    """Deep merging of mappings combines descriptor targets correctly."""
    client = _make_client(tmp_path)
    left = {
        "anilist:1": {
            "tmdb:10": {"1": None},
        }
    }
    right = {
        "anilist:1": {
            "tvdb:20": {"1": "1-12"},
        },
        "tmdb:2": {"anilist:3": {"1": None}},
    }

    merged = client._deep_merge(left, right)

    assert merged["anilist:1"]["tmdb:10"] == {"1": None}
    assert merged["anilist:1"]["tvdb:20"] == {"1": "1-12"}
    assert merged["tmdb:2"] == {"anilist:3": {"1": None}}


def test_resolve_path_handles_relative_paths_and_urls(tmp_path: Path) -> None:
    """Resolving relative paths and URLs works correctly."""
    client = _make_client(tmp_path)
    parent_path = tmp_path / "parent.yaml"
    parent_path.write_text("{}", encoding="utf-8")

    child_dir = tmp_path / "includes"
    child_dir.mkdir()
    child_path = child_dir / "child.json"
    child_path.write_text("{}", encoding="utf-8")

    resolved_file = client._resolve_path("includes/child.json", str(parent_path))
    resolved_url = client._resolve_path(
        "child.json", "https://example.com/mappings/root.yaml"
    )

    assert resolved_file == child_path.resolve().as_posix()
    assert resolved_url == "https://example.com/mappings/child.json"


def test_dict_str_keys_normalizes_nested_structures(tmp_path: Path) -> None:
    """Normalizing dictionary keys to strings works for nested structures."""
    client = _make_client(tmp_path)
    data = {1: {"nested": {2: "value"}}, "list": [{3: "v"}]}
    normalized = client._dict_str_keys(data)
    assert normalized == {
        "1": {"nested": {"2": "value"}},
        "list": [{"3": "v"}],
    }


@pytest.mark.asyncio
async def test_get_provenance_preservesdescriptor_keys(tmp_path: Path) -> None:
    """Getting provenance preserves the original descriptor keys."""
    data_path = tmp_path / "data"
    data_path.mkdir()

    custom_path = data_path / "mappings.json"
    custom_path.write_text(
        json.dumps(
            {
                "123": {"tmdb:1": {"1": None}},
                "abc:def:s1": {"tmdb:2:s1": {"1": None}},
            }
        ),
        encoding="utf-8",
    )

    client = MappingsClient(data_path=data_path, upstream_url=None)
    try:
        await client.load_mappings()
    finally:
        await client.close()

    provenance = client.get_provenance()
    assert set(provenance.keys()) == {"123", "abc:def:s1"}


def test_is_file_handles_invalid_path(tmp_path: Path) -> None:
    """_is_file should guard against invalid path-like inputs."""
    client = _make_client(tmp_path)

    class BrokenPath:
        def __fspath__(self) -> str:
            raise TypeError("boom")

    assert client._is_file(cast(Any, BrokenPath())) is False


def test_decode_mappings_handles_zstd_error(tmp_path: Path) -> None:
    """Zstandard decoding errors should return an empty mapping."""
    client = _make_client(tmp_path)
    payload = b"not-zstd"

    result = client._decode_mappings(payload, "mappings.json.zst")

    assert result == {}


def test_decode_mappings_unknown_extension_defaults_to_json(tmp_path: Path) -> None:
    """Unknown extensions should be parsed as JSON."""
    client = _make_client(tmp_path)
    payload = b'{"anilist:1": {"tmdb:2": {"1": null}}}'

    result = client._decode_mappings(payload, "mappings.data")

    assert "anilist:1" in result


@pytest.mark.asyncio
async def test_finalize_mappings_with_invalid_includes(tmp_path: Path) -> None:
    """Non-list includes should be ignored while provenance is tracked."""
    client = _make_client(tmp_path)
    mappings = {"$includes": "bad", "anilist:1": {"tmdb:2": {"1": None}}}

    merged = await client._finalize_mappings(
        "source.json", cast(mappings_module.AnimapDict, mappings), set()
    )

    assert "anilist:1" in merged
    assert client.get_provenance() == {"anilist:1": ["source.json"]}


@pytest.mark.asyncio
async def test_load_includes_skips_loaded_and_circular(tmp_path: Path) -> None:
    """Includes already loaded or in the chain should be skipped."""
    client = _make_client(tmp_path)
    client._loaded_sources.add("included.json")

    result = await client._load_includes(
        ["included.json", "circular.json"],
        loaded_chain={"circular.json"},
        parent="root.json",
    )

    assert result == {}


@pytest.mark.asyncio
async def test_load_mappings_invalid_source(tmp_path: Path) -> None:
    """Invalid sources should be skipped without error."""
    client = _make_client(tmp_path)

    result = await client._load_mappings("not a file or url")

    assert result == {}


@pytest.mark.asyncio
async def test_load_mappings_merges_custom_and_upstream(tmp_path: Path) -> None:
    """Custom mappings should override upstream mappings and drop system keys."""
    data_path = tmp_path / "data"
    data_path.mkdir()
    custom_path = data_path / "mappings.json"
    custom_path.write_text("{}", encoding="utf-8")

    client = MappingsClient(data_path=data_path, upstream_url="http://example.test")

    async def fake_load_mappings(src: str):
        if src.startswith("http"):
            return {"anilist:1": {"tmdb:1": {"1": None}}, "$meta": {"x": 1}}
        return {"anilist:1": {"tmdb:2": {"1": None}}}

    client._load_mappings = fake_load_mappings  # type: ignore[method-assign]

    merged = await client.load_mappings()

    assert merged == {"anilist:1": {"tmdb:1": {"1": None}, "tmdb:2": {"1": None}}}


@pytest.mark.asyncio
async def test_load_mappings_handles_multiple_custom_files(tmp_path: Path) -> None:
    """Multiple custom mappings files should still load the first."""
    data_path = tmp_path / "data"
    data_path.mkdir()
    (data_path / "mappings.json").write_text("{}", encoding="utf-8")
    (data_path / "mappings.yaml").write_text("{}", encoding="utf-8")

    client = MappingsClient(data_path=data_path, upstream_url=None)

    async def fake_load_mappings(_src: str):
        return {"anilist:1": {"tmdb:1": {"1": None}}}

    client._load_mappings = fake_load_mappings  # type: ignore[method-assign]

    merged = await client.load_mappings()

    assert "anilist:1" in merged


@pytest.mark.asyncio
async def test_load_mappings_file_missing(tmp_path: Path) -> None:
    """Missing mapping files should return empty mappings."""
    client = _make_client(tmp_path)

    result = await client._load_mappings_file(str(tmp_path / "missing.json"), set())

    assert result == {}


@pytest.mark.asyncio
async def test_load_mappings_url_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """URL load should retry on client errors and return empty on failure."""
    client = _make_client(tmp_path)
    calls = {"count": 0}

    class FakeSession:
        def get(self, _url: str):
            calls["count"] += 1
            raise aiohttp.ClientError("boom")

    async def _get_session():
        return FakeSession()

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client, "_get_session", _get_session)
    monkeypatch.setattr("anibridge.app.core.mappings.asyncio.sleep", _fast_sleep)

    result = await client._load_mappings_url("http://example", set())

    assert result == {}
    assert calls["count"] == 3


def test_decode_mappings_handles_yaml(tmp_path: Path) -> None:
    """YAML payloads should be decoded into mappings."""
    client = _make_client(tmp_path)
    payload = b"anilist:1:\n  tmdb:2:\n    '1': null\n"

    result = client._decode_mappings(payload, "mappings.yaml")

    assert result["anilist:1"]["tmdb:2"]["1"] is None


def test_decode_mappings_invalid_json_returns_empty(tmp_path: Path) -> None:
    """Invalid JSON should return an empty mapping."""
    client = _make_client(tmp_path)

    result = client._decode_mappings(b"{", "mappings.json")

    assert result == {}


def test_decode_mappings_zstd_payload(tmp_path: Path) -> None:
    """Zstandard-compressed payloads should be decoded."""
    client = _make_client(tmp_path)
    raw = b'{"anilist:1": {"tmdb:2": {"1": null}}}'
    compressed = zstd.compress(raw)

    result = client._decode_mappings(compressed, "mappings.json.zst")

    assert "anilist:1" in result


@pytest.mark.asyncio
async def test_load_includes_handles_exceptions(tmp_path: Path) -> None:
    """Include load failures should be logged and ignored."""
    client = _make_client(tmp_path)

    async def _boom(_src: str, _chain: set[str]):
        raise RuntimeError("boom")

    client._load_mappings = _boom  # type: ignore[method-assign]

    result = await client._load_includes(["bad.json"], set(), "root.json")

    assert result == {}


@pytest.mark.asyncio
async def test_load_source_resets_provenance(tmp_path: Path) -> None:
    """load_source should clear previous provenance entries."""
    client = _make_client(tmp_path)
    client._provenance = {"x": ["old"]}

    async def _load(_src: str, _chain: set[str] | None = None):
        return {"anilist:1": {"tmdb:2": {"1": None}}}

    client._load_mappings = _load  # type: ignore[method-assign]

    result = await client.load_source("source.json")

    assert "anilist:1" in result
    assert client.get_provenance() == {}


@pytest.mark.asyncio
async def test_load_mappings_url_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful URL loads should decode payloads."""
    client = _make_client(tmp_path)
    payload = b'{"anilist:1": {"tmdb:2": {"1": null}}}'

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self) -> None:
            return None

        async def read(self):
            return payload

    class FakeSession:
        def get(self, _url: str):
            return FakeResponse()

    async def _get_session():
        return FakeSession()

    monkeypatch.setattr(client, "_get_session", _get_session)

    result = await client._load_mappings_url("http://example", set())

    assert "anilist:1" in result


@pytest.mark.asyncio
async def test_load_mappings_uses_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File sources should be routed to the file loader."""
    client = _make_client(tmp_path)
    file_path = tmp_path / "mappings.json"
    file_path.write_text("{}", encoding="utf-8")

    async def _load_file(_path: str, _chain: set[str]):
        return {"anilist:1": {"tmdb:2": {"1": None}}}

    monkeypatch.setattr(client, "_load_mappings_file", _load_file)

    result = await client._load_mappings(str(file_path))

    assert "anilist:1" in result
