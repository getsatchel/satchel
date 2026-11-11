"""`satchel workspace create`: first workspace + admin for OIDC login; idempotent."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, func, select
from typer.testing import CliRunner

from satchel.cli.main import app
from satchel.store.db import admin_engine_for, admin_session, get_admin_engine, get_engine
from satchel.store.models import AppUser, Workspace
from tests.helpers import unique_slug

runner = CliRunner()


@pytest.fixture
def cli_db(app_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("SATCHEL_DATABASE_URL", app_engine.url.render_as_string(hide_password=False))
    for cached in (get_engine, get_admin_engine):
        cached.cache_clear()
    yield app_engine  # type: ignore[misc]
    for cached in (get_engine, get_admin_engine):
        cached.cache_clear()


def test_create_is_idempotent(cli_db: Engine) -> None:
    slug = unique_slug("acme")
    args = ["workspace", "create", slug, "--name", "Acme Inc", "--admin-email", "a@acme.test"]
    r = runner.invoke(app, args)
    assert r.exit_code == 0, r.output
    assert f"created workspace '{slug}'" in r.output
    assert "created admin user a@acme.test" in r.output
    again = runner.invoke(app, args)
    assert again.exit_code == 0 and "already exist" in again.output
    with admin_session(engine=cli_db) as s:
        ws = s.scalars(select(Workspace).where(Workspace.slug == slug)).one()
        assert ws.name == "Acme Inc"
        n = s.scalar(select(func.count()).select_from(AppUser).where(AppUser.workspace_id == ws.id))
        assert n == 1
        assert s.scalars(select(AppUser.role).where(AppUser.workspace_id == ws.id)).one() == "admin"


def test_existing_non_admin_is_promoted_and_name_kept(cli_db: Engine) -> None:
    slug = unique_slug("promo")
    with admin_session(engine=cli_db) as s:
        ws = Workspace(slug=slug, name="Original")
        s.add(ws)
        s.flush()
        s.add(AppUser(workspace_id=ws.id, email="x@y.test", display_name="x", role="analyst"))
    r = runner.invoke(
        app, ["workspace", "create", slug, "--name", "Other", "--admin-email", "x@y.test"]
    )
    assert r.exit_code == 0 and "promoted x@y.test to admin" in r.output
    with admin_session(engine=cli_db) as s:
        assert s.scalars(select(Workspace.name).where(Workspace.slug == slug)).one() == "Original"


def test_admin_email_is_required(cli_db: Engine) -> None:
    r = runner.invoke(app, ["workspace", "create", "x"])
    assert r.exit_code != 0


def test_not_blocked_by_production_guard(cli_db: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SATCHEL_ENV", "production")  # admin commands are exempt from the guard
    # ...but production never derives the worker URL, so the job must pass it explicitly.
    monkeypatch.setenv("SATCHEL_ADMIN_DATABASE_URL",
                       admin_engine_for(cli_db).url.render_as_string(hide_password=False))
    slug = unique_slug("prod")
    r = runner.invoke(app, ["workspace", "create", slug, "--admin-email", "p@p.test"])
    assert r.exit_code == 0, r.output
