"""Tests for liveness probe routes."""

from litestar.app import Litestar
from litestar.testing.client.sync_client import TestClient

from anibridge.app.web.routes.z import livez as livez_module


def test_livez_route_serves_livez() -> None:
    app = Litestar(route_handlers=[livez_module.router])
    client = TestClient(app)

    livez_response = client.get("/livez")

    assert livez_response.status_code == 200
    assert livez_response.json() == {"status": "ok"}

    healthz_response = client.get("/healthz")
    assert healthz_response.status_code == 200
    assert healthz_response.json() == {"status": "ok"}
