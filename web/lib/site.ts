/**
 * Every piece of product copy, navigation and marketing content the
 * public pages render.
 *
 * One module rather than strings scattered through JSX, for three
 * reasons: a name change is one edit instead of a grep; the pages
 * become layout with no content decisions in them; and the claims the
 * product makes about itself sit in a single reviewable list rather
 * than being discovered one component at a time.
 *
 * Nothing here is fetched. These are statements about how the system
 * is built, not measurements of how it is running -- the data-honesty
 * rule governs numbers the BACKEND produces, and there are none on
 * these pages. Where a figure appears it is a configured constant of
 * the system (chunk sizes, the RRF k), not a metric.
 */

export const site = {
  name: "DocuMind",
  tagline: "Ask your documents. Get answers you can verify.",
  subcopy:
    "A multi-tenant retrieval platform for teams. Upload your documents, ask in plain language, and get answers grounded in real retrieved passages — every claim traceable to the page it came from.",
  // Used in <title> suffixes and the footer.
  description:
    "Multi-tenant RAG for teams: cited answers, strict tenant isolation, streaming responses.",
} as const;

/** Top-bar links, right-aligned. Order is the order rendered. */
export const topNav = [
  { href: "/how-it-works", label: "How it works" },
  { href: "/how-its-built", label: "How it's built" },
] as const;

/** The three trust points under the hero. */
export const trustPoints = [
  {
    title: "Tenant isolation",
    body: "Every query is scoped by an organization id taken from the signed token — never from the request. Four independent filters sit between a question and a passage.",
  },
  {
    title: "Cited answers",
    body: "Citations resolve against passages the server supplied, so a reference either points at real text in your documents or it is removed.",
  },
  {
    title: "Streaming responses",
    body: "Answers stream token by token as the model writes them, with retrieval and rerank timings reported alongside.",
  },
] as const;

/** Landing-page feature grid. */
export const features = [
  {
    title: "Hybrid retrieval",
    body: "Vector search and full-text search run as separate legs and are fused by rank, not by score — the two produce numbers that are not comparable.",
  },
  {
    title: "Parent–child chunking",
    body: "Small chunks are embedded and searched so matching is precise; the larger passage around the match is what the model reads, so answers have context.",
  },
  {
    title: "A refusal is an answer",
    body: "When nothing relevant is found the system says so instead of writing fluent prose over the least-bad match in the corpus.",
  },
  {
    title: "Answers you can open",
    body: "Each citation carries the document, the page, and the passage the model actually read, so a reader can check the claim rather than trust it.",
  },
  {
    title: "Built for many tenants",
    body: "One deployment, many organizations. The same email can exist in several of them and never see the other's documents.",
  },
  {
    title: "Costs you can see",
    body: "Token counts come from the provider's own usage reporting and are recorded per organization, per day.",
  },
] as const;

/**
 * "How it works" — the non-technical walkthrough. Deliberately free of
 * jargon: no embeddings, no cosine, no reranker.
 */
export const howItWorksSteps = [
  {
    n: 1,
    title: "You upload your documents",
    plain:
      "PDFs, Markdown or text files. They go into a private space that belongs to your organization and nobody else's.",
    detail:
      "Processing happens in the background, so a large file does not make you wait. You see each document move from queued, to processing, to ready.",
  },
  {
    n: 2,
    title: "We cut them into readable pieces",
    plain:
      "A whole document is too big to search well, so it is split into passages — roughly a few paragraphs each — keeping tables and code blocks whole.",
    detail:
      "Think of it as adding a very fine index to a book. The split is deliberate: searching a whole chapter finds the chapter, searching a paragraph finds the answer.",
  },
  {
    n: 3,
    title: "You ask a question in your own words",
    plain:
      "No keywords, no syntax. Ask the way you would ask a colleague who has read everything.",
    detail:
      "If you are mid-conversation and say \"what about contractors?\", the question is first rewritten into a standalone one so the search still makes sense on its own.",
  },
  {
    n: 4,
    title: "We find the passages that could answer it",
    plain:
      "Two searches run at once: one that matches on meaning, and one that matches on the actual words. Their results are combined.",
    detail:
      "Meaning-matching finds a passage about \"reimbursement\" when you asked about \"expenses\". Word-matching catches exact terms, product names and error codes that meaning-matching can blur. Most systems pick one. Using both covers the other's blind spot.",
  },
  {
    n: 5,
    title: "A second pass re-reads the shortlist",
    plain:
      "The first search is fast and approximate. A slower, more careful model then re-reads the shortlist against your question and reorders it.",
    detail:
      "This is the difference between \"these look roughly related\" and \"this one actually addresses the question\".",
  },
  {
    n: 6,
    title: "If nothing fits, we say so",
    plain:
      "The best match is scored. If it is too weak, you get an honest \"I could not find this\" instead of an invented answer.",
    detail:
      "This matters more than it sounds. Search always returns its best results — even when the best result is irrelevant. Without this check, the model would be handed the least-bad text in your corpus and would write confidently over it.",
  },
  {
    n: 7,
    title: "The answer is written, and cited",
    plain:
      "The model may only use the passages it was given, and must cite each claim. The answer streams back as it is written.",
    detail:
      "Every reference is checked against the passages the server supplied. A citation pointing at something that was never retrieved is removed rather than shown — a reference that leads nowhere is worse than none.",
  },
] as const;

/**
 * "How it's built" — the technical account. Each entry states a
 * decision AND the alternative it was chosen over, because a decision
 * without its rejected alternative is just a description.
 */
export const architectureSections = [
  {
    id: "isolation",
    title: "Tenant isolation",
    lead: "The property everything else is subordinate to. One database, one schema, an org_id discriminator — and the discipline that makes that safe.",
    points: [
      {
        k: "org_id comes from the token, never the request",
        v: "A client cannot name the tenant it wants. The id is read from the verified JWT and re-checked against the user's live row, so a stolen or stale claim cannot widen access.",
      },
      {
        k: "Four independent filters on the path to a passage",
        v: "The vector leg, the keyword leg, the hydration fetch and the parent lookup each apply the tenant filter. They are mutually redundant on purpose, and each one is pinned by its own test that executes that statement alone — so no layer can quietly cover for a filter somebody deleted.",
      },
      {
        k: "404, never 403, for another tenant's id",
        v: "A 403 confirms the id exists somewhere. 404 is indistinguishable from never having existed, so ids cannot be probed across tenants.",
      },
      {
        k: "The cache key is hashed with the org id inside it",
        v: "Not as a key prefix — inside the hash. A prefix can be confused by content containing the delimiter; a hash cannot. Two tenants asking the identical question get different keys.",
      },
    ],
  },
  {
    id: "chunking",
    title: "Chunking",
    lead: "Parent–child. Small pieces are embedded and searched; the larger passage around the winner is what the model reads.",
    points: [
      {
        k: "Why two sizes instead of one",
        v: "One size cannot be right twice. A chunk small enough to match precisely is too small to answer from; a chunk large enough to answer from dilutes the match. Splitting the two jobs is the central retrieval decision in this system.",
      },
      {
        k: "Blocks are never split mid-structure",
        v: "Text is grouped into paragraph, table and code blocks first, and a block is never broken across a boundary. A table cut in half retrieves as two fragments that each mean nothing.",
      },
      {
        k: "Children overlap",
        v: "A sliding window with overlap, so a sentence that straddles a boundary still appears whole in one child. Without it, the answer to a question can fall exactly into a seam.",
      },
      {
        k: "Measured, not asserted",
        v: "A test chunks the real corpus with the production chunker and fails if any single child contains every term an answer needs — proving the parent is doing the work it is there to do.",
      },
    ],
  },
  {
    id: "retrieval",
    title: "Retrieval and ranking",
    lead: "Two retrievers, fused by rank, then re-read by a cross-encoder, then gated.",
    points: [
      {
        k: "Fused by RANK, not score",
        v: "Cosine similarity is bounded; lexical rank scores are not, and vary with term frequency and document length. There is no constant that normalises one to the other, so any weighted sum needs a fudge factor that every new upload invalidates. Rank discards the magnitudes and keeps the only thing both retrievers agree on.",
      },
      {
        k: "A cross-encoder reorders the shortlist",
        v: "The first pass scores question and passage separately and compares the results. The second reads them together, which is slower and markedly better — so it runs on a shortlist, not the corpus.",
      },
      {
        k: "The confidence gate",
        v: "Retrieval always returns its top results; \"nothing is relevant\" and \"this is exactly it\" look identical in shape. The gate turns the first into an honest refusal instead of a hallucination — and skips the generation cost entirely.",
      },
      {
        k: "Thresholds are measured, not guessed",
        v: "Calibrated against a hand-labelled question set, by a selection rule fixed in advance. The measurement also established its own limit: the classes overlap, so no threshold separates them perfectly. The gate is a cheap filter, not a correctness guarantee.",
      },
      {
        k: "A degraded reranker does not open the floodgates",
        v: "If the reranking service fails, results fall back to fused order — whose scores are not relevance and cannot be compared to the threshold. A separate calibrated signal takes over, so an outage does not quietly turn into answering everything.",
      },
    ],
  },
  {
    id: "grounding",
    title: "Grounding and prompt safety",
    lead: "Retrieved text is attacker-controlled data. It is never allowed to read as an instruction.",
    points: [
      {
        k: "Retrieved content is data, not instructions",
        v: "In a multi-tenant product the corpus is whatever customers uploaded. A passage can contain \"ignore your previous instructions\" as easily as an expense policy. Rules live in the system prompt, state that the context is untrusted, and are never interleaved with it.",
      },
      {
        k: "The model has no tools",
        v: "Not an empty tool list — absent. A capability the model does not have cannot be talked into existence, which is the half of prompt-injection defence that does not depend on the model obeying anything.",
      },
      {
        k: "Citations are server-resolved integers",
        v: "The model emits a bare number. If it emitted structured references it could invent a plausible filename and page that appeared nowhere. An integer has two possible fates: it maps to a passage we supplied, or it is deleted.",
      },
      {
        k: "What a citation does and does not prove",
        v: "It proves the cited text was retrieved and shown to the model. It does not prove the answer follows from it. Stated plainly rather than implied — citation correctness is mechanical, answer correctness needs evaluation.",
      },
    ],
  },
  {
    id: "security",
    title: "Security",
    lead: "The browser never holds a credential, and the API is not reachable from it.",
    points: [
      {
        k: "The token lives in an httpOnly cookie",
        v: "A backend-for-frontend holds the JWT server-side and attaches it to each proxied call. JavaScript cannot read the cookie, so a cross-site scripting bug cannot steal a session.",
      },
      {
        k: "An allowlist, not a passthrough",
        v: "Only named API paths are reachable through the proxy; everything else is a 404. A new backend endpoint is invisible to the browser until someone exposes it deliberately.",
      },
      {
        k: "CSRF: a custom header plus an origin check",
        v: "A cross-origin form cannot set a custom header, and setting one from script forces a preflight this app does not answer. The origin is compared against the request's own host.",
      },
      {
        k: "A strict Content Security Policy with per-request nonces",
        v: "No inline script is permitted. Model output is rendered as sanitised markdown with images disabled outright — an image URL in a generated answer is an exfiltration channel.",
      },
      {
        k: "Passwords and rate limits",
        v: "Passwords are bcrypt hashes and are never logged or returned. Login and registration are limited per IP, because a per-account limit cannot see credential stuffing spread across many accounts.",
      },
    ],
  },
  {
    id: "operations",
    title: "Operations",
    lead: "What happens when a dependency is slow, broken, or expensive.",
    points: [
      {
        k: "Ingestion is asynchronous and idempotent",
        v: "Uploads are processed by background workers. A task that dies is redelivered rather than dropped, and re-running it replaces its own output instead of duplicating it.",
      },
      {
        k: "No database session is held while an answer streams",
        v: "A stream lasts seconds; a connection pool has tens of slots. Holding one per in-flight answer would block logins and uploads behind a model writing prose, and would keep a transaction open long enough to cause table bloat.",
      },
      {
        k: "Per-tenant rate limits and concurrency caps",
        v: "A token bucket per organization, evaluated atomically so interleaved requests cannot exceed it. Concurrent answer streams are capped by a timestamped set that heals itself if a worker dies mid-stream.",
      },
      {
        k: "Failure modes are chosen, not inherited",
        v: "When the limiter's store is unavailable, reads fail open and the expensive endpoint fails closed onto a local bucket — so an incident degrades reads rather than handing out unlimited generation.",
      },
      {
        k: "Usage is metered from the provider's own counts",
        v: "Token counts come from the provider's usage reporting rather than a local estimate, recorded per organization and per day. Rejected requests are deliberately not metered: charging for them would make the rate limiter an amplifier.",
      },
    ],
  },
] as const;

/** The stack, grouped. Names only — versions change. */
export const techStack = [
  { group: "API", items: ["FastAPI", "Python", "Pydantic", "SQLAlchemy", "Alembic"] },
  { group: "Data", items: ["PostgreSQL", "pgvector", "Full-text search", "Redis"] },
  { group: "Workers", items: ["Celery", "PyMuPDF"] },
  { group: "Models", items: ["Gemini embeddings", "Gemini generation", "Cohere reranking"] },
  { group: "Web", items: ["Next.js", "React", "TypeScript", "Tailwind CSS"] },
  { group: "Quality", items: ["pytest", "Real Postgres in tests", "Mutation testing", "A labelled evaluation set"] },
] as const;

/**
 * Payment providers, for the roadmap section.
 *
 * `available: false` on every entry and the UI renders them as
 * forthcoming. Nothing here is wired to anything — saying "coming
 * soon" is a roadmap statement; a checkout button that did not work
 * would be a lie.
 */
export const paymentRoadmap = {
  heading: "Billing is coming",
  body: "Usage is already metered per organization and per day — tokens, questions, cache hits and refusals. Billing turns that existing meter into an invoice. Until it ships, nothing in this product charges anyone.",
  providers: [
    {
      name: "Stripe",
      note: "Cards, subscriptions and usage-based pricing, billed against the per-organization meter already in place.",
      available: false,
    },
    {
      name: "PayPal",
      note: "A second option for teams that cannot use card rails, covering the same plans.",
      available: false,
    },
  ],
} as const;

/** Plan outlines for the roadmap. No prices — none have been set. */
export const planOutlines = [
  {
    name: "Free",
    body: "A single organization, a small document allowance, and the full retrieval pipeline. Intended for evaluating answer quality on your own corpus.",
  },
  {
    name: "Team",
    body: "Higher document and question limits, more concurrent answer streams, and the usage dashboard across a longer window.",
  },
  {
    name: "Enterprise",
    body: "Dedicated infrastructure, data residency, SSO, and an audit trail over queries and documents.",
  },
] as const;

/**
 * What a user is told when a document fails to process.
 *
 * The API sends a CODE, never the provider's exception -- that text
 * named the embedding provider, the model and an upstream Google URL,
 * and told the person who uploaded a PDF nothing they could do. The
 * sentence lives here because it is product copy, and `action` is the
 * part that matters: each code maps to one thing the reader can
 * actually do next.
 */
export const failureCopy = {
  rate_limited: {
    title: "Too many documents at once",
    body: "The indexing service hit its rate limit part-way through this file, so it was not finished.",
    action: "Delete it and upload again in a few minutes. Large files are more likely to hit this.",
  },
  timeout: {
    title: "Indexing took too long",
    body: "The indexing service did not respond in time. The file itself is probably fine.",
    action: "Delete it and upload again.",
  },
  provider_unavailable: {
    title: "The indexing service was unavailable",
    body: "This file could not be processed because the service it depends on could not be reached.",
    action: "Delete it and upload again shortly.",
  },
  no_text: {
    title: "No readable text in this file",
    body: "The file opened, but there was no text to index -- this usually means it is a scan or an image-only diagram.",
    action: "Upload a version with selectable text, or export the diagram's contents as text.",
  },
  unreadable: {
    title: "This file could not be opened",
    body: "The file appears to be damaged or is not the format its extension claims.",
    action: "Check it opens on your machine, then upload it again.",
  },
  unsupported_type: {
    title: "This file type is not supported",
    body: "Only PDF, Markdown and plain text can be indexed.",
    action: "Convert it and upload again.",
  },
  unknown: {
    // Honest rather than reassuring. Guessing a cause sends people
    // round a loop they cannot exit.
    title: "This file could not be processed",
    body: "Something went wrong while indexing it, and we could not determine what.",
    action: "Try uploading it again. If it fails a second time, contact an administrator.",
  },
} as const;

export type FailureCode = keyof typeof failureCopy;
