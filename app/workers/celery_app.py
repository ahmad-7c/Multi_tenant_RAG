from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "documind",
    broker=settings.CELERY_BROKER_URL or settings.REDIS_URL,
    backend=settings.CELERY_RESULT_BACKEND or settings.REDIS_URL,
    # Explicit, not autodiscovery: the worker must import the module that
    # defines the task, or it registers a name it cannot execute and every
    # message is rejected as "unregistered task".
    include=["app.workers.ingestion"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Celery 6 will stop retrying the broker connection at startup by
    # default; set explicitly so a worker that boots before Redis waits
    # instead of exiting.
    broker_connection_retry_on_startup=True,
    # ---- delivery semantics -------------------------------------------
    # Default Celery acks a message the moment a worker receives it, so a
    # worker killed mid-task loses the job silently. We want the opposite:
    # ack only after the task returns, so a crash causes REDELIVERY. This
    # is only safe because ingest_document is idempotent -- it deletes its
    # own chunks and redoes the work from scratch -- so running it twice
    # converges on the same state.
    task_acks_late=True,
    # Pairs with acks_late. Without it one worker prefetches a batch of
    # long ingestion jobs and sits on them while other workers idle; worse,
    # prefetched-but-unstarted messages are the ones most likely to be
    # redelivered after a crash.
    worker_prefetch_multiplier=1,
    # acks_late alone does NOT cover a hard kill (SIGKILL, OOM killer, pod
    # eviction). By default Celery marks such a task FAILED. This requeues
    # it instead -- which is the whole point of acks_late.
    task_reject_on_worker_lost=True,
    # ---- time limits ---------------------------------------------------
    # Soft limit raises SoftTimeLimitExceeded inside the task so it can
    # fail the document cleanly; the hard limit kills the worker process
    # 60s later if it ignored that. Without these, one hung embedding call
    # holds a concurrency slot forever.
    task_soft_time_limit=settings.CELERY_TASK_SOFT_TIME_LIMIT,
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT,
    # ---- Redis-specific: the acks_late trap ----------------------------
    # On a Redis broker there is no real "unacked" state. acks_late is
    # emulated with a visibility timeout: if a message is not acked within
    # it, Redis redelivers it. The default is 3600s, so a task that
    # legitimately runs longer than an hour gets handed to a SECOND worker
    # while the first is still running -- two workers ingesting one
    # document at once. It must therefore exceed the hard time limit, which
    # is the longest a task can possibly still be alive.
    broker_transport_options={
        "visibility_timeout": settings.CELERY_VISIBILITY_TIMEOUT,
    },
    # ---- hygiene -------------------------------------------------------
    # ingest_document returns None, so every result row is a dead key.
    # Without a TTL they accumulate in Redis forever.
    result_expires=settings.CELERY_RESULT_EXPIRES,
    # PyMuPDF holds native memory; recycling the child process bounds any
    # leak instead of letting a long-lived worker grow unbounded.
    worker_max_tasks_per_child=settings.CELERY_MAX_TASKS_PER_CHILD,
)
