"""The GitHub Action script (``action/satchel-context.sh``) against a live server: preview a pull
request, run the gate, write the comment and outputs, then publish on merge."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import Engine

from satchel.api.app import create_app
from satchel.mcp.auth import create_token
from satchel.settings import Settings
from tests.test_evals import EMB, make_env
from tests.test_sync_upload import NEW_GUIDE, pr_folder

SCRIPT = Path(__file__).resolve().parents[2] / "action" / "satchel-context.sh"
pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "curl", "jq", "tar")),
    reason="the action needs bash, curl, jq and tar")


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from tests.helpers import build_jaffle_db

    return build_jaffle_db(tmp_path_factory.mktemp("wh") / "jaffle.duckdb")


@pytest.fixture
def server(app_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
           ) -> Iterator[str]:
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SATCHEL_CHAT_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SATCHEL_RESULT_DIR", str(tmp_path / "results"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(app_engine, EMB, settings=Settings()),
                                        host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(5)


def run(url: str, token: str, folder: Path, tmp: Path, mode: str) -> tuple[int, dict[str, str],
                                                                             str]:
    out, summary = tmp / f"{mode}.out", tmp / f"{mode}.md"
    env = {"PATH": os.environ["PATH"], "SATCHEL_URL": url, "SATCHEL_TOKEN": token,
           "CONTEXT_PATH": str(folder), "MODE": mode, "COMMENT": "false", "PR_NUMBER": "7",
           "PR_TITLE": "Add loyalty guide", "GITHUB_REF": "refs/pull/7/merge",
           "GITHUB_SHA": "abc1234def", "GITHUB_OUTPUT": str(out),
           "GITHUB_STEP_SUMMARY": str(summary), "WAIT_MINUTES": "2"}
    p = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True,
                       timeout=300)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines()
                   ) if out.exists() else {}
    return p.returncode, outputs, (summary.read_text() if summary.exists() else "") + p.stdout


def test_action_previews_gates_and_publishes(
    app_engine: Engine, tmp_path: Path, warehouse: Path, server: str
) -> None:
    e = make_env(app_engine, tmp_path, warehouse, gate=True, chat=None, prefix="act")
    curator, _ = create_token(e.slug, "ci@act.test", "ci", role="curator", engine=app_engine)
    admin, _ = create_token(e.slug, "ci-admin@act.test", "ci", role="admin", engine=app_engine)
    folder = pr_folder(tmp_path)

    code, outputs, text = run(server, curator, folder, tmp_path, "preview")
    assert outputs.get("preview-id"), text
    assert outputs["gate-status"] in ("pass", "fail"), text
    assert code == (0 if outputs["gate-status"] == "pass" else 1), text
    assert "Satchel context preview" in text and f"`{NEW_GUIDE}`" in text
    assert "| `retrieval` v1 | completed | retrieval |" in text, text
    assert "Published when this pull request merges" in text

    code, outputs, text = run(server, curator, folder, tmp_path, "publish")
    assert code == 1 and "HTTP 403" in text  # publishing needs an admin token
    code, outputs, text = run(server, admin, folder, tmp_path, "publish")
    assert code == 0 and outputs["release-seq"], text
    assert "published: 1 created" in text, text

    code, outputs, text = run(server, curator, folder, tmp_path, "preview")
    assert code == 0 and outputs["preview-id"] == "" and "changes no context" in text, text
