"""Caching decorators for LRU, TTL, and file-based caching. Supports async functions."""

import asyncio
import atexit
import contextlib
import functools
import hashlib
import inspect
import threading
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ParamSpec, Protocol, TypeVar, cast, overload

from cachetools import LRUCache as CachetoolsLRUCache
from cachetools import TTLCache as CachetoolsTTLCache
from diskcache import Cache as DiskCache

__all__ = ["CacheInfo", "cache", "file_cache", "lru_cache", "ttl_cache"]

P = ParamSpec("P")
T = TypeVar("T")


_DEFAULT_CACHE_DIR: Path | None = None
_UNBOUNDED_MAXSIZE = 2**31 - 1
_MISSING = object()
_DISK_CACHES: set[DiskCache] = set()
_DISK_CACHES_LOCK = threading.RLock()


def _resolve_default_cache_dir() -> Path:
    """Resolve the default cache directory without eager settings import."""
    global _DEFAULT_CACHE_DIR
    if _DEFAULT_CACHE_DIR is None:
        from anibridge.app.config.settings import get_config

        _DEFAULT_CACHE_DIR = get_config().data_path / ".cache"
    return _DEFAULT_CACHE_DIR


def _close_disk_cache(cache: DiskCache) -> None:
    with contextlib.suppress(Exception):
        cache.close()


def _close_all_disk_caches() -> None:
    with _DISK_CACHES_LOCK:
        caches = tuple(_DISK_CACHES)
        _DISK_CACHES.clear()
    for cache in caches:
        _close_disk_cache(cache)


atexit.register(_close_all_disk_caches)


def _register_disk_cache(cache: DiskCache) -> None:
    with _DISK_CACHES_LOCK:
        _DISK_CACHES.add(cache)


class CachedFunction(Protocol[P, T]):
    """Protocol for cached functions with cache management methods."""

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> T:
        """Call the cached function."""
        ...

    def cache_clear(self) -> None:
        """Clear the cache."""
        ...

    def cache_info(self) -> CacheInfo:
        """Get cache information."""
        ...

    @overload
    def __get__(
        self,
        instance: None,
        owner: type[Any] | None = None,
    ) -> CachedFunction[P, T]: ...

    @overload
    def __get__(
        self,
        instance: object,
        owner: type[Any] | None = None,
    ) -> BoundCachedFunction[T]: ...


class CachedAsyncFunction(Protocol[P, T]):
    """Protocol for cached async functions with cache management methods."""

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> Awaitable[T]:
        """Call the cached async function."""
        ...

    def cache_clear(self) -> None:
        """Clear the cache."""
        ...

    def cache_info(self) -> CacheInfo:
        """Get cache information."""
        ...

    @overload
    def __get__(
        self,
        instance: None,
        owner: type[Any] | None = None,
    ) -> CachedAsyncFunction[P, T]: ...

    @overload
    def __get__(
        self,
        instance: object,
        owner: type[Any] | None = None,
    ) -> BoundCachedAsyncFunction[T]: ...


class BoundCachedFunction(Protocol[T]):
    """Protocol for bound cached sync methods."""

    def __call__(self, *args: Any, **kwargs: Any) -> T: ...

    def cache_clear(self) -> None: ...

    def cache_info(self) -> CacheInfo: ...


class BoundCachedAsyncFunction(Protocol[T]):
    """Protocol for bound cached async methods."""

    def __call__(self, *args: Any, **kwargs: Any) -> Awaitable[T]: ...

    def cache_clear(self) -> None: ...

    def cache_info(self) -> CacheInfo: ...


@dataclass(frozen=True, slots=True)
class CacheInfo:
    """Cache statistics snapshot."""

    hits: int
    misses: int
    maxsize: int | None
    currsize: int
    ttl: float | None = None


def _generic_hash(obj: Any, _visited_ids: set[int] | None = None) -> int:
    """Generate a hash for arbitrary objects, handling unhashable types."""
    if _visited_ids is None:
        _visited_ids = set()

    obj_id = id(obj)
    if obj_id in _visited_ids:
        return hash("<cycle>")

    _visited_ids.add(obj_id)
    try:
        h = hash(obj)
    except TypeError:
        if isinstance(obj, list | tuple):
            h = hash(tuple(_generic_hash(item, _visited_ids) for item in obj))
        elif isinstance(obj, set):
            h = hash(frozenset(_generic_hash(item, _visited_ids) for item in obj))
        elif isinstance(obj, dict):
            h = hash(
                tuple(
                    sorted(
                        (_generic_hash(k, _visited_ids), _generic_hash(v, _visited_ids))
                        for k, v in obj.items()
                    )
                )
            )
        else:
            h = hash(obj_id)
    finally:
        # Use finally to ensure cleanup even if exception occurs
        _visited_ids.discard(obj_id)
    return h


def _make_key(
    args: tuple[Any, ...], kwargs: dict[str, Any], strict: bool = True
) -> int | tuple[Any, ...] | None:
    """Generate a cache key from args and kwargs. Supports unhashable types."""
    if strict:
        try:
            key = (args, tuple(sorted(kwargs.items())))
            hash(key)
            return key
        except TypeError:
            return None

    try:
        return _generic_hash((args, kwargs))
    except Exception:
        return None


@overload
def ttl_cache(
    ttl: float = 300, *, key: Callable[..., Any] | None = None
) -> Callable[[Callable[P, T]], CachedFunction[P, T]]: ...


@overload
def ttl_cache(
    ttl: float = 300, *, key: Callable[..., Any] | None = None
) -> Callable[[Callable[P, Awaitable[T]]], CachedAsyncFunction[P, T]]: ...


def ttl_cache(
    ttl: float = 300, *, key: Callable[..., Any] | None = None
) -> Callable[
    [Callable[P, T] | Callable[P, Awaitable[T]]],
    CachedFunction[P, T] | CachedAsyncFunction[P, T],
]:
    """Decorator that caches function results with a time-to-live.

    Args:
        ttl (float): Time in seconds before cache expires (default: 300)
        key (Callable | None): Optional function to generate cache key from args/kwargs.
            Should accept the same arguments as the decorated function and return a
            hashable key.

    Returns:
        Decorator: Decorated function with TTL-based caching

    Example:
        @ttl_cache(ttl=60)
        def expensive_function(x):
            return x ** 2

        @ttl_cache(ttl=120, key=lambda x, y: (x, y))
        async def async_expensive_function(x, y):
            await asyncio.sleep(1)
            return x + y
    """

    def decorator(
        func: Callable[P, T] | Callable[P, Awaitable[T]],
    ) -> CachedFunction[P, T] | CachedAsyncFunction[P, T]:
        """Inner decorator function."""
        cache = CachetoolsTTLCache(maxsize=_UNBOUNDED_MAXSIZE, ttl=ttl)
        stats_lock = threading.RLock()
        is_async = inspect.iscoroutinefunction(func)
        in_flight: dict[Any, asyncio.Future[T]] = {}

        def cache_clear() -> None:
            with stats_lock:
                cache.clear()

        hits = 0
        misses = 0

        def cache_info() -> CacheInfo:
            with stats_lock:
                hits_snapshot = hits
                misses_snapshot = misses
            return CacheInfo(
                hits=hits_snapshot,
                misses=misses_snapshot,
                maxsize=None,
                currsize=len(cache),
                ttl=ttl,
            )

        def get_cache_key(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
            """Generate cache key using custom function or default."""
            if key is not None:
                try:
                    return key(*args, **kwargs)
                except Exception:
                    return None
            return _make_key(args, kwargs, strict=False)

        if is_async:

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                nonlocal hits, misses
                cache_key = get_cache_key(args, kwargs)
                if cache_key is None:
                    result = func(*args, **kwargs)
                    return await cast(Awaitable[T], result)

                should_compute = False
                pending: asyncio.Future[T]
                with stats_lock:
                    cached = cache.get(cache_key, _MISSING)
                    if cached is not _MISSING:
                        hits += 1
                        return cast(T, cached)

                    pending = in_flight.get(cache_key)  # type: ignore[assignment]
                    if pending is None:
                        pending = asyncio.get_running_loop().create_future()
                        in_flight[cache_key] = pending
                        misses += 1
                        should_compute = True

                if not should_compute:
                    return await asyncio.shield(pending)

                try:
                    result = func(*args, **kwargs)
                    awaited_result = await cast(Awaitable[T], result)
                except Exception as exc:
                    with stats_lock:
                        active = in_flight.pop(cache_key, None)
                        if active is not None and not active.done():
                            active.set_exception(exc)
                    raise

                with stats_lock:
                    existing = cache.get(cache_key, _MISSING)
                    final_value: T
                    if existing is not _MISSING:
                        final_value = cast(T, existing)
                    else:
                        cache[cache_key] = awaited_result
                        final_value = awaited_result

                    active = in_flight.pop(cache_key, None)
                    if active is not None and not active.done():
                        active.set_result(final_value)

                return final_value

            async_wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
            async_wrapper.cache_info = cache_info  # type: ignore[attr-defined]
            return cast(CachedAsyncFunction[P, T], async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            nonlocal hits, misses
            cache_key = get_cache_key(args, kwargs)
            if cache_key is None:
                return cast(T, func(*args, **kwargs))

            with stats_lock:
                cached = cache.get(cache_key, _MISSING)
            if cached is not _MISSING:
                with stats_lock:
                    hits += 1
                return cast(T, cached)

            with stats_lock:
                misses += 1
            result = cast(T, func(*args, **kwargs))

            with stats_lock:
                existing = cache.get(cache_key, _MISSING)
                if existing is not _MISSING:
                    return cast(T, existing)
                cache[cache_key] = result
            return result

        sync_wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
        sync_wrapper.cache_info = cache_info  # type: ignore[attr-defined]
        return cast(CachedFunction[P, T], sync_wrapper)

    return decorator


@overload
def lru_cache(
    maxsize: int = 128, *, key: Callable[..., Any] | None = None
) -> Callable[[Callable[P, T]], CachedFunction[P, T]]: ...


@overload
def lru_cache(
    maxsize: int = 128, *, key: Callable[..., Any] | None = None
) -> Callable[[Callable[P, Awaitable[T]]], CachedAsyncFunction[P, T]]: ...


def lru_cache(
    maxsize: int = 128, *, key: Callable[..., Any] | None = None
) -> Callable[
    [Callable[P, T] | Callable[P, Awaitable[T]]],
    CachedFunction[P, T] | CachedAsyncFunction[P, T],
]:
    """LRU cache decorator for both sync and async functions.

    Args:
        maxsize (int): Maximum number of cached items.
        key (Callable | None): Optional function to generate cache key from args/kwargs.
            Should accept the same arguments as the decorated function and return a
            hashable key.

    Returns:
        Decorator: Decorated function with LRU caching.

    Example:
        @lru_cache(maxsize=256)
        async def fetch_data(url):
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    return await response.text()

        # Custom key that only considers the first argument
        @lru_cache(maxsize=100, key=lambda user_id, include_details=False: user_id)
        async def get_user(user_id, include_details=False):
            return await fetch_user_data(user_id, include_details)

        # Works with sync functions too
        @lru_cache(maxsize=50, key=lambda x, y, z=None: (x, y))
        def compute(x, y, z=None):
            return x + y
    """

    def decorator(
        func: Callable[P, T] | Callable[P, Awaitable[T]],
    ) -> CachedFunction[P, T] | CachedAsyncFunction[P, T]:
        """Inner decorator function."""
        cache = CachetoolsLRUCache(maxsize=maxsize)
        cache_lock = threading.RLock()
        is_async = inspect.iscoroutinefunction(func)
        in_flight: dict[Any, asyncio.Future[T]] = {}

        def get_cache_key(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
            """Generate cache key using custom function or default."""
            if key is not None:
                try:
                    return key(*args, **kwargs)
                except Exception:
                    return None
            else:
                return _make_key(args, kwargs, strict=False)

        if is_async:
            hits = 0
            misses = 0

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                nonlocal hits, misses

                cache_key = get_cache_key(args, kwargs)
                if cache_key is None:
                    result = func(*args, **kwargs)
                    return await cast(Awaitable[T], result)

                should_compute = False
                pending: asyncio.Future[T]
                with cache_lock:
                    cached = cache.get(cache_key, _MISSING)
                    if cached is not _MISSING:
                        hits += 1
                        return cast(T, cached)

                    pending = in_flight.get(cache_key)  # type: ignore[assignment]
                    if pending is None:
                        pending = asyncio.get_running_loop().create_future()
                        in_flight[cache_key] = pending
                        misses += 1
                        should_compute = True

                if not should_compute:
                    return await asyncio.shield(pending)

                try:
                    result = func(*args, **kwargs)
                    awaited_result = await cast(Awaitable[T], result)
                except Exception as exc:
                    with cache_lock:
                        active = in_flight.pop(cache_key, None)
                        if active is not None and not active.done():
                            active.set_exception(exc)
                    raise

                with cache_lock:
                    existing = cache.get(cache_key, _MISSING)
                    final_value: T
                    if existing is not _MISSING:
                        final_value = cast(T, existing)
                    else:
                        cache[cache_key] = awaited_result
                        final_value = awaited_result

                    active = in_flight.pop(cache_key, None)
                    if active is not None and not active.done():
                        active.set_result(final_value)

                return final_value

            def cache_clear() -> None:
                with cache_lock:
                    cache.clear()

            def cache_info() -> CacheInfo:
                with cache_lock:
                    hits_snapshot = hits
                    misses_snapshot = misses
                    currsize = len(cache)
                return CacheInfo(
                    hits=hits_snapshot,
                    misses=misses_snapshot,
                    maxsize=maxsize,
                    currsize=currsize,
                )

            async_wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
            async_wrapper.cache_info = cache_info  # type: ignore[attr-defined]
            return cast(CachedAsyncFunction[P, T], async_wrapper)
        else:
            hits = 0
            misses = 0

            @functools.wraps(func)
            def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                nonlocal hits, misses

                cache_key = get_cache_key(args, kwargs)
                if cache_key is None:
                    return cast(T, func(*args, **kwargs))

                with cache_lock:
                    cached = cache.get(cache_key, _MISSING)
                    if cached is not _MISSING:
                        hits += 1
                        return cast(T, cached)

                    misses += 1

                result = cast(T, func(*args, **kwargs))

                with cache_lock:
                    existing = cache.get(cache_key, _MISSING)
                    if existing is not _MISSING:
                        return cast(T, existing)
                    cache[cache_key] = result

                return result

            def cache_clear() -> None:
                with cache_lock:
                    cache.clear()

            def cache_info() -> CacheInfo:
                with cache_lock:
                    hits_snapshot = hits
                    misses_snapshot = misses
                    currsize = len(cache)
                return CacheInfo(
                    hits=hits_snapshot,
                    misses=misses_snapshot,
                    maxsize=maxsize,
                    currsize=currsize,
                )

            sync_wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
            sync_wrapper.cache_info = cache_info  # type: ignore[attr-defined]
            return cast(CachedFunction[P, T], sync_wrapper)

    return decorator


@overload
def file_cache(
    cache_dir: str | Path | None = None,
    ttl: float | None = None,
    *,
    key: Callable[..., Any] | None = None,
) -> Callable[[Callable[P, Awaitable[T]]], CachedAsyncFunction[P, T]]: ...


@overload
def file_cache(
    cache_dir: str | Path | None = None,
    ttl: float | None = None,
    *,
    key: Callable[..., Any] | None = None,
) -> Callable[[Callable[P, T]], CachedFunction[P, T]]: ...


def file_cache(
    cache_dir: str | Path | None = None,
    ttl: float | None = None,
    *,
    key: Callable[..., Any] | None = None,
) -> Callable[
    [Callable[P, T] | Callable[P, Awaitable[T]]],
    CachedFunction[P, T] | CachedAsyncFunction[P, T],
]:
    """Decorator that caches function results to disk using pickle.

    Args:
        cache_dir (str | Path): Directory to store cache files (default: ".cache")
        ttl (float | None): Optional time-to-live in seconds (None = no expiration)
        key (Callable | None): Optional function to generate cache key from args/kwargs.
            Should accept the same arguments as the decorated function and return a
            hashable key.

    Returns:
        Decorator: Decorated function with file-based caching

    Example:
        @file_cache(cache_dir="./my_cache", ttl=3600)
        def process_large_dataset(data_path):
            # Expensive computation
            return result

        @file_cache(ttl=600, key=lambda endpoint, **kwargs: endpoint)
        async def fetch_api_data(endpoint, **kwargs):
            # API call - cache only based on endpoint, ignore other params
            return data

        # Cache based on specific parameters only
        @file_cache(cache_dir="./cache", key=lambda x, y, z=None: (x, y))
        def compute(x, y, z=None):
            # z is not part of the cache key
            return x + y
    """
    if cache_dir is None:
        resolved_cache_dir = _resolve_default_cache_dir()
    else:
        resolved_cache_dir = Path(cache_dir)

    def decorator(
        func: Callable[P, T] | Callable[P, Awaitable[T]],
    ) -> CachedFunction[P, T] | CachedAsyncFunction[P, T]:
        """Inner decorator function."""
        func_name = str(getattr(func, "__name__", "unknown_function"))
        func_cache_dir = resolved_cache_dir / func_name
        func_cache_dir.mkdir(parents=True, exist_ok=True)
        disk_cache = DiskCache(str(func_cache_dir))
        _register_disk_cache(disk_cache)
        stats_lock = threading.RLock()
        is_async = inspect.iscoroutinefunction(func)
        in_flight: dict[Any, asyncio.Future[T]] = {}
        hits = 0
        misses = 0

        def close_cache() -> None:
            with _DISK_CACHES_LOCK:
                _DISK_CACHES.discard(disk_cache)
            _close_disk_cache(disk_cache)

        def get_store_key(cache_key: Any) -> str:
            """Generate a stable string key for disk cache lookup."""
            try:
                key_str = str(cache_key)
            except Exception:
                key_str = repr(cache_key)
            return hashlib.md5(key_str.encode()).hexdigest()

        def cache_clear() -> None:
            """Clear all cache entries for this function."""
            with stats_lock:
                disk_cache.clear()

        def cache_info() -> CacheInfo:
            with stats_lock:
                hits_snapshot = hits
                misses_snapshot = misses
            return CacheInfo(
                hits=hits_snapshot,
                misses=misses_snapshot,
                maxsize=None,
                currsize=len(disk_cache),
                ttl=ttl,
            )

        def get_cache_key(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
            """Generate cache key using custom function or default."""
            if key is not None:
                try:
                    return key(*args, **kwargs)
                except Exception:
                    return None
            return _make_key(args, kwargs, strict=False)

        if is_async:

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                nonlocal hits, misses
                cache_key = get_cache_key(args, kwargs)
                if cache_key is None:
                    result = func(*args, **kwargs)
                    return await cast(Awaitable[T], result)

                store_key = get_store_key(cache_key)
                should_compute = False
                pending: asyncio.Future[T]
                with stats_lock:
                    cached_value = disk_cache.get(
                        store_key, default=_MISSING, retry=True
                    )
                    if cached_value is not _MISSING:
                        hits += 1
                        return cast(T, cached_value)

                    pending = in_flight.get(store_key)  # type: ignore[assignment]
                    if pending is None:
                        pending = asyncio.get_running_loop().create_future()
                        in_flight[store_key] = pending
                        misses += 1
                        should_compute = True

                if not should_compute:
                    return await asyncio.shield(pending)

                try:
                    result = func(*args, **kwargs)
                    awaited_result = await cast(Awaitable[T], result)
                except Exception as exc:
                    with stats_lock:
                        active = in_flight.pop(store_key, None)
                        if active is not None and not active.done():
                            active.set_exception(exc)
                    raise

                with stats_lock:
                    current = disk_cache.get(store_key, default=_MISSING, retry=True)
                    final_value: T
                    if current is not _MISSING:
                        final_value = cast(T, current)
                    else:
                        final_value = awaited_result
                        with contextlib.suppress(Exception):
                            disk_cache.set(
                                store_key,
                                awaited_result,
                                expire=ttl,
                                retry=True,
                            )

                    active = in_flight.pop(store_key, None)
                    if active is not None and not active.done():
                        active.set_result(final_value)

                return final_value

            async_wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
            async_wrapper.cache_info = cache_info  # type: ignore[attr-defined]
            weakref.finalize(async_wrapper, close_cache)
            return cast(CachedAsyncFunction[P, T], async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            nonlocal hits, misses
            cache_key = get_cache_key(args, kwargs)
            if cache_key is None:
                return cast(T, func(*args, **kwargs))

            store_key = get_store_key(cache_key)
            with stats_lock:
                cached_value = disk_cache.get(store_key, default=_MISSING, retry=True)
            if cached_value is not _MISSING:
                with stats_lock:
                    hits += 1
                return cast(T, cached_value)

            with stats_lock:
                misses += 1
            result = cast(T, func(*args, **kwargs))
            with stats_lock, contextlib.suppress(Exception):
                disk_cache.set(store_key, result, expire=ttl, retry=True)
            return result

        sync_wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
        sync_wrapper.cache_info = cache_info  # type: ignore[attr-defined]
        weakref.finalize(sync_wrapper, close_cache)
        return cast(CachedFunction[P, T], sync_wrapper)

    return decorator


@overload
def cache[**P, T](
    func: Callable[P, T],
) -> CachedFunction[P, T]: ...


@overload
def cache[**P, T](
    func: Callable[P, Awaitable[T]],
) -> CachedAsyncFunction[P, T]: ...


def cache[**P, T](
    func: Callable[P, T] | Callable[P, Awaitable[T]],
) -> CachedFunction[P, T] | CachedAsyncFunction[P, T]:
    """Generic cache decorator that applies an LRU cache with cache size of 1.

    Args:
        func (Callable): Function to be cached.

    Returns:
        Decorator: Decorated function with LRU caching.

    Example:
        @cache
        def compute_square(x):
            return x * x

        @cache
        async def fetch_data(url):
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    return await response.text()
    """
    return cast(
        CachedFunction[P, T] | CachedAsyncFunction[P, T], lru_cache(maxsize=1)(func)
    )
