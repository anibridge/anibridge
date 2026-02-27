"""Tests for async task scheduling helpers."""

import asyncio

import pytest

from anibridge.app.utils.async_tasks import schedule_task


@pytest.mark.asyncio
async def test_schedule_task_runs_successfully() -> None:
    """Scheduling a task should execute the coroutine."""
    flag = {"done": False}
    done = asyncio.Event()

    async def _work():
        await asyncio.sleep(0)
        flag["done"] = True
        done.set()

    schedule_task(_work(), name="ok")
    await done.wait()

    assert flag["done"] is True


@pytest.mark.asyncio
async def test_schedule_task_logs_failures() -> None:
    """Failing tasks should be handled without raising to the caller."""

    async def _boom():
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    schedule_task(_boom(), name="boom")
    await asyncio.sleep(0)
