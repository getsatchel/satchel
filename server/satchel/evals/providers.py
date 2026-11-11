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
from typing import cast

from sqlalchemy import Engine

from satchel.evals.runner import EvalProvider
from satchel.evals.suite import ModelSpec
from satchel.llm.connections import ChatTarget, build_chat, deployment_target, resolve_target
from satchel.settings import Settings, get_settings
from satchel.store.db import admin_session

log = logging.getLogger("satchel.evals")


def parse_model_override(raw: str, default_provider: str) -> ModelSpec:
    """``claude-haiku`` | ``anthropic:claude-haiku`` | ``openai_compat:gpt-x``."""
    prov, sep, name = raw.partition(":")
    if sep and prov in ("anthropic", "openai_compat"):
        return ModelSpec(provider=prov, name=name)
    return ModelSpec(provider=default_provider, name=raw)


def _env_target(provider: str) -> ChatTarget | None:
    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
    else:
        key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    return ChatTarget(name="environment", provider=provider,  # type: ignore[arg-type]
                      base_url=None, model=None, api_key=key, managed_by="deployment")


def resolve_provider(
    suite_model: ModelSpec | None,
    override: str | None = None,
    settings: Settings | None = None,
    *,
    workspace_id: uuid.UUID | None = None,
    engine: Engine | None = None,
) -> EvalProvider | None:
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
    if override:
        default = lookup(None)
        spec = parse_model_override(
            override, spec.provider if spec else (default.provider if default else "anthropic"))
    if spec is None:
        target = lookup(None)
        model = None
    else:
        target = lookup(spec.provider) or _env_target(spec.provider)
        model = spec.name
    if target is None:
        if spec is not None:
            log.warning("eval model is %s but no %s connection or API key is configured",
                        spec.provider, spec.provider)
        return None
    chat = build_chat(target, st, model)
    return cast(EvalProvider, chat) if chat is not None else None
