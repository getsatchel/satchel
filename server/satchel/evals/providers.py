"""Pick the model that drives the reference agent and judges (design §10).

Precedence: an injected provider (tests), then ``--model`` / the request's override, then the
suite's pinned ``model``, then the workspace's model (the deployment's ``SATCHEL_CHAT_*`` or the
default LLM connection, see :mod:`satchel.llm.connections`). A pinned or overridden provider
uses the deployment provider when it matches, else the workspace's connection of that provider,
else ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``. ``None`` means no LLM is available:
agent-driven cases are then ``skipped`` (``context_used`` suites still run).
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Literal, cast

from sqlalchemy import Engine

from satchel.evals.runner import EvalProvider
from satchel.evals.suite import ModelSpec
from satchel.llm.connections import (
    KEYLESS,
    ChatTarget,
    build_chat,
    deployment_target,
    gemini_env_key,
    resolve_target,
)
from satchel.settings import Settings, get_settings
from satchel.store.db import admin_session

log = logging.getLogger("satchel.evals")


def parse_model_override(raw: str, default_provider: str) -> ModelSpec:
    """``claude-haiku`` | ``anthropic:claude-haiku`` | ``openai_compat:gpt-x`` |
    ``gemini:gemini-2.5-pro``."""
    prov, sep, name = raw.partition(":")
    if sep and prov in ("anthropic", "openai_compat", "gemini", "bedrock", "vertex"):
        return ModelSpec(provider=prov, name=name)
    return ModelSpec(provider=default_provider, name=raw)


def _env_target(provider: str) -> ChatTarget | None:
    if provider in KEYLESS:  # the server's cloud identity; region and project from its env
        return ChatTarget(name="environment", provider=provider,  # type: ignore[arg-type]
                          base_url=None, model=None, api_key=None, managed_by="deployment")
    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
    elif provider == "gemini":
        key = gemini_env_key()
    else:
        key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    return ChatTarget(name="environment", provider=provider,  # type: ignore[arg-type]
                      base_url=None, model=None, api_key=key, managed_by="deployment")


@dataclass(frozen=True)
class Resolution:
    """Which model a run would use, and why: shown before a run, used by the run."""

    target: ChatTarget | None
    model: str | None  # the model name asked for; None = the target's default
    source: Literal["override", "suite", "workspace"]
    pinned: ModelSpec | None  # the suite's own model, if it pins one
    problem: str | None = None


def resolve(
    suite_model: ModelSpec | None,
    override: str | None = None,
    settings: Settings | None = None,
    *,
    workspace_id: uuid.UUID | None = None,
    engine: Engine | None = None,
) -> Resolution:
    st = settings or get_settings()

    def lookup(provider: str | None) -> ChatTarget | None:
        if workspace_id is None:
            dep = deployment_target(st)
            return dep if dep and (provider is None or dep.provider == provider) else None
        try:
            with admin_session(engine=engine) as s:
                return resolve_target(s, workspace_id, st, provider)
        except Exception as e:  # noqa: BLE001 - an unusable connection means "no LLM"
            log.error("LLM connection for workspace %s is unusable: %s", workspace_id, e)
            return None

    spec = suite_model
    source: Literal["override", "suite", "workspace"] = "suite" if spec else "workspace"
    if override and override.strip():
        default = lookup(None)
        spec = parse_model_override(
            override.strip(),
            spec.provider if spec else (default.provider if default else "anthropic"))
        source = "override"
    if spec is None:
        target = lookup(None)
        if target is None:
            return Resolution(None, None, source, suite_model,
                              "no model is set up: add one in Settings > Models")
        return Resolution(target, None, source, suite_model)
    target = lookup(spec.provider) or _env_target(spec.provider)
    if target is None:
        log.warning("eval model is %s but no %s connection or API key is configured",
                    spec.provider, spec.provider)
        kind = _LABEL.get(spec.provider, spec.provider)
        who = "the suite pins" if source == "suite" else "the run asks for"
        return Resolution(None, spec.name, source, suite_model,
                          f"{who} {spec.provider}:{spec.name}, but there is no {kind} model in "
                          "Settings > Models: pick another model, or add one")
    return Resolution(target, spec.name, source, suite_model)


_LABEL = {"anthropic": "Anthropic", "gemini": "Gemini", "openai_compat": "OpenAI-compatible",
          "bedrock": "Amazon Bedrock", "vertex": "Vertex AI"}


def resolve_provider(
    suite_model: ModelSpec | None,
    override: str | None = None,
    settings: Settings | None = None,
    *,
    workspace_id: uuid.UUID | None = None,
    engine: Engine | None = None,
) -> EvalProvider | None:
    st = settings or get_settings()
    res = resolve(suite_model, override, st, workspace_id=workspace_id, engine=engine)
    if res.target is None:
        return None
    chat = build_chat(res.target, st, res.model)
    return cast(EvalProvider, chat) if chat is not None else None
