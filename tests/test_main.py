"""Tests for top-level application process helpers."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import main


class _SchedulerStub:
    """Scheduler double used to verify top-level runtime cleanup."""

    def __init__(self, _config) -> None:
        self.initialized = False
        self.started = False
        self.stopped = False

    async def initialize(self) -> None:
        self.initialized = True

    async def start(self) -> None:
        self.started = True

    async def wait_for_completion(self) -> None:
        await asyncio.Event().wait()

    async def stop(self) -> None:
        self.stopped = True

    def request_shutdown(self) -> None:
        return None


@pytest.mark.asyncio
async def test_shutdown_web_server_requests_graceful_exit() -> None:
    """Graceful shutdown should mark the server for exit and await completion."""
    server = SimpleNamespace(should_exit=False, force_exit=False)

    async def complete_soon() -> None:
        await asyncio.sleep(0)

    server_task = asyncio.create_task(complete_soon())

    await main._shutdown_web_server(
        server,  # ty:ignore[invalid-argument-type]
        server_task,
        timeout_duration=0.1,
        force_timeout_duration=0.1,
    )

    assert server.should_exit is True
    assert server.force_exit is False
    assert server_task.done() is True


@pytest.mark.asyncio
async def test_shutdown_web_server_forces_exit_when_graceful_stop_hangs() -> None:
    """Hung web shutdowns should escalate to force_exit and task cancellation."""
    server = SimpleNamespace(should_exit=False, force_exit=False)
    blocker = asyncio.Event()

    async def never_finishes() -> None:
        await blocker.wait()

    server_task = asyncio.create_task(never_finishes())

    await main._shutdown_web_server(
        server,  # ty:ignore[invalid-argument-type]
        server_task,
        timeout_duration=0.01,
        force_timeout_duration=0.01,
    )

    assert server.should_exit is True
    assert server.force_exit is True
    assert server_task.cancelled() is True


@pytest.mark.asyncio
async def test_run_returns_failure_and_stops_scheduler_when_web_server_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An early web-server failure should be fatal and clean up the scheduler."""
    config = SimpleNamespace(
        log_level="INFO",
        data_path=tmp_path,
        threads=1,
        web=SimpleNamespace(
            enabled=True,
            host="127.0.0.1",
            port=8000,
            has_auth=False,
        ),
    )
    scheduler = _SchedulerStub(config)
    app = SimpleNamespace(state=SimpleNamespace())

    class _FailingServer:
        def __init__(self, _config) -> None:
            self.should_exit = False
            self.force_exit = False

        async def _serve(self) -> None:
            raise OSError("bind failed")

    monkeypatch.setattr(main, "initialize_runtime", lambda: config)
    monkeypatch.setattr(main, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(main, "create_app", lambda: app)
    monkeypatch.setattr(main, "SchedulerClient", lambda _config: scheduler)
    monkeypatch.setattr(main.uvicorn, "Server", _FailingServer)
    monkeypatch.setattr(main, "_setup_signal_handlers_for_scheduler", lambda _: None)

    result = await main.run()

    assert result == 1
    assert scheduler.initialized is True
    assert scheduler.started is True
    assert scheduler.stopped is True
    main.get_app_state().set_scheduler(None)


@pytest.mark.asyncio
async def test_wait_for_runtime_prioritizes_simultaneous_server_failure() -> None:
    """A server error should remain fatal when scheduler completion races it."""

    class _CompletedScheduler:
        async def wait_for_completion(self) -> None:
            return None

    async def _fail_server() -> None:
        raise RuntimeError("server failed")

    server_task = asyncio.create_task(_fail_server())
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="Web server stopped unexpectedly"):
        await main._wait_for_runtime(
            _CompletedScheduler(),  # ty:ignore[invalid-argument-type]
            server_task,
        )


def test_warns_for_unauthenticated_wildcard_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wildcard web exposure without auth should emit a startup warning."""
    warnings: list[str] = []
    config = SimpleNamespace(web=SimpleNamespace(host="0.0.0.0", has_auth=False))
    monkeypatch.setattr(
        main.log,
        "warning",
        lambda message, *_args: warnings.append(message),
    )

    main._warn_for_unauthenticated_public_bind(config)

    assert warnings
