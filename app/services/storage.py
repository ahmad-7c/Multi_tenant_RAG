"""Object storage behind one small interface.

The API process writes uploaded bytes; the Celery worker reads them back.
Those are different processes, and in any real deployment different hosts.
So what the document row carries is a *key* -- an opaque identifier the
storage backend knows how to resolve -- not a path, which would silently
require both processes to share a filesystem.
"""
import os
import re
import unicodedata
import uuid
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path

from app.core.config import settings


class StorageError(Exception):
    pass


class StorageObjectNotFound(StorageError):
    """Raised by every backend for a missing key.

    Backends translate their own errors into this one so callers never have
    to branch on which backend is configured.
    """


# Anything that is not an obviously safe filename character. Deliberately an
# allowlist: a denylist of "bad" characters has to anticipate every
# separator, encoding trick and reserved name on every OS, and only has to
# be wrong once.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9 ._()\-]")
_COLLAPSE_DOTS = re.compile(r"\.{2,}")


def sanitize_filename(filename: str) -> str:
    """Reduce a user-supplied filename to a single safe path segment.

    The storage key embeds this value, so an unsanitized `../../..` would
    let an uploader address bytes outside their own prefix.
    """
    # Normalize first: a composed/decomposed unicode trick should not be
    # able to smuggle a character past the allowlist below.
    name = unicodedata.normalize("NFKD", filename or "")

    # Take the last segment under BOTH separators. os.path.basename alone
    # is not enough: on Linux it treats a backslash as an ordinary
    # character, so "..\\..\\sam" would survive intact.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]

    name = _UNSAFE_CHARS.sub("_", name)
    # Collapse runs of dots so "....//....//x" cannot leave ".." behind,
    # and strip leading dots so the result is never a traversal segment or
    # a hidden file.
    name = _COLLAPSE_DOTS.sub(".", name).strip(". ").strip()

    # Everything can legitimately sanitize away (e.g. "..."). An empty
    # final segment would make the key end in "/" and collide with its own
    # directory, so fall back to a fixed name -- the key is still unique
    # because of the document_id segment.
    if not name:
        return "upload"

    # Object stores accept very long keys but filesystems do not; 200 bytes
    # leaves room for the two uuid segments under a 255-byte limit.
    return name[:200]


def build_storage_key(*, org_id: uuid.UUID, document_id: uuid.UUID, filename: str) -> str:
    """The one place a storage key is constructed.

    org_id first so a bucket listing groups by tenant and "remove
    everything belonging to this org" is a prefix delete. document_id next
    so two uploads of "report.pdf" cannot overwrite each other. The
    original filename last, because object stores show it in listings and
    it is what a human recognizes.
    """
    return f"{org_id}/{document_id}/{sanitize_filename(filename)}"


class Storage(ABC):
    @abstractmethod
    def save(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def read(self, key: str) -> bytes: ...

    @abstractmethod
    def delete(self, key: str) -> None:
        """Must be idempotent.

        DELETE /documents has to converge on "gone" even if a previous
        attempt half-failed, otherwise a document row becomes impossible to
        remove.
        """

    @abstractmethod
    def exists(self, key: str) -> bool: ...


class LocalDiskStorage(Storage):
    """Dev backend. Writes under a single root directory."""

    def __init__(self, root: str):
        self._root = Path(root).resolve()

    def _resolve(self, key: str) -> Path:
        # Defence in depth. build_storage_key already sanitizes the
        # filename, but a backend that trusts its caller is one refactor
        # away from being an arbitrary-file-write primitive. Resolving and
        # re-checking containment is cheap and does not depend on anyone
        # else having done the right thing.
        candidate = (self._root / key).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise ValueError(f"storage key escapes the storage root: {key!r}")
        return candidate

    def save(self, key: str, data: bytes) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary name in the same directory, then rename.
        # os.replace is atomic on both POSIX and Windows, so the worker can
        # never observe a half-written file -- which would otherwise fail
        # ingestion as "corrupt PDF" and be impossible to reproduce.
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def read(self, key: str) -> bytes:
        path = self._resolve(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise StorageObjectNotFound(key) from exc

    def delete(self, key: str) -> None:
        path = self._resolve(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        # Prune the now-empty {org_id}/{document_id}/ directory so the dev
        # tree does not accumulate thousands of empty folders. Best-effort:
        # a non-empty parent is fine and is not an error.
        try:
            path.parent.rmdir()
        except OSError:
            pass

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()


class S3Storage(Storage):
    """Production backend for any S3-compatible endpoint.

    One implementation covers AWS S3, Supabase Storage, Cloudflare R2 and
    MinIO: they differ only in endpoint URL and credentials, which are
    Settings.
    """

    def __init__(
        self,
        *,
        bucket: str,
        client=None,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ):
        self._bucket = bucket
        if client is not None:
            # Injected for tests -- a fake client is what lets the S3 path
            # be covered with no network and no credentials.
            self._client = client
        else:
            # Imported here, not at module scope, so a dev box running the
            # local backend never needs boto3 installed.
            import boto3

            self._client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                region_name=region,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
            )

    def _not_found_errors(self) -> tuple[type[BaseException], ...]:
        exceptions = getattr(self._client, "exceptions", None)
        candidates = []
        for name in ("NoSuchKey", "ClientError"):
            exc = getattr(exceptions, name, None)
            if isinstance(exc, type) and issubclass(exc, BaseException):
                candidates.append(exc)
        return tuple(candidates) or (Exception,)

    def save(self, key: str, data: bytes) -> None:
        # No ACL argument at all. The bucket is private; passing
        # ACL="public-read" here would make every tenant document readable
        # by anyone who guesses a key, and many S3-compatible providers
        # reject the parameter outright.
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data)

    def read(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except self._not_found_errors() as exc:
            raise StorageObjectNotFound(key) from exc
        return response["Body"].read()

    def delete(self, key: str) -> None:
        # S3 DELETE is already idempotent: deleting an absent key succeeds.
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except self._not_found_errors():
            return False
        return True


@lru_cache(maxsize=1)
def get_storage() -> Storage:
    """Build the configured backend once per process.

    Cached because the S3 client sets up a connection pool; rebuilding it
    per request would mean a new TLS handshake per upload.
    """
    if settings.STORAGE_BACKEND == "s3":
        if not settings.S3_BUCKET:
            # Fail loudly at first use rather than accepting uploads and
            # losing them: a misconfigured bucket must not look like a
            # healthy API.
            raise RuntimeError("STORAGE_BACKEND=s3 requires S3_BUCKET to be set")
        return S3Storage(
            bucket=settings.S3_BUCKET,
            endpoint_url=settings.S3_ENDPOINT_URL,
            region=settings.S3_REGION,
            access_key_id=settings.S3_ACCESS_KEY_ID,
            secret_access_key=settings.S3_SECRET_ACCESS_KEY,
        )
    return LocalDiskStorage(root=settings.STORAGE_LOCAL_ROOT)
