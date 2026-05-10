"""Tests for mappings API routes."""

import asyncio
from collections.abc import Callable, Coroutine
from contextlib import contextmanager
from typing import cast

import pytest
from litestar.app import Litestar
from litestar.router import Router
from litestar.testing.client.sync_client import TestClient

from anibridge.app.config.database import db
from anibridge.app.exceptions import (
    AniListFilterError,
    AniListSearchError,
    BooruQuerySyntaxError,
    MappingIdMismatchError,
)
from anibridge.app.models.db.animap import AnimapEntry, AnimapMapping, AnimapProvenance
from anibridge.app.web.routes.api import mappings as mappings_api_module


def _build_app() -> Litestar:
    return Litestar(
        route_handlers=[
            Router(path="/api", route_handlers=[mappings_api_module.router])
        ]
    )


@contextmanager
def _fresh_tables():
    with db() as ctx:
        ctx.session.query(AnimapProvenance).delete()
        ctx.session.query(AnimapMapping).delete()
        ctx.session.query(AnimapEntry).delete()
        ctx.session.commit()
    try:
        yield
    finally:
        with db() as ctx:
            ctx.session.query(AnimapProvenance).delete()
            ctx.session.query(AnimapMapping).delete()
            ctx.session.query(AnimapEntry).delete()
            ctx.session.commit()


def test_query_capabilities_include_distinct_provider_values() -> None:
    """Provider query fields expose provider suggestions from the database."""
    with _fresh_tables():
        with db() as ctx:
            ctx.session.add_all(
                [
                    AnimapEntry(provider="tmdb", entry_id="10", entry_scope=None),
                    AnimapEntry(provider="anilist", entry_id="1", entry_scope=None),
                    AnimapEntry(provider="tmdb", entry_id="11", entry_scope="s1"),
                ]
            )
            ctx.session.commit()

        client = TestClient(_build_app())
        response = client.get("/api/mappings/query-capabilities")

    assert response.status_code == 200
    fields = response.json()["fields"]
    source_provider = next(
        field for field in fields if field["key"] == "source.provider"
    )
    target_provider = next(
        field for field in fields if field["key"] == "target.provider"
    )

    assert source_provider["values"] == ["anilist", "tmdb"]
    assert target_provider["values"] == ["anilist", "tmdb"]


def test_list_mappings_and_override_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    update_mapping = cast(
        Callable[
            [str, mappings_api_module.MappingOverridePayload],
            Coroutine[object, object, mappings_api_module.MappingDetailModel],
        ],
        mappings_api_module.update_mapping.fn,
    )

    class _MappingsService:
        async def list_mappings(self, **kwargs):
            assert kwargs["page"] == 2
            assert kwargs["per_page"] == 1
            assert kwargs["q"] == "anilist"
            assert kwargs["custom_only"] is True
            assert kwargs["with_anilist"] is True
            return (
                [
                    {
                        "descriptor": "anilist:1",
                        "provider": "anilist",
                        "entry_id": "1",
                        "scope": None,
                        "edges": [],
                        "custom": False,
                        "sources": [],
                        "anilist": None,
                    }
                ],
                3,
            )

    class _OverridesService:
        async def get_mapping_config(self):
            return {
                "mappings_url": "https://example.com/mappings.json",
                "includes": ["./community.yaml"],
                "path": "/tmp/mappings.json",
                "format": "json",
            }

        async def get_mapping_detail(self, descriptor: str):
            return {
                "descriptor": descriptor,
                "provider": "anilist",
                "entry_id": "1",
                "scope": None,
                "layers": {},
                "targets": [],
            }

        async def save_override(self, **kwargs):
            return {
                "descriptor": kwargs["descriptor"],
                "provider": "anilist",
                "entry_id": "1",
                "scope": None,
                "layers": {},
                "targets": [],
            }

        async def save_mapping_config(self, **kwargs):
            return {
                "mappings_url": "https://example.com/mappings.json",
                "includes": kwargs["includes"],
                "path": "/tmp/mappings.json",
                "format": "json",
            }

    monkeypatch.setattr(
        mappings_api_module,
        "get_mappings_service",
        lambda: _MappingsService(),
    )
    monkeypatch.setattr(
        mappings_api_module,
        "get_mapping_overrides_service",
        lambda: _OverridesService(),
    )

    client = TestClient(_build_app())
    listed = client.get(
        "/api/mappings",
        params={
            "page": 2,
            "per_page": 1,
            "q": "anilist",
            "custom_only": "true",
            "with_anilist": "true",
        },
    )
    assert listed.status_code == 200
    assert listed.json()["pages"] == 3

    config = client.get("/api/mappings/config")
    assert config.status_code == 200
    assert config.json()["mappings_url"] == "https://example.com/mappings.json"
    assert config.json()["includes"] == ["./community.yaml"]

    detail = client.get("/api/mappings/anilist:1")
    assert detail.status_code == 200

    created = client.post(
        "/api/mappings",
        json={"descriptor": "anilist:1", "targets": []},
    )
    assert created.status_code == 200

    updated_config = client.put(
        "/api/mappings/config",
        json={"includes": ["./community.yaml", "https://example.com/base.json"]},
    )
    assert updated_config.status_code == 200
    assert updated_config.json()["includes"] == [
        "./community.yaml",
        "https://example.com/base.json",
    ]

    updated = asyncio.run(
        update_mapping(
            "anilist:1",
            mappings_api_module.MappingOverridePayload(
                descriptor="anilist:1",
                targets=[],
            ),
        )
    )
    assert updated.descriptor == "anilist:1"

    with pytest.raises(MappingIdMismatchError):
        asyncio.run(
            update_mapping(
                "anilist:1",
                mappings_api_module.MappingOverridePayload(
                    descriptor="anilist:2",
                    targets=[],
                ),
            )
        )


@pytest.mark.parametrize(
    ("exc", "expected_status"),
    [
        (BooruQuerySyntaxError("bad"), 400),
        (AniListFilterError("bad filter"), 400),
        (AniListSearchError("lookup failed"), 502),
    ],
)
def test_list_mappings_translates_service_errors(
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    expected_status: int,
) -> None:
    class _MappingsService:
        async def list_mappings(self, **kwargs):
            raise exc

    monkeypatch.setattr(
        mappings_api_module,
        "get_mappings_service",
        lambda: _MappingsService(),
    )
    response = TestClient(_build_app()).get("/api/mappings")

    assert response.status_code == expected_status
