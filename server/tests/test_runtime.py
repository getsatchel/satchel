"""M8 runtime pieces: production guard, JSON logging, PORT, worker SIGTERM (no database)."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from satchel import runtime
from satchel.cli.main import app
from satchel.settings import Settings
from satchel.worker.main import run_forever

runner = CliRunner()

PROD_OK = dict(
    env="production", public_url="https://satchel.example.com", oidc_issuer="https://idp",
    oidc_client_id="id", oidc_client_secret="secret",
    admin_database_url="postgresql+psycopg://satchel_worker:pw@db/satchel",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in [k for k in os.environ if k.startswith("SATCHEL_") and k != "SATCHEL_CONFIG"]:
        monkeypatch.delenv(k)


# ---- production guard -----------------------------------------------------------------------


def test_development_has_no_problems() -> None:
    assert runtime.production_problems(Settings(dev_auth=True)) == []


def test_production_ok_when_configured() -> None:
    assert runtime.production_problems(Settings(**PROD_OK)) == []  # type: ignore[arg-type]


def test_production_lists_every_problem() -> None:
    problems = runtime.production_problems(Settings(env="production", dev_auth=True))
    text = "\n".join(problems)
    assert "SATCHEL_DEV_AUTH" in text
    assert "SATCHEL_OIDC_ISSUER" in text and "SATCHEL_OIDC_CLIENT_SECRET" in text
    assert "https://" in text and len(problems) == 4  # + SATCHEL_ADMIN_DATABASE_URL


def test_insecure_public_url_escape_hatch() -> None:
    st = Settings(**{**PROD_OK, "public_url": "http://internal:8080",
                     "allow_insecure_public_url": True})  # type: ignore[arg-type]
    assert runtime.production_problems(st) == []
    assert runtime.production_problems(Settings(**{**PROD_OK, "public_url": "http://x"}))  # type: ignore[arg-type]


def test_insecure_url_env_var_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SATCHEL_ALLOW_INSECURE_PUBLIC_URL", "1")
    assert Settings().allow_insecure_public_url is True


@pytest.mark.parametrize("cmd", [["serve", "api"], ["serve", "mcp"], ["worker", "--once"]])
def test_serve_and_worker_refuse_bad_production(
    cmd: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SATCHEL_ENV", "production")
    monkeypatch.setenv("SATCHEL_DEV_AUTH", "1")
    r = runner.invoke(app, cmd)
    assert r.exit_code == 1
    assert "refusing to start" in r.output and "SATCHEL_DEV_AUTH" in r.output


def test_port_env_is_used_and_flag_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    import satchel.cli.main as main

    seen: list[int] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: seen.append(k["port"]))
    monkeypatch.setattr(main, "_require_schema", lambda: None)
    monkeypatch.setenv("PORT", "9123")
    assert runner.invoke(app, ["serve", "api"]).exit_code == 0
    assert runner.invoke(app, ["serve", "api", "--port", "7001"]).exit_code == 0
    monkeypatch.delenv("PORT")
    assert runner.invoke(app, ["serve", "api"]).exit_code == 0
    assert seen == [9123, 7001, 8080]


# ---- structured logging ---------------------------------------------------------------------


def _record(msg: str = "hello %s", args: object = ("x",), level: int = logging.WARNING,
            name: str = "satchel.test") -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, msg, args, None)  # type: ignore[arg-type]


def test_json_formatter_fields() -> None:
    doc = json.loads(runtime.JsonFormatter().format(_record()))
    assert doc["severity"] == "WARNING" and doc["message"] == "hello x"
    assert doc["logger"] == "satchel.test" and doc["time"].endswith("+00:00")
    assert "logging.googleapis.com/trace" not in doc


def test_json_formatter_exception_in_message() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = logging.LogRecord("l", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    doc = json.loads(runtime.JsonFormatter().format(rec))
    assert doc["severity"] == "ERROR" and "ValueError: boom" in doc["message"]


def test_parse_trace_headers() -> None:
    tid = "105445aa7843bc8bf206b12000100000"
    assert runtime.parse_trace({"x-cloud-trace-context": f"{tid}/1;o=1"}) == tid
    assert runtime.parse_trace({"traceparent": f"00-{tid}-00f067aa0ba902b7-01"}) == tid
    assert runtime.parse_trace({"traceparent": "garbage"}) is None
    assert runtime.parse_trace({}) is None


def test_trace_middleware_feeds_formatter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    tid = "105445aa7843bc8bf206b12000100000"
    seen: list[dict[str, object]] = []
    api = FastAPI()
    api.add_middleware(runtime.TraceMiddleware)

    @api.get("/x")
    def x() -> dict[str, str]:
        seen.append(json.loads(runtime.JsonFormatter().format(_record())))
        return {}

    c = TestClient(api)
    c.get("/x", headers={"X-Cloud-Trace-Context": f"{tid}/7;o=1"})
    c.get("/x")
    assert seen[0]["logging.googleapis.com/trace"] == f"projects/proj/traces/{tid}"
    assert "logging.googleapis.com/trace" not in seen[1]  # reset between requests


def test_access_log_goes_through_json_formatter() -> None:
    rec = _record('%s - "%s %s HTTP/%s" %d', ("10.0.0.1:5555", "GET", "/healthz", "1.1", 200),
                  logging.INFO, "uvicorn.access")
    doc = json.loads(runtime.JsonFormatter().format(rec))
    assert doc["httpRequest"]["status"] == 200 and doc["httpRequest"]["requestUrl"] == "/healthz"
    assert doc["httpRequest"]["remoteIp"] == "10.0.0.1"


def test_uvicorn_log_config() -> None:
    assert runtime.uvicorn_log_config("text") is None
    import logging.config

    cfg = runtime.uvicorn_log_config("json")
    assert cfg is not None
    logging.config.dictConfig(cfg)
    h = logging.getLogger("uvicorn.access").handlers[0]
    assert isinstance(h.formatter, runtime.JsonFormatter)
    assert isinstance(logging.getLogger("uvicorn").handlers[0].formatter, runtime.JsonFormatter)


def test_configure_logging_json() -> None:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        runtime.configure_logging("json")
        assert isinstance(root.handlers[0].formatter, runtime.JsonFormatter)
        runtime.configure_logging("text")
        assert not isinstance(root.handlers[0].formatter, runtime.JsonFormatter)
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])


# ---- worker shutdown ------------------------------------------------------------------------


@pytest.fixture
def restore_signals() -> Iterator[None]:
    old = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGTERM, old[0])
    signal.signal(signal.SIGINT, old[1])


def test_worker_exits_on_sigterm_between_passes(restore_signals: None) -> None:
    passes: list[int] = []

    def one_pass() -> None:
        passes.append(1)
        os.kill(os.getpid(), signal.SIGTERM)  # arrives while the pass is running

    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers need the main thread")
    # A 24 h interval: only SIGTERM can end the loop. The in-flight pass completes first.
    run_forever(interval_min=1440, pass_fn=one_pass)
    assert passes == [1]


def test_production_requires_admin_database_url() -> None:
    cfg = {k: v for k, v in PROD_OK.items() if k != "admin_database_url"}
    problems = runtime.production_problems(Settings(**cfg))  # type: ignore[arg-type]
    assert any("SATCHEL_ADMIN_DATABASE_URL" in p for p in problems)
