"""What the application downloads at runtime, and what it must not.

Written after a real outage. api and worker were attached only to a
Docker network declared `internal: true`, which has no gateway, and the
first thing that failed was not an embedding call -- it was
`chunk_pages -> count_tokens -> tiktoken.get_encoding("cl100k_base")`,
which fetches its vocabulary from
openaipublic.blob.core.windows.net on first use. The ingest died with no
provider anywhere in the traceback.

Two properties are pinned here:

1. The encoding loads with NO network, given the cache the image bakes.
2. The set of hosts the application contacts at runtime is a closed,
   reviewed list -- so a new one cannot appear without this test failing.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
DOCKERFILE = ROOT / "Dockerfile"


# --------------------------------------------------------------------
# 1. The encoding loads offline
# --------------------------------------------------------------------


def test_the_token_encoding_loads_without_network(monkeypatch):
    """No HTTP call, given a populated cache.

    `requests.get` is the exact call tiktoken.load.read_file makes for an
    https blobpath, so raising from it is a faithful stand-in for having
    no route out. Patched BEFORE the lru_cache is cleared, so the
    encoding really is constructed again inside the block.

    STATED LIMIT: this proves "no download given a cache", not "the
    image has a cache". The cache's presence in the artefact is a
    Dockerfile property and is pinned separately below -- and was
    verified by running the built image with `--network none`.
    """
    import requests

    from app.services import chunking

    def _blocked(*args, **kwargs):
        raise AssertionError(f"tiktoken tried to download: {args!r}")

    monkeypatch.setattr(requests, "get", _blocked)
    monkeypatch.setattr(requests.Session, "get", _blocked)
    chunking._encoding_for.cache_clear()

    assert chunking.count_tokens("hello world") == 2
    # And the real path that failed on the server, not just the leaf.
    from types import SimpleNamespace

    pages = [SimpleNamespace(page_number=1, text="# Policy\n\nThirty days.")]
    assert chunking.chunk_pages(pages), "chunk_pages produced nothing offline"


def test_the_image_bakes_the_vocabulary():
    """The Dockerfile half of the property above.

    Four things have to be true together, and three of them fail
    silently: the cache dir has to be set with ENV (not just for one
    RUN, or runtime looks elsewhere and downloads again), something has
    to populate it, the result has to be readable by uid 10001, and the
    build has to FAIL if the download produced nothing -- otherwise an
    empty directory ships and the fetch moves back to runtime, where it
    only breaks on a box with no egress.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )

    assert "ENV TIKTOKEN_CACHE_DIR=" in code, "the cache dir is not set at runtime"
    assert "cl100k_base" in code, "nothing populates the cache"
    assert re.search(r'chown -R 10001:10001 "\$TIKTOKEN_CACHE_DIR"', code), (
        "the cache is not readable by the non-root runtime user"
    )
    assert re.search(r'test -n "\$\(ls -A "\$TIKTOKEN_CACHE_DIR"\)"', code), (
        "an empty cache would not fail the build"
    )
    # And the build proves it loads with the network blocked.
    assert "requests.get = _blocked" in text, (
        "the build does not verify the encoding loads offline"
    )


def test_the_encoding_production_uses_is_the_one_that_is_baked():
    """EMBEDDING_MODEL decides which encoding is loaded, indirectly.

    _encoding_for tries encoding_for_model(EMBEDDING_MODEL) and falls
    back to cl100k_base on KeyError. gemini-embedding-001 is not a name
    tiktoken knows, so the fallback is what production takes -- and
    cl100k_base is therefore the one that MUST be in the image. If
    someone sets an OpenAI model that resolves to a third encoding, this
    test fails and names it.
    """
    import tiktoken

    from app.core.config import Settings

    model = Settings.model_fields["EMBEDDING_MODEL"].default
    try:
        needed = tiktoken.encoding_for_model(model).name
    except KeyError:
        needed = "cl100k_base"

    baked = re.findall(r'"(\w+_base)"', DOCKERFILE.read_text(encoding="utf-8"))
    assert needed in baked, (
        f"EMBEDDING_MODEL={model} needs the {needed!r} encoding, and the "
        f"image only bakes {sorted(set(baked))}"
    )


# --------------------------------------------------------------------
# 2. The closed list of outbound hosts
# --------------------------------------------------------------------

#: Every host the running application may contact, and why.
#:
#: Audited rather than assumed: grepped for huggingface, transformers,
#: sentence_transformers, nltk, spacy, torch, from_pretrained, hf_hub,
#: urlretrieve and wget across app/ -- zero hits. No model or corpus is
#: fetched at runtime.
ALLOWED_RUNTIME_HOSTS = {
    # Embeddings and answers.
    "generativelanguage.googleapis.com",
    # Reranking. RERANKER_BASE_URL is configurable, so an operator can
    # point this at Jina or Voyage; the DEFAULT is what is pinned here.
    "api.cohere.com",
}

#: Not outbound at all: these appear in comments and in CORS examples.
NON_OUTBOUND_HOSTS = {"localhost", "x"}

#: Reached only at BUILD time, never at runtime -- the whole point of
#: baking the vocabulary. Listed so the audit records that it was
#: considered rather than missed.
BUILD_TIME_ONLY_HOSTS = {"openaipublic.blob.core.windows.net"}


def test_no_unexpected_outbound_host_appears_in_the_application():
    """A closed list, so a new dependency cannot quietly add a hop.

    The failure this guards against is not malice, it is convenience: a
    library that fetches something helpful on first use turns an
    air-gapped-ish deployment into one that needs a fourth party up. The
    tiktoken download was exactly that, and nothing in the repo
    mentioned it.
    """
    found: dict[str, list[str]] = {}
    for path in APP.rglob("*.py"):
        for match in re.finditer(r"https?://([a-zA-Z0-9._-]+)", path.read_text(encoding="utf-8")):
            host = match.group(1)
            found.setdefault(host, []).append(str(path.relative_to(ROOT)))

    unexpected = {
        host: sorted(set(files))
        for host, files in found.items()
        if host not in ALLOWED_RUNTIME_HOSTS and host not in NON_OUTBOUND_HOSTS
    }
    assert not unexpected, (
        "new outbound host(s) in app/. If this is a real runtime "
        f"dependency, add it to ALLOWED_RUNTIME_HOSTS with a reason: {unexpected}"
    )


def test_the_providers_are_still_the_two_hosts_we_think_they_are():
    """Pinned against Settings, not against a remembered string.

    A base URL default changed in config.py without this list being
    updated would otherwise leave the audit above asserting a host the
    application no longer uses.
    """
    from urllib.parse import urlparse

    from app.core.config import Settings

    for field in ("GEMINI_BASE_URL", "RERANKER_BASE_URL"):
        default = Settings.model_fields[field].default
        host = urlparse(default).hostname
        assert host in ALLOWED_RUNTIME_HOSTS, f"{field} points at {host}"


@pytest.mark.parametrize(
    "library",
    ["transformers", "sentence_transformers", "nltk", "spacy", "torch", "huggingface_hub"],
)
def test_no_model_downloading_library_is_imported(library):
    """None of these is a dependency, and none should become one quietly.

    Each downloads weights or corpora on first use. Adding any of them
    reintroduces the failure mode this file exists for, in a place where
    the traceback would again name no provider.
    """
    hits = [
        str(path.relative_to(ROOT))
        for path in APP.rglob("*.py")
        if re.search(rf"^\s*(import|from)\s+{library}\b", path.read_text(encoding="utf-8"), re.M)
    ]
    assert not hits, f"{library} is imported in {hits}"


def test_boto3_is_only_reachable_through_the_unused_s3_backend():
    """The one conditional outbound dependency, kept conditional.

    boto3 is installed and would contact AWS if STORAGE_BACKEND=s3. It
    is imported INSIDE the S3 client factory, so `local` -- what
    production runs -- never touches it. A module-level import would make
    every deployment depend on a library it does not use.
    """
    storage = (APP / "services" / "storage.py").read_text(encoding="utf-8")
    module_level = re.findall(r"^import boto3|^from boto3", storage, re.M)
    assert not module_level, "boto3 is imported at module level"
    assert re.search(r"^\s+import boto3", storage, re.M), "boto3 import not found"
