"""Apply ONE mutation by name, so the harness script stays readable.

Each mutation is a plausible mistake, not vandalism: the kind of edit a
reviewer would wave through. If the suite still passes, the tests do not
actually pin the behaviour they claim to.
"""
import io
import sys

MUTATIONS = {}


def mutation(name):
    def wrap(fn):
        MUTATIONS[name] = fn
        return fn

    return wrap


def edit(path, old, new, count=1):
    s = io.open(path, encoding="utf-8").read()
    if old not in s:
        raise SystemExit(f"MUTATION TARGET NOT FOUND in {path}: {old[:70]!r}")
    io.open(path, "w", encoding="utf-8").write(s.replace(old, new, count))


# 1. The limiter forgets the org: one global bucket for every tenant.
@mutation("limiter-drops-org")
def _():
    edit(
        "app/services/rate_limit.py",
        'def answer_key(org_id: str) -> str:\n    return f"{PREFIX}answer:org:{org_id}"',
        'def answer_key(org_id: str) -> str:\n    return f"{PREFIX}answer"',
    )
    edit(
        "app/services/rate_limit.py",
        'def default_key(org_id: str) -> str:\n    return f"{PREFIX}default:org:{org_id}"',
        'def default_key(org_id: str) -> str:\n    return f"{PREFIX}default"',
    )


# 2. The bucket is no longer atomic: a read, then a write, as two calls.
@mutation("limiter-not-atomic")
def _():
    edit(
        "app/services/rate_limit.py",
        """        try:
            allowed, remaining, retry_after = await self._client.eval(
                TOKEN_BUCKET_LUA, 1, key, rate, float(burst), float(cost), ttl
            )
        except Exception as exc:""",
        """        try:
            # MUTATION: read-modify-write instead of one atomic script.
            import time as _t

            raw = await self._client.hgetall(key)
            now = _t.time()
            tokens = float(raw.get("tokens", burst)) if raw else float(burst)
            ts = float(raw.get("ts", now)) if raw else now
            tokens = min(float(burst), tokens + max(0.0, now - ts) * rate)
            if tokens >= cost:
                allowed, tokens, retry_after = 1, tokens - cost, 0.0
            else:
                allowed, retry_after = 0, (cost - tokens) / rate
            await self._client.hset(key, mapping={"tokens": tokens, "ts": now})
            await self._client.expire(key, ttl)
            remaining = tokens
        except Exception as exc:""",
    )


# 3. The confidence gate is skipped: every question gets an answer.
@mutation("skip-the-gate")
def _():
    import re

    path = "app/services/reranker.py"
    s = io.open(path, encoding="utf-8").read()
    match = re.search(r"def should_answer\(([^)]*)\)[^:]*:\n", s)
    if not match:
        raise SystemExit("should_answer not found")
    insert_at = match.end()
    io.open(path, "w", encoding="utf-8").write(
        s[:insert_at] + "    return True  # MUTATION\n" + s[insert_at:]
    )


# 4. The usage query forgets the tenant filter.
@mutation("usage-drops-org")
def _():
    edit(
        "app/services/usage_service.py",
        "        .where(UsageEvent.org_id == org_id)\n",
        "",
    )


# 5. Retry-After is dropped from the 429.
@mutation("ignore-retry-after")
def _():
    edit(
        "app/api/limits.py",
        '        headers={"Retry-After": str(retry_after)},\n',
        "",
    )


# 6. The SSE slot is released after the loop instead of in a finally,
#    so a client disconnect leaks it.
@mutation("leak-slot-on-disconnect")
def _():
    edit(
        "app/api/v1/routes/conversations.py",
        """        try:
            async for chunk in event_stream():
                yield chunk
        finally:
            await _release_slot()""",
        """        async for chunk in event_stream():
            yield chunk
        await _release_slot()  # MUTATION: not in a finally""",
    )


# 7. The usage endpoint stops checking for admin.
@mutation("usage-not-admin-only")
def _():
    edit(
        "app/api/v1/routes/usage.py",
        "    current_user: User = Depends(require_admin),",
        "    current_user: User = Depends(get_current_user),",
    )
    edit(
        "app/api/v1/routes/usage.py",
        "from app.api.deps import require_admin",
        "from app.api.deps import get_current_user, require_admin",
    )


# 8. Metering raises instead of swallowing, so a bookkeeping failure
#    becomes a lost answer.
@mutation("metering-raises")
def _():
    edit(
        "app/services/usage_service.py",
        """    except Exception:
        logger.warning(
            "could not record usage for org %s on %s; the request is "
            "unaffected and this interval will under-report",
            org_id,
            endpoint,
            exc_info=True,
        )""",
        """    except Exception:
        raise  # MUTATION
        logger.warning(
            "could not record usage for org %s on %s; the request is "
            "unaffected and this interval will under-report",
            org_id,
            endpoint,
            exc_info=True,
        )""",
    )


# 9. recall@k scores an unlabelled case as 0.0 instead of None.
@mutation("recall-zero-not-none")
def _():
    edit(
        "eval/metrics.py",
        """    if target.is_empty:
        return None
    if k <= 0:
        return 0.0""",
        """    if target.is_empty:
        return 0.0  # MUTATION
    if k <= 0:
        return 0.0""",
    )


# 10. The thought-part filter is removed.
@mutation("keep-thought-parts")
def _():
    edit(
        "app/services/llm.py",
        'if isinstance(part, dict) and part.get("thought") is not True',
        "if isinstance(part, dict)",
    )


# 11. The judge caches on the case-independent question only, so a
#     changed answer is served a stale verdict.
@mutation("judge-cache-ignores-answer")
def _():
    edit(
        "eval/judge.py",
        "    for part in (model, SYSTEM, question, context, answer):",
        "    for part in (model, SYSTEM, question, context):  # MUTATION",
    )


# 12. The run snapshot drops the golden set version.
@mutation("snapshot-drops-version")
def _():
    edit(
        "eval/runs.py",
        '        "golden_set_version": golden.version,\n',
        "",
    )


# ---------------------------------------------------------------- B1 ------
# The UI's trace chips and source panel. Each of these is a mistake that
# reads as correct: a flag dropped, a default flipped, a filter removed.


# 13. The score gate forgets that a FALLBACK ranking is RRF, not relevance.
#     The provider still advertises calibrated scores, so this looks right.
@mutation("b1-expose-score-under-fallback")
def _():
    edit(
        "app/services/reranker.py",
        "    return reranker.scores_are_calibrated and not used_fallback",
        "    return reranker.scores_are_calibrated  # MUTATION",
    )


# 14. Site 1 of the score gate: the Citation is built with the score
#     attached unconditionally.
@mutation("b1-validate-always-exposes-scores")
def _():
    edit(
        "app/services/citations.py",
        "    answer: str, blocks: list[ContextBlock], *, expose_scores: bool = False",
        "    answer: str, blocks: list[ContextBlock], *, expose_scores: bool = True",
    )


# 15. Site 2 of the score gate: the payload serialises whatever it was
#     given, which publishes a CACHED score on replay.
@mutation("b1-payload-ignores-score-gate")
def _():
    edit(
        "app/api/v1/routes/conversations.py",
        '        "score": citation.score if expose_score else None,',
        '        "score": citation.score,  # MUTATION',
    )


# 16. Every retrieved block ships its passage, not only the cited ones --
#     the shape of "while we are here, send the sources panel everything".
@mutation("b1-passages-for-uncited-sources")
def _():
    edit(
        "app/api/v1/routes/conversations.py",
        """        "citations": [
            _citation_payload(c, expose_score=expose_scores)
            for c in outcome.citations
        ],""",
        """        "citations": [
            _citation_payload(c, expose_score=expose_scores)
            for c in outcome.citations
        ]
        + [  # MUTATION: ship every retrieved block, cited or not.
            {
                "marker": b.marker,
                "document": b.filename,
                "document_id": str(b.document_id),
                "chunk_id": str(b.parent_id),
                "page": b.page_number,
                "snippet": b.content[:300],
                "passage": b.content[:1200],
                "matched_chunk_id": None,
                "score": None,
            }
            for b in (prepared.prompt.blocks if prepared.prompt else [])
            if b.marker not in {c.marker for c in outcome.citations}
        ],""",
    )


# 17. The passage's own fetch drops the tenant filter. Defence in depth
#     behind retrieval's filter, which is exactly why it is easy to argue
#     away in review as redundant.
@mutation("b1-passage-fetch-drops-org-filter")
def _():
    edit(
        "app/services/reranker.py",
        "            WHERE c.org_id = :org_id AND c.id = ANY(:ids)",
        "            WHERE c.id = ANY(:ids)  -- MUTATION",
    )
    edit(
        "app/services/reranker.py",
        '        {"org_id": str(org_id), "ids": list(best.keys())},',
        '        {"ids": list(best.keys())},  # MUTATION',
    )


# ------------------------------------------------------------- B1b --------
# The three tenant filters in retrieval.py, each removable on its own.
# Before B1b all three were mutually redundant AND individually unpinned:
# removing any one left the suite green.


# 18. The vector leg forgets the tenant.
@mutation("b1b-vector-leg-drops-org-filter")
def _():
    edit(
        "app/services/retrieval.py",
        """    WHERE org_id = :org_id
      AND parent_id IS NOT NULL
      AND embedding IS NOT NULL""",
        """    WHERE parent_id IS NOT NULL
      AND embedding IS NOT NULL""",
    )


# 19. The keyword leg forgets the tenant.
@mutation("b1b-keyword-leg-drops-org-filter")
def _():
    edit(
        "app/services/retrieval.py",
        """    WHERE org_id = :org_id
      AND parent_id IS NOT NULL
      AND content_tsv @@ websearch_to_tsquery(:ts_config, :query_text)""",
        """    WHERE parent_id IS NOT NULL
      AND content_tsv @@ websearch_to_tsquery(:ts_config, :query_text)""",
    )


# 20. Hydration trusts the ids it was handed.
@mutation("b1b-hydration-drops-org-filter")
def _():
    edit(
        "app/services/retrieval.py",
        "    WHERE c.org_id = :org_id AND c.id = ANY(:ids)",
        "    WHERE c.id = ANY(:ids)",
    )


# ==================================================================
# Phase 5 / D1. The production configuration gate.
#
# These matter more than most in this file, because the gate's whole
# value is that it FIRES. A gate that quietly stopped firing looks
# identical from outside: the process boots, serves, and nobody finds
# out until the day it should have refused.
# ==================================================================


# 21. The gate runs, collects nothing, reports success. The most
#     plausible regression of all: a refactor that keeps the signature
#     and loses the body.
@mutation("startup-gate-never-fires")
def _():
    edit(
        "app/core/startup_checks.py",
        """    problems: list[str] = []
""",
        """    problems: list[str] = []
    return problems  # MUTATION
""",
    )


# 22. The gate is wired in, but runs only OUTSIDE production -- the
#     exact inversion a hurried `if` rewrite produces.
@mutation("startup-gate-skips-production")
def _():
    edit(
        "app/core/startup_checks.py",
        """    if not is_production(settings):
        return""",
        """    if is_production(settings):
        return""",
    )


# 23. passthrough is accepted again. Reads as a tidy-up, because
#     `hosted` is checked two lines below and this looks redundant.
@mutation("startup-allows-passthrough-reranker")
def _():
    edit(
        "app/core/startup_checks.py",
        'if settings.RERANKER_PROVIDER == "passthrough":',
        "if False:  # MUTATION",
    )


# 24. The reranker key check loses its negation, so hosted-with-no-key
#     passes. One character.
@mutation("startup-allows-hosted-reranker-without-key")
def _():
    edit(
        "app/core/startup_checks.py",
        'if settings.RERANKER_PROVIDER == "hosted" and _absent(settings.RERANKER_API_KEY):',
        'if settings.RERANKER_PROVIDER == "hosted" and not _absent(settings.RERANKER_API_KEY):',
    )


# 25. Only the embedding use of the Gemini key is checked. The two
#     blocks are near-identical, so deleting one reads as
#     deduplication -- and the LLM would still have no key.
@mutation("startup-checks-gemini-key-for-embeddings-only")
def _():
    edit(
        "app/core/startup_checks.py",
        """    if settings.LLM_PROVIDER == "gemini" and _absent(settings.GEMINI_API_KEY):
        problems.append("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set")
""",
        "",
    )


# 26. Truthiness becomes `is not None`, so an empty `GEMINI_API_KEY=`
#     line in the env file now passes. Presence stops being checked
#     while the check still appears to be there.
@mutation("startup-accepts-an-empty-gemini-key")
def _():
    edit(
        "app/core/startup_checks.py",
        '    return not (value or "").strip()',
        "    return value is None  # MUTATION",
    )


# 27. Exact matches only; the prefix rule goes. So
#     "change-me-for-real-this-time" boots.
@mutation("startup-placeholder-prefix-rule-dropped")
def _():
    edit(
        "app/core/startup_checks.py",
        '    return normalised.startswith("change-me") or normalised.startswith("changeme")',
        "    return False  # MUTATION",
    )


# 28. The error names the offending secret, "to help the operator".
#     Boot behaviour is unchanged -- only the logs of every failed
#     deploy now contain a key.
@mutation("startup-error-echoes-the-secret")
def _():
    edit(
        "app/core/startup_checks.py",
        'f"JWT_SECRET_KEY is shorter than {MIN_JWT_SECRET_CHARS} characters"',
        'f"JWT_SECRET_KEY {secret!r} is shorter than {MIN_JWT_SECRET_CHARS} chars"',
    )


# 29. `and` becomes `or`, so EITHER budget at zero is refused. This
#     one fires too much rather than too little -- which is its own
#     failure, because a check that blocks a correct deploy is a check
#     someone removes.
@mutation("startup-refuses-one-disabled-budget")
def _():
    edit(
        "app/core/startup_checks.py",
        "if not settings.BUDGET_DAILY_GLOBAL_UNITS and not settings.BUDGET_DAILY_ORG_UNITS:",
        "if not settings.BUDGET_DAILY_GLOBAL_UNITS or not settings.BUDGET_DAILY_ORG_UNITS:",
    )


# 30. The interactive docs come back in production. Likeliest real
#     cause: someone debugging a deploy who wanted /docs for five
#     minutes and did not put it back.
@mutation("docs-exposed-in-production")
def _():
    edit(
        "app/main.py",
        "_docs_enabled = not is_production(settings)",
        "_docs_enabled = True  # MUTATION",
    )


# 31. The gate is no longer CALLED. The function, its tests and its
#     coverage all survive; only the wiring is gone, which is what
#     makes this the hardest of the set to notice by reading.
@mutation("gate-not-wired-into-lifespan")
def _():
    edit(
        "app/main.py",
        "    enforce_production_safety(settings)",
        "    pass  # MUTATION: gate not called",
    )


# 32. Readiness stops actually asking the database. The endpoint, its
#     route and its 503 branch all survive; only the statement that
#     could fail is gone -- so it answers "ready" for a process with no
#     database, which is the exact condition it was added to catch.
@mutation("readiness-never-queries")
def _():
    edit(
        "app/main.py",
        '        db.execute(sql_text("SELECT 1"))',
        "        pass  # MUTATION: never asks the database",
    )


# 33. The container healthcheck goes back to /health. Nothing in Python
#     changes, every readiness test but one still passes, and the deploy
#     silently loses its rollback trigger. This is the original defect.
@mutation("healthcheck-targets-health-not-ready")
def _():
    edit(
        "Dockerfile",
        "c.request('GET','/health/ready')",
        "c.request('GET','/health')",
    )


# 34. `trusted_proxies` returns to the edge. Likeliest real cause: a
#     copy-pasted Caddy snippet for a deployment that genuinely sits
#     behind a load balancer. It makes a client-supplied
#     X-Forwarded-For trusted, and the per-IP login limiter reads it.
@mutation("edge-trusts-forwarded-for")
def _():
    # Tabs built from chr() rather than written as escapes: the
    # Caddyfile is tab-indented and an editor that helpfully converts
    # them would make this mutation silently un-appliable.
    tab = chr(9)
    edit(
        "deploy/Caddyfile",
        tab + "email {$ACME_EMAIL:}",
        tab + "email {$ACME_EMAIL:}" + chr(10)
        + tab + "servers {" + chr(10)
        + tab + tab + "trusted_proxies static private_ranges" + chr(10)
        + tab + "}",
    )


# 35. The Server header is advertised again -- a one-line deletion in a
#     header block nobody re-reads.
@mutation("edge-advertises-its-server")
def _():
    tab = chr(9)
    edit("deploy/Caddyfile", chr(10) + tab + tab + "-Server", "")


# 36. The .gitignore negation is dropped, so `.env.production.example`
#     is swallowed by the blanket `.env.*` rule. Passes on any machine
#     that already has the file; fails only on a fresh clone, i.e. CI.
@mutation("prod-example-falls-back-into-gitignore")
def _():
    edit(
        ".gitignore",
        "!.env.production.example",
        "# MUTATION: !.env.production.example",
    )


# 37. The CORS gate stops recognising loopback origins. The check, its
#     constant and its message all remain; only the comparison goes --
#     which is what a refactor of _origin_host would plausibly break.
@mutation("cors-gate-ignores-loopback")
def _():
    edit(
        "app/core/startup_checks.py",
        "        if _origin_host(origin) in LOOPBACK_HOSTS:",
        "        if False:  # MUTATION",
    )


if __name__ == "__main__":
    name = sys.argv[1]
    if name == "--list":
        for key in MUTATIONS:
            print(key)
        raise SystemExit(0)
    if name not in MUTATIONS:
        raise SystemExit(f"unknown mutation {name}; known: {sorted(MUTATIONS)}")
    MUTATIONS[name]()
    print(f"applied mutation: {name}")
