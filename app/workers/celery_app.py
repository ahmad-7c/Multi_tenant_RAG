from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "documind",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.workers.ingestion"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    # Default Celery acks a task the moment a worker receives it. Combined
    # with the ingestion task's idempotent "delete existing chunks, redo
    # the work" design, we want the opposite: ack only after the task
    # finishes, so a worker that crashes mid-ingestion gets its task
    # redelivered instead of silently dropped.
    task_acks_late=True,
    # Pairs with acks_late: without this a worker can prefetch several
    # long-running ingestion tasks at once and sit on them instead of
    # letting idle workers pick them up.
    worker_prefetch_multiplier=1,
)
