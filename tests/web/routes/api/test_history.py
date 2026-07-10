"""Tests for history API routes."""

import pytest

from anibridge.app.web.routes.api import history as history_api_module
from anibridge.app.web.services.history_service import HistoryGroup, HistoryPage


class _FakeHistoryService:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, int]] = []
        self.deleted_operations: list[tuple[str, int]] = []
        self.retried: list[tuple[str, int]] = []
        self.undone: list[tuple[str, int]] = []
        self.page_requests: list[dict] = []

    async def get_page(self, **kwargs) -> HistoryPage:
        self.page_requests.append(kwargs)
        if kwargs["outcome"] == "invalid":
            raise ValueError("'invalid' is not a valid SyncOutcome")
        return HistoryPage(
            groups=[
                HistoryGroup(
                    id=1,
                    run_id=1,
                    profile_name=kwargs["profile"],
                    outcome="synced",
                    timestamp="2026-01-01T00:00:00+00:00",
                )
            ],
            limit=kwargs["limit"],
            has_more=False,
            latest_group_id=1,
            stats={"synced": 1} if kwargs["include_stats"] else None,
        )

    async def delete_group(self, profile: str, group_id: int) -> None:
        self.deleted.append((profile, group_id))

    async def delete_operation(self, profile: str, operation_id: int) -> None:
        self.deleted_operations.append((profile, operation_id))

    async def retry_group(self, profile: str, group_id: int) -> None:
        self.retried.append((profile, group_id))

    async def undo_operation(self, profile: str, operation_id: int) -> None:
        self.undone.append((profile, operation_id))


@pytest.fixture
def history_service(monkeypatch: pytest.MonkeyPatch) -> _FakeHistoryService:
    service = _FakeHistoryService()
    monkeypatch.setattr(history_api_module, "get_history_service", lambda: service)
    return service


@pytest.fixture
def history_client(api_client_for):
    return api_client_for(history_api_module, "/api/history")


def test_history_page_route_delegates_filters_to_service(
    history_client,
    history_service: _FakeHistoryService,
) -> None:
    page = history_client.get(
        "/api/history/default",
        params={
            "limit": 10,
            "before_id": 5,
            "include_stats": "false",
            "outcome": "synced",
            "source_namespace": "plex",
            "target_namespace": "anilist",
            "resource_kind": "record",
        },
    )

    assert page.status_code == 200
    assert page.json()["groups"][0]["profile_name"] == "default"
    assert history_service.page_requests[0]["include_stats"] is False
    assert history_service.page_requests[0]["resource_kind"] == "record"


def test_history_page_route_rejects_conflicting_cursors(history_client) -> None:
    response = history_client.get(
        "/api/history/default",
        params={"before_id": 5, "after_id": 2},
    )

    assert response.status_code == 400


def test_history_page_route_rejects_invalid_limit(history_client) -> None:
    response = history_client.get("/api/history/default", params={"limit": 0})

    assert response.status_code == 400


def test_history_page_route_rejects_invalid_filters(history_client) -> None:
    response = history_client.get(
        "/api/history/default",
        params={"outcome": "invalid"},
    )

    assert response.status_code == 400
    assert "SyncOutcome" in response.json()["detail"]


@pytest.mark.parametrize(
    (
        "method",
        "path",
        "expected_calls_attr",
        "expected_call",
        "response_key",
        "response_value",
    ),
    [
        pytest.param(
            "delete",
            "/api/history/default/groups/12",
            "deleted",
            ("default", 12),
            None,
            {"ok": True},
            id="delete-group",
        ),
        pytest.param(
            "delete",
            "/api/history/default/operations/12",
            "deleted_operations",
            ("default", 12),
            None,
            {"ok": True},
            id="delete-operation",
        ),
        pytest.param(
            "post",
            "/api/history/default/groups/12/retry",
            "retried",
            ("default", 12),
            None,
            {"ok": True},
            id="retry",
        ),
        pytest.param(
            "post",
            "/api/history/default/operations/12/undo",
            "undone",
            ("default", 12),
            None,
            {"ok": True},
            id="undo",
        ),
    ],
)
def test_history_mutation_routes_delegate_to_service(
    history_client,
    history_service: _FakeHistoryService,
    method: str,
    path: str,
    expected_calls_attr: str,
    expected_call: tuple[str, int],
    response_key: str | None,
    response_value: int | dict[str, bool],
) -> None:
    response = getattr(history_client, method)(path)

    assert response.status_code == 200
    if response_key is None:
        assert response.json() == response_value
    else:
        assert response.json()[response_key]["id"] == response_value
    assert getattr(history_service, expected_calls_attr) == [expected_call]
