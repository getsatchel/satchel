"""Build a dbt project's ``manifest.json`` on the server (``dbt deps`` + ``dbt parse``), so a dbt
source can read a normal dbt repository instead of a committed ``target/``.

The dbt adapter stays a pure "files in, snapshot out" adapter: :func:`satchel.sources.service.
load_snapshot` checks out the project folder, calls :func:`build`, and hands the resulting
``manifest.json`` / ``semantic_manifest.json`` to the adapter as if they had been uploaded.
``catalog.json`` needs the warehouse (``dbt docs generate``) and is not built; crawling the
source's connection already provides column types.

The project's Jinja macros, ``dbt_project.yml`` and packages are chosen by a workspace admin, so
dbt runs as a separate process, never in the server's interpreter, with:

- a scrubbed environment built from scratch (no ``SATCHEL_*``, database URLs or cloud
  credentials; only proxy and CA settings pass through), ``HOME`` and ``TMPDIR`` in a temporary
  folder, telemetry off, and a generated ``profiles.yml`` whose only target is an in-memory
  DuckDB (``dbt parse`` never connects to it). ``env_var('X', 'default')`` sees no server
  variables, so it takes its default; ``env_var('X')`` without one fails the parse. Admin-set,
  non-secret ``--vars`` are passed instead.
- a wall-clock timeout that kills the whole process group, ``setrlimit`` limits (address space,
  CPU time, file size, no core dumps) set by a tiny launcher before ``exec`` (thread-safe, unlike
  ``preexec_fn``), output written to a file and only its tail read, and a size cap on the
  manifest read back.
- checks before dbt runs: the project's ``*-paths`` and local packages must stay inside the
  checkout (a project cannot point ``model-paths`` at server folders), and package URLs must be
  https and not link-local or metadata addresses.

``dbt deps`` needs the network (hub.getdbt.com, GitHub for git packages); a source can skip it
when its packages are vendored in ``dbt_packages/``. This is not a container sandbox: the
process runs as the server's user and can reach whatever the server's network can (on GKE / EKS
that includes the metadata endpoint). dbt's Jinja context has no file, process or network access,
and ``dbt parse`` does not execute Python models or hooks, so reaching it would take a dbt bug;
see docs/integrations/dbt.md for the deployment-level mitigations.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from satchel.sources import git

DBT_KEYS = ("dbt_project_dir", "dbt_skip_deps", "dbt_vars", "dbt_target_schema",
            "dbt_target_name")
DEFAULT_BIN = "/opt/dbt/bin/dbt"  # the satchel image (deploy/docker/Dockerfile, INSTALL_DBT)
DEFAULT_TARGET = "prod"
PROFILE = "satchel"
TIMEOUT_S = 300.0
MAX_MANIFEST_BYTES = 256 * 1024 * 1024
MAX_LOG_TAIL = 64 * 1024
# setrlimit values for the dbt process (Linux; best effort elsewhere).
LIMIT_AS = 4 * 1024**3
LIMIT_FSIZE = 1024**3
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_IDENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_$-]{0,127}$")
_SECRET_WORDS = ("token", "secret", "password", "api_key", "apikey", "credential", "private_key")
MAX_VARS = 50
MAX_VARS_BYTES = 8 * 1024
# Environment passed through to dbt (deps behind a proxy / custom CA); nothing else is.
_PASS_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy",
             "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "TZ")
_PATH_KEYS = ("model-paths", "seed-paths", "test-paths", "analysis-paths", "macro-paths",
              "snapshot-paths", "docs-paths", "asset-paths", "function-paths",
              "packages-install-path", "target-path", "log-path", "clean-targets")

# Sets the resource limits, then becomes dbt. Run with ``python -I -S`` so nothing from the
# server's environment or site-packages is imported.
_LAUNCHER = """
import os, resource, sys
def lim(name, value):
    r = getattr(resource, name, None)
    if r is None:
        return
    try:
        soft, hard = resource.getrlimit(r)
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        resource.setrlimit(r, (value, value))
    except (ValueError, OSError):
        pass
lim("RLIMIT_AS", int(sys.argv[1]))
lim("RLIMIT_CPU", int(sys.argv[2]))
lim("RLIMIT_FSIZE", int(sys.argv[3]))
lim("RLIMIT_CORE", 0)
os.execv(sys.argv[4], sys.argv[4:])
"""


class DbtBuildError(ValueError):
    """The project could not be built; the message is safe to show (no env, no secrets)."""


@dataclass(frozen=True)
class BuildOptions:
    project_dir: str
    target_schema: str
    target_name: str = DEFAULT_TARGET
    skip_deps: bool = False
    vars: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Built:
    files: dict[str, bytes]  # manifest (+ semantic_manifest), as file inputs of the dbt adapter
    dbt_version: str | None


@dataclass(frozen=True)
class DbtStatus:
    available: bool
    message: str
    version: str | None = None  # dbt Core's version when available


@dataclass(frozen=True)
class DbtInfo:
    core: str
    duckdb: str


# ----------------------------------------------------------------------------- configuration


def check_config(kind: str, cfg: dict[str, Any]) -> None:
    """Validate the ``dbt_*`` keys against the rest of the config (host keys + adapter keys).

    A dbt source from git reads either a committed ``manifest`` or builds one from
    ``dbt_project_dir``, never both."""
    used = [k for k in DBT_KEYS if cfg.get(k) not in (None, "", False, {})]
    if not used:
        return
    if kind != "dbt":
        raise DbtBuildError(f"{', '.join(used)}: only dbt sources build a project")
    if not cfg.get("dbt_project_dir"):
        raise DbtBuildError(f"{', '.join(used)} needs dbt_project_dir (the dbt project folder "
                            "in the repository)")
    if not cfg.get("git_url"):
        raise DbtBuildError("dbt_project_dir needs git_url: the project is checked out from "
                            "the repository")
    both = [k for k in ("manifest", "semantic_manifest") if cfg.get(k)]
    if both:
        raise DbtBuildError(
            f"set either a committed manifest path or dbt_project_dir, not both: dbt parse "
            f"writes manifest.json and semantic_manifest.json (remove {', '.join(both)})")
    try:
        git.validate_path(str(cfg["dbt_project_dir"]))
    except git.GitSourceError as e:
        raise DbtBuildError(f"dbt_project_dir: {e}") from None
    if not cfg.get("dbt_target_schema"):
        raise DbtBuildError(
            "dbt_target_schema is required: the schema your production target builds into "
            "(target.schema in profiles.yml, e.g. analytics). Satchel names tables by it.")
    for k in ("dbt_target_schema", "dbt_target_name"):
        v = cfg.get(k)
        if v is not None and not _IDENT.match(str(v)):
            raise DbtBuildError(f"{k}: letters, digits, _ $ - only (at most 128)")
    if not isinstance(cfg.get("dbt_skip_deps", False), bool):
        raise DbtBuildError("dbt_skip_deps: true or false")
    check_vars(cfg.get("dbt_vars"))


def check_vars(v: Any) -> dict[str, Any]:
    """``--vars`` for dbt: a small map of names to scalars. Stored and shown in plain text, so
    names that look like secrets are refused."""
    if v in (None, {}):
        return {}
    if not isinstance(v, dict):
        raise DbtBuildError("dbt_vars: a map of variable names to values")
    if len(v) > MAX_VARS:
        raise DbtBuildError(f"dbt_vars: at most {MAX_VARS} variables")
    for k, val in v.items():
        if not isinstance(k, str) or not _NAME.match(k):
            raise DbtBuildError(f"dbt_vars: {k!r} is not a valid variable name")
        if any(w in k.lower() for w in _SECRET_WORDS):
            raise DbtBuildError(f"dbt_vars: {k} looks like a secret; vars are stored and shown "
                                "in plain text, and dbt parse needs no credentials")
        if val is not None and not isinstance(val, str | int | float | bool):
            raise DbtBuildError(f"dbt_vars: {k} must be a string, number or boolean")
    if len(json.dumps(v)) > MAX_VARS_BYTES:
        raise DbtBuildError(f"dbt_vars: at most {MAX_VARS_BYTES >> 10} KB")
    return dict(v)


def options_from_config(kind: str, cfg: dict[str, Any]) -> BuildOptions | None:
    if kind != "dbt" or not cfg.get("dbt_project_dir") or not cfg.get("git_url"):
        return None
    return BuildOptions(
        project_dir=git.validate_path(str(cfg["dbt_project_dir"])),
        target_schema=str(cfg.get("dbt_target_schema") or ""),
        target_name=str(cfg.get("dbt_target_name") or DEFAULT_TARGET),
        skip_deps=bool(cfg.get("dbt_skip_deps")),
        vars=check_vars(cfg.get("dbt_vars")),
    )


# ----------------------------------------------------------------------------- the dbt binary


def find_dbt(configured: str | None = None) -> Path | None:
    """``SATCHEL_DBT_BIN``, else the image's ``/opt/dbt/bin/dbt``, else ``dbt`` on PATH."""
    for cand in ([configured] if configured else [DEFAULT_BIN, "dbt"]):
        exe = shutil.which(cand) if "/" not in cand else (cand if os.access(cand, os.X_OK)
                                                            and Path(cand).is_file() else None)
        if exe:
            return Path(exe)
    return None


MIN_CORE = (1, 8)  # adapters decoupled from core; the flags build() passes exist
_INFO: dict[tuple[str, int], DbtInfo | str] = {}  # (path, mtime) -> info or the refusal


def identify(exe: Path, *, timeout: float = 60.0) -> DbtInfo:
    """Run ``dbt --version`` once per binary (cached by path and mtime) and require dbt Core
    with the dbt-duckdb plugin. dbt Fusion, which accepts the same commands but is a different
    engine with its own manifest, is refused, as is anything that does not answer like dbt."""
    try:
        key = (str(exe), exe.stat().st_mtime_ns)
    except OSError as e:
        raise DbtBuildError(f"cannot read {exe}: {e.strerror or e}") from None
    if key not in _INFO:
        _INFO[key] = _identify(exe, timeout)
    got = _INFO[key]
    if isinstance(got, str):
        raise DbtBuildError(got)
    return got


def _identify(exe: Path, timeout: float) -> DbtInfo | str:
    with _workdir() as work:
        log = work / "logs" / "version.txt"
        try:
            run_dbt(exe, ["--version"], cwd=work, env=sandbox_env(work, exe), log=log,
                    timeout=timeout, scrub=[(str(work), "<build>")])
        except DbtBuildError as e:
            return f"{exe} is not a working dbt: {e}"
        out = re.sub(r"\x1b\[[0-9;]*m", "", log.read_text("utf-8", "replace"))
    fusion = re.search(r"dbt[- ]fusion\s+(\S+)", out, re.I)
    if fusion:
        return (f"{exe} is dbt Fusion {fusion.group(1)}; Satchel builds manifests with dbt Core. "
                "Install dbt Core and dbt-duckdb (deploy/docker/dbt-requirements.txt) and point "
                "SATCHEL_DBT_BIN at it, or use the satchel image")
    core = re.search(r"installed(?: version)?:\s*v?(\d+\.\d+[^\s]*)", out)
    if not core:
        return f"{exe} did not report a dbt Core version (dbt --version printed: {out[-300:]!r})"
    nums = tuple(int(x) for x in re.findall(r"\d+", core.group(1))[:2])
    if nums < MIN_CORE:
        return (f"{exe} is dbt Core {core.group(1)}; Satchel needs dbt Core "
                f"{MIN_CORE[0]}.{MIN_CORE[1]} or later")
    duck = re.search(r"^\s*-\s*duckdb:\s*(\S+)", out, re.M)
    if not duck:
        return (f"{exe} is dbt Core {core.group(1)} without the dbt-duckdb plugin, which "
                "dbt parse needs here (pip install dbt-duckdb into the same environment)")
    return DbtInfo(core=core.group(1), duckdb=duck.group(1))


def status(configured: str | None = None) -> DbtStatus:
    exe = find_dbt(configured)
    if exe is None:
        where = f"SATCHEL_DBT_BIN={configured} is not an executable" if configured else \
            "no dbt found (SATCHEL_DBT_BIN, /opt/dbt/bin/dbt or dbt on PATH)"
        return DbtStatus(False, f"dbt is not installed on this server: {where}. Use the satchel "
                         "image built with INSTALL_DBT=true, or commit target/manifest.json and "
                         "give its path instead.")
    try:
        info = identify(exe)
    except DbtBuildError as e:
        return DbtStatus(False, str(e))
    return DbtStatus(True, f"dbt parse runs on the server (dbt Core {info.core}, dbt-duckdb "
                     f"{info.duckdb})", version=info.core)


# ----------------------------------------------------------------------------- preflight


def _no_jinja(where: str, v: str) -> None:
    if "{{" in v or "{%" in v:
        raise DbtBuildError(f"{where}: Jinja is not supported here when Satchel builds the "
                            "project; write the value out")


def _inside(root: Path, project: Path, rel: str, where: str) -> None:
    """``rel`` (relative to the project folder) must stay inside the checkout at ``root``."""
    _no_jinja(where, rel)
    if rel.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:", rel):
        raise DbtBuildError(f"{where}: {rel} must be a relative path inside the repository")
    p = (project / rel).resolve()
    if p != root and not p.is_relative_to(root):
        raise DbtBuildError(f"{where}: {rel} points outside the repository checkout")


def _yaml(p: Path) -> Any:
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else None
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise DbtBuildError(f"{p.name} is not valid YAML: {e}") from None


def _check_url(where: str, url: str) -> None:
    _no_jinja(where, url)
    if not url.startswith("https://"):
        # git@host:org/repo (ssh) and http:// are refused; private packages need credentials,
        # which the build does not have.
        raise DbtBuildError(f"{where}: {url} must be an https:// URL")
    try:
        git.validate_url(url.split("#", 1)[0])
    except git.GitSourceError as e:
        raise DbtBuildError(f"{where}: {e}") from None


def preflight(root: Path, project: Path) -> bool:
    """Check ``dbt_project.yml`` and the package files before dbt sees them. Returns True when
    the project declares packages (``dbt deps`` has work to do)."""
    root = root.resolve()
    project = project.resolve()
    proj = _yaml(project / "dbt_project.yml")
    if not isinstance(proj, dict):
        raise DbtBuildError("dbt_project.yml is missing or empty in the project folder")
    for key in _PATH_KEYS:
        v = proj.get(key)
        for rel in ([v] if isinstance(v, str) else v if isinstance(v, list) else []):
            _inside(root, project, str(rel), f"dbt_project.yml {key}")
    has_packages = False
    for fname in ("packages.yml", "dependencies.yml"):
        doc = _yaml(project / fname)
        if not isinstance(doc, dict):
            continue
        pkgs = doc.get("packages") or []
        if not isinstance(pkgs, list):
            raise DbtBuildError(f"{fname}: packages must be a list")
        for i, pkg in enumerate(pkgs):
            where = f"{fname} packages[{i}]"
            if not isinstance(pkg, dict):
                raise DbtBuildError(f"{where}: not a package entry")
            has_packages = True
            if "local" in pkg:
                _inside(root, project, str(pkg["local"]), where)
            if "git" in pkg:
                _check_url(where, str(pkg["git"]))
            if "tarball" in pkg:
                _check_url(where, str(pkg["tarball"]))
            if "private" in pkg:
                raise DbtBuildError(f"{where}: private packages need credentials, which the "
                                    "build does not have; vendor them in dbt_packages/ and "
                                    "skip dbt deps")
    return has_packages


# ----------------------------------------------------------------------------- the process


def sandbox_env(work: Path, exe: Path) -> dict[str, str]:
    """The whole environment dbt gets: built from scratch, never copied from the server's."""
    env = {
        "PATH": os.pathsep.join([str(exe.parent), "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": str(work / "home"),
        "TMPDIR": str(work / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "DBT_PROFILES_DIR": str(work / "profiles"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        "DO_NOT_TRACK": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        # git packages (dbt deps): https only, no prompts, no host git config
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ALLOW_PROTOCOL": "https",
    }
    for k in _PASS_ENV:
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def profiles_yml(opts: BuildOptions) -> str:
    """A profile whose only target is an in-memory DuckDB: ``dbt parse`` needs an adapter but
    never opens a connection. JSON is valid YAML, so names cannot inject anything."""
    target = {"type": "duckdb", "path": ":memory:", "schema": opts.target_schema, "threads": 1}
    return json.dumps({PROFILE: {"target": opts.target_name,
                                 "outputs": {opts.target_name: target}}}, indent=2) + "\n"


def _tail(log: Path, scrub: list[tuple[str, str]]) -> str:
    """The useful end of dbt's output (dbt prints the error last): the last lines of the last
    64 KB, with local paths replaced, timestamps and colours removed, at most 1500 characters."""
    try:
        with log.open("rb") as f:
            f.seek(max(0, log.stat().st_size - MAX_LOG_TAIL))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for old, new in scrub:
        text = text.replace(old, new)
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    lines = [re.sub(r"^\d\d:\d\d:\d\d\s+", "", ln).rstrip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    return " | ".join(lines[-15:])[-1500:]


def run_dbt(exe: Path, args: list[str], *, cwd: Path, env: dict[str, str], log: Path,
            timeout: float, scrub: list[tuple[str, str]]) -> None:
    """Run ``dbt <args>`` under the launcher; raise :class:`DbtBuildError` with the log tail."""
    cpu = int(timeout) + 30
    cmd = [sys.executable, "-I", "-S", "-c", _LAUNCHER, str(LIMIT_AS), str(cpu),
           str(LIMIT_FSIZE), str(exe), *args]
    with log.open("ab") as out:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            raise DbtBuildError(f"cannot start dbt: {e.strerror or e}") from None
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            raise DbtBuildError(
                f"dbt {args[0]} timed out after {int(timeout)} s. {_tail(log, scrub)}".strip()
            ) from None
        finally:
            if proc.poll() is None:
                _kill(proc)
    if code != 0:
        why = f"killed by signal {-code}" if code < 0 else f"exit {code}"
        raise DbtBuildError(f"dbt {args[0]} failed ({why}): {_tail(log, scrub)}")


def _kill(proc: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


@contextmanager
def _workdir() -> Iterator[Path]:
    work = Path(tempfile.mkdtemp(prefix="satchel-dbt-"))
    try:
        for d in ("home", "tmp", "profiles", "target", "logs"):
            (work / d).mkdir()
        yield work
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _read(p: Path, name: str) -> bytes:
    size = p.stat().st_size
    if size > MAX_MANIFEST_BYTES:
        raise DbtBuildError(f"dbt wrote a {name} larger than {MAX_MANIFEST_BYTES >> 20} MB")
    return p.read_bytes()


def build(root: Path, opts: BuildOptions, *, dbt_bin: str | None = None,
          timeout: float = TIMEOUT_S) -> Built:
    """``dbt deps`` (unless skipped or no packages) and ``dbt parse`` on the project folder of a
    checkout at ``root``; returns the manifest files for the dbt adapter."""
    exe = find_dbt(dbt_bin)
    if exe is None:
        raise DbtBuildError(status(dbt_bin).message)
    identify(exe)
    try:
        project = git.inside(root, opts.project_dir)
    except git.GitSourceError as e:
        raise DbtBuildError(f"dbt_project_dir: {e}") from None
    if not (project / "dbt_project.yml").is_file():
        raise DbtBuildError(f"no dbt_project.yml in {opts.project_dir} at this commit; set "
                            "dbt_project_dir to the folder that holds it (. for the root)")
    has_packages = preflight(root, project)
    with _workdir() as work:
        (work / "profiles" / "profiles.yml").write_text(profiles_yml(opts))
        env = sandbox_env(work, exe)
        log = work / "logs" / "output.txt"
        scrub = [(str(project.resolve()), "<project>"), (str(project), "<project>"),
                 (str(work.resolve()), "<build>"), (str(work), "<build>"),
                 (str(root.resolve()), "<repo>"), (str(root), "<repo>")]
        common = ["--project-dir", str(project), "--profiles-dir", str(work / "profiles"),
                  "--log-path", str(work / "logs"), "--no-use-colors", "--no-version-check",
                  "--no-send-anonymous-usage-stats"]
        deadline = timeout
        if has_packages and not opts.skip_deps:
            t0 = time.monotonic()
            run_dbt(exe, ["deps", *common], cwd=project, env=env, log=log, timeout=deadline,
                    scrub=scrub)
            deadline = max(1.0, timeout - (time.monotonic() - t0))
        args = ["parse", *common, "--profile", PROFILE, "--target", opts.target_name,
                "--target-path", str(work / "target"), "--no-partial-parse"]
        if opts.vars:
            args += ["--vars", json.dumps(opts.vars)]
        run_dbt(exe, args, cwd=project, env=env, log=log, timeout=deadline, scrub=scrub)
        manifest_p = work / "target" / "manifest.json"
        if not manifest_p.is_file():
            raise DbtBuildError(f"dbt parse wrote no manifest.json: {_tail(log, scrub)}")
        files = {"manifest": _read(manifest_p, "manifest.json")}
        sem = work / "target" / "semantic_manifest.json"
        if sem.is_file():
            files["semantic_manifest"] = _read(sem, "semantic_manifest.json")
    return Built(files=files, dbt_version=_version(files["manifest"]))


def _version(manifest: bytes) -> str | None:
    try:
        meta = json.loads(manifest).get("metadata") or {}
    except (ValueError, AttributeError):
        return None
    v = meta.get("dbt_version") if isinstance(meta, dict) else None
    return str(v) if v else None
