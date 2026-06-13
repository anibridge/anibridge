"""AniList Client."""

import asyncio
from collections.abc import Iterable
from typing import Any, ClassVar

import aiohttp
import msgspec
from anibridge.providers.list.anilist.client import global_anilist_limiter
from anibridge.utils.cache import LRUDict, cache, ttl_cache

from anibridge.app import __version__
from anibridge.app.exceptions import AniListFilterError, AniListSearchError
from anibridge.app.logging import get_logger
from anibridge.app.models.schemas.anilist import Media

__all__ = ["AnilistClient"]

log = get_logger(__name__)


class AnilistClient:
    """Client for interacting with the AniList GraphQL API.

    Provides read-only helpers for fetching AniList genres/tags, resolving filtered
    media identifiers, and retrieving batched media metadata. All requests share a
    single aiohttp session and obey a conservative rate limit.
    """

    API_URL: ClassVar[str] = "https://graphql.anilist.co"

    def __init__(self, anilist_token: str | None) -> None:
        """Initialize the AniList client."""
        self.anilist_token = anilist_token
        self._session: aiohttp.ClientSession | None = None
        self.offline_anilist_entries: LRUDict[int, Media] = LRUDict(maxsize=1024)

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the aiohttp session."""
        if self._session is None or self._session.closed:
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"AniBridge/{__version__}",
            }
            if self.anilist_token:
                headers["Authorization"] = f"Bearer {self.anilist_token}"

            self._session = aiohttp.ClientSession(headers=headers)

        return self._session

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def initialize(self) -> None:
        """Prepare the client for use by clearing cached entries."""
        self.offline_anilist_entries.clear()

    @cache
    def _get_genres_and_tags(self) -> tuple[Iterable[str], Iterable[str]]:
        """Get the list of AniList genres and tags."""
        query = """
        query {
            genres: GenreCollection
            tags: MediaTagCollection {
                name
            }
        }
        """
        log.debug("Fetching AniList genres and tags")
        response = asyncio.run(self._make_request(query))
        genres = response["data"]["genres"]
        tags = [tag["name"] for tag in response["data"]["tags"]]
        return genres, tags

    @property
    def available_genres(self) -> Iterable[str]:
        """Get the list of available AniList genres."""
        genres, _ = self._get_genres_and_tags()
        return genres

    @property
    def available_tags(self) -> Iterable[str]:
        """Get the list of available AniList tags."""
        _, tags = self._get_genres_and_tags()
        return tags

    @ttl_cache(ttl=3600, maxsize=128)
    async def search_media_ids(
        self,
        *,
        filters: dict[str, Any],
        max_results: int = 1000,
        per_page: int = 50,
    ) -> list[int]:
        """Execute a filtered media search returning AniList identifiers only."""
        if not filters:
            raise AniListFilterError("AniList search requires at least one filter")

        per_page = max(1, min(int(per_page), 50))
        max_results = max(1, int(max_results))

        variable_types = {
            "averageScore_greater": "Int",
            "averageScore_lesser": "Int",
            "averageScore": "Int",
            "duration_greater": "Int",
            "duration_lesser": "Int",
            "duration": "Int",
            "endDate_greater": "FuzzyDateInt",
            "endDate_lesser": "FuzzyDateInt",
            "endDate": "FuzzyDateInt",
            "episodes_greater": "Int",
            "episodes_lesser": "Int",
            "episodes": "Int",
            "format_in": "[MediaFormat]",
            "format": "MediaFormat",
            "genre_in": "[String]",
            "genre_not_in": "[String]",
            "genre": "String",
            "id_in": "[Int]",
            "id_not_in": "[Int]",
            "id": "Int",
            "popularity_greater": "Int",
            "popularity_lesser": "Int",
            "popularity": "Int",
            "search": "String",
            "sort": "[MediaSort]",
            "startDate_greater": "FuzzyDateInt",
            "startDate_lesser": "FuzzyDateInt",
            "startDate": "FuzzyDateInt",
            "status_in": "[MediaStatus]",
            "status_not_in": "[MediaStatus]",
            "status": "MediaStatus",
            "tag_in": "[String]",
            "tag_not_in": "[String]",
            "tag": "String",
        }

        base_variables: dict[str, Any] = {"perPage": per_page}
        arg_parts = ["type: ANIME"]
        base_var_defs = ["$perPage: Int!"]

        for key, value in filters.items():
            if value is None:
                continue
            var_type = variable_types.get(key)
            if not var_type:
                raise AniListFilterError(f"Unsupported AniList filter argument '{key}'")
            base_var_defs.append(f"${key}: {var_type}")
            arg_parts.append(f"{key}: ${key}")
            base_variables[key] = value

        if len(arg_parts) == 1:
            raise AniListFilterError(
                "AniList search requires at least one supported filter"
            )

        arg_str = ", ".join(arg_parts)
        result: list[int] = []
        seen: set[int] = set()
        current_page = 1
        pages_per_request = 100

        log.debug(
            "Executing AniList media ID search with filters $${filters: %s}$$ "
            "to retrieve up to $$'%s'$$ results",
            filters,
            max_results,
        )

        while len(result) < max_results:
            pages_remaining = (max_results - len(result) + per_page - 1) // per_page
            if pages_remaining <= 0:
                break

            batch_size = min(pages_per_request, pages_remaining)
            batch_var_defs = list(base_var_defs)
            request_vars = dict(base_variables)
            page_aliases: list[tuple[str, str, int]] = []

            start_idx = (current_page - 1) * per_page + 1
            end_idx = (current_page + batch_size - 1) * per_page
            log.debug(
                "Requesting AniList pages $$'[%s..%s] (%s..%s)'$$",
                current_page,
                current_page + batch_size - 1,
                start_idx,
                end_idx,
            )

            for idx in range(batch_size):
                alias = f"batch{idx + 1}"
                page_var = f"page_{idx + 1}"
                page_number = current_page + idx
                batch_var_defs.append(f"${page_var}: Int!")
                request_vars[page_var] = page_number
                page_aliases.append((alias, page_var, page_number))

            query_sections = [
                f"""
                {alias}: Page(page: ${page_var}, perPage: $perPage) {{
                    pageInfo {{ hasNextPage }}
                    media({arg_str}) {{ id }}
                }}
                """
                for alias, page_var, _page_number in page_aliases
            ]

            query = f"""
            query ({", ".join(batch_var_defs)}) {{
                {", ".join(query_sections)}
            }}
            """

            try:
                response = await self._make_request(query, request_vars)
            except Exception as exc:
                raise AniListSearchError("AniList search request failed") from exc
            data = response.get("data", {}) or {}

            stop = False
            for alias, _page_var, _page_number in page_aliases:
                page_data = data.get(alias) or {}
                media = page_data.get("media") or []

                if not media:
                    stop = True
                    break

                for item in media:
                    try:
                        aid = int(item["id"])
                    except KeyError, TypeError, ValueError:
                        continue
                    if aid not in seen:
                        result.append(aid)
                        seen.add(aid)
                    if len(result) >= max_results:
                        break

                if len(result) >= max_results:
                    break

                page_info = page_data.get("pageInfo") or {}
                if not page_info.get("hasNextPage"):
                    stop = True
                    break

            current_page += batch_size
            if len(result) >= max_results or stop:
                break

        return result[:max_results]

    async def batch_get_anime(self, anilist_ids: list[int]) -> list[Media]:
        """Retrieves detailed information about a list of anime.

        Attempts to fetch anime data from local cache first, falling back to
        batch API requests for entries not found in cache. Processes requests
        in batches of 10 to avoid overwhelming the API.
        """
        BATCH_SIZE = 50

        if not anilist_ids:
            return []

        result: list[Media] = []
        missing_ids = []

        cached_ids = [id_ for id_ in anilist_ids if id_ in self.offline_anilist_entries]
        if cached_ids:
            log.debug(
                "Pulling AniList data from local cache in batched mode "
                "$${anilist_ids: %s}$$",
                cached_ids,
            )
            result.extend(self.offline_anilist_entries[id_] for id_ in cached_ids)

        missing_ids = [
            id_ for id_ in anilist_ids if id_ not in self.offline_anilist_entries
        ]
        if not missing_ids:
            return result

        for i in range(0, len(missing_ids), BATCH_SIZE):
            batch_ids = missing_ids[i : i + BATCH_SIZE]
            log.debug(
                "Pulling AniList data from API in batched mode $${anilist_ids: %s}$$",
                batch_ids,
            )

            query = f"""
            query BatchGetAnime($ids: [Int]) {{
                Page(perPage: {len(batch_ids)}) {{
                    media(id_in: $ids, type: ANIME) {{
                        {Media.model_dump_graphql()}
                    }}
                }}
            }}
            """

            variables = {"ids": batch_ids}
            response = await self._make_request(query, variables)

            media_list = response.get("data", {}).get("Page", {}).get("media", []) or []
            media_by_id = {m["id"]: msgspec.convert(m, type=Media) for m in media_list}

            for anilist_id in batch_ids:
                media = media_by_id.get(anilist_id)
                if not media:
                    continue
                self.offline_anilist_entries[anilist_id] = media
                result.append(media)

        return result

    async def _make_request(self, query: str, variables: dict | None = None) -> dict:
        """Make a rate-limited AniList GraphQL request with bounded retries."""
        max_attempts = 3
        non_retryable_statuses = {
            401: "Unauthorized API request (401). Verify your AniList token is valid.",
            403: (
                "Request forbidden (403). The API may be down or your token may "
                "lack permissions."
            ),
            404: "Not found (404). The requested resource might not exist.",
        }

        session = await self._get_session()
        for attempt in range(1, max_attempts + 1):
            try:
                await global_anilist_limiter.acquire(asynchronous=True)

                async with session.post(
                    self.API_URL, json={"query": query, "variables": variables or {}}
                ) as response:
                    if response.status in non_retryable_statuses:
                        clean_query = " ".join(query.split())
                        raise aiohttp.ClientError(
                            non_retryable_statuses[response.status]
                            + f"; query={clean_query}; variables={variables}"
                        )

                    if response.status == 429:
                        retry_after = response.headers.get("Retry-After", "3")
                        delay = int(retry_after) if retry_after.isdigit() else 3

                        if attempt < max_attempts:
                            log.warning(
                                "AniList API rate limited (attempt %s/%s), retrying in "
                                "%ss",
                                attempt,
                                max_attempts,
                                delay,
                            )
                            await asyncio.sleep(delay)
                            continue

                        raise aiohttp.ClientError(
                            f"AniList API rate limited (429) after {max_attempts} "
                            "attempts"
                        )

                    response.raise_for_status()
                    return await response.json()

            except (
                TimeoutError,
                aiohttp.ClientConnectionError,
                aiohttp.ClientResponseError,
            ) as exc:
                if attempt < max_attempts:
                    log.warning(
                        "Retrying AniList request (attempt %s/%s): %s",
                        attempt,
                        max_attempts,
                        exc,
                    )
                    await asyncio.sleep(1)
                    continue

                clean_query = " ".join(query.split())
                raise aiohttp.ClientError(
                    "AniList request failed after 3 attempts. "
                    f"error={exc.__class__.__name__}: {exc}; "
                    f"query={clean_query}; variables={variables}"
                ) from exc

        clean_query = " ".join(query.split())
        raise aiohttp.ClientError(
            f"AniList request failed unexpectedly; query={clean_query}; "
            f"variables={variables}"
        )
