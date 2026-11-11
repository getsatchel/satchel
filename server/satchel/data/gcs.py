"""Google Cloud Storage backend for :mod:`satchel.data.objstore` (``gs://``).

``google-cloud-storage`` is an optional dependency (extra ``gcp``) imported lazily; credentials
are Application Default Credentials only (workload identity, ``gcloud auth application-default``),
never key files.
"""

from __future__ import annotations

import threading
from typing import Any

from satchel.install_hints import extra_hint

GCP_EXTRA_HINT = (
    "Google Cloud Storage support is not installed. Install the 'gcp' extra: "
    f"{extra_hint('gcp')}, "
    "or use the container image, which includes it."
)


class GcsBucket:
    """``BucketLike`` over a real ``google.cloud.storage`` bucket."""

    def __init__(self, bucket: Any) -> None:
        self._b = bucket

    def put(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._b.blob(name).upload_from_string(data, content_type=content_type)

    def size(self, name: str) -> int | None:
        blob = self._b.get_blob(name)
        return None if blob is None else int(blob.size or 0)

    def read_range(self, name: str, start: int, end: int) -> bytes:
        if end <= start:
            return b""
        # download_as_bytes' ``end`` is inclusive.
        return bytes(self._b.blob(name).download_as_bytes(start=start, end=end - 1))

    def delete(self, name: str) -> bool:
        from google.api_core.exceptions import NotFound

        try:
            self._b.blob(name).delete()
        except NotFound:
            return False
        return True

    def list(self, prefix: str, limit: int) -> list[tuple[str, int]]:
        out = [(b.name, int(b.size or 0))
               for b in self._b.list_blobs(prefix=prefix, max_results=limit)]
        return sorted(out)


_lock = threading.Lock()
_client: Any = None


def reset_client() -> None:
    global _client
    with _lock:
        _client = None


def open_gcs_bucket(name: str) -> GcsBucket:
    try:
        from google.cloud import storage  # type: ignore[attr-defined,unused-ignore]
    except ImportError as e:
        raise RuntimeError(GCP_EXTRA_HINT) from e
    global _client
    with _lock:
        if _client is None:
            _client = storage.Client()  # Application Default Credentials
        return GcsBucket(_client.bucket(name))
