/**
 * The API's response shapes, mirrored from the backend's Pydantic
 * schemas.
 *
 * Hand-written rather than generated: the surface is small, and a
 * generator would also pull in every field the UI must NOT rely on.
 * Where a field is nullable here it is nullable on the wire, and the
 * null means something specific -- see the comments. Rendering a 0 in
 * place of one of these nulls is the data-honesty rule being broken.
 */

export type Role = "admin" | "member";

export type User = {
  id: string;
  org_id: string;
  email: string;
  role: Role;
  created_at: string;
};

export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

/** Mirrors app/services/document_failures.py. Mapped to copy in site.ts. */
export type FailureCode =
  | "rate_limited"
  | "timeout"
  | "provider_unavailable"
  | "no_text"
  | "unreadable"
  | "unsupported_type"
  | "unknown";

export type DocumentListItem = {
  id: string;
  filename: string;
  status: DocumentStatus;
  file_size_bytes: number;
  created_at: string;
};

export type DocumentDetail = DocumentListItem & {
  processed_at: string | null;
  /**
   * Why processing failed, as a code -- never the provider's exception.
   * The raw text named the embedding provider, the model and an
   * upstream URL, and told the uploader nothing actionable. Null
   * unless `status` is "failed".
   */
  failure_code: FailureCode | null;
  chunk_count: number;
};

export type Conversation = {
  id: string;
  title: string | null;
  created_at: string;
};

/**
 * NULL for user turns and for rows written before the column existed.
 *
 * `assist` is a conversational turn (a greeting, "what can you do")
 * answered without retrieval or a model call -- so it has no
 * citations and no timings, and it is NOT a refusal. Counting it as
 * one would inflate the refusal rate on the usage dashboard.
 */
export type MessageKind = "answer" | "refusal" | "error" | "assist" | null;

export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  kind: MessageKind;
  cited_chunk_ids: string[] | null;
  created_at: string;
};

/** One citation on the `citations` SSE event. */
export type Citation = {
  marker: number;
  document: string;
  document_id: string;
  chunk_id: string;
  page: number;
  snippet: string;
  /** The parent passage the model read. Null on pre-B1 cached entries. */
  passage: string | null;
  /** The child that matched, for highlighting inside `passage`. */
  matched_chunk_id: string | null;
  /**
   * Cross-encoder relevance, roughly 0..1.
   * NULL whenever `scores_calibrated` is false -- under a passthrough
   * reranker or a fallback the underlying number is a rank reciprocal,
   * not a relevance, and must never be drawn as one.
   */
  score: number | null;
};

/** The authoritative final event of an answer turn. */
export type CitationsEvent = {
  answer: string;
  citations: Citation[];
  cached: boolean;
  gated: boolean;
  /** What this turn is. `gated` alone cannot distinguish an assist. */
  kind: MessageKind;
  rewritten_question: string | null;
  /** The gate score. Null unless `scores_calibrated`. */
  confidence: number | null;
  /** When false, hide every score and confidence element. */
  scores_calibrated: boolean;
  used_reranker_fallback: boolean;
  /** Null on a cache hit: nothing was retrieved. Never render a 0. */
  candidate_count: number | null;
  /** Null when the step did not run; 0 when it ran and found nothing. */
  parent_count: number | null;
  retrieve_ms: number | null;
  rerank_ms: number | null;
  /**
   * MODEL time-to-first-token. NOT user-perceived time to first text:
   * it excludes auth, rate limiting, retrieval and rerank. Labelled
   * "model first token" in the UI for that reason.
   */
  first_token_ms: number | null;
};

export type UsageDay = {
  day: string;
  requests: number;
  embedding_tokens: number;
  llm_input_tokens: number;
  llm_output_tokens: number;
  rerank_calls: number;
  cache_hits: number;
  ok: number;
  refused: number;
  errored: number;
  rejected: number;
  /** Null when no row that day recorded a latency. Not 0. */
  avg_latency_ms: number | null;
};

export type UsageResponse = {
  days: number;
  totals: UsageDay | null;
  series: UsageDay[];
};

export type Health = {
  status: string;
  /** Operator-level deployment facts. NEVER rendered to a user. */
  degraded?: string[];
};
