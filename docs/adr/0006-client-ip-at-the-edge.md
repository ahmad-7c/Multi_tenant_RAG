# 0006. Client IP at the edge, and the BFF problem

Date: 2026-11-05
Status: Accepted for the direct case. The BFF case is **recorded, not
implemented** (D-3 explicitly defers it).

## Context

Two things in this system key on a client IP address:

- the per-IP login and register rate limits, which exist because a
  per-ACCOUNT limit cannot see credential stuffing -- that attack tries
  one password against ten thousand accounts and never trips a counter
  on any single one of them;
- nothing else. No authorization decision reads the IP.

`client_ip` in `app/api/limits.py` reads `X-Forwarded-For` when
`TRUST_FORWARDED_FOR` is set, taking the **leftmost** entry, which is
the conventional position for the original client.

An unconditionally trusted `X-Forwarded-For` is strictly worse than no
header at all: the caller sets a different value per request and
receives a fresh, empty bucket each time, so the limit protects
nothing while continuing to look like it works.

## Decision, for the direct case

`TRUST_FORWARDED_FOR=true` in production, **and only** together with
an edge that overwrites the header:

```
header_up X-Forwarded-For {remote_host}
```

`{remote_host}` is `{http.request.remote.host}` -- the direct
connection peer, which no client can influence -- and
`header_up <field> <value>` replaces any existing values for the
field. Verified against
`caddyserver.com/docs/caddyfile/directives/reverse_proxy` and
`/docs/caddyfile/concepts` (2026-11).

### Why write it explicitly when Caddy already does this

Caddy ignores inbound `X-Forwarded-*` by default, specifically to
prevent spoofing, and its docs recommend configuring real-client-IP
parsing through the global `trusted_proxies` option rather than per
proxy. The explicit directive is still written, for one reason:
`trusted_proxies` **reverses** the default for peers inside its
ranges, and this repository shipped
`trusted_proxies static private_ranges` in the same file that carried
a comment asserting the header could not be spoofed.

That combination was live for the whole of D1. It mattered more than
it looks: depending on how Docker publishes a port, the peer address
can be the bridge gateway -- itself a private address -- which would
have made every client on the internet a trusted proxy.

A `header_up` on this handler runs regardless of the global option, so
the explicit line survives the mistake being made a second time. It is
defence in depth against an error already committed once here.

### How it is proven

Two layers, deliberately different in kind:

- `tests/test_prod_compose.py::test_the_edge_overwrites_forwarded_for`
  pins the directive's presence, and
  `::test_no_trusted_proxies_at_the_edge` pins the absence of the
  option that would undo it. Both read the file.
- `tests/test_edge_live.py` sends a **rotating forged header** through a
  running Caddy and asserts the per-IP limiter still trips. A file grep
  cannot tell a working directive from a typo or an unrecognised
  placeholder; this observes the consequence.

The live test keys on the limiter rather than on a reflected header,
because no endpoint echoes the client IP and adding one would ship a
debug surface to production to make a test easier.

## The BFF problem, recorded and NOT solved

Through the Next.js BFF the browser never talks to this API. Next does,
server-side. So the peer Caddy sees is the **BFF host**, and
`{remote_host}` is the BFF's address for every end user alive.

Consequences, stated plainly:

- the per-IP login limit becomes a per-DEPLOYMENT limit. Every user of
  the product shares one bucket.
- that is a correctness problem in both directions: credential stuffing
  through the BFF is limited only in aggregate, and one abusive client
  can exhaust the bucket for every legitimate user -- a denial of
  service against everybody by one caller.
- it is invisible. Nothing fails; the limiter keeps returning decisions
  that look reasonable.

The current deployment is direct (no BFF in front of this API in
production yet), which is why this is deferred rather than urgent. It
must not be forgotten when P9 lands.

### Proposed design (do not implement without the human)

The BFF sends the end-user address in a **dedicated header**, and the
backend trusts that header only when an accompanying shared secret
matches:

- `X-DocuMind-Client-IP: <end user address>`
- `X-DocuMind-Edge-Auth: <shared secret>`

with, on the backend:

- the secret compared in **constant time** (`hmac.compare_digest`), so
  the comparison cannot be turned into a byte-at-a-time oracle by
  timing it;
- a new setting, absent by default, so the header is ignored entirely
  unless an operator configures a secret. Absent must mean "ignore",
  never "accept";
- the secret held only in the server env file and in Vercel's
  server-side environment, never in a `NEXT_PUBLIC_` variable;
- precedence fixed and tested: when the header is present and the
  secret matches, it wins over `X-Forwarded-For`; when it is present
  and the secret does NOT match, the request is treated as if the
  header were absent and the event is logged once -- not rejected,
  because a rotation window would otherwise take the product down;
- Caddy must **strip both headers inbound** (`header_up -X-DocuMind-*`
  or an equivalent), so a direct caller cannot present them at all.
  Without that strip the secret is the only thing standing between a
  stranger and a forged client IP, and it moves from defence in depth
  to a single point of failure.

A dedicated header rather than extending `X-Forwarded-For` because the
two have different trust stories, and overloading one field with
"sometimes trusted, sometimes not" is how the current bug class is
born in the first place.

### Alternatives considered

- **Trust `X-Forwarded-For` from the BFF's address only.** Rejected:
  Vercel function egress addresses are not stable or documented as a
  fixed set, so the allowlist would be wrong by the time it was
  written, and wrong in the fail-open direction.
- **Drop per-IP limiting behind the BFF and limit per account.**
  Rejected for the reason the per-IP limit exists: a per-account limit
  cannot see credential stuffing. This would remove the only control
  that can.
- **mTLS between the BFF and the backend.** Stronger, and a real
  option later. Rejected for now as disproportionate: it adds
  certificate lifecycle management to a single-instance Compose
  deployment, which is the kind of scope the Phase 5 rules exclude.

## Consequences

- Direct clients: the limiter is sound, and proven by a live test.
- Through a BFF: the limiter degrades to per-deployment and MUST be
  revisited at P9. This ADR is the record that it was understood rather
  than missed.
- `TRUST_FORWARDED_FOR=true` is safe **only** while Caddy is the sole
  ingress. Publishing the api port alongside it reopens the spoof, and
  `docker-compose.prod.yml` deliberately gives `api` no `ports:` key.

## Resolved at P9 (2026-11-08)

Implemented, with the shape this ADR proposed and one simplification it
did not anticipate.

- The BFF sends `X-Client-IP` plus `X-Client-IP-Secret`; the backend
  trusts the address only when `CLIENT_IP_SECRET` is configured and
  `hmac.compare_digest` matches, then validates it with
  `ipaddress.ip_address` and caps its length. Any failure falls through
  to the previous behaviour, so **unset means off** rather than
  "trusted because unconfigured".
- `forwardableHeaders` strips both headers INBOUND before the route sets
  them. Strip then set, in that order: without it a browser could
  nominate its own bucket, which is worse than no per-IP limit because
  the limit would still look enforced.

**The simplification: no Caddyfile change was needed.** The proposal
assumed the edge would have to be taught to pass a real client address
through. It does not, because the header has a different NAME. Caddy
rewrites only `X-Forwarded-For` (`header_up X-Forwarded-For
{remote_host}`) and forwards unknown headers untouched, so `X-Client-IP`
arrives intact. Choosing a new name instead of reusing `X-Forwarded-For`
turned a three-component change into a two-component one and left the
edge config — the part with the spoofing history — alone.

**Which platform header to read, and why it is not spoofable.** Vercel's
request-headers reference says of `x-forwarded-for`: "we currently
overwrite the X-Forwarded-For header and do not forward external IPs.
This restriction is in place to prevent IP spoofing." Custom
`X-Forwarded-For` is purchasable as a Trusted Proxy on Enterprise plans
only. `x-vercel-forwarded-for` is documented as identical to it "However,
x-forwarded-for could be overwritten if you're using a proxy on top of
Vercel", and `x-real-ip` as simply "identical". So the BFF prefers
`x-vercel-forwarded-for` — the one that stays correct if a CDN is ever
put in front — and falls back to the other two. Verified against
vercel.com/docs/headers/request-headers (last updated 2025-12-13).

**Two defences no functional test can observe**, each pinned by reading
the source instead: the constant-time comparison (`==` returns the same
booleans, and swapping it left the whole suite green) and the length cap
(redundant with the address parse for any input anyone can construct).
Same reasoning as B1's doubly-nulled citation score — where two defences
are redundant, each needs its own pin or the layer that goes is the one
nobody notices.

The alternatives above stand as recorded. mTLS remains the stronger
option and is still disproportionate for a single-instance Compose
deployment.

**Not verified live.** The code is tested and the suite is green, but
nothing has run against the real Vercel deployment. `deploy/runbook.md`
section 12.7 gives the three checks that would establish it, including
the one that matters: eleven forged-header logins from one address must
still reach 429.
