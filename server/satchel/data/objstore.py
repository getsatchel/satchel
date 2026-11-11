"""Object storage behind a tiny bucket interface: ``gs://`` (Google Cloud Storage) and ``s3://``
(Amazon S3 or an S3-compatible endpoint).

The cloud SDKs are optional extras imported lazily (``gcp``: google-cloud-storage, ``aws``:
boto3). Credentials always come from the platform's default chain (GCP Application Default
Credentials / workload identity; the AWS default chain: IRSA, EKS Pod Identity, ECS task role,
instance profile), never key files. Tests inject in-memory buckets with :func:`register_bucket`.
"""

from __future__ import annotations

import threading
from typing import Any, Protocol

SCHEMES = ("gs", "s3")


class BucketLike(Protocol):
    """The few object operations satchel needs."""

    def put(self, name: str, data: bytes, content_type: str = ...) -> None: ...

    def size(self, name: str) -> int | None:
        """Object size in bytes, or None when it does not exist."""

    def read_range(self, name: str, start: int, end: int) -> bytes:
        """Bytes ``[start, end)`` of the object."""

    def delete(self, name: str) -> bool:
        """Delete; False when it was already gone."""

    def list(self, prefix: str, limit: int) -> list[tuple[str, int]]:
        """``(name, size)`` of the objects under ``prefix``, sorted, at most ``limit``."""


def is_object_uri(value: str) -> bool:
    """True for ``gs://...`` and ``s3://...``."""
    return any(value.startswith(f"{s}://") for s in SCHEMES)


def split_uri(uri: str) -> tuple[str, str, str]:
    """``s3://bucket/some/prefix`` -> ('s3', 'bucket', 'some/prefix')."""
    scheme, sep, rest = uri.partition("://")
    if not sep or scheme not in SCHEMES:
        raise ValueError(f"not a gs:// or s3:// URI: {uri!r}")
    bucket, _, key = rest.partition("/")
    if not bucket:
        raise ValueError(f"{scheme}:// URI has no bucket: {uri!r}")
    return scheme, bucket, key.strip("/")


def join_uri(scheme: str, bucket: str, key: str) -> str:
    return f"{scheme}://{bucket}/{key}"


def describe(scheme: str) -> str:
    return {"gs": "GCS", "s3": "S3"}[scheme]


_lock = threading.Lock()
_buckets: dict[tuple[str, str], BucketLike] = {}
_overrides: dict[tuple[str, str], BucketLike] = {}


def register_bucket(scheme: str, name: str, bucket: BucketLike | None) -> None:
    """Use ``bucket`` for ``<scheme>://name`` instead of a real client (tests); None removes it."""
    with _lock:
        if bucket is None:
            _overrides.pop((scheme, name), None)
        else:
            _overrides[(scheme, name)] = bucket


def open_bucket(scheme: str, name: str) -> BucketLike:
    key = (scheme, name)
    with _lock:
        if key in _overrides:
            return _overrides[key]
        if key in _buckets:
            return _buckets[key]
    if scheme == "gs":
        from satchel.data import gcs

        b: BucketLike = gcs.open_gcs_bucket(name)
    elif scheme == "s3":
        from satchel.data import s3

        b = s3.open_s3_bucket(name)
    else:
        raise ValueError(f"unsupported object store scheme: {scheme!r}")
    with _lock:
        return _buckets.setdefault(key, b)


def reset_clients() -> None:
    """Forget cached buckets and clients (tests)."""
    from satchel.data import gcs, s3

    with _lock:
        _buckets.clear()
    gcs.reset_client()
    s3.reset_client()


class RangeFile:
    """Read-only seekable file over an object, fetched with ranged reads.

    Parquet reads the footer first and then only the column chunks it needs, so a page read
    touches a few KiB instead of the whole object. The tail is prefetched in one request."""

    TAIL = 256 * 1024
    CHUNK = 1024 * 1024

    def __init__(self, bucket: BucketLike, name: str, size: int) -> None:
        self._bucket, self._name, self._size = bucket, name, size
        self._pos = 0
        self.closed = False
        self._tail_start = max(0, size - self.TAIL)
        self._tail: bytes | None = None
        self.requests = 0

    def _fetch(self, start: int, end: int) -> bytes:
        self.requests += 1
        return self._bucket.read_range(self._name, start, end)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = 0) -> int:
        base = {0: 0, 1: self._pos, 2: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def size(self) -> int:
        return self._size

    def read(self, n: int = -1) -> bytes:
        if self._pos >= self._size:
            return b""
        end = self._size if n is None or n < 0 else min(self._size, self._pos + n)
        if self._pos >= self._tail_start:
            if self._tail is None:
                self._tail = self._fetch(self._tail_start, self._size)
            out = self._tail[self._pos - self._tail_start : end - self._tail_start]
        else:
            out = self._fetch(self._pos, end)
        self._pos += len(out)
        return out

    def readinto(self, b: Any) -> int:
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)

    def close(self) -> None:
        self.closed = True
