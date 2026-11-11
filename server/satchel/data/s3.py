"""Amazon S3 backend for :mod:`satchel.data.objstore` (``s3://``).

``boto3`` is an optional dependency (extra ``aws``) imported lazily. Credentials come from the
AWS default chain only (IRSA / EKS Pod Identity web identity, ECS task role, instance profile,
``AWS_PROFILE`` for local use); satchel never reads access keys from its own settings.

The endpoint follows the standard AWS environment variables, so an S3-compatible store or an
emulator needs no satchel-specific setting: ``AWS_ENDPOINT_URL_S3`` (or ``AWS_ENDPOINT_URL``)
points the client elsewhere, and a custom endpoint switches to path-style addressing, which
emulators and MinIO expect. Objects inherit the bucket's default encryption (SSE-S3 or SSE-KMS
configured on the bucket, as the Terraform module does).
"""

from __future__ import annotations

import os
import threading
from typing import Any

AWS_EXTRA_HINT = (
    "Amazon S3 support is not installed. Install the 'aws' extra: "
    "pip install 'satchel[aws]' (or uv tool install 'satchel[aws]'), "
    "or use the container image, which includes it."
)


def custom_endpoint() -> str | None:
    return os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL") or None


class S3Bucket:
    """``BucketLike`` over a boto3 S3 client and one bucket."""

    def __init__(self, client: Any, name: str) -> None:
        self._c = client
        self.name = name

    @staticmethod
    def _missing(e: Exception) -> bool:
        resp = getattr(e, "response", None) or {}
        code = str(resp.get("Error", {}).get("Code", ""))
        status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return code in ("404", "NoSuchKey", "NotFound") or status == 404

    def put(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._c.put_object(Bucket=self.name, Key=name, Body=data, ContentType=content_type)

    def size(self, name: str) -> int | None:
        try:
            head = self._c.head_object(Bucket=self.name, Key=name)
        except Exception as e:
            if self._missing(e):
                return None
            raise
        return int(head.get("ContentLength") or 0)

    def read_range(self, name: str, start: int, end: int) -> bytes:
        if end <= start:
            return b""
        # HTTP ranges are inclusive.
        resp = self._c.get_object(Bucket=self.name, Key=name, Range=f"bytes={start}-{end - 1}")
        return bytes(resp["Body"].read())

    def delete(self, name: str) -> bool:
        # DeleteObject succeeds for a missing key, so check first to report "already gone".
        if self.size(name) is None:
            return False
        self._c.delete_object(Bucket=self.name, Key=name)
        return True

    def list(self, prefix: str, limit: int) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        pages = self._c.get_paginator("list_objects_v2").paginate(
            Bucket=self.name, Prefix=prefix)
        for page in pages:
            for o in page.get("Contents", []):
                out.append((str(o["Key"]), int(o.get("Size") or 0)))
                if len(out) >= limit:
                    return sorted(out)
        return sorted(out)


_lock = threading.Lock()
_client: Any = None


def reset_client() -> None:
    global _client
    with _lock:
        _client = None


def make_client() -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as e:
        raise RuntimeError(AWS_EXTRA_HINT) from e
    cfg = Config(
        retries={"max_attempts": 5, "mode": "standard"},
        s3={"addressing_style": "path" if custom_endpoint() else "auto"},
    )
    # boto3 reads AWS_ENDPOINT_URL(_S3) and AWS_REGION / AWS_DEFAULT_REGION itself.
    return boto3.session.Session().client("s3", config=cfg)


def open_s3_bucket(name: str) -> S3Bucket:
    global _client
    with _lock:
        if _client is None:
            _client = make_client()
        return S3Bucket(_client, name)
