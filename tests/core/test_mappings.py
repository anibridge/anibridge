"""Tests for the core mappings client (descriptor graph)."""

import asyncio
import json
from compression import zstd
from pathlib import Path
from typing import Any, cast

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import anibridge.app.core.mappings as mappings_module
from anibridge.app.core.mappings import MappingsClient


def _make_client(tmp_path: Path) -> MappingsClient:
    return MappingsClient(data_path=tmp_path, upstream_url=None)


def test_content_hash_is_independent_of_source_completion_order(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    first = b'{"anilist:1": {"tmdb:2": {"1": null}}}'
    second = b'{"anilist:3": {"tmdb:4": {"1": null}}}'
    client._decode_mappings(first, "first.json")
    client._decode_mappings(second, "second.json")
    expected = client.get_content_hash()

    client.clear_cache()
    client._decode_mappings(second, "second.json")
    client._decode_mappings(first, "first.json")

    assert client.get_content_hash() == expected


def test_content_hash_tracks_source_content_and_resets(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    empty_hash = client.get_content_hash()
    client._decode_mappings(b"{}", "source.json")
    first_hash = client.get_content_hash()
    client._decode_mappings(b'{"anilist:1": {}}', "source.json")

    assert client.get_content_hash() != first_hash
    assert first_hash != empty_hash

    client.clear_cache()

    assert client.get_content_hash() == empty_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("sources", [("a.json", "b.json"), ("b.json", "a.json")])
async def test_provenance_is_independent_of_source_completion_order(
    tmp_path: Path, sources: tuple[str, str]
) -> None:
    client = _make_client(tmp_path)
    for source in sources:
        await client._finalize_mappings(
            source, {"anilist:1": {"tmdb:2": {"1": None}}}, set()
        )

    assert client.get_provenance() == {"anilist:1": ["a.json", "b.json"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect", [False, True])
async def test_load_mappings_url_accepts_large_response_headers(
    tmp_path: Path, redirect: bool
) -> None:
    expected = {"anilist:1": {"tmdb:2": {"1": None}}}
    payload = zstd.compress(json.dumps(expected).encode())
    headers = {"Content-Security-Policy": "a" * (16 * 1024)}

    async def download(_request: web.Request) -> web.Response:
        if redirect:
            raise web.HTTPFound("/asset.json.zst", headers=headers)
        return web.Response(body=payload, headers=headers)

    async def asset(_request: web.Request) -> web.Response:
        return web.Response(body=payload)

    app = web.Application()
    app.router.add_get("/mappings.json.zst", download)
    app.router.add_get("/asset.json.zst", asset)

    async with TestServer(app) as server, _make_client(tmp_path) as client:
        result = await client._load_mappings_url(
            str(server.make_url("/mappings.json.zst")), set()
        )

        assert result == expected
        assert client.is_snapshot_complete()


@pytest.mark.asyncio
async def test_get_session_uses_bounded_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mapping requests should bound connect, read, and total wait time."""
    captured: dict[str, Any] = {}

    class FakeSession:
        closed = False

        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(mappings_module.aiohttp, "ClientSession", FakeSession)
    client = _make_client(tmp_path)

    await client._get_session()

    timeout = cast(aiohttp.ClientTimeout, captured["timeout"])
    assert timeout.total == 60
    assert timeout.connect == 10
    assert timeout.sock_read == 30


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


def test_resolve_path_does_not_use_working_directory_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "child.json").write_text("{}", encoding="utf-8")
    parent = tmp_path / "nested/root.json"

    client = _make_client(tmp_path)

    assert client._resolve_path("child.json", str(parent)) == str(
        tmp_path / "nested/child.json"
    )
    assert client._resolve_path(str(tmp_path / "missing.json"), str(parent)) == str(
        tmp_path / "missing.json"
    )


@pytest.mark.parametrize("suffix", ["json.zst", "yaml", "yml.zst"])
def test_decode_mappings_uses_url_path_for_format_detection(
    tmp_path: Path, suffix: str
) -> None:
    expected = {"anilist:1": {"tmdb:2": {"1": None}}}
    payload = (
        json.dumps(expected).encode()
        if suffix.startswith("json")
        else b"anilist:1:\n  tmdb:2:\n    '1': null\n"
    )
    if suffix.endswith("zst"):
        payload = zstd.compress(payload)
    client = _make_client(tmp_path)

    result = client._decode_mappings(
        payload, f"https://example.com/mappings.{suffix}?token=example#download"
    )

    assert result == expected
    assert client.is_snapshot_complete()


@pytest.mark.asyncio
async def test_get_provenance_preservesdescriptor_keys(tmp_path: Path) -> None:
    """Getting provenance preserves the original descriptor keys."""
    data_path = tmp_path / "data"
    data_path.mkdir()

    custom_path = data_path / "mappings.json"
    custom_path.write_bytes(
        json.dumps(
            {
                "123": {"tmdb:1": {"1": None}},
                "abc:def:s1": {"tmdb:2:s1": {"1": None}},
            }
        ).encode("utf-8")
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
    """Malformed includes must not authorize replacing the mapping snapshot."""
    client = _make_client(tmp_path)
    mappings = {"$includes": "bad", "anilist:1": {"tmdb:2": {"1": None}}}

    merged = await client._finalize_mappings(
        "source.json", cast(mappings_module.AnimapDict, mappings), set()
    )

    assert "anilist:1" in merged
    assert client.get_provenance() == {"anilist:1": ["source.json"]}
    assert client.is_snapshot_complete() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("include", [None, 1, {}, "", "   "])
async def test_finalize_mappings_rejects_invalid_include_entries(
    tmp_path: Path, include: object
) -> None:
    client = _make_client(tmp_path)

    await client._finalize_mappings(
        "source.json",
        cast(mappings_module.AnimapDict, {"$includes": [include]}),
        set(),
    )

    assert client.is_snapshot_complete() is False


@pytest.mark.asyncio
async def test_load_includes_skips_circular_sources(tmp_path: Path) -> None:
    """Includes already in the current chain should be skipped."""
    client = _make_client(tmp_path)

    result = await client._load_includes(
        ["circular.json"],
        loaded_chain={str(tmp_path / "circular.json")},
        parent=str(tmp_path / "root.json"),
    )

    assert result == {}
    assert client.is_snapshot_complete()


@pytest.mark.asyncio
@pytest.mark.parametrize("delayed_source", ["a.json", "b.json"])
async def test_shared_include_inheritance_is_independent_of_completion_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delayed_source: str
) -> None:
    sources = {
        "root.json": {"$includes": ["a.json", "b.json"]},
        "a.json": {
            "$includes": ["shared.json"],
            "anilist:1": {"tvdb:2": {"1": "7"}},
        },
        "b.json": {
            "$includes": ["shared.json"],
            "anilist:1": {"tmdb:2": {"1": "2"}},
        },
        "shared.json": {"anilist:1": {"tmdb:2": {"1": "1"}}},
    }
    for name, payload in sources.items():
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")

    client = _make_client(tmp_path)
    sibling_finished = asyncio.Event()
    original_load = client._load_mappings

    async def ordered_load(src: str, loaded_chain: set[str] | None = None):
        name = Path(src).name
        if name == delayed_source:
            await sibling_finished.wait()
        result = await original_load(src, loaded_chain)
        if name in {"a.json", "b.json"} and name != delayed_source:
            sibling_finished.set()
        return result

    monkeypatch.setattr(client, "_load_mappings", ordered_load)

    result = await client.load_source(str(tmp_path / "root.json"))

    assert result["anilist:1"] == {
        "tmdb:2": {"1": "1"},
        "tvdb:2": {"1": "7"},
    }
    assert client.is_snapshot_complete()


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

    client._load_mappings = fake_load_mappings  # ty:ignore[invalid-assignment]

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

    client._load_mappings = fake_load_mappings  # ty:ignore[invalid-assignment]

    merged = await client.load_mappings()

    assert "anilist:1" in merged


@pytest.mark.asyncio
async def test_load_mappings_file_missing(tmp_path: Path) -> None:
    """Missing mapping files should return empty mappings."""
    client = _make_client(tmp_path)

    result = await client._load_mappings_file(str(tmp_path / "missing.json"), set())

    assert result == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [aiohttp.ClientError, TimeoutError])
async def test_load_mappings_url_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    """URL load should exhaust retryable failures and mark the snapshot incomplete."""
    client = _make_client(tmp_path)
    calls = {"count": 0}

    class FakeSession:
        def get(self, _url: str):
            calls["count"] += 1
            raise error_type("boom")

    async def _get_session():
        return FakeSession()

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client, "_get_session", _get_session)
    monkeypatch.setattr("anibridge.app.core.mappings.asyncio.sleep", _fast_sleep)

    result = await client._load_mappings_url("http://example", set())

    assert result == {}
    assert calls["count"] == 3
    assert client.is_snapshot_complete() is False


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
    assert client.is_snapshot_complete() is False


def test_decode_mappings_empty_object_is_complete(tmp_path: Path) -> None:
    """A valid empty object should remain an authoritative snapshot."""
    client = _make_client(tmp_path)

    result = client._decode_mappings(b"{}", "mappings.json")

    assert result == {}
    assert client.is_snapshot_complete() is True


@pytest.mark.parametrize(
    "payload", [b"", b" \n", b"# empty document\n", b"---\n# empty document\n...\n"]
)
@pytest.mark.parametrize("suffix", ["yaml", "yml", "yaml.zst"])
def test_decode_mappings_accepts_empty_yaml_documents(
    tmp_path: Path, payload: bytes, suffix: str
) -> None:
    client = _make_client(tmp_path)
    if suffix.endswith("zst"):
        payload = zstd.compress(payload)

    assert client._decode_mappings(payload, f"mappings.{suffix}") == {}
    assert client.is_snapshot_complete()


@pytest.mark.parametrize(
    ("payload", "source"),
    [
        (b"null", "mappings.json"),
        (b"null", "mappings.yaml"),
        (b"", "mappings.json"),
        (b"~", "mappings.yaml"),
        (b"!!null", "mappings.yaml"),
        (b'!!null ""', "mappings.yaml"),
        (b"1: {}", "mappings.yaml"),
        (b"[]", "mappings.json"),
    ],
)
def test_decode_mappings_rejects_non_mapping_documents(
    tmp_path: Path, payload: bytes, source: str
) -> None:
    client = _make_client(tmp_path)

    assert client._decode_mappings(payload, source) == {}
    assert client.is_snapshot_complete() is False


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

    client._load_mappings = _boom  # ty:ignore[invalid-assignment]

    result = await client._load_includes(["bad.json"], set(), "root.json")

    assert result == {}
    assert client.is_snapshot_complete() is False


@pytest.mark.asyncio
async def test_load_source_resets_provenance(tmp_path: Path) -> None:
    """load_source should clear previous provenance entries."""
    client = _make_client(tmp_path)
    client._provenance = {"x": ["old"]}

    async def _load(_src: str, _chain: set[str] | None = None):
        return {"anilist:1": {"tmdb:2": {"1": None}}}

    client._load_mappings = _load  # ty:ignore[invalid-assignment]

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
