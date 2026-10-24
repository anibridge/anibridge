"""Mappings Client Module."""

import asyncio
from compression import zstd
from hashlib import md5
from pathlib import Path
from typing import Any, ClassVar, cast
from urllib.parse import urljoin, urlparse

import aiohttp
import anyio
import msgspec.json
import yaml

from anibridge.app import __version__
from anibridge.app.logging import get_logger

try:
    from yaml import CSafeLoader as YamlLoader
except ImportError:
    from yaml import SafeLoader as YamlLoader

__all__ = ["AnimapDict", "MappingsClient"]

log = get_logger(__name__)

type AnimapDict = dict[str, dict[str, Any]]

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=10, sock_read=30)


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
        self._provenance: dict[str, list[str]] = {}
        self._session: aiohttp.ClientSession | None = None
        self._source_hashes: dict[str, str] = {}
        self._snapshot_complete = True

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the aiohttp session."""
        if self._session is None or self._session.closed:
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"AniBridge/{__version__}",
            }
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=_HTTP_TIMEOUT,
                max_field_size=64 * 1024,
            )
        return self._session

    async def close(self):
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()

    def clear_cache(self) -> None:
        """Release loaded mapping data to free memory."""
        self._provenance.clear()
        self._source_hashes.clear()

    def is_snapshot_complete(self) -> bool:
        """Return whether every source in the current snapshot loaded successfully."""
        return self._snapshot_complete

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
        source_path = urlparse(src).path if self._is_url(src) else src
        suffixes = [suffix.lower() for suffix in Path(source_path).suffixes]
        if suffixes and suffixes[-1] == ".zst":
            try:
                payload = zstd.decompress(payload)
                suffixes = suffixes[:-1]
            except zstd.ZstdError:
                self._snapshot_complete = False
                log.error(
                    "Error decompressing Zstandard payload $$'%s'$$",
                    src,
                )
                log.exception("Zstandard decompression error details")
                return {}
            except Exception:
                self._snapshot_complete = False
                log.error(
                    "Unexpected error decompressing Zstandard payload $$'%s'$$",
                    src,
                )
                log.exception("Zstandard decompression error details")
                return {}

        self._source_hashes[src] = md5(payload).hexdigest()

        suffix = suffixes[-1] if suffixes else ""
        try:
            if suffix in {".yaml", ".yml"}:
                text = payload.decode()
                decoded = yaml.load(text, Loader=YamlLoader)
                if decoded is None:
                    node = yaml.compose(text, Loader=YamlLoader)
                    if node is None or (
                        isinstance(node, yaml.ScalarNode)
                        and node.value == ""
                        and node.start_mark.index == node.end_mark.index
                    ):
                        return {}
            else:
                if suffix != ".json":
                    log.warning(
                        "Unknown file type for $$'%s'$$, defaulting to JSON parsing",
                        src,
                    )
                decoded = msgspec.json.decode(payload)

            if not isinstance(decoded, dict):
                self._snapshot_complete = False
                log.error("Mappings file $$'%s'$$ must contain an object", src)
                return {}
            if any(not isinstance(key, str) for key in decoded):
                self._snapshot_complete = False
                log.error("Mappings file $$'%s'$$ must use string keys", src)
                return {}
            return cast(AnimapDict, decoded)
        except msgspec.DecodeError, yaml.YAMLError:
            self._snapshot_complete = False
            log.error("Error decoding file $$'%s'$$", src)
            log.exception("Decode error details")
        except Exception:
            self._snapshot_complete = False
            log.error("Unexpected error reading file $$'%s'$$", src)
            log.exception("Unexpected decode error details")
        return {}

    async def _finalize_mappings(
        self, src: str, mappings: AnimapDict, loaded_chain: set[str]
    ) -> AnimapDict:
        """Merge includes and track provenance for a decoded source payload."""
        if not mappings:
            log.warning("No mappings found in $$'%s'$$", src)
            return {}

        includes_value: object = mappings.get("$includes", [])
        if isinstance(includes_value, list):
            includes = []
            for item in includes_value:
                if not isinstance(item, str) or not item.strip():
                    self._snapshot_complete = False
                    log.error(
                        "The $includes key in $$'%s'$$ must contain non-empty strings",
                        src,
                    )
                    continue
                includes.append(item.strip())
        else:
            includes = []
            self._snapshot_complete = False
            log.error(
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

    def _resolve_path(self, include_path: str, parent_path: str) -> str:
        """Resolve a path relative to the parent path.

        Args:
            include_path (str): Path to resolve
            parent_path (str): Parent path to resolve against

        Returns:
            str: Resolved path
        """
        if self._is_url(include_path):
            return include_path
        if self._is_url(parent_path):
            return urljoin(parent_path, include_path)
        return (Path(parent_path).parent / include_path).resolve().as_posix()

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
            tasks.append(asyncio.create_task(_load_one(resolved_include)))

        if not tasks:
            return mappings

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                self._snapshot_complete = False
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
        file_path = anyio.Path(file)
        try:
            payload = await file_path.read_bytes()
        except Exception:
            self._snapshot_complete = False
            log.error(
                "Unexpected error reading file $$'%s'$$",
                str(file_path),
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
            self._snapshot_complete = False
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
        if not self._is_url(src):
            src = (await anyio.Path(src).resolve()).as_posix()
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
            self._snapshot_complete = False
            log.warning("Invalid mappings source: $$'%s'$$, skipping", src)
            return {}

    def _deep_merge(self, base: AnimapDict, override: AnimapDict) -> AnimapDict:
        """Recursively merge override into base in-place."""
        for key, value in override.items():
            existing = base.get(key)
            if (
                existing is not None
                and isinstance(existing, dict)
                and isinstance(value, dict)
            ):
                self._deep_merge(existing, value)
            else:
                base[key] = value
        return base

    async def load_mappings(self) -> AnimapDict:
        """Load mappings from files and URLs and merge them together.

        Loads custom mappings from local files (if they exist) and default mappings
        from the CDN URL, then merges them with custom mappings taking precedence.
        Filters out any keys starting with '$' from the final result.

        Returns:
            AnimapDict: Merged mappings with system keys removed
        """
        self.clear_cache()
        self._snapshot_complete = True

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

        n_upstream = len(db_mappings)
        n_custom = len(custom_mappings)
        merged_mappings = self._deep_merge(db_mappings, custom_mappings)
        del db_mappings, custom_mappings

        log.debug(
            "Loaded %s upstream, %s custom, and %s merged mappings entries",
            n_upstream,
            n_custom,
            len(merged_mappings),
        )

        for key in [k for k in merged_mappings if k.startswith("$")]:
            del merged_mappings[key]
        return merged_mappings

    async def load_source(self, src: str) -> AnimapDict:
        """Load mappings from a single source without merging.

        This resets provenance and source fingerprints so callers can inspect the
        payload and provenance produced by that source and its includes.

        Args:
            src (str): File path or URL to load.

        Returns:
            AnimapDict: Parsed mapping payload, or an empty dict on failure.
        """
        self.clear_cache()
        self._snapshot_complete = True
        return await self._load_mappings(src)

    def get_content_hash(self) -> str:
        """Fingerprint source contents independently of download completion order."""
        sources = sorted(self._source_hashes.items())
        return md5(msgspec.json.encode(sources)).hexdigest()

    def get_provenance(self) -> dict[str, list[str]]:
        """Return provenance with sources ordered independently of completion time."""
        return {str(k): sorted(v) for k, v in self._provenance.items()}
