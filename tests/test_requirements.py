"""requirements.txt must cover what the app imports to BOOT.

This file exists because of one outage that no other test could see.
`python-multipart` was installed in the developer's venv as a
transitive dependency and absent from requirements.txt. FastAPI raises
at IMPORT time -- not at request time -- when a route declares `Form`
or `UploadFile` without it, so:

  - every local run passed, because the venv had it;
  - the whole test suite passed, for the same reason;
  - the production image could not `import app.main` at all.

Found by building the image and importing the app inside it, which is
now also a CI step. These tests are the cheap local half of that: they
fail in milliseconds and name the reason, instead of failing a deploy.
"""
import re
from pathlib import Path

import pytest

# Reads repository files that are absent from the runtime image.
pytestmark = pytest.mark.repo

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"


def _pins() -> dict[str, str]:
    """Package name -> pinned version, for uncommented lines only."""
    pins = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9._-]+)(\[[^\]]+\])?==(.+)$", line)
        if match:
            pins[match.group(1).lower()] = match.group(3).strip()
    return pins


def test_python_multipart_is_pinned():
    """Required by the upload route, and it fails at import time.

    Do not remove this pin because nothing imports `multipart` by name:
    nothing does. FastAPI imports it on your behalf while BUILDING the
    route, and the failure is a RuntimeError during module import.
    """
    assert "python-multipart" in _pins(), (
        "python-multipart is not pinned; the upload route declares "
        "UploadFile/Form and FastAPI raises at import time without it"
    )


def test_the_upload_route_still_needs_it():
    """The other half of the pair.

    If the upload endpoint ever stops using multipart, the test above
    becomes cargo cult. This asserts the reason still holds, so the pin
    is justified by the code rather than by this file's existence.
    """
    source = (ROOT / "app" / "api" / "v1" / "routes" / "documents.py").read_text(
        encoding="utf-8"
    )
    assert "UploadFile" in source or "Form(" in source, (
        "no multipart usage found; re-justify or drop the python-multipart pin"
    )


def test_every_requirement_is_pinned_exactly():
    """No ranges, no carets.

    An unpinned dependency means the image built today and the image
    built tomorrow are different artefacts, which makes a rollback to a
    SHA a rollback to something that was never tested.
    """
    loose = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            loose.append(line)
    assert loose == [], f"unpinned requirements: {loose}"
