"""The production compose file's safety properties, pinned.

The dev compose file publishes 5434, 5433, 6379 and 8000 to the host.
That is correct for a laptop and would be an open Postgres on a public
IPv4. The two files are edited side by side, which is exactly how a
`ports:` block gets copied into the wrong one.

These tests read the FILE rather than a running stack, deliberately:
the property is "this file cannot publish a database", and a test that
needed Docker would be skipped on every machine where Docker is not up
-- including CI, which is where it has to hold.

A running-stack check (`docker compose ps --format`, an external port
scan) proves something different and stronger, and is listed in the D1
report as not yet done.
"""
import re
from pathlib import Path

import pytest
import yaml

# Reads repository files that are absent from the runtime image.
pytestmark = pytest.mark.repo

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.prod.yml"

#: The only service allowed to publish, and the only ports it may use.
EDGE_SERVICE = "caddy"
ALLOWED_PUBLISHED = {"80", "443"}

#: Must never be reachable from the host or the internet.
NEVER_PUBLISHED = ("db", "redis")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def services(compose) -> dict:
    return compose["services"]


def test_the_file_parses(services):
    """The control: every assertion below is vacuous if the file is
    empty or the service names changed."""
    assert set(services) >= {"caddy", "api", "worker", "migrate", "db", "redis"}


def test_only_the_edge_service_publishes_ports(services):
    publishers = sorted(n for n, s in services.items() if s.get("ports"))
    assert publishers == [EDGE_SERVICE], (
        f"these services publish ports to the host: {publishers}. "
        f"Only {EDGE_SERVICE} may."
    )


def test_the_edge_publishes_only_http_and_https(services):
    published = set()
    for entry in services[EDGE_SERVICE]["ports"]:
        # "80:80", "443:443", or the long form.
        host = str(entry).split(":")[0] if not isinstance(entry, dict) else str(
            entry.get("published")
        )
        published.add(host)
    assert published == ALLOWED_PUBLISHED, published


@pytest.mark.parametrize("name", NEVER_PUBLISHED)
def test_the_datastores_publish_nothing(services, name):
    """Stated per service as well as collectively.

    The collective test above would also pass if `db` were renamed,
    silently dropping the service this rule exists for.
    """
    assert "ports" not in services[name], (
        f"{name} must never be reachable from the host"
    )
    assert "expose" not in services[name] or services[name].get("expose"), (
        "expose is container-network only and harmless, but an empty "
        "block suggests an edit in progress"
    )


@pytest.mark.parametrize("name", NEVER_PUBLISHED)
def test_the_datastores_are_on_the_internal_network_only(services, name):
    """Belt and braces, and the stronger of the two.

    Absence of `ports:` depends on nobody adding one. Membership of a
    network declared `internal: true` is a property of the topology:
    Docker gives that network no gateway, so even a service that did
    publish a port could not route out.
    """
    assert services[name]["networks"] == ["internal"], services[name]["networks"]


def test_the_internal_network_really_is_internal(compose):
    internal = compose["networks"]["internal"]
    assert internal.get("internal") is True, (
        "without `internal: true` the network has a gateway and the "
        "per-service network lists above prove nothing"
    )


def test_the_edge_is_the_only_service_on_both_networks(services):
    on_edge = sorted(n for n, s in services.items() if "edge" in (s.get("networks") or []))
    assert on_edge == [EDGE_SERVICE], on_edge


# ----------------------------------------------- data that must live ----


def test_uploads_is_a_named_volume_shared_by_api_and_worker(compose, services):
    """D-1. The api writes document bytes and the worker reads them
    back by storage_key, so a per-service volume means every ingestion
    fails on a missing file -- and only for files the other container
    happened to write."""
    assert "uploads" in compose["volumes"]
    for name in ("api", "worker"):
        mounts = services[name]["volumes"]
        assert any(str(m).startswith("uploads:") for m in mounts), (
            f"{name} does not mount the shared uploads volume"
        )


def test_the_database_data_is_on_a_named_volume(compose, services):
    assert "db_data" in compose["volumes"]
    assert any(str(m).startswith("db_data:") for m in services["db"]["volumes"])


def test_caddy_certificates_persist(compose):
    """Without this volume every restart re-requests a certificate and
    hits Let's Encrypt's rate limit within a day."""
    assert "caddy_data" in compose["volumes"]


# ------------------------------------------------ operational rules ----


def test_migrations_run_before_the_api_and_worker_start(services):
    """Never inside the api container's startup: two api containers
    would race the same migration, and a failed migration would present
    as a crash-looping api rather than a job that exited non-zero."""
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert services["migrate"].get("restart") == "no", (
        "a restarting migrate job retries a failed migration forever"
    )
    for name in ("api", "worker"):
        dep = services[name]["depends_on"]["migrate"]
        assert dep["condition"] == "service_completed_successfully"


def test_redis_never_evicts(services):
    """Redis holds the Celery BROKER here as well as the answer cache.
    Under an LRU policy it would evict queued ingestion tasks at the
    memory cap: uploads would silently never process, with nothing in
    any log to say why."""
    assert "--maxmemory-policy noeviction" in services["redis"]["command"]


def test_redis_requires_a_password(services):
    assert "--requirepass" in services["redis"]["command"]


def test_every_long_running_service_restarts_itself(services):
    for name in ("caddy", "api", "worker", "db", "redis"):
        assert services[name].get("restart") == "unless-stopped", name


def test_every_service_has_a_memory_limit(services):
    """One service leaking must not take the 2 GB box down with it."""
    for name, spec in services.items():
        limit = spec.get("deploy", {}).get("resources", {}).get("limits", {})
        assert limit.get("memory"), f"{name} has no memory limit"


#: 2048M of RAM, less ~256M for the kernel, sshd, the Docker daemon
#: and page cache. The 2 GB swap file is a cushion for a spike, never
#: part of this budget: a Postgres backend that lands in swap is slower
#: than the query timeout it was trying to meet.
MEMORY_BUDGET_M = 1792


def test_the_memory_limits_fit_the_box(services):
    """Compose does not enforce a total, and nothing else notices when
    two services are each raised by 256M.

    This test found the first set of limits summing to 2208M on a
    2048M box. Over-commitment does not fail at deploy time -- it
    fails later, as the kernel OOM-killing whichever process asked
    for memory last, which during a deploy is the newcomer.

    migrate is COUNTED even though it is one-shot: it runs while the
    previous api and worker are still up, so a deploy is the real
    peak rather than the steady state.
    """
    per_service = {
        name: int(str(spec["deploy"]["resources"]["limits"]["memory"]).rstrip("M"))
        for name, spec in services.items()
    }
    total = sum(per_service.values())
    assert total <= MEMORY_BUDGET_M, (
        f"limits sum to {total}M, over the {MEMORY_BUDGET_M}M budget "
        f"for a 2048M box: {per_service}"
    )


def test_postgres_fits_inside_its_own_limit(services):
    """shared_buffers and maintenance_work_mem are allocated regardless
    of load, so the two of them alone must leave room for backends.

    maintenance_work_mem is the one that matters here: HNSW index
    builds use it, and a REINDEX is how this container discovers its
    limit the hard way.
    """
    command = " ".join(services["db"]["command"].split())
    settings = dict(
        part.split("=") for part in command.split() if "=" in part
    )
    fixed = int(settings["shared_buffers"].rstrip("MB")) + int(
        settings["maintenance_work_mem"].rstrip("MB")
    )
    limit = int(str(services["db"]["deploy"]["resources"]["limits"]["memory"]).rstrip("M"))
    assert fixed < limit * 0.75, (
        f"shared_buffers + maintenance_work_mem = {fixed}M of a {limit}M "
        "limit, leaving too little for connections"
    )


def test_logs_are_rotated(services):
    """Without rotation one container's logs fill the disk and every
    service dies at once, which reads as a hardware failure."""
    for name, spec in services.items():
        opts = spec.get("logging", {}).get("options", {})
        assert opts.get("max-size"), f"{name} has unrotated logs"


def test_the_hostname_is_not_hard_coded():
    """The public hostname must come from the environment, so the same
    file serves localhost and the real DuckDNS name."""
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert "{$API_HOSTNAME:localhost}" in caddyfile
    assert "duckdns.org" not in caddyfile.lower(), (
        "a hostname in the repo is one more place to edit per environment"
    )


def test_sse_buffering_is_disabled_at_the_edge(services):
    """flush_interval -1 is Caddy's low-latency mode. With buffering on,
    a streamed answer arrives as one block when it finishes -- which
    looks exactly like a slow model."""
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert "flush_interval -1" in caddyfile


def test_the_edge_body_limit_is_above_the_app_limit():
    """So an oversized upload is refused by the application, with a
    sentence a user can act on, rather than by the edge with a bare
    413 and no explanation."""
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert "max_size 5MB" in caddyfile

    example = (ROOT / ".env.production.example").read_text(encoding="utf-8")
    app_limit = next(
        int(line.split("=")[1])
        for line in example.splitlines()
        if line.startswith("MAX_UPLOAD_BYTES=")
    )
    assert app_limit < 5 * 1000 * 1000, app_limit


def test_the_edge_overwrites_forwarded_for():
    """D-3. The edge SETS X-Forwarded-For from the connection peer.

    This test used to assert the directive's ABSENCE, on the reasoning
    that Caddy already ignores an inbound X-Forwarded-For by default
    (it does -- the reverse_proxy docs say so in as many words) and
    that any directive could only weaken it. The reasoning was right
    about the default and wrong about the risk: `trusted_proxies`
    reverses that default, and this file shipped
    `trusted_proxies static private_ranges` for the whole of D1. A
    `header_up` on this handler runs regardless of that option, so the
    explicit line is what survives the mistake being made again.

    `{remote_host}` is `{http.request.remote.host}`, the direct
    connection source, which no client can influence. `header_up
    <field> <value>` replaces any existing values.

    This pins the FILE. The behaviour is proven separately by
    tests/test_edge_live.py, which sends a forged header through a
    running Caddy -- a file-level grep cannot tell a working directive
    from a typo'd one.
    """
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    directives = [
        line.strip()
        for line in caddyfile.splitlines()
        if not line.strip().startswith("#")
    ]
    assert "header_up X-Forwarded-For {remote_host}" in directives, (
        "the edge must overwrite X-Forwarded-For from the connection peer; "
        "an appended header puts a forged value leftmost, which is what "
        "client_ip reads"
    )


def test_no_trusted_proxies_at_the_edge():
    """The setting that would undo Caddy's own spoofing protection.

    Caddy ignores an inbound client-supplied X-Forwarded-For by
    default. `trusted_proxies` REVERSES that for peers inside the
    listed ranges -- Caddy then accepts and preserves whatever they
    sent. This file shipped `trusted_proxies static private_ranges`,
    which matters because depending on how Docker publishes a port the
    peer address can be the bridge gateway, itself a private address;
    every client on the internet would then be a trusted proxy.

    `client_ip` in app/api/limits.py reads the LEFTMOST entry, so a
    caller sending a fresh forged value per request gets a fresh empty
    bucket each time and credential stuffing meets no per-IP limit at
    all. TRUST_FORWARDED_FOR=true in production depends on this
    absence.

    Pinned separately from the `header_up` test below because they are
    two different ways to reach the same hole, and the Caddyfile's own
    comment claimed protection it had already given away one block up.
    """
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    directives = [
        line.strip()
        for line in caddyfile.splitlines()
        if not line.strip().startswith("#")
    ]
    assert not any("trusted_proxies" in line for line in directives), (
        "trusted_proxies makes Caddy trust a client-supplied "
        "X-Forwarded-For, which the per-IP login limiter reads"
    )


def test_the_server_header_is_removed():
    """P2 requires no server header. Caddy sets one by default.

    Unpinned until now: the rationale lived in the global options
    block, 25 lines from the directive, where it described
    `trusted_proxies` instead -- so the comment looked like coverage
    and nothing actually checked the header was stripped.
    """
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    directives = [
        line.strip()
        for line in caddyfile.splitlines()
        if not line.strip().startswith("#")
    ]
    assert "-Server" in directives, "the Caddyfile does not remove the Server header"


def test_the_edge_never_receives_an_empty_acme_email():
    """An EMPTY ACME_EMAIL makes the Caddyfile unparseable.

    Caddy's `{$VAR:default}` falls back only when the variable is
    UNSET. Set to the empty string, `email {$ACME_EMAIL:}` expands to a
    bare `email` with no argument and Caddy refuses the whole config:

        Error: parsing caddyfile tokens for 'email': wrong argument
        count or unexpected line ending after 'email'

    So the edge would not start at all -- not degrade, not warn. This
    file used to pass `${ACME_EMAIL:-}`, which hands over exactly that
    empty string, and `.env.production.example` ships ACME_EMAIL as a
    placeholder an operator may well leave blank.

    Compose's `:-` substitutes on unset OR empty, so a non-empty
    default here is what closes it. Found with `caddy validate`; every
    grep-based test in this repo passed against the broken version.
    """
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    line = next(
        line for line in compose.splitlines()
        if "ACME_EMAIL:" in line and not line.strip().startswith("#")
    )
    _, _, value = line.partition("ACME_EMAIL:")
    value = value.strip()
    assert value.startswith("${ACME_EMAIL:-"), value
    default = value[len("${ACME_EMAIL:-"):].rstrip("}")
    assert default, (
        "the compose default for ACME_EMAIL is empty; an empty value "
        "makes the Caddyfile unparseable and the edge will not start"
    )


def test_the_prod_project_name_does_not_collide_with_dev():
    """The two stacks must not share a compose project.

    docker-compose.yml has no explicit `name`, so its project comes
    from the directory, which normalises to "documind" for a checkout
    in a folder called DocuMind. Both files define db, redis, api and
    worker, and a shared project means a shared volume namespace:
    prod's `db_data` would resolve to `documind_db_data`, the volume
    the dev database already uses.

    `up -d` would then recreate the dev db container as the prod one on
    top of the dev data directory. Postgres ignores POSTGRES_PASSWORD
    when the data dir exists, so the outcome is an auth failure the app
    surfaces as a 500 -- or a stack labelled production quietly serving
    development data.

    Caught by inspecting the resolved config before running `up`, not
    by running it.
    """
    compose = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    name = next(
        line.split(":", 1)[1].strip()
        for line in compose.splitlines()
        if line.startswith("name:")
    )
    assert name, "docker-compose.prod.yml sets no project name"
    # The dev project is the directory name, lowercased with separators
    # stripped by compose. Anything equal to it shares volumes.
    dev_project = re.sub(r"[^a-z0-9]", "", ROOT.name.lower())
    assert re.sub(r"[^a-z0-9]", "", name.lower()) != dev_project, (
        f"the prod project name ({name}) collides with the dev project "
        f"derived from the directory ({dev_project}); they would share "
        "the db_data and uploads volumes"
    )
