#!/usr/bin/env bash
# Start the API for end-to-end tests. Playwright's webServer runs this.
set -e
cd "$(dirname "$0")/.."
. scripts/e2e_env.sh
./venv/Scripts/python.exe -m alembic upgrade head
exec ./venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8020
