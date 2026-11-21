"""Keyless model providers: Amazon Bedrock and Vertex AI, with the server's cloud identity."""

from __future__ import annotations

import io
import json
from typing import Any

import httpx
import pytest
from botocore.exceptions import ClientError

import satchel.llm.connections as lc
from satchel.llm.chat import (
    BedrockChat,
    ChatError,
    ToolSpec,
    VertexAnthropicChat,
    VertexOpenAIChat,
)
from satchel.settings import Settings
from tests.test_llm_connections import Env, env, st  # noqa: F401 - fixture

TOOLS = [ToolSpec(name="start_analysis", description="d", input_schema={"type": "object"})]
REPLY = {"stop_reason": "tool_use", "usage": {"input_tokens": 7, "output_tokens": 3},
         "content": [{"type": "tool_use", "id": "t1", "name": "start_analysis",
                      "input": {"question": "q"}}]}


class FakeBedrock:
    def __init__(self, reply: Any = REPLY, error: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply, self.error = reply, error

    def invoke_model(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        if self.error:
            raise ClientError({"Error": {"Code": self.error, "Message": "nope"}}, "InvokeModel")
        return {"body": io.BytesIO(json.dumps(self.reply).encode())}


class FakeCreds:
    valid, token = True, "ya29.token"


def test_bedrock_sends_the_messages_api_through_invoke_model() -> None:
    fake = FakeBedrock()
    model = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
    chat = BedrockChat(model, "eu-west-1", client=fake)
    turn = chat.complete_with_tools("sys", [{"role": "user", "content": "q"}], TOOLS)
    assert turn.tool_uses[0].name == "start_analysis" and chat.model_id == f"bedrock:{model}"
    call = fake.calls[0]
    body = json.loads(call["body"])
    assert call["modelId"] == model and "model" not in body
    assert body["anthropic_version"] == "bedrock-2023-05-31" and body["system"] == "sys"
    assert chat.usage.input_tokens == 7
    with pytest.raises(ChatError, match="AccessDeniedException"):
        BedrockChat(model, "eu-west-1", client=FakeBedrock(error="AccessDeniedException")
                    ).complete_with_tools("s", [{"role": "user", "content": "q"}], TOOLS)


def test_vertex_claude_and_gemini_use_the_google_token() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path.endswith(":rawPredict"):
            return httpx.Response(200, json=REPLY)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    claude = VertexAnthropicChat("claude-sonnet-4-5@20250929", "acme", "europe-west1",
                                 client=client, credentials=FakeCreds())
    claude.complete_with_tools("sys", [{"role": "user", "content": "q"}], TOOLS)
    req = seen[-1]
    assert str(req.url) == ("https://europe-west1-aiplatform.googleapis.com/v1/projects/acme/"
                            "locations/europe-west1/publishers/anthropic/models/"
                            "claude-sonnet-4-5@20250929:rawPredict")
    assert req.headers["authorization"] == "Bearer ya29.token"
    body = json.loads(req.content)
    assert body["anthropic_version"] == "vertex-2023-10-16" and "model" not in body

    gemini = VertexOpenAIChat("gemini-2.5-flash", "acme", "global", client=client,
                              credentials=FakeCreds())
    assert gemini.complete_json("s", [{"role": "user", "content": "q"}], {"type": "object"}) == {
        "ok": True}
    req = seen[-1]
    assert str(req.url) == ("https://aiplatform.googleapis.com/v1/projects/acme/locations/"
                            "global/endpoints/openapi/chat/completions")
    assert json.loads(req.content)["model"] == "google/gemini-2.5-flash"
    assert req.headers["authorization"] == "Bearer ya29.token"
    assert gemini.model_id == "vertex:gemini-2.5-flash"


def test_build_chat_picks_the_client_and_the_location(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("AWS_REGION", "AWS_DEFAULT_REGION", "GOOGLE_CLOUD_PROJECT", "CLOUD_ML_REGION",
              "CLOUDSDK_CORE_PROJECT"):
        monkeypatch.delenv(k, raising=False)
    s = Settings()

    def target(provider: str, model: str | None, **kw: Any) -> lc.ChatTarget:
        return lc.ChatTarget("t", provider, None, model, None, **kw)  # type: ignore[arg-type]

    monkeypatch.setenv("AWS_REGION", "eu-west-2")
    b = lc.build_chat(target("bedrock", "anthropic.claude-x"), s)
    assert isinstance(b, BedrockChat) and b.region == "eu-west-2"
    b = lc.build_chat(target("bedrock", "anthropic.claude-x", region="us-east-1"), s)
    assert isinstance(b, BedrockChat) and b.region == "us-east-1"
    assert lc.build_chat(target("bedrock", None), s) is None  # no model
    assert lc.build_chat(target("vertex", "claude-x"), s) is None  # no project anywhere
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "acme")
    v = lc.build_chat(target("vertex", "claude-x"), s)
    assert isinstance(v, VertexAnthropicChat) and "/locations/global/" in v._url
    v = lc.build_chat(target("vertex", "gemini-2.5-pro", region="us-central1"), s)
    assert isinstance(v, VertexOpenAIChat) and "us-central1-aiplatform" in v._url
    dep = lc.deployment_target(Settings(chat_provider="bedrock", chat_model="m",
                                        chat_region="ap-southeast-2"))
    assert dep is not None and dep.region == "ap-southeast-2" and dep.api_key is None


def test_keyless_connections_over_the_api(env: Env,  # noqa: F811
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("CLOUDSDK_CORE_PROJECT", raising=False)
    client = env.make(st())

    def create(**body: Any) -> httpx.Response:
        return client.post("/v1/llm-connections", headers=env.h(), json=body)

    r = create(name="b", provider="bedrock", default_model="m", api_key="AKIA-nope-000000")
    assert r.status_code == 422 and "takes no API key" in r.json()["detail"]
    r = create(name="b", provider="bedrock")
    assert r.status_code == 422 and "inference profile" in r.json()["detail"]
    r = create(name="b", provider="bedrock", default_model="eu.anthropic.claude-x",
               region="Eu West")
    assert r.status_code == 422 and "region" in r.json()["detail"]
    r = create(name="b", provider="bedrock", default_model="eu.anthropic.claude-x",
               region="eu-west-1", project="ignored")
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["region"] == "eu-west-1" and out["project"] is None and not out["has_api_key"]
    r = create(name="v", provider="vertex", default_model="gemini-2.5-flash")
    assert r.status_code == 422 and "GCP project" in r.json()["detail"]
    r = create(name="v", provider="vertex", default_model="gemini-2.5-flash", project="acme-1",
               region="europe-west4")
    assert r.status_code == 201 and r.json()["project"] == "acme-1", r.text
    r = client.patch(f"/v1/llm-connections/{r.json()['id']}", headers=env.h(),
                     json={"region": "us-central1"})
    assert r.json()["region"] == "us-central1" and r.json()["project"] == "acme-1"
    # The test endpoint builds the client from the saved row (no network: it fails to auth).
    listed = client.get("/v1/llm-connections", headers=env.h()).json()["connections"]
    assert {c["provider"] for c in listed} == {"bedrock", "vertex"}
