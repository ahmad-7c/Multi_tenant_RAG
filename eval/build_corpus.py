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


VENDOR_HEADER = "Acme Vendor Risk - Confidential"
VENDOR_FOOTER = "Vendor Security Review Standard v2.1 - Page {page} of {total}"

# Added for the Phase 4 golden-set expansion. A THIRD acme document
# rather than new sections inside the first two: regenerating an
# existing PDF reflows its pages, and eight existing cases pin
# expect_pages against the current layout. A correct citation would
# start scoring as wrong, which reads as a retrieval regression.
VENDOR_PAGES = [
    # ---- page 1: the exact term (a form id) ----
    [
        "# Scope",
        "",
        "This standard applies to every third-party vendor that will",
        "process, store or transmit Acme data. It does not apply to",
        "one-off purchases of shrink-wrapped software with no data",
        "access.",
        "",
        "# Starting a review",
        "",
        "A review is started by filing Form VSR-204 with the vendor",
        "risk team. The form must name a business sponsor and the data",
        "the vendor will touch. Reviews are started at least thirty",
        "business days before the intended contract signature.",
    ],
    # ---- page 2: the classification table ----
    [
        "# Data classification tiers",
        "",
        "Every vendor engagement is assigned a tier. The tier drives",
        "both the evidence required and how long records are kept.",
        "",
        "| Tier              | Data the vendor sees   | Retention |",
        "| ----------------- | ---------------------- | --------- |",
        "| Tier 1 Public     | published material     | 2 years   |",
        "| Tier 2 Internal   | internal documents     | 5 years   |",
        "| Tier 3 Restricted | customer or staff data | 10 years  |",
        "",
        "Tier 3 Restricted data must not leave the Acme network",
        "without an approved encryption gateway.",
    ],
    # ---- page 3: one long procedure, deliberately > CHILD_TOKENS ----
    #
    # Ten steps under a SINGLE heading, so chunking.py packs them into
    # one parent and then slides several 125-token children across it.
    # That is what makes the spans_two_chunks cases real: the support
    # for them straddles a child boundary, so no single embedded child
    # contains the whole answer and only the parent sent to the LLM
    # does. A procedure broken up by sub-headings would start a fresh
    # parent per step and the case would prove nothing.
    [
        "# Review procedure",
        "",
        "1. The business sponsor files Form VSR-204 and names the data",
        "   involved.",
        "2. The vendor risk team assigns a tier within two business",
        "   days.",
        "3. The vendor receives the security questionnaire for the",
        "   assigned tier.",
        "4. The security team reviews the returned questionnaire within",
        "   five business days of receipt.",
        "5. Where the questionnaire shows a gap, the vendor submits a",
        "   written remediation plan with owners and dates before the",
        "   review continues.",
        "6. The security team verifies any remediation the plan calls",
        "   complete.",
        "7. For Tier 3 Restricted engagements the vendor also supplies",
        "   a penetration test report no more than twelve months old.",
        "8. The vendor risk team records a decision of approved,",
        "   approved with conditions, or declined.",
        "9. Conditions are tracked by the business sponsor until they",
        "   are closed.",
        "10. The decision and its evidence are filed against the",
        "    contract.",
    ],
    # ---- page 4 ----
    [
        "# Reassessment",
        "",
        "An approved vendor is reassessed annually. A reassessment is",
        "also triggered before that date by a material change: a new",
        "data tier, a sub-processor the vendor had not declared, or a",
        "reported breach affecting the vendor.",
        "",
        "# Exclusions",
        "",
        "This standard does not cover vendor commercial terms,",
        "pricing or payment schedules, which are owned by",
        "procurement.",
    ],
]


def build() -> list[str]:
    acme_dir = os.path.join(CORPUS_DIR, "acme")
    os.makedirs(acme_dir, exist_ok=True)

    written = []
    for filename, header, footer, pages in (
        ("travel-policy.pdf", TRAVEL_HEADER, TRAVEL_FOOTER, TRAVEL_PAGES),
        ("engineering-runbook.pdf", RUNBOOK_HEADER, RUNBOOK_FOOTER, RUNBOOK_PAGES),
        ("vendor-security-review.pdf", VENDOR_HEADER, VENDOR_FOOTER, VENDOR_PAGES),
    ):
        path = os.path.join(acme_dir, filename)
        _write_pdf(path, header, footer, pages)
        written.append(path)
    return written


if __name__ == "__main__":
    for path in build():
        print(f"wrote {path} ({os.path.getsize(path)} bytes)")
