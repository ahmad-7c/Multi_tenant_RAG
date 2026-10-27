"""Generate the golden set's PDF documents from source.

The PDFs are build artefacts, not hand-committed binaries. A corpus you
cannot read in a diff is a corpus nobody will correct, and "why does the
per-diem case expect page 2" becomes unanswerable the moment the only
copy of the layout is inside a PDF.

What these exercise that a markdown fixture cannot:
  * real PyMuPDF extraction, including its line ordering
  * page numbers that reach a user-facing citation
  * running headers/footers, which cleaning.py must strip -- they sit in
    the top/bottom CLEAN_EDGE_LINES of every page, are under
    CLEAN_MAX_HEADER_CHARS, and appear on far more than
    CLEAN_REPEAT_RATIO of pages, so detection should fire
  * a table and a fenced code block surviving extraction intact

Four pages each, because CLEAN_MIN_PAGES_FOR_HEADER_DETECTION is 3: with
fewer there is no way to tell a running header from a line that
legitimately appears twice, and the stripping deliberately does not run.

    venv/Scripts/python.exe -m eval.build_corpus
"""
import os

import pymupdf

CORPUS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden", "corpus")

# Monospace throughout. Not for looks -- a fixture whose extracted line
# breaks depend on proportional-font wrapping is a fixture that changes
# meaning when PyMuPDF changes its text layout heuristics.
FONT = "cour"
FONT_SIZE = 10
LINE_HEIGHT = 14
MARGIN_X = 54
TOP_Y = 56
FOOTER_Y = 756


def _write_pdf(path: str, header: str, footer_template: str, pages: list[list[str]]) -> None:
    document = pymupdf.open()
    total = len(pages)
    for index, lines in enumerate(pages, start=1):
        page = document.new_page(width=612, height=792)
        # Running header: first line of every page.
        page.insert_text(
            (MARGIN_X, TOP_Y), header, fontname=FONT, fontsize=FONT_SIZE
        )
        y = TOP_Y + LINE_HEIGHT * 2
        for line in lines:
            page.insert_text((MARGIN_X, y), line, fontname=FONT, fontsize=FONT_SIZE)
            y += LINE_HEIGHT
        # Running footer: last line of every page, with a page number, so
        # _normalise_for_comparison's digit collapsing is what makes it
        # detectable at all ("Page 1 of 4" vs "Page 2 of 4").
        page.insert_text(
            (MARGIN_X, FOOTER_Y),
            footer_template.format(page=index, total=total),
            fontname=FONT,
            fontsize=FONT_SIZE,
        )
    document.save(path)
    document.close()


TRAVEL_HEADER = "Acme Corporation - Internal Use Only"
TRAVEL_FOOTER = "Travel and Expense Policy v4.2 - Page {page} of {total}"

TRAVEL_PAGES = [
    # ---- page 1 ----
    [
        "# Submitting expense reports",
        "",
        "Expense reports must be submitted within thirty days of the",
        "end of travel. Reports submitted after this window require",
        "written approval from a department director before they can",
        "be reimbursed.",
        "",
        "# Receipts",
        "",
        "Itemised receipts are required for any single expense over",
        "25 USD. Meals claimed under the per-diem rate do not require",
        "receipts.",
    ],
    # ---- page 2: the table ----
    [
        "# Per-diem rates",
        "",
        "Per-diem depends on the destination band. The bands below are",
        "reviewed annually by the finance team.",
        "",
        "| Destination band   | Daily meals | Daily incidentals |",
        "| ------------------ | ----------- | ----------------- |",
        "| Domestic standard  | 64 USD      | 12 USD            |",
        "| Domestic high-cost | 79 USD      | 12 USD            |",
        "| International      | 95 USD      | 20 USD            |",
        "",
        "Per-diem is claimed for each full day of travel.",
    ],
    # ---- page 3 ----
    [
        "# Mileage",
        "",
        "Personal vehicle mileage is reimbursed at the published",
        "federal rate. The rate is reviewed every January and",
        "announced by the finance team. Mileage for commuting between",
        "home and the primary office is never reimbursable.",
        "",
        "# Approval thresholds",
        "",
        "A direct manager approves expense reports up to 5,000 USD.",
        "Reports above 5,000 USD additionally require finance review.",
        "Reports above 25,000 USD require the CFO.",
    ],
    # ---- page 4 ----
    [
        "# Air travel",
        "",
        "Economy class is the default for all flights. Premium economy",
        "is permitted for flights with a scheduled duration over eight",
        "hours. Business class requires CFO approval regardless of",
        "duration.",
        "",
        "# Cancellations",
        "",
        "Refundable fares require a documented business reason. Change",
        "fees on non-refundable fares are reimbursable.",
    ],
]

RUNBOOK_HEADER = "Acme Platform Engineering - Confidential"
RUNBOOK_FOOTER = "Deployment Runbook rev 11 - Page {page} of {total}"

RUNBOOK_PAGES = [
    # ---- page 1 ----
    [
        "# Deployment windows",
        "",
        "Production deploys run Tuesday through Thursday, 09:00 to",
        "16:00 UTC. Deploys are frozen from December 20 to January 2.",
        "",
        "# Required approvals",
        "",
        "Every production deploy needs a second engineer to approve the",
        "release checklist before the pipeline will proceed.",
    ],
    # ---- page 2: the code block ----
    [
        "# Rolling back a release",
        "",
        "Roll back to the previous known-good revision with the",
        "platform CLI. The command is idempotent.",
        "",
        "```bash",
        "# roll back the api service to the prior revision",
        "acmectl rollback --service api --to previous \\",
        "    --cluster prod-eu --confirm",
        "```",
        "",
        "A rollback does NOT revert database migrations. Reverting a",
        "migration is a separate, manual change.",
    ],
    # ---- page 3: second table ----
    [
        "# Environments",
        "",
        "| Environment | Cluster     | Deploy approval |",
        "| ----------- | ----------- | --------------- |",
        "| development | dev-eu      | none            |",
        "| staging     | stg-eu      | one engineer    |",
        "| production  | prod-eu     | two engineers   |",
        "",
        "Staging mirrors production configuration except for secrets.",
    ],
    # ---- page 4 ----
    [
        "# On-call escalation",
        "",
        "The primary on-call engineer acknowledges a page within five",
        "minutes. Unacknowledged pages escalate to the secondary after",
        "ten minutes, and to the engineering manager after twenty.",
    ],
]


def build() -> list[str]:
    acme_dir = os.path.join(CORPUS_DIR, "acme")
    os.makedirs(acme_dir, exist_ok=True)

    written = []
    for filename, header, footer, pages in (
        ("travel-policy.pdf", TRAVEL_HEADER, TRAVEL_FOOTER, TRAVEL_PAGES),
        ("engineering-runbook.pdf", RUNBOOK_HEADER, RUNBOOK_FOOTER, RUNBOOK_PAGES),
    ):
        path = os.path.join(acme_dir, filename)
        _write_pdf(path, header, footer, pages)
        written.append(path)
    return written


if __name__ == "__main__":
    for path in build():
        print(f"wrote {path} ({os.path.getsize(path)} bytes)")
