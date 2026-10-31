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
