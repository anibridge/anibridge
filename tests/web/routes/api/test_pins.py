"""Tests for pin management API routes."""

from datetime import UTC, datetime

import pytest

from anibridge.app.core.sync import RefPayload
from anibridge.app.models.schemas.provider import ProviderMediaMetadata
from anibridge.app.web.routes.api import pins as pins_api_module
from anibridge.app.web.services.pin_service import (
    PinEntry,
    PinFieldOption,
    PinSearchResult,
)


class _FakePinService:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []
        self.upsert_calls: list[tuple[str, str, list[str], bool]] = []

    def list_options(self) -> list[PinFieldOption]:
        return [PinFieldOption(value="status", label="Status")]

    async def list_pins(self, profile: str, *, with_media: bool) -> list[PinEntry]:
        return [
            PinEntry(
                profile_name=profile,
                target_namespace="anilist",
                target_ref=RefPayload("123"),
                fields=["status"],
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
        ]

    async def get_pin(
        self, profile: str, media_key: str, *, with_media: bool
    ) -> PinEntry | None:
        if media_key == "missing":
            return None
        return PinEntry(
            profile_name=profile,
            target_namespace="anilist",
            target_ref=RefPayload(media_key),
            fields=["status"],
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )

    async def upsert_pin(
        self, profile: str, media_key: str, fields: list[str], *, with_media: bool
    ) -> PinEntry:
        self.upsert_calls.append((profile, media_key, fields, with_media))
        if media_key == "bad":
            raise ValueError("Unsupported field")
        return PinEntry(
            profile_name=profile,
            target_namespace="anilist",
            target_ref=RefPayload(media_key),
            fields=fields,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )

    def delete_pin(self, profile: str, media_key: str) -> None:
        self.deleted.append((profile, media_key))

    async def search_pins(
        self,
        profile: str,
        q: str,
        *,
        limit: int,
    ) -> list[PinSearchResult]:
        return [
            PinSearchResult(
                media=ProviderMediaMetadata(
                    namespace="anilist",
                    key="456",
                    title=f"Result {q}",
                ),
                pin=None,
            )
        ][:limit]


@pytest.fixture
def fake_pin_service(monkeypatch: pytest.MonkeyPatch) -> _FakePinService:
    service = _FakePinService()
    monkeypatch.setattr(pins_api_module, "get_pin_service", lambda: service)
    return service


@pytest.fixture
def pins_client(api_client_for):
    return api_client_for(pins_api_module, "/api/pins")


def test_get_pin_fields_returns_service_options(pins_client, fake_pin_service):
    response = pins_client.get("/api/pins/fields")

    assert response.status_code == 200
    assert response.json() == {"options": [{"value": "status", "label": "Status"}]}


def test_list_pin_route_returns_service_items(pins_client, fake_pin_service) -> None:
    listed = pins_client.get("/api/pins/default", params={"with_media": "true"})
    assert listed.status_code == 200
    assert listed.json()["pins"][0]["target_ref"]["key"] == "123"


def test_search_pin_route_returns_target_results(pins_client, fake_pin_service) -> None:
    response = pins_client.get("/api/pins/default/search", params={"q": "bebop"})

    assert response.status_code == 200
    assert response.json()["results"] == [
        {
            "media": {
                "namespace": "anilist",
                "key": "456",
                "title": "Result bebop",
                "poster_url": None,
                "external_url": None,
                "labels": None,
            },
            "pin": None,
        }
    ]


@pytest.mark.parametrize(
    ("media_key", "with_media", "expected_status"),
    [
        pytest.param("123", True, 200, id="existing"),
        pytest.param("missing", False, 404, id="missing"),
    ],
)
def test_get_pin_route_handles_found_and_missing_entries(
    pins_client,
    fake_pin_service: _FakePinService,
    media_key: str,
    with_media: bool,
    expected_status: int,
) -> None:
    fetched = pins_client.get(
        f"/api/pins/default/{media_key}",
        params={"with_media": str(with_media).lower()},
    )

    assert fetched.status_code == expected_status
    if expected_status == 200:
        assert fetched.json()["target_ref"]["key"] == media_key
    else:
        assert fetched.json()["detail"] == "Pin not found"


def test_upsert_pin_route_normalizes_payload_and_handles_errors(
    pins_client,
    fake_pin_service,
) -> None:
    response = pins_client.put(
        "/api/pins/default/123",
        params={"with_media": "true"},
        json={"fields": [" progress ", "status", "STATUS"]},
    )

    assert response.status_code == 200
    assert fake_pin_service.upsert_calls[-1] == (
        "default",
        "123",
        ["status", "progress"],
        True,
    )

    invalid = pins_client.put("/api/pins/default/bad", json={"fields": ["status"]})
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "Unsupported field"


def test_delete_pin_route_returns_ok(pins_client, fake_pin_service) -> None:
    response = pins_client.delete("/api/pins/default/123")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert fake_pin_service.deleted == [("default", "123")]
