"""Source files read from a git repository (Cube models, a SQLMesh project, a committed dbt
manifest).

A source with ``git_url`` reads its file inputs as paths inside that repository, at ``git_ref``
(a branch, tag or commit; default the repository's default branch). Each sync makes a shallow,
sparse, blob-filtered clone of just those paths into a temporary folder and deletes it after.

The clone runs the ``git`` binary with a locked-down configuration, because the URL and the
repository content come from a workspace admin, not from the operator:

- https only (``GIT_ALLOW_PROTOCOL``); no credentials in the URL; the host is resolved once,
  rejected if it is link-local or a cloud metadata address, and pinned for the clone
  (``http.curloptResolve``) so a second DNS answer cannot redirect it; redirects are refused.
- No system or user git config, no prompts, no hooks, no submodules, ``core.symlinks=false``
  (a symlink in the repository is checked out as a small text file, so it cannot point at server
  files), and paths must stay inside the checkout.
- The token goes in an ``Authorization`` header through ``GIT_CONFIG_*`` environment variables,
  never on the command line or in the URL, and is scrubbed from error messages.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

GIT_KEYS = ("git_url", "git_ref", "git_username", "git_token")
DEFAULT_USERNAME = "x-access-token"  # GitHub; GitLab and Azure DevOps accept any username
TIMEOUT_S = 120.0
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,199}$")
_USER = re.compile(r"^[A-Za-z0-9._@+-]{1,100}$")
# Tests only: accept file:// repositories (never set from configuration).
_ALLOW_FILE_FOR_TESTS = False
_BLOCKED_HOSTS = {"metadata.google.internal", "metadata", "instance-data", "metadata.azure.com"}


class GitSourceError(ValueError):
    pass


@dataclass(frozen=True)
class GitSpec:
    url: str
    ref: str | None = None
    username: str | None = None
    token: str | None = None


@dataclass(frozen=True)
class Checkout:
    root: Path
    commit: str


def spec_from_config(cfg: dict[str, object]) -> GitSpec | None:
    url = cfg.get("git_url")
    if not url:
        return None
    return GitSpec(url=str(url), ref=str(cfg["git_ref"]) if cfg.get("git_ref") else None,
                   username=str(cfg["git_username"]) if cfg.get("git_username") else None,
                   token=str(cfg["git_token"]) if cfg.get("git_token") else None)


def validate_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if _ALLOW_FILE_FOR_TESTS and parts.scheme == "file":
        return url
    if parts.scheme != "https" or not parts.hostname:
        raise GitSourceError("git_url must be an https:// repository URL")
    if parts.username or parts.password:
        raise GitSourceError("git_url must not embed credentials; use the token field")
    if parts.query or parts.fragment:
        raise GitSourceError("git_url must not have a query or fragment")
    host = parts.hostname.lower()
    if host in _BLOCKED_HOSTS:
        raise GitSourceError(f"git_url host {host} is a cloud metadata endpoint")
    _check_ip(host, host)
    return url


def validate_ref(ref: str | None) -> str | None:
    if ref is None or not ref.strip():
        return None
    ref = ref.strip()
    if not _REF.match(ref) or ".." in ref or ref.endswith((".lock", "/")) or "//" in ref:
        raise GitSourceError("git_ref: a branch, tag or commit (letters, digits, . _ / + -)")
    return ref


def validate_username(u: str | None) -> str | None:
    if u is None or not u.strip():
        return None
    if not _USER.match(u.strip()):
        raise GitSourceError("git_username: letters, digits and . _ @ + - only")
    return u.strip()


def validate_path(p: str) -> str:
    """A path inside the repository: relative, no ``..``; ``.`` or empty is the root."""
    raw = p.strip().replace("\\", "/")
    if raw in ("", ".", "./"):
        return "."
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise GitSourceError(f"{p}: give a path inside the repository, not an absolute path")
    parts = [x for x in PurePosixPath(raw).parts if x not in ("", ".")]
    if any(x == ".." for x in parts) or any(x == ".git" for x in parts):
        raise GitSourceError(f"{p}: the path must stay inside the repository")
    return "/".join(parts) or "."


def _check_ip(host: str, ip_text: str) -> None:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return
    if (ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_loopback
            or str(ip) == "fd00:ec2::254"):
        raise GitSourceError(f"git_url host {host} resolves to a link-local, loopback or "
                             "metadata address")


def _pin(url: str) -> str | None:
    """Resolve the host once, check every address, and return a ``host:port:ip`` pin."""
    parts = urlsplit(url)
    if parts.scheme == "file":
        return None
    host = parts.hostname or ""
    port = parts.port or 443
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise GitSourceError(f"cannot resolve {host}: {e.strerror or e}") from None
    addrs = sorted({str(i[4][0]) for i in infos})
    if not addrs:
        raise GitSourceError(f"cannot resolve {host}")
    for a in addrs:
        _check_ip(host, a)
    first = addrs[0]
    return f"{host}:{port}:{'[' + first + ']' if ':' in first else first}"


def _env(spec: GitSpec, home: Path, pin: str | None) -> dict[str, str]:
    cfg: list[tuple[str, str]] = [
        ("core.symlinks", "false"),
        ("core.hooksPath", "/dev/null"),
        ("core.fsmonitor", "false"),
        ("protocol.allow", "never"),
        ("protocol.https.allow", "always"),
        *([("protocol.file.allow", "always")] if _ALLOW_FILE_FOR_TESTS else []),
        ("http.followRedirects", "false"),
        ("submodule.recurse", "false"),
        ("advice.detachedHead", "false"),
        ("init.defaultBranch", "main"),
    ]
    if pin:
        cfg.append(("http.curloptResolve", pin))
    if spec.token:
        cred = base64.b64encode(
            f"{spec.username or DEFAULT_USERNAME}:{spec.token}".encode()).decode()
        cfg.append(("http.extraHeader", f"Authorization: Basic {cred}"))
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https:file" if _ALLOW_FILE_FOR_TESTS else "https",
        "GIT_ASKPASS": "/bin/false",
        "GCM_INTERACTIVE": "never",
        "GIT_CONFIG_COUNT": str(len(cfg)),
        "LC_ALL": "C",
    }
    for i, (k, v) in enumerate(cfg):
        env[f"GIT_CONFIG_KEY_{i}"] = k
        env[f"GIT_CONFIG_VALUE_{i}"] = v
    for k in ("SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "https_proxy", "NO_PROXY",
              "no_proxy", "GIT_SSL_CAINFO"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def _git_bin() -> str:
    exe = shutil.which("git")
    if exe is None:
        raise GitSourceError("git is not installed on this server (it is in the satchel image)")
    return exe


def _run(args: list[str], env: dict[str, str], cwd: Path, spec: GitSpec,
         timeout: float) -> str:
    try:
        r = subprocess.run([_git_bin(), *args], cwd=cwd, env=env, capture_output=True,
                           text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise GitSourceError(f"git timed out after {int(timeout)} s") from None
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        text = " ".join(line.strip() for line in msg if line.strip())[-600:]
        if spec.token:
            text = text.replace(spec.token, "***")
        if "Authentication failed" in text or "could not read Username" in text or " 401" in text \
                or " 403" in text:
            text = f"authentication failed (check the token and username). {text}"
        raise GitSourceError(f"git {args[0]} failed: {text}")
    return r.stdout


def _checked(spec: GitSpec) -> GitSpec:
    return GitSpec(url=validate_url(spec.url), ref=validate_ref(spec.ref),
                   username=validate_username(spec.username), token=spec.token)


def resolve(spec: GitSpec, *, timeout: float = 30.0, pin: bool = True) -> str:
    """The commit the ref points at (``git ls-remote``): a cheap connectivity and auth test."""
    spec = _checked(spec)
    with tempfile.TemporaryDirectory(prefix="satchel-git-") as tmp:
        env = _env(spec, Path(tmp), _pin(spec.url) if pin else None)
        want = spec.ref or "HEAD"
        if re.fullmatch(r"[0-9a-f]{7,40}", want):
            return want  # a commit: ls-remote cannot confirm it without fetching
        out = _run(["ls-remote", "--", spec.url, want, f"refs/heads/{want}", f"refs/tags/{want}"],
                   env, Path(tmp), spec, timeout)
        for line in out.splitlines():
            sha = line.split("\t", 1)[0]
            if sha:
                return sha
        raise GitSourceError(f"no branch or tag named {want!r} in {spec.url}")


@contextmanager
def checkout(spec: GitSpec, paths: list[str], *, timeout: float = TIMEOUT_S,
             pin: bool = True) -> Iterator[Checkout]:
    """Shallow, sparse clone of ``paths`` at ``spec.ref``, deleted on exit."""
    spec = _checked(spec)
    wanted = sorted({validate_path(p) for p in paths})
    tmp = Path(tempfile.mkdtemp(prefix="satchel-git-"))
    try:
        home = tmp / "home"
        repo = tmp / "repo"
        home.mkdir()
        repo.mkdir()
        env = _env(spec, home, _pin(spec.url) if pin else None)
        _run(["init", "-q"], env, repo, spec, timeout)
        _run(["remote", "add", "origin", spec.url], env, repo, spec, timeout)
        _run(["fetch", "-q", "--depth", "1", "--filter=blob:none", "--no-tags",
              "--no-recurse-submodules", "origin", spec.ref or "HEAD"], env, repo, spec, timeout)
        if "." not in wanted:
            _run(["sparse-checkout", "set", "--no-cone", "--",
                  *[f"/{p}" for p in wanted], *[f"/{p}/**" for p in wanted]],
                 env, repo, spec, timeout)
        _run(["checkout", "-q", "--detach", "FETCH_HEAD"], env, repo, spec, timeout)
        commit = _run(["rev-parse", "HEAD"], env, repo, spec, timeout).strip()
        yield Checkout(root=repo, commit=commit)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def inside(root: Path, rel: str) -> Path:
    """``root/rel``, refusing anything that resolves outside ``root``."""
    p = (root / validate_path(rel)).resolve()
    base = root.resolve()
    if p != base and not p.is_relative_to(base):
        raise GitSourceError(f"{rel}: the path must stay inside the repository")
    return p
