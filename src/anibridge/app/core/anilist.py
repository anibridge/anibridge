"""AniList Client."""

import asyncio
from collections.abc import Iterable
from typing import Any

import aiohttp
from anibridge.utils.cache import cache, ttl_cache
from anibridge.utils.limiter import Limiter

from anibridge.app import __version__, log
from anibridge.app.exceptions import AniListFilterError, AniListSearchError
from anibridge.app.models.schemas.anilist import Media

__all__ = ["AniListClient"]

# Rate limit of 30 req/min with a burst capacity of 4
anilist_limiter = Limiter(rate=30 / 60, capacity=4)


class AniListClient:
    """Client for interacting with the AniList GraphQL API.

    Provides read-only helpers for fetching AniList genres/tags, resolving filtered
    media identifiers, and retrieving batched media metadata. All requests share a
    single aiohttp session and obey a conservative rate limit.
    """

    API_URL = "https://graphql.anilist.co"

    def __init__(self, anilist_token: str | None) -> None:
        """Initialize the AniList client.

        Args:
            anilist_token (str | None): Authentication token for AniList API; if None,
                client operates in public mode for read-only queries.
        """
        self.anilist_token = anilist_token
        self._session: aiohttp.ClientSession | None = None
        self.offline_anilist_entries: dict[int, Media] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the aiohttp session.

        Returns:
            aiohttp.ClientSession: The active session for making HTTP requests.
        """
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

    async def close(self):
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def initialize(self) -> None:
        """Prepare the client for use by clearing cached entries."""
        self.offline_anilist_entries.clear()

    @cache
    def _get_genres_and_tags(self) -> tuple[Iterable[str], Iterable[str]]:
        """Get the list of AniList genres and tags.

        Returns:
            Iterable[str]: List of AniList genres and tags.
        """
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
        """Get the list of available AniList genres.

        Returns:
            Iterable[str]: List of AniList genres.
        """
        genres, _ = self._get_genres_and_tags()
        return genres

    @property
    def available_tags(self) -> Iterable[str]:
        """Get the list of available AniList tags.

        Returns:
            Iterable[str]: List of AniList tags.
        """
        _, tags = self._get_genres_and_tags()
        return tags

    @ttl_cache(ttl=3600)
    async def search_media_ids(
        self,
        *,
        filters: dict[str, Any],
        max_results: int = 1000,
        per_page: int = 50,
    ) -> list[int]:
        """Execute a filtered media search returning AniList identifiers only.

        Args:
            filters (dict[str, Any]): GraphQL-compatible media arguments. Keys must
                match AniList's `Media` query arguments (e.g. `search` or
                `duration_greater`).
            max_results (int): Maximum number of identifiers to return.
            per_page (int): AniList page size to request per API call.

        Returns:
            list[int]: Ordered AniList identifiers matching the filter.
        """
        if not filters:
            raise AniListFilterError("AniList search requires at least one filter")

        per_page = max(1, min(int(per_page), 50))
        max_results = max(1, int(max_results))

        variable_types = {
            "search": "String",
            "format": "MediaFormat",
            "format_in": "[MediaFormat]",
            "status": "MediaStatus",
            "status_in": "[MediaStatus]",
            "status_not_in": "[MediaStatus]",
            "duration": "Int",
            "duration_greater": "Int",
            "duration_lesser": "Int",
            "episodes": "Int",
            "episodes_greater": "Int",
            "episodes_lesser": "Int",
            "genre": "String",
            "genre_in": "[String]",
            "genre_not_in": "[String]",
            "tag": "String",
            "tag_in": "[String]",
            "tag_not_in": "[String]",
            "averageScore": "Int",
            "averageScore_greater": "Int",
            "averageScore_lesser": "Int",
            "popularity": "Int",
            "popularity_greater": "Int",
            "popularity_lesser": "Int",
            "startDate": "FuzzyDateInt",
            "startDate_greater": "FuzzyDateInt",
            "startDate_lesser": "FuzzyDateInt",
            "endDate": "FuzzyDateInt",
            "endDate_greater": "FuzzyDateInt",
            "endDate_lesser": "FuzzyDateInt",
            "sort": "[MediaSort]",
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

        Args:
            anilist_ids (list[int]): The AniList IDs of the anime to retrieve.

        Returns:
            list[Media]: Detailed information about the requested anime.

        Raises:
            aiohttp.ClientError: If the API request fails.
        """
        BATCH_SIZE = 50

        if not anilist_ids:
            return []

        result: list[Media] = []
        missing_ids = []

        cached_ids = [id for id in anilist_ids if id in self.offline_anilist_entries]
        if cached_ids:
            log.debug(
                "Pulling AniList data from local cache in batched mode "
                "$${anilist_ids: %s}$$",
                cached_ids,
            )
            result.extend(self.offline_anilist_entries[id] for id in cached_ids)

        missing_ids = [
            id for id in anilist_ids if id not in self.offline_anilist_entries
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
            media_by_id = {m["id"]: Media(**m) for m in media_list}

            for anilist_id in batch_ids:
                media = media_by_id.get(anilist_id)
                if not media:
                    continue
                self.offline_anilist_entries[anilist_id] = media
                result.append(media)

        return result

    @anilist_limiter
    async def _make_request(
        self, query: str, variables: dict | None = None, retry_count: int = 0
    ) -> dict:
        """Makes a rate-limited request to the AniList GraphQL API.

        Handles rate limiting, authentication, and automatic retries for
        rate limit exceeded responses.

        Args:
            query (str): GraphQL query string
            variables (dict | None): Variables for the GraphQL query
            retry_count (int): Number of retries attempted (used for temporary errors)

        Returns:
            dict: JSON response from the API

        Raises:
            aiohttp.ClientError: If the request fails for any reason other than rate
                limiting

        Note:
            - Implements rate limiting of 30 requests per minute
            - Automatically retries after waiting if rate limit is exceeded
            - Includes Authorization header using the stored token
        """
        if retry_count >= 3:
            log.error("AniList request failed after 3 attempts")
            raise aiohttp.ClientError("Failed to make request after 3 tries")

        if variables is None:
            variables = {}

        session = await self._get_session()
        variable_keys = ", ".join(sorted(variables.keys()))
        log.debug(
            "AniList request attempt $$'%s'$$ with variables $${%s}$$",
            retry_count + 1,
            variable_keys,
        )

        try:
            async with session.post(
                self.API_URL, json={"query": query, "variables": variables}
            ) as response:
                if response.status == 429:  # Handle rate limit retries
                    retry_after = int(response.headers.get("Retry-After", 60))
                    log.warning(
                        "Rate limit exceeded, waiting %s seconds (attempt %s/3)",
                        retry_after,
                        retry_count + 1,
                    )
                    await asyncio.sleep(retry_after + 1)
                    return await self._make_request(
                        query=query, variables=variables, retry_count=retry_count + 1
                    )
                elif response.status == 502:  # Bad Gateway
                    log.warning(
                        "Received 502 Bad Gateway from AniList, retrying "
                        "(attempt %s/3)",
                        retry_count + 1,
                    )
                    await asyncio.sleep(1)
                    return await self._make_request(
                        query=query, variables=variables, retry_count=retry_count + 1
                    )

                try:
                    response.raise_for_status()
                except aiohttp.ClientResponseError as exc:
                    log.error(
                        "AniList API request failed with status %s %s",
                        exc.status,
                        exc.message,
                    )
                    response_text = await response.text()
                    log.error("AniList API response payload: %s", response_text)
                    raise

                return await response.json()

        except TimeoutError, aiohttp.ClientError:
            log.warning(
                "Connection error while making request to AniList API, retrying"
            )
            log.exception("AniList API connection error details")
            await asyncio.sleep(1)
            return await self._make_request(
                query=query, variables=variables, retry_count=retry_count + 1
            )
