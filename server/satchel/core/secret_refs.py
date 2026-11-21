"""Secret references: a secret Satchel reads when it needs it, instead of storing it.

A reference names where the secret lives:

- ``aws-sm:<name or ARN>[#key]``: AWS Secrets Manager, read with the server's AWS identity
  (instance role, IRSA, EKS Pod Identity). A full ARN picks the region; a name uses the default.
- ``gcp-sm:projects/<p>/secrets/<s>[/versions/<v>][#key]``: GCP Secret Manager, read with the
  server's Google identity (Workload Identity, the VM's service account). Version defaults to
  ``latest``.
- ``env:NAME``: an environment variable. Only from the CLI and the context repo: over the API it
  would let an admin read any variable the server has, its own keys included.

``#key`` picks one field of a secret stored as a JSON object (``#password``, ``#private_key``).

References that the API accepts must match ``SATCHEL_SECRET_REF_ALLOW``: comma-separated globs
such as ``aws-sm:arn:aws:secretsmanager:eu-west-1:123456789012:secret:satchel/*``. Unset, the
API accepts none, so an admin of the UI can't read secrets the deployment didn't mean to share.

Values are cached in memory for ``SATCHEL_SECRET_CACHE_S`` (default 300 s) so a rotated secret
is picked up without a restart; :func:`resolve` with ``fresh=True`` (Test) skips the cache.
"""

from __future__ import annotations

import fnmatch
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from satchel.settings import Settings

SCHEMES = ("aws-sm", "gcp-sm", "env")
API_SCHEMES = ("aws-sm", "gcp-sm")


class SecretRefError(ValueError):
    """A reference that is malformed, not allowed, or that could not be read."""


@dataclass(frozen=True)
class Ref:
    scheme: str
    target: str
    key: str | None

    def __str__(self) -> str:
        return f"{self.scheme}:{self.target}" + (f"#{self.key}" if self.key else "")


def is_ref(value: object) -> bool:
    """Whether ``value`` looks like a reference (any scheme)."""
    return isinstance(value, str) and value.split(":", 1)[0] in SCHEMES and ":" in value


def parse(ref: str) -> Ref:
    scheme, sep, rest = ref.strip().partition(":")
    if not sep or scheme not in SCHEMES or not rest:
        raise SecretRefError(
            "a secret reference looks like aws-sm:<name or ARN>, "
            "gcp-sm:projects/<p>/secrets/<s>, or env:NAME (optionally #key)")
    target, _, raw_key = rest.partition("#")
    target, key = target.strip(), raw_key.strip() or None
    if not target or any(c in target for c in " \t\r\n"):
        raise SecretRefError(f"{scheme}: the secret name is empty or has spaces")
    if scheme == "gcp-sm":
        parts = target.split("/")
        if len(parts) not in (4, 6) or parts[0] != "projects" or parts[2] != "secrets" or (
                len(parts) == 6 and parts[4] != "versions"):
            raise SecretRefError(
                "gcp-sm: use projects/<project>/secrets/<secret>[/versions/<version>]")
    return Ref(scheme, target, key)


def allowed_patterns(settings: Settings) -> list[str]:
    return [p.strip() for p in (settings.secret_ref_allow or "").split(",") if p.strip()]


def check_api_ref(ref: str, settings: Settings) -> Ref:
    """A reference given over the API: a secret manager, and inside the allow list."""
    r = parse(ref)
    if r.scheme not in API_SCHEMES:
        raise SecretRefError(f"{r.scheme}: references can't be set here; use aws-sm: or gcp-sm:")
    pats = allowed_patterns(settings)
    if not pats:
        raise SecretRefError("secret manager references are off: the deployment sets none in "
                             "SATCHEL_SECRET_REF_ALLOW")
    plain = f"{r.scheme}:{r.target}"
    if not any(fnmatch.fnmatchcase(plain, p) for p in pats):
        raise SecretRefError(f"{plain} is outside SATCHEL_SECRET_REF_ALLOW")
    return r


def is_api_ref(value: object) -> bool:
    """A secret field's value that is a secret manager reference, not the secret itself."""
    return isinstance(value, str) and value.strip().startswith(("aws-sm:", "gcp-sm:"))


def refuse_storing(settings: Settings, what: str) -> None:
    """Raise when the deployment stores no secrets (``SATCHEL_SECRETS=references``)."""
    if settings.secrets == "references":
        raise SecretRefError(
            f"this deployment stores no secrets (SATCHEL_SECRETS=references): give {what} as a "
            "secret manager reference (aws-sm:... or gcp-sm:...)")


# --- reading ------------------------------------------------------------------------------------

Fetcher = Callable[[Ref], str]


def _aws(r: Ref) -> str:
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        raise SecretRefError("aws-sm: needs the aws extra (boto3)") from None
    region = r.target.split(":")[3] if r.target.startswith("arn:") else None
    try:
        out = boto3.client("secretsmanager", region_name=region).get_secret_value(
            SecretId=r.target)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "error")
        raise SecretRefError(f"aws-sm: {code} reading {r.target}") from None
    except BotoCoreError as e:
        raise SecretRefError(f"aws-sm: {type(e).__name__} reading {r.target}") from None
    if "SecretString" not in out:
        raise SecretRefError(f"aws-sm: {r.target} is binary; store it as a string")
    return str(out["SecretString"])


def _gcp(r: Ref) -> str:
    import base64

    import httpx

    try:
        import google.auth
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import Request
    except ImportError:
        raise SecretRefError("gcp-sm: needs google-auth") from None
    name = r.target if "/versions/" in r.target else f"{r.target}/versions/latest"
    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(Request())  # type: ignore[no-untyped-call]
    except GoogleAuthError as e:
        raise SecretRefError(f"gcp-sm: no Google credentials ({type(e).__name__})") from None
    resp = httpx.get(f"https://secretmanager.googleapis.com/v1/{name}:access",
                     headers={"authorization": f"Bearer {creds.token}"}, timeout=10)
    if resp.status_code != 200:
        status = (resp.json().get("error", {}).get("status") if resp.headers.get(
            "content-type", "").startswith("application/json") else None) or resp.status_code
        raise SecretRefError(f"gcp-sm: {status} reading {r.target}")
    return base64.b64decode(resp.json()["payload"]["data"]).decode()


def _env(r: Ref) -> str:
    value = os.environ.get(r.target)
    if value is None:
        raise SecretRefError(f"env:{r.target}: the environment variable is not set")
    return value


FETCHERS: dict[str, Fetcher] = {"aws-sm": _aws, "gcp-sm": _gcp, "env": _env}

_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def _pick(r: Ref, raw: str) -> str:
    if r.key is None:
        return raw
    try:
        obj = json.loads(raw)
    except ValueError:
        raise SecretRefError(f"{r}: #{r.key} needs the secret to be a JSON object") from None
    if not isinstance(obj, dict) or r.key not in obj:
        raise SecretRefError(f"{r}: the secret has no field {r.key!r}")
    value = obj[r.key]
    return value if isinstance(value, str) else json.dumps(value)


def resolve(ref: str, settings: Settings, *, fresh: bool = False) -> str:
    """The secret's value. Errors name the reference and the reason, never the value."""
    r = parse(ref)
    if r.scheme == "env":
        return _pick(r, _env(r))
    plain = f"{r.scheme}:{r.target}"
    now = time.monotonic()
    with _lock:
        hit = _cache.get(plain)
    if hit and not fresh and hit[0] > now:
        raw = hit[1]
    else:
        raw = FETCHERS[r.scheme](r)
        with _lock:
            _cache[plain] = (now + settings.secret_cache_s, raw)
    return _pick(r, raw)


def clear_cache() -> None:
    with _lock:
        _cache.clear()
