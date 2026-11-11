"""BigQuery connector (``pip install 'satchel[bigquery]'``).

Auth is Application Default Credentials (workload identity on GKE / Cloud Run, or
``GOOGLE_APPLICATION_CREDENTIALS``) unless the connection stores a service-account JSON key.
BigQuery has no read-only session: reads are enforced by the gateway guardrail and by IAM
(grant ``roles/bigquery.dataViewer`` on the data and ``roles/bigquery.jobUser`` on the job
project, nothing more). Every query job carries a cost guard (``maximum_bytes_billed``), a
server-side job timeout and the label ``app=satchel``.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import re
from typing import Any

import pyarrow as pa

from satchel.data.connectors.base import AssetSpec, SqlTimeout, split_list

GIB = 1024**3
DEFAULT_MAX_GIB_BILLED = 10
INTROSPECT_TIMEOUT_S = 120.0
LABELS = {"app": "satchel"}
SCOPES = ("https://www.googleapis.com/auth/bigquery",)
_KEY_FIELDS = ("client_email", "private_key", "token_uri")
_DATASET = re.compile(r"^[A-Za-z0-9_]{1,1024}$")
_PROJECT = re.compile(r"^[a-z][a-z0-9.:-]{4,62}[a-z0-9]$")


def parse_service_account(secret: str) -> dict[str, Any]:
    """The stored secret: a service-account JSON key. Raises ``ValueError`` with a message fit
    for the admin (never the key itself)."""
    try:
        info = json.loads(secret)
    except json.JSONDecodeError:
        raise ValueError("the service-account key is not valid JSON (paste the whole .json "
                         "file)") from None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ValueError('the key must be a service-account JSON key ("type": '
                         '"service_account"); leave it empty to use Application Default '
                         "Credentials")
    missing = [k for k in _KEY_FIELDS if not info.get(k)]
    if missing:
        raise ValueError(f"the service-account key has no {', '.join(missing)}")
    return info


def check_config(config: dict[str, Any]) -> None:
    for key in ("project", "data_project"):
        p = config.get(key)
        if p and not _PROJECT.match(str(p)):
            raise ValueError(f"{key} {p!r} is not a Google Cloud project ID")
    for ds in split_list(config.get("datasets")):
        if not _DATASET.match(ds):
            raise ValueError(f"dataset {ds!r}: use the dataset ID (letters, digits, "
                             "underscores); set Data project for another project's datasets")


def _quote(*parts: str) -> str:
    return "`" + ".".join(p.replace("`", "") for p in parts) + "`"


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def _unquote_option(v: Any) -> str | None:
    """TABLE_OPTIONS.option_value holds a SQL string literal, e.g. ``"Orders, one row each"``."""
    if v is None:
        return None
    s = str(v)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), s[1:-1])
    return s or None


def make_client(config: dict[str, Any], secret: str | None) -> Any:
    try:
        from google.cloud import bigquery
    except ImportError:
        raise ValueError("BigQuery needs the google-cloud-bigquery package: "
                         "pip install 'satchel[bigquery]'") from None
    credentials = None
    if secret:
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            parse_service_account(secret), scopes=list(SCOPES))
    return bigquery.Client(project=config.get("project") or None, credentials=credentials,
                           location=config.get("location") or None)


class BigQueryConnector:
    kind = "bigquery"
    dialect = "bigquery"

    def __init__(
        self, config: dict[str, Any], secret: str | None = None, *, client: Any = None
    ) -> None:
        if not config.get("project"):
            raise ValueError("bigquery connection needs config.project (the job project)")
        check_config(config)
        self.config = config
        self.project = str(config["project"])
        self.data_project = str(config.get("data_project") or self.project)
        self.location = config.get("location") or None
        self.datasets = split_list(config.get("datasets"))
        gib = config.get("maximum_gib_billed") or DEFAULT_MAX_GIB_BILLED
        self.maximum_bytes_billed = int(float(gib) * GIB)
        self._client = client if client is not None else make_client(config, secret)

    def job_config(self, timeout_s: float) -> Any:
        from google.cloud import bigquery

        cfg = bigquery.QueryJobConfig(
            use_legacy_sql=False,
            maximum_bytes_billed=self.maximum_bytes_billed,
            job_timeout_ms=max(1000, int(timeout_s * 1000)),
            labels=dict(LABELS),
        )
        if len(self.datasets) == 1:
            # Unqualified table names resolve in the only dataset.
            cfg.default_dataset = f"{self.data_project}.{self.datasets[0]}"
        return cfg

    def _run(self, sql: str, *, limit: int | None, timeout_s: float) -> pa.Table:
        job = self._client.query(sql, job_config=self.job_config(timeout_s),
                                 location=self.location, timeout=timeout_s)
        try:
            rows = job.result(timeout=timeout_s, max_results=limit)
        except concurrent.futures.TimeoutError as e:
            _cancel(job)
            raise SqlTimeout(f"statement exceeded {timeout_s:g}s timeout") from e
        except Exception as e:
            readable = _translate(e, timeout_s, self.maximum_bytes_billed)
            if readable is e:
                raise
            raise readable from e
        table = rows.to_arrow(create_bqstorage_client=False)
        return table if limit is None else table.slice(0, limit)

    def execute(self, sql: str, *, limit: int, timeout_s: float) -> pa.Table:
        return self._run(sql, limit=max(limit, 1), timeout_s=timeout_s)

    def list_datasets(self) -> list[str]:
        if self.datasets:
            return self.datasets
        return sorted(d.dataset_id for d in self._client.list_datasets(self.data_project))

    def introspect(self) -> list[AssetSpec]:
        out: list[AssetSpec] = [AssetSpec("database", self.data_project)]
        for ds in self.list_datasets():
            out.append(AssetSpec("schema", ds, self.data_project))
            out.extend(self._dataset_assets(ds))
        return out

    def introspect_sql(self, dataset: str) -> str:
        def info(view: str) -> str:
            return _quote(self.data_project, dataset, "INFORMATION_SCHEMA", view)

        return (
            "SELECT c.table_name, c.column_name, c.data_type, f.description AS column_comment, "
            "o.option_value AS table_comment "
            f"FROM {info('COLUMNS')} c "
            f"LEFT JOIN {info('COLUMN_FIELD_PATHS')} f ON f.table_name = c.table_name "
            "AND f.column_name = c.column_name AND f.field_path = c.column_name "
            f"LEFT JOIN {info('TABLE_OPTIONS')} o ON o.table_name = c.table_name "
            "AND o.option_name = 'description' "
            "ORDER BY c.table_name, c.ordinal_position"
        )

    def _dataset_assets(self, dataset: str) -> list[AssetSpec]:
        rows = self._run(self.introspect_sql(dataset), limit=None,
                         timeout_s=INTROSPECT_TIMEOUT_S).to_pylist()
        return dataset_specs(dataset, rows)

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()


def dataset_specs(dataset: str, rows: list[dict[str, Any]]) -> list[AssetSpec]:
    """INFORMATION_SCHEMA rows (table_name, column_name, data_type, column_comment,
    table_comment), ordered by table and position -> table and column assets."""
    out: list[AssetSpec] = []
    seen: set[str] = set()
    for r in rows:
        t = f"{dataset}.{r['table_name']}"
        if t not in seen:
            seen.add(t)
            out.append(AssetSpec("table", t, dataset,
                                 comment=_unquote_option(r.get("table_comment"))))
        out.append(AssetSpec("column", f"{t}.{r['column_name']}", t,
                             str(r["data_type"]) if r.get("data_type") else None,
                             r.get("column_comment") or None))
    return out


def _cancel(job: Any) -> None:
    # Best effort; the server-side job timeout still applies.
    with contextlib.suppress(Exception):
        job.cancel()


def _translate(e: Exception, timeout_s: float, max_bytes: int) -> Exception:
    """Make BigQuery's cost-guard and timeout failures readable for the agent."""
    reasons = {str(err.get("reason", "")) for err in getattr(e, "errors", None) or []
               if isinstance(err, dict)}
    msg = str(e)
    if "bytesBilledLimitExceeded" in reasons or "bytesBilledLimitExceeded" in msg:
        return ValueError(
            f"the query would bill more than {max_bytes / GIB:g} GiB, the connection's cost "
            "guard (maximum bytes billed). Select fewer columns, filter on the partition "
            "column, or query a smaller table.")
    if "timeout" in reasons or "Job timed out" in msg or "jobTimeout" in msg:
        return SqlTimeout(f"statement exceeded {timeout_s:g}s timeout")
    return e
