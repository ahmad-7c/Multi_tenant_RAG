"""Running Alembic in-process must not silence the application.

logging.config.fileConfig() defaults to disable_existing_loggers=True,
which disables every logger that already exists at the moment it runs.
Alembic's env.py calls it, and the test harness (plus any "migrate on
startup" entrypoint) drives Alembic in-process AFTER the app modules
have been imported -- so every app.* logger is already created and gets
switched off.

The symptom is brutal to diagnose: the application silently stops
logging the instant it runs a migration, which is precisely when you
most want output. It was found because three LLM tests passed alone and
failed in the full suite.
"""
import logging


def test_app_loggers_still_emit_after_alembic_has_run(engine, caplog):
    """`engine` is the session fixture that runs Alembic to head."""
    logger = logging.getLogger("app.services.llm")

    assert not logger.disabled, (
        "app.services.llm was disabled by Alembic's fileConfig call; "
        "env.py needs disable_existing_loggers=False"
    )

    with caplog.at_level(logging.WARNING, logger="app.services.llm"):
        logger.warning("canary")

    assert [r for r in caplog.records if r.getMessage() == "canary"]


def test_env_py_passes_disable_existing_loggers_false():
    from pathlib import Path

    source = Path("alembic/env.py").read_text(encoding="utf-8")
    code = "\n".join(line.split("#")[0] for line in source.splitlines())

    assert "disable_existing_loggers=False" in code
