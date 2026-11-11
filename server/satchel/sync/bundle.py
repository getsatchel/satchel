"""Unpack an uploaded context repo bundle (``.tar.gz`` of the repo folder) into a temp directory.

Only regular files and directories are extracted; links, devices, absolute paths and ``..`` are
refused, and the size, the unpacked size and the file count are capped.
"""

from __future__ import annotations

import io
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

MAX_BUNDLE = 20 * 1024 * 1024
MAX_UNPACKED = 100 * 1024 * 1024
MAX_FILES = 5000


class BundleError(ValueError):
    """The upload is not a usable context bundle."""


@contextmanager
def unpacked(data: bytes) -> Iterator[Path]:
    if len(data) > MAX_BUNDLE:
        raise BundleError(f"the bundle is larger than {MAX_BUNDLE >> 20} MB")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = tar.getmembers()
    except (tarfile.TarError, OSError, EOFError) as e:
        raise BundleError(f"not a .tar.gz archive: {e}") from None
    if len(members) > MAX_FILES:
        raise BundleError(f"the bundle has more than {MAX_FILES} entries")
    with tempfile.TemporaryDirectory(prefix="satchel-bundle-") as tmp:
        total = 0
        for m in members:
            p = PurePosixPath(m.name)
            if p.is_absolute() or ".." in p.parts:
                raise BundleError(f"unsafe path in the bundle: {m.name}")
            if not (m.isfile() or m.isdir()):
                raise BundleError(f"only files and folders are allowed: {m.name}")
            total += m.size
            if total > MAX_UNPACKED:
                raise BundleError(f"the bundle unpacks to more than {MAX_UNPACKED >> 20} MB")
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                tar.extractall(tmp, members=tar.getmembers(), filter="data")
        except (tarfile.TarError, OSError) as e:
            raise BundleError(f"could not unpack the bundle: {e}") from None
        root = Path(tmp)
        entries = list(root.iterdir())
        # `tar -C repo .` and `tar repo` both work: descend into a lone top-level folder that
        # holds the repo config.
        if len(entries) == 1 and (entries[0] / "satchel.config.yaml").is_file():
            root = entries[0]
        yield root
