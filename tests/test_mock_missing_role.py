"""Issue #215: every mock interview route that names a role answers a missing role with the plain
404, never a 500 and never a dialog for a role that does not exist. Start without a mode (or with
an unknown one) used to render the practice panel for the missing role and fail with
"'route' is undefined". No network; nothing is sent or stored by any of these routes."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, reset_engine

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

MISSING = 987654


@pytest.fixture
def client():
    from kestrel.web.app import build_app

    async def fresh():
        await reset_engine()
        for suffix in ("", "-wal", "-shm"):
            paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
        migrate.run_migrations()
        forget_engine()

    asyncio.run(fresh())
    with TestClient(build_app(worker=False), raise_server_exceptions=False) as c:
        yield c
    asyncio.run(reset_engine())


@pytest.mark.parametrize("role_id", [0, MISSING])
@pytest.mark.parametrize("mode", ["", "not-a-mode", "behavioral"])
def test_start_for_a_missing_role_is_404(client, role_id, mode):
    r = client.post(f"/mock/{role_id}/start", data={"mode": mode} if mode else {})
    assert r.status_code == 404, (role_id, mode, r.status_code, r.text[:120])


@pytest.mark.parametrize(("method", "path", "data"), [
    ("get", "/mock/{id}/online", None),
    ("post", "/mock/{id}/route", {"confirmed": "1"}),
    ("get", "/mock/{id}/route", None),
])
def test_the_sibling_routes_answer_a_missing_role_with_404(client, method, path, data):
    url = path.format(id=MISSING)
    r = client.post(url, data=data) if method == "post" else client.get(url)
    assert r.status_code == 404, (method, url, r.status_code, r.text[:120])
    assert "pp-online" not in r.text and "pp-route" not in r.text
