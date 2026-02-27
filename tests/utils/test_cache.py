"""Tests for caching utilities."""

import asyncio
from collections.abc import Callable
from typing import cast

import pytest

from anibridge.app.utils.cache import _generic_hash, file_cache, lru_cache, ttl_cache


def test_generic_hash_order_insensitive_for_dicts():
    """Test that _generic_hash produces the same hash for dicts in different orders."""
    data_one = {"b": [1, 2], "a": {"x": 1}}
    data_two = {"a": {"x": 1}, "b": [1, 2]}

    assert _generic_hash(data_one) == _generic_hash(data_two)


def test_generic_hash_handles_cycles():
    """Test that _generic_hash can handle cyclic data structures."""
    cyclic = []
    cyclic.append(cyclic)

    result = _generic_hash(cyclic)

    assert isinstance(result, int)


def test_lru_cache_caches_unhashable_arguments():
    """Test that lru_cache caches results for unhashable arguments."""
    call_count = 0

    @lru_cache(maxsize=8)
    def compute(values):
        nonlocal call_count
        call_count += 1
        return sum(values)

    assert compute([1, 2, 3]) == 6
    assert compute([1, 2, 3]) == 6
    assert call_count == 1


def test_ttl_cache_caches_unhashable_arguments():
    """Test that ttl_cache caches results for unhashable arguments."""
    call_count = 0

    @ttl_cache(ttl=60)
    def compute(values):
        nonlocal call_count
        call_count += 1
        return sum(values)

    assert compute([4, 5]) == 9
    assert compute([4, 5]) == 9
    assert call_count == 1


def test_file_cache_sync_caches(tmp_path) -> None:
    """Synchronous file_cache should store and reuse results."""
    calls = {"count": 0}

    @file_cache(cache_dir=tmp_path)  # type: ignore[misc]
    def add(x: int, y: int) -> int:
        calls["count"] += 1
        return x + y

    assert add(1, 2) == 3
    assert add(1, 2) == 3
    assert calls["count"] == 1

    add.cache_clear()


def test_file_cache_sync_unpickleable_result(tmp_path) -> None:
    """Unpickleable results should not be cached."""
    calls = {"count": 0}

    @file_cache(cache_dir=tmp_path)
    def make_callable(x: int):
        calls["count"] += 1
        return lambda: x

    result = cast(Callable[[], int], make_callable(1))
    assert result() == 1
    result = cast(Callable[[], int], make_callable(1))
    assert result() == 1
    assert calls["count"] == 2


def test_file_cache_custom_key_error(tmp_path) -> None:
    """Key errors should skip caching."""
    calls = {"count": 0}

    def _bad_key(*_args, **_kwargs):
        raise ValueError("boom")

    @file_cache(cache_dir=tmp_path, key=_bad_key)  # type: ignore[misc]
    def calc(x: int) -> int:
        calls["count"] += 1
        return x

    assert calc(5) == 5
    assert calc(5) == 5
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_file_cache_async_caches(tmp_path) -> None:
    """Async file_cache should store and reuse results."""
    calls = {"count": 0}

    @file_cache(cache_dir=tmp_path)
    async def fetch(x: int) -> int:
        calls["count"] += 1
        await asyncio.sleep(0)
        return x

    assert await fetch(1) == 1
    assert await fetch(1) == 1
    assert calls["count"] == 1

    fetch.cache_clear()


@pytest.mark.asyncio
async def test_lru_cache_async_single_flight() -> None:
    """Concurrent async calls with the same key should compute once."""
    calls = {"count": 0}

    @lru_cache(maxsize=16)
    async def compute(x: int) -> int:
        calls["count"] += 1
        await asyncio.sleep(0.01)
        return x * 2

    results = await asyncio.gather(
        compute(7),
        compute(7),
        compute(7),
    )
    assert results == [14, 14, 14]
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_ttl_cache_async_single_flight() -> None:
    """Concurrent async calls with the same key should compute once."""
    calls = {"count": 0}

    @ttl_cache(ttl=60)
    async def compute(x: int) -> int:
        calls["count"] += 1
        await asyncio.sleep(0.01)
        return x + 1

    results = await asyncio.gather(
        compute(9),
        compute(9),
        compute(9),
    )
    assert results == [10, 10, 10]
    assert calls["count"] == 1
