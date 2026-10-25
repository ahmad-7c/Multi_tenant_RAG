"""Celery configuration tests.

Config is not usually worth testing, but these particular settings are
load-bearing for correctness rather than performance: get them wrong and
the symptom is a duplicated or silently-dropped ingestion, not an error.
"""
from app.core.config import settings
from app.workers.celery_app import celery_app


def test_acks_late_is_enabled():
    # Ack after the task returns, so a crashed worker gets the task
    # redelivered rather than losing it.
    assert celery_app.conf.task_acks_late is True


def test_prefetch_is_one():
    assert celery_app.conf.worker_prefetch_multiplier == 1


def test_task_is_requeued_when_the_worker_is_lost():
    # acks_late does not cover SIGKILL/OOM on its own: without this Celery
    # marks such a task FAILED instead of requeueing it.
    assert celery_app.conf.task_reject_on_worker_lost is True


def test_visibility_timeout_exceeds_the_hard_time_limit():
    """The single most important number in this file.

    On Redis, acks_late is emulated with a visibility timeout. If it is
    shorter than the longest a task can still be alive, Redis redelivers
    the message to a second worker while the first is still running -- two
    workers ingesting one document concurrently.
    """
    visibility = celery_app.conf.broker_transport_options["visibility_timeout"]
    assert visibility > celery_app.conf.task_time_limit


def test_soft_time_limit_is_below_the_hard_limit():
    # The soft limit must fire first so the task gets a chance to mark the
    # document failed before the process is killed.
    assert celery_app.conf.task_soft_time_limit < celery_app.conf.task_time_limit


def test_results_expire():
    # ingest_document returns None, so every result is a dead key; without
    # a TTL they accumulate in Redis indefinitely.
    assert celery_app.conf.result_expires == settings.CELERY_RESULT_EXPIRES
    assert celery_app.conf.result_expires > 0


def test_worker_recycles_child_processes():
    assert celery_app.conf.worker_max_tasks_per_child > 0


def test_only_json_is_accepted():
    # pickle would let a compromised broker execute arbitrary code in the
    # worker.
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.task_serializer == "json"


def test_ingestion_task_is_registered_under_its_declared_name():
    # The producer enqueues by name. If the worker has not imported the
    # defining module, the name resolves to nothing and every message is
    # rejected as an unregistered task.
    assert "app.workers.ingestion.ingest_document" in celery_app.tasks
