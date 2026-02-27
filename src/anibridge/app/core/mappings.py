"""Mappings Client Module."""

import asyncio
import json
from compression import zstd
from pathlib import Path
from typing import Any, ClassVar, cast
from urllib.parse import urljoin, urlparse

import aiohttp
import orjson
import yaml
from anibridge.utils.cache import ttl_cache
from yaml import CSafeLoader as YamlLoader

from anibridge.app import __version__, log

__all__ = ["AnimapDict", "MappingsClient"]

type AnimapDict = dict[str, dict[str, Any]]


class MappingsClient:
    """Load mappings from files or URLs and merge them together."""

    MAPPING_FILES: ClassVar[list[str]] = [
        "mappings.yaml",
        "mappings.yml",
        "mappings.json",
    ]

    _MAX_INCLUDE_CONCURRENCY: ClassVar[int] = 8

    def __init__(self, data_path: Path, upstream_url: str | None) -> None:
        """Initialize the MappingsClient with the data path.

        Args:
            data_path (Path): Path to the data directory for storing mappings and cache
                              files.
            upstream_url (str | None): URL to the upstream mappings source JSON or YAML
                                      file. If None, no upstream mappings will be used.
        """
        self.data_path = data_path
        self.upstream_url = upstream_url
        self._loaded_sources: set[str] = set()
        self._provenance: dict[str, list[str]] = {}
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the aiohttp session."""
        if self._session is None or self._session.closed:
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"AniBridge/{__version__}",
            }
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def close(self):
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self) -> MappingsClient:
        """Context manager enter method.

        Returns:
            MappingsClient: The initialized mappings client instance.
        """
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit method.

        Args:
            exc_type: Exception type if an exception occurred.
            exc_val: Exception value if an exception occurred.
            exc_tb: Traceback object if an exception occurred.
        """
        await self.close()

    def _is_file(self, src: str) -> bool:
        """Check if the source is a file.

        Args:
            src (str): Source to check

        Returns:
            bool: True if the source is a file, False otherwise
        """
        try:
            parsed = Path(src)
        except Exception:
            return False
        return parsed.is_file()

    def _is_url(self, src: str) -> bool:
        """Check if the source is a URL.

        Args:
            src (str): Source to check

        Returns:
            bool: True if the source is a URL, False otherwise
        """
        parsed = urlparse(src)
        return bool(parsed.scheme) and bool(parsed.netloc)

    def _decode_mappings(self, payload: bytes, src: str) -> AnimapDict:
        """Decode a raw JSON/YAML payload from a given source."""
        suffixes = [suffix.lower() for suffix in Path(src).suffixes]
        if suffixes and suffixes[-1] == ".zst":
            try:
                payload = zstd.decompress(payload)
                suffixes = suffixes[:-1]
            except zstd.ZstdError:
                log.error(
                    "Error decompressing Zstandard payload $$'%s'$$",
                    src,
                )
                log.exception("Zstandard decompression error details")
                return {}
            except Exception:
                log.error(
                    "Unexpected error decompressing Zstandard payload $$'%s'$$",
                    src,
                )
                log.exception("Zstandard decompression error details")
                return {}

        suffix = suffixes[-1] if suffixes else ""
        try:
            if suffix in {".yaml", ".yml"}:
                return self._dict_str_keys(
                    yaml.load(payload.decode(), Loader=YamlLoader)
                )
            if suffix == ".json":
                return orjson.loads(payload)

            log.warning(
                "Unknown file type for $$'%s'$$, defaulting to JSON parsing",
                src,
            )
            return orjson.loads(payload)
        except json.JSONDecodeError, yaml.YAMLError:
            log.error("Error decoding file $$'%s'$$", src)
            log.exception("Decode error details")
        except Exception:
            log.error("Unexpected error reading file $$'%s'$$", src)
            log.exception("Unexpected decode error details")
        return {}

    async def _finalize_mappings(
        self, src: str, mappings: AnimapDict, loaded_chain: set[str]
    ) -> AnimapDict:
        """Merge includes and track provenance for a decoded source payload."""
        self._loaded_sources.add(src)

        if not mappings:
            log.warning("No mappings found in $$'%s'$$", src)
            return {}

        includes_value: dict | list = mappings.get("$includes", [])
        if isinstance(includes_value, list):
            includes = [str(item) for item in includes_value]
        else:
            includes = []
            log.warning(
                "The $includes key in $$'%s'$$ is not a list, ignoring all entries",
                src,
            )

        merged = self._deep_merge(
            await self._load_includes(includes, loaded_chain, src), mappings
        )

        for key in merged:
            if not str(key).startswith("$"):
                k = str(key)
                lst = self._provenance.setdefault(k, [])
                if src not in lst:
                    lst.append(src)

        return merged

    def _dict_str_keys(self, d: dict | list) -> Any:
        """Ensure all keys in a dictionary are strings.

        Args:
            d (dict | list): Dictionary or list to convert

        Returns:
            dict | list: Dictionary with all keys as strings or a list
        """
        if isinstance(d, dict):
            return {str(k): self._dict_str_keys(v) for k, v in d.items()}
        elif isinstance(d, list):
            return [self._dict_str_keys(i) for i in d]
        else:
            return d

    def _resolve_path(self, include_path: str, parent_path: str) -> str:
        """Resolve a path relative to the parent path.

        Args:
            include_path (str): Path to resolve
            parent_path (str): Parent path to resolve against

        Returns:
            str: Resolved path
        """
        is_url = self._is_url(include_path)
        is_file = self._is_file(include_path)
        is_parent_url = self._is_url(str(parent_path))
        is_parent_file = self._is_file(parent_path)

        # Absolute URL or absolute path
        if is_url or (is_file and Path(parent_path).is_absolute()):
            return include_path
        # Relative URL
        if is_parent_url:
            return urljoin(parent_path, include_path)
        # Relative path
        if is_parent_file:
            parent_dir = Path(parent_path).parent
            resolved_path = (parent_dir / include_path).resolve()
            return resolved_path.as_posix()
        # Invalid path
        return include_path

    async def _load_includes(
        self, includes: list[str], loaded_chain: set[str], parent: str
    ) -> AnimapDict:
        """Load mappings from included files or URLs.

        Args:
            includes (list[str]): List of file paths or URLs to include
            loaded_chain (set[str]): Set of already loaded includes to prevent circular
                                     includes
            parent (str): Parent path or URL to resolve relative paths against

        Returns:
            AnimapDict: Merged mappings from all included files
        """
        mappings: dict[str, dict[str, Any]] = {}
        semaphore = asyncio.Semaphore(self._MAX_INCLUDE_CONCURRENCY)

        async def _load_one(resolved_include: str) -> AnimapDict:
            async with semaphore:
                new_loaded_chain = loaded_chain | {resolved_include}
                return await self._load_mappings(resolved_include, new_loaded_chain)

        tasks: list[asyncio.Task[AnimapDict]] = []

        for include in includes:
            resolved_include = self._resolve_path(include, parent)

            if resolved_include in loaded_chain:
                log.warning(
                    "Circular include detected: $$'%s'$$ has already been loaded in "
                    "this chain",
                    resolved_include,
                )
                continue
            if resolved_include in self._loaded_sources:
                log.info(
                    "Skipping already loaded include: $$'%s'$$",
                    resolved_include,
                )
                continue

            tasks.append(asyncio.create_task(_load_one(resolved_include)))

        if not tasks:
            return mappings

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                log.error("Failed to load include: %s", result)
                try:
                    raise result
                except Exception:
                    log.exception("Include load error details")
                continue
            mappings = self._deep_merge(cast(AnimapDict, result), mappings)

        return mappings

    async def _load_mappings_file(
        self, file: str, loaded_chain: set[str]
    ) -> AnimapDict:
        """Load mappings from a local file."""
        file_path = Path(file)
        try:
            payload = file_path.read_bytes()
        except Exception:
            log.error(
                "Unexpected error reading file $$'%s'$$",
                file_path.resolve(),
            )
            log.exception("Mappings file read error details")
            return {}

        mappings = self._decode_mappings(payload, str(file_path))
        return await self._finalize_mappings(str(file_path), mappings, loaded_chain)

    async def _load_mappings_url(
        self, url: str, loaded_chain: set[str], retry_count: int = 0
    ) -> AnimapDict:
        """Load mappings from a URL with basic retry handling."""
        session = await self._get_session()
        mappings_raw: bytes | None = None

        try:
            async with session.get(url) as response:
                response.raise_for_status()
                mappings_raw = await response.read()
        except TimeoutError, aiohttp.ClientError:
            if retry_count < 2:
                log.warning(
                    "Error reaching mappings URL $$'%s'$$, retrying...",
                    url,
                )
                log.exception("Mappings URL retry error details")
                await asyncio.sleep(1)
                return await self._load_mappings_url(url, loaded_chain, retry_count + 1)
            log.error("Error reaching mappings URL $$'%s'$$", url)
            log.exception("Mappings URL error details")
        except Exception:
            log.error(
                "Unexpected error fetching mappings from URL $$'%s'$$",
                url,
            )
            log.exception("Mappings URL error details")

        if mappings_raw is None:
            return {}

        mappings = self._decode_mappings(mappings_raw, url)
        return await self._finalize_mappings(url, mappings, loaded_chain)

    async def _load_mappings(
        self, src: str, loaded_chain: set[str] | None = None
    ) -> AnimapDict:
        """Load mappings from a file or URL.

        Args:
            src (str): Path to the file or URL to load mappings from
            loaded_chain (set[str]): Set of already loaded includes to prevent
                                     circular includes (default: empty set)

        Returns:
            AnimapDict: Mappings loaded from the file or URL
        """
        if loaded_chain is None:
            loaded_chain = set()
        loaded_chain = loaded_chain | {src}

        if self._is_file(src):
            log.info("Loading mappings from file $$'%s'$$", src)
            return await self._load_mappings_file(src, loaded_chain)
        elif self._is_url(src):
            log.info("Loading mappings from URL $$'%s'$$", src)
            return await self._load_mappings_url(src, loaded_chain)
        else:
            log.warning("Invalid mappings source: $$'%s'$$, skipping", src)
            return {}

    def _deep_merge(self, d1: AnimapDict, d2: AnimapDict) -> AnimapDict:
        """Recursively merge two dictionaries with last-write-wins semantics."""
        result = d1.copy()

        for key, value in d2.items():
            if key not in result:
                result[key] = value
                continue

            if isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value

        return result

    async def load_mappings(self) -> AnimapDict:
        """Load mappings from files and URLs and merge them together.

        Loads custom mappings from local files (if they exist) and default mappings
        from the CDN URL, then merges them with custom mappings taking precedence.
        Filters out any keys starting with '$' from the final result.

        Returns:
            AnimapDict: Merged mappings with system keys removed
        """
        self._loaded_sources = set()
        self._provenance = {}

        if self.upstream_url is not None:
            log.debug(
                "Using upstream mappings URL $$'%s'$$",
                self.upstream_url,
            )
            db_mappings = await self._load_mappings(str(self.upstream_url))
        else:
            log.debug("No upstream mappings URL configured, skipping")
            db_mappings = {}

        existing_custom_mapping_files = [
            f for f in self.MAPPING_FILES if (self.data_path / f).exists()
        ]

        if existing_custom_mapping_files:
            custom_mappings_path = str(
                (self.data_path / existing_custom_mapping_files[0]).resolve()
            )
            custom_mappings = await self._load_mappings(custom_mappings_path)
        else:
            custom_mappings_path = ""
            custom_mappings = {}

        if len(existing_custom_mapping_files) > 1:
            log.warning(
                "Found multiple custom mappings files: %s. Only one mappings file "
                "can be used at a time. Defaulting to $$'%s'$$",
                existing_custom_mapping_files,
                custom_mappings_path,
            )

        if custom_mappings_path:
            log.debug(
                "Using custom mappings file $$'%s'$$",
                custom_mappings_path,
            )

        merged_mappings = self._deep_merge(db_mappings, custom_mappings)

        log.debug(
            "Loaded %s upstream, %s custom, and %s merged mappings entries",
            len(db_mappings),
            len(custom_mappings),
            len(merged_mappings),
        )

        return {k: v for k, v in merged_mappings.items() if not k.startswith("$")}

    @ttl_cache(ttl=300)
    async def load_source(self, src: str) -> AnimapDict:
        """Load mappings from a single source without merging.

        This resets internal provenance and loaded-source tracking so callers can
        inspect the raw payload and provenance produced by that specific source
        (plus any of its includes).

        Args:
            src (str): File path or URL to load.

        Returns:
            AnimapDict: Parsed mapping payload, or an empty dict on failure.
        """
        self._loaded_sources = set()
        self._provenance = {}
        return await self._load_mappings(src)

    def get_provenance(self) -> dict[str, list[str]]:
        """Return a copy of the provenance map collected during the last load."""
        return {str(k): list(v) for k, v in self._provenance.items()}
