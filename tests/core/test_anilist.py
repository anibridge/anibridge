"""Tests for the AniList client."""

import asyncio

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from anibridge.app.core.anilist import AniListClient
from anibridge.app.exceptions import AniListFilterError
from anibridge.app.models.schemas.anilist import Media, MediaFormat, MediaStatus


@pytest.mark.asyncio
async def test_search_media_ids_requires_filter() -> None:
    """Reject empty filters when searching for media IDs."""
    client = AniListClient(anilist_token=None)

    with pytest.raises(AniListFilterError):
        await client.search_media_ids(filters={})

    await _clear_search_media_ids_cache(client)


@pytest.mark.asyncio
async def test_search_media_ids_rejects_unknown_filter() -> None:
    """Reject unsupported filter arguments."""
    client = AniListClient(anilist_token=None)

    with pytest.raises(AniListFilterError):
        await client.search_media_ids(filters={"fake": 1})

    await _clear_search_media_ids_cache(client)


@pytest.mark.asyncio
async def test_search_media_ids_collects_unique_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collect unique identifiers across paged responses."""
    client = AniListClient(anilist_token=None)

    call_vars: list[dict] = []

    async def fake_make_request(
        self: AniListClient, query: str, variables: dict | None = None
    ):
        call_vars.append(variables or {})
        page = (variables or {}).get("page_1", 1)
        if page <= 1:
            return {
                "data": {
                    "batch1": {
                        "pageInfo": {"hasNextPage": True},
                        "media": [{"id": 1}, {"id": 2}],
                    }
                }
            }
        return {
            "data": {
                "batch1": {
                    "pageInfo": {"hasNextPage": False},
                    "media": [{"id": 3}],
                }
            }
        }

    monkeypatch.setattr(
        AniListClient, "_make_request", fake_make_request, raising=False
    )

    result = await client.search_media_ids(
        filters={"search": "test"}, max_results=3, per_page=100
    )

    assert result == [1, 2, 3]
    assert call_vars and call_vars[0]["perPage"] == 50

    await _clear_search_media_ids_cache(client)


@pytest.mark.asyncio
async def test_batch_get_anime_combines_cached_and_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Combine cached entries with freshly fetched media in requested order."""
    client = AniListClient(anilist_token=None)

    cached_media = Media(id=1, status=MediaStatus.FINISHED, format=MediaFormat.TV)
    client.offline_anilist_entries[1] = cached_media

    request_ids: list[list[int]] = []

    async def fake_request(
        self: AniListClient, query: str, variables: dict | None = None
    ) -> dict:
        request_ids.append(list((variables or {}).get("ids", [])))
        return {
            "data": {
                "Page": {
                    "media": [
                        {
                            "id": 2,
                            "status": MediaStatus.FINISHED,
                            "format": MediaFormat.MOVIE,
                        },
                        {
                            "id": 3,
                            "status": MediaStatus.RELEASING,
                            "format": MediaFormat.ONA,
                        },
                    ]
                }
            }
        }

    monkeypatch.setattr(AniListClient, "_make_request", fake_request, raising=False)

    media = await client.batch_get_anime([1, 2, 3])

    assert request_ids == [[2, 3]]
    assert [m.id for m in media] == [1, 2, 3]
    assert set(client.offline_anilist_entries) == {1, 2, 3}


@pytest.mark.asyncio
async def test_search_media_ids_filters_none_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None-valued filters should be ignored during search."""
    client = AniListClient(anilist_token=None)

    seen: list[dict] = []

    async def fake_make_request(
        self: AniListClient, query: str, variables: dict | None = None
    ) -> dict:
        seen.append(variables or {})
        return {
            "data": {
                "batch1": {
                    "pageInfo": {"hasNextPage": False},
                    "media": [{"id": 99}],
                }
            }
        }

    monkeypatch.setattr(
        AniListClient, "_make_request", fake_make_request, raising=False
    )

    result = await client.search_media_ids(filters={"search": None, "genre": "Drama"})

    assert result == [99]
    assert seen and "search" not in seen[0]

    await _clear_search_media_ids_cache(client)


@pytest.mark.asyncio
async def test_search_media_ids_empty_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty pages should return an empty result list."""
    client = AniListClient(anilist_token=None)

    async def fake_make_request(
        self: AniListClient, query: str, variables: dict | None = None
    ) -> dict:
        return {
            "data": {
                "batch1": {
                    "pageInfo": {"hasNextPage": False},
                    "media": [],
                }
            }
        }

    monkeypatch.setattr(
        AniListClient, "_make_request", fake_make_request, raising=False
    )

    result = await client.search_media_ids(filters={"search": "nothing"})

    assert result == []

    await _clear_search_media_ids_cache(client)


def test_available_genres_and_tags_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Genre/tag helpers should return parsed data."""
    client = AniListClient(anilist_token=None)

    async def fake_make_request(_query: str, _variables: dict | None = None) -> dict:
        return {
            "data": {
                "genres": ["Action"],
                "tags": [{"name": "Test"}],
            }
        }

    monkeypatch.setattr(client, "_make_request", fake_make_request)

    assert list(client.available_genres) == ["Action"]
    assert list(client.available_tags) == ["Test"]


@pytest.mark.asyncio
async def test_search_media_ids_rejects_all_none_filters() -> None:
    """Filters that only contain None values should be rejected."""
    client = AniListClient(anilist_token=None)

    with pytest.raises(AniListFilterError):
        await client.search_media_ids(filters={"search": None})


@pytest.mark.asyncio
async def test_batch_get_anime_returns_empty_for_no_ids() -> None:
    """Empty ID lists should return an empty result."""
    client = AniListClient(anilist_token=None)

    assert await client.batch_get_anime([]) == []


@pytest.mark.asyncio
async def test_batch_get_anime_uses_cache_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cached entries should be returned without API calls."""
    client = AniListClient(anilist_token=None)
    client.offline_anilist_entries[1] = Media(
        id=1,
        status=MediaStatus.FINISHED,
        format=MediaFormat.TV,
    )

    async def _boom(*_args, **_kwargs):
        raise AssertionError("unexpected request")

    monkeypatch.setattr(AniListClient, "_make_request", _boom, raising=False)

    media = await client.batch_get_anime([1])

    assert [m.id for m in media] == [1]


@pytest.mark.asyncio
async def test_make_request_retries_on_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rate-limited requests should retry and succeed."""
    client = AniListClient(anilist_token=None)

    class DummyResponse:
        def __init__(self, status: int, headers: dict | None = None, json_data=None):
            self.status = status
            self.headers = headers or {}
            self._json = json_data or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self) -> None:
            return None

        async def json(self):
            return self._json

        async def text(self) -> str:
            return ""

    class DummySession:
        def __init__(self, responses: list[DummyResponse]) -> None:
            self.responses = responses
            self.calls = 0

        def post(self, _url: str, **_kwargs):
            self.calls += 1
            return self.responses.pop(0)

    responses = [
        DummyResponse(429, headers={"Retry-After": "0"}),
        DummyResponse(200, json_data={"data": {"ok": True}}),
    ]
    session = DummySession(responses)

    async def _get_session():
        return session

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client, "_get_session", _get_session)
    monkeypatch.setattr("anibridge.app.core.anilist.asyncio.sleep", _fast_sleep)

    result = await client._make_request("query")

    assert result == {"data": {"ok": True}}
    assert session.calls == 2


@pytest.mark.asyncio
async def test_make_request_retries_on_bad_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad gateway responses should retry and succeed."""
    client = AniListClient(anilist_token=None)

    class DummyResponse:
        def __init__(self, status: int, json_data=None):
            self.status = status
            self.headers = {}
            self._json = json_data or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self) -> None:
            return None

        async def json(self):
            return self._json

        async def text(self) -> str:
            return ""

    class DummySession:
        def __init__(self, responses: list[DummyResponse]) -> None:
            self.responses = responses
            self.calls = 0

        def post(self, _url: str, **_kwargs):
            self.calls += 1
            return self.responses.pop(0)

    responses = [
        DummyResponse(502),
        DummyResponse(200, json_data={"data": {"ok": True}}),
    ]
    session = DummySession(responses)

    async def _get_session():
        return session

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client, "_get_session", _get_session)
    monkeypatch.setattr("anibridge.app.core.anilist.asyncio.sleep", _fast_sleep)

    result = await client._make_request("query")

    assert result == {"data": {"ok": True}}
    assert session.calls == 2


@pytest.mark.asyncio
async def test_make_request_retries_until_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated client errors should raise after max attempts."""
    client = AniListClient(anilist_token=None)

    class DummySession:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, _url: str, **_kwargs):
            self.calls += 1
            raise aiohttp.ClientError("boom")

    session = DummySession()

    async def _get_session():
        return session

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client, "_get_session", _get_session)
    monkeypatch.setattr("anibridge.app.core.anilist.asyncio.sleep", _fast_sleep)

    with pytest.raises(aiohttp.ClientError):
        await client._make_request("query")

    assert session.calls == 3


@pytest.mark.asyncio
async def test_make_request_logs_response_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Response errors should log and retry once."""
    client = AniListClient(anilist_token=None)

    class DummyResponse:
        def __init__(self, status: int, json_data=None, text_data: str = "") -> None:
            self.status = status
            self.headers = {}
            self._json = json_data or {}
            self._text = text_data

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self) -> None:
            if self.status >= 400:
                request_info = aiohttp.RequestInfo(
                    url=URL("http://example.com"),
                    method="POST",
                    headers=CIMultiDictProxy(CIMultiDict()),
                    real_url=URL("http://example.com"),
                )
                raise aiohttp.ClientResponseError(
                    request_info,
                    (),
                    status=self.status,
                    message="boom",
                )

        async def json(self):
            return self._json

        async def text(self) -> str:
            return self._text

    class DummySession:
        def __init__(self, responses: list[DummyResponse]) -> None:
            self.responses = responses
            self.calls = 0

        def post(self, _url: str, **_kwargs):
            self.calls += 1
            return self.responses.pop(0)

    responses = [
        DummyResponse(500, text_data="fail"),
        DummyResponse(200, json_data={"data": {"ok": True}}),
    ]
    session = DummySession(responses)

    async def _get_session():
        return session

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client, "_get_session", _get_session)
    monkeypatch.setattr("anibridge.app.core.anilist.asyncio.sleep", _fast_sleep)

    result = await client._make_request("query")

    assert result == {"data": {"ok": True}}
    assert session.calls == 2


def test_get_session_sets_auth_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Session creation should include auth headers when token is present."""
    headers_seen = {}

    class DummySession:
        def __init__(self, headers: dict) -> None:
            self.headers = headers
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    def _client_session(*, headers: dict):
        headers_seen.update(headers)
        return DummySession(headers)

    monkeypatch.setattr(
        "anibridge.app.core.anilist.aiohttp.ClientSession", _client_session
    )

    client = AniListClient(anilist_token="token")
    session = asyncio.run(client._get_session())

    assert session.headers["Authorization"] == "Bearer token"
    assert headers_seen["User-Agent"].startswith("AniBridge/")


async def _clear_search_media_ids_cache(client: AniListClient) -> None:
    cache = getattr(client.search_media_ids, "cache", None)
    if cache is not None:
        await cache.clear()
