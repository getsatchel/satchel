"""Where cached result sets live (design §11): a small ``ResultStore`` interface with a local
directory implementation and an object-store one, chosen by ``SATCHEL_RESULT_DIR``
(``gs://bucket/prefix`` selects GCS, ``s3://bucket/prefix`` selects S3).

``query_result.path`` holds a URI: ``gs://`` or ``s3://bucket/prefix/<workspace>/<id>.parquet``,
or ``file:///abs/path.parquet``. Rows written before this change hold a path relative to the
result directory; those keep resolving against the local directory. Reading never depends on
which store is currently configured, so a deployment can switch stores and old results stay
readable until they expire.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from satchel.data.objstore import (
    BucketLike,
    RangeFile,
    is_object_uri,
    join_uri,
    open_bucket,
    split_uri,
)
from satchel.settings import get_settings

# Row groups are small so a page read decodes one or two groups, not the whole file. The
# gateway caps results at ``sql_row_cap`` rows (1000 by default), so this is a few groups.
ROW_GROUP_ROWS = 1000

Page = tuple[list[str], list[tuple[Any, ...]]]


class ResultStore(Protocol):
    def write(self, workspace_id: uuid.UUID, result_id: uuid.UUID, table: pa.Table) -> str:
        """Persist ``table``; return the URI to store in ``query_result.path``."""

    def read_page(self, ref: str, offset: int, limit: int) -> Page:
        """Rows ``[offset, offset+limit)``; ``FileNotFoundError`` when the object is gone."""

    def exists(self, ref: str) -> bool: ...

    def delete(self, ref: str) -> bool:
        """Remove the object; False when it was already gone."""


def _rows_via_duckdb(table: pa.Table) -> Page:
    """Python rows with DuckDB's type mapping, so every store yields identical values."""
    con = duckdb.connect(":memory:")
    try:
        con.register("page", table)
        cur = con.execute("SELECT * FROM page")
        return [d[0] for d in cur.description], cur.fetchall()
    finally:
        con.close()


class LocalResultStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, ref: str) -> Path:
        if ref.startswith("file://"):
            return Path(unquote(urlparse(ref).path))
        return self.root / ref  # legacy relative path

    def write(self, workspace_id: uuid.UUID, result_id: uuid.UUID, table: pa.Table) -> str:
        path = (self.root / f"{workspace_id}/{result_id}.parquet").resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, path, row_group_size=ROW_GROUP_ROWS)
        return path.as_uri()

    def read_page(self, ref: str, offset: int, limit: int) -> Page:
        path = self._path(ref)
        if not path.exists():
            raise FileNotFoundError(f"result file is gone: {ref}")
        con = duckdb.connect(":memory:")
        try:
            cur = con.execute(
                "SELECT * FROM read_parquet(?) LIMIT ? OFFSET ?", [str(path), limit, offset]
            )
            return [d[0] for d in cur.description], cur.fetchall()
        finally:
            con.close()

    def exists(self, ref: str) -> bool:
        return self._path(ref).exists()

    def delete(self, ref: str) -> bool:
        try:
            self._path(ref).unlink()
        except FileNotFoundError:
            return False
        return True


class ObjectResultStore:
    """Parquet objects in a GCS or S3 bucket, read back with ranged reads (footer + the row
    groups a page touches), so any replica can serve ``get_result`` without downloading the
    object."""

    def __init__(
        self, scheme: str, bucket_name: str, prefix: str = "", *, bucket: BucketLike | None = None
    ) -> None:
        self.scheme = scheme
        self.bucket_name = bucket_name
        self.prefix = prefix.strip("/")
        self._bucket = bucket

    @classmethod
    def from_uri(cls, uri: str, *, bucket: BucketLike | None = None) -> ObjectResultStore:
        scheme, name, prefix = split_uri(uri)
        return cls(scheme, name, prefix, bucket=bucket)

    def _open(self, scheme: str, bucket_name: str) -> BucketLike:
        if self._bucket is not None and (scheme, bucket_name) == (self.scheme, self.bucket_name):
            return self._bucket
        return open_bucket(scheme, bucket_name)

    def _locate(self, ref: str) -> tuple[BucketLike, str]:
        scheme, name, key = split_uri(ref)
        return self._open(scheme, name), key

    def write(self, workspace_id: uuid.UUID, result_id: uuid.UUID, table: pa.Table) -> str:
        key = "/".join(p for p in (self.prefix, str(workspace_id), f"{result_id}.parquet") if p)
        buf = io.BytesIO()
        pq.write_table(table, buf, row_group_size=ROW_GROUP_ROWS)
        self._open(self.scheme, self.bucket_name).put(
            key, buf.getvalue(), "application/vnd.apache.parquet"
        )
        return join_uri(self.scheme, self.bucket_name, key)

    def read_page(self, ref: str, offset: int, limit: int) -> Page:
        bucket, key = self._locate(ref)
        size = bucket.size(key)
        if size is None:
            raise FileNotFoundError(f"result file is gone: {ref}")
        pf = pq.ParquetFile(RangeFile(bucket, key, size))
        md = pf.metadata
        wanted: list[int] = []
        first_start = 0
        start = 0
        for i in range(md.num_row_groups):
            n = md.row_group(i).num_rows
            if start + n > offset and start < offset + limit:
                if not wanted:
                    first_start = start
                wanted.append(i)
            start += n
        if not wanted:
            return _rows_via_duckdb(pf.schema_arrow.empty_table())
        table = pf.read_row_groups(wanted).slice(offset - first_start, limit)
        return _rows_via_duckdb(table)

    def exists(self, ref: str) -> bool:
        bucket, key = self._locate(ref)
        return bucket.size(key) is not None

    def delete(self, ref: str) -> bool:
        bucket, key = self._locate(ref)
        return bucket.delete(key)


def configured_store() -> ResultStore:
    """The store new results are written to, from ``SATCHEL_RESULT_DIR``."""
    d = get_settings().result_dir
    if is_object_uri(d):
        return ObjectResultStore.from_uri(d)
    return LocalResultStore(Path(d).expanduser())


def store_for_ref(ref: str) -> ResultStore:
    """The store that can read ``ref`` (independent of the configured one)."""
    if is_object_uri(ref):
        scheme, name, _ = split_uri(ref)
        return ObjectResultStore(scheme, name)
    d = get_settings().result_dir
    if ref.startswith("file://") or not is_object_uri(d):
        return LocalResultStore(Path(d).expanduser())
    raise FileNotFoundError(f"result file is gone: {ref} (relative path, result dir is {d})")
