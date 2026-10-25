"""Storage service tests.

The storage layer is the one place where a user-supplied string (the
uploaded filename) ends up in something that looks like a path, so most of
these tests are about that string not being trusted.
"""
import uuid

import pytest

from app.services import storage as storage_module
from app.services.storage import (
    LocalDiskStorage,
    S3Storage,
    StorageObjectNotFound,
    build_storage_key,
    get_storage,
    sanitize_filename,
)


# ---------------------------------------------------------------- keys ----


def test_key_format_is_org_then_document_then_filename():
    org_id = uuid.uuid4()
    document_id = uuid.uuid4()

    key = build_storage_key(org_id=org_id, document_id=document_id, filename="report.pdf")

    # org_id first so a bucket listing is grouped by tenant, which makes
    # "delete everything belonging to this org" a prefix operation.
    assert key == f"{org_id}/{document_id}/report.pdf"


def test_key_is_unique_per_document_even_for_identical_filenames():
    org_id = uuid.uuid4()
    a = build_storage_key(org_id=org_id, document_id=uuid.uuid4(), filename="report.pdf")
    b = build_storage_key(org_id=org_id, document_id=uuid.uuid4(), filename="report.pdf")

    # The document_id segment is what prevents two uploads of "report.pdf"
    # from overwriting each other.
    assert a != b


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../etc/passwd",
        "..\\..\\windows\\system32\\config\\sam",
        "/etc/shadow",
        "C:\\Windows\\win.ini",
        "....//....//secret.pdf",
        "a/b/c.pdf",
    ],
)
def test_sanitize_filename_strips_every_path_component(hostile):
    cleaned = sanitize_filename(hostile)

    assert "/" not in cleaned
    assert "\\" not in cleaned
    assert not cleaned.startswith(".")


def test_sanitize_filename_keeps_a_usable_name():
    assert sanitize_filename("Q3 Report (final).pdf") == "Q3 Report (final).pdf"


def test_sanitize_filename_falls_back_when_nothing_survives():
    # "..." sanitizes down to nothing; an empty final segment would make
    # the key end in "/" and collide with the directory itself.
    assert sanitize_filename("...") == "upload"
    assert sanitize_filename("") == "upload"


def test_build_storage_key_sanitizes_the_filename():
    org_id = uuid.uuid4()
    document_id = uuid.uuid4()

    key = build_storage_key(
        org_id=org_id, document_id=document_id, filename="../../../etc/passwd"
    )

    assert key == f"{org_id}/{document_id}/passwd"


# ----------------------------------------------------------- local disk ----


@pytest.fixture()
def local(tmp_path):
    return LocalDiskStorage(root=str(tmp_path / "storage"))


def test_local_save_read_round_trip(local):
    key = f"{uuid.uuid4()}/{uuid.uuid4()}/doc.txt"

    local.save(key, b"hello bytes")

    assert local.read(key) == b"hello bytes"


def test_local_save_creates_intermediate_directories(local):
    key = f"{uuid.uuid4()}/{uuid.uuid4()}/nested.txt"
    local.save(key, b"x")
    assert local.exists(key)


def test_local_read_missing_key_raises_storage_object_not_found(local):
    with pytest.raises(StorageObjectNotFound):
        local.read("nope/nope/nope.txt")


def test_local_delete_removes_the_object(local):
    key = f"{uuid.uuid4()}/{uuid.uuid4()}/doc.txt"
    local.save(key, b"x")

    local.delete(key)

    assert not local.exists(key)


def test_local_delete_is_idempotent(local):
    # Deleting an already-absent object must not raise: DELETE /documents
    # has to converge on "gone" even if a previous attempt half-failed, or
    # a document row becomes impossible to remove.
    local.delete("never/existed.txt")


@pytest.mark.parametrize(
    "escaping_key",
    ["../outside.txt", "a/../../outside.txt", "/absolute/outside.txt"],
)
def test_local_refuses_a_key_that_escapes_the_root(local, escaping_key):
    # Defence in depth: build_storage_key already sanitizes, but the
    # backend must not rely on its caller having done so.
    with pytest.raises(ValueError):
        local.save(escaping_key, b"x")
    with pytest.raises(ValueError):
        local.read(escaping_key)


def test_local_storage_writes_under_its_configured_root(tmp_path):
    root = tmp_path / "configured-root"
    store = LocalDiskStorage(root=str(root))
    key = f"{uuid.uuid4()}/{uuid.uuid4()}/doc.txt"

    store.save(key, b"x")

    assert (root / key).read_bytes() == b"x"


# -------------------------------------------------------------------- s3 ----


class _FakeS3Client:
    """Minimal stand-in for a boto3 S3 client.

    Asserting against this is the point: it proves S3Storage calls
    put_object/get_object/delete_object with the bucket and key we expect,
    without a network or credentials.
    """

    class exceptions:  # noqa: N801 - mirrors boto3's client.exceptions shape
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple] = []

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        self.calls.append(("put", Bucket, Key, kwargs))
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket, Key):
        self.calls.append(("get", Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey("missing")
        return {"Body": _FakeBody(self.objects[(Bucket, Key)])}

    def delete_object(self, *, Bucket, Key):
        self.calls.append(("delete", Bucket, Key))
        self.objects.pop((Bucket, Key), None)

    def head_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey("missing")
        return {}


class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


@pytest.fixture()
def s3():
    client = _FakeS3Client()
    return S3Storage(bucket="documind-test", client=client), client


def test_s3_save_read_round_trip(s3):
    store, _ = s3
    key = "org/doc/file.pdf"

    store.save(key, b"pdf bytes")

    assert store.read(key) == b"pdf bytes"


def test_s3_save_targets_the_configured_bucket_and_key(s3):
    store, client = s3
    store.save("org/doc/file.pdf", b"x")

    verb, bucket, key, _ = client.calls[0]
    assert (verb, bucket, key) == ("put", "documind-test", "org/doc/file.pdf")


def test_s3_never_sets_a_public_acl(s3):
    store, client = s3
    store.save("org/doc/file.pdf", b"x")

    _, _, _, kwargs = client.calls[0]
    # The bucket is private. Uploading with a public-read ACL would make
    # every tenant document world-readable to anyone who guesses a key.
    assert "ACL" not in kwargs or kwargs["ACL"] == "private"


def test_s3_read_missing_key_raises_storage_object_not_found(s3):
    store, _ = s3
    # Translated into the same exception the local backend raises, so
    # callers never branch on which backend is configured.
    with pytest.raises(StorageObjectNotFound):
        store.read("org/doc/absent.pdf")


def test_s3_delete_is_idempotent(s3):
    store, _ = s3
    store.delete("org/doc/absent.pdf")


# --------------------------------------------------------------- factory ----


def test_factory_returns_local_backend_when_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(storage_module.settings, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(storage_module.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    get_storage.cache_clear()

    assert isinstance(get_storage(), LocalDiskStorage)
    get_storage.cache_clear()


def test_factory_rejects_s3_backend_without_a_bucket(monkeypatch):
    monkeypatch.setattr(storage_module.settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(storage_module.settings, "S3_BUCKET", None)
    get_storage.cache_clear()

    # Fails at startup rather than on the first upload: a misconfigured
    # bucket must not look like a healthy API that loses documents.
    with pytest.raises(RuntimeError):
        get_storage()
    get_storage.cache_clear()
