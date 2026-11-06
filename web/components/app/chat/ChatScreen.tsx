"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { useUser } from "@/components/app/UserContext";
import { AnswerMarkdown } from "@/components/app/chat/AnswerMarkdown";
import { RefusalCard } from "@/components/app/chat/RefusalCard";
import { SourcesPanel } from "@/components/app/chat/SourcesPanel";
import { Logo } from "@/components/marketing/Logo";
import { Button } from "@/components/ui/Button";
import { SourcePill, TraceChip } from "@/components/ui/chips";
import { EmptyState, InlineBanner } from "@/components/ui/primitives";
import { apiFetch, queryKeys } from "@/lib/api";
import { StreamAborted, streamAnswer } from "@/lib/stream";
import type { Citation, CitationsEvent, Conversation, Message } from "@/lib/types";

/** A turn being streamed right now. */
type Live = {
  question: string;
  text: string;
  outcome: CitationsEvent | null;
  error: string | null;
  /**
   * The message ids that existed BEFORE this turn was asked.
   *
   * The live turn and the persisted turn are the same exchange shown
   * two different ways: the live one carries trace chips and clickable
   * citations, the persisted one is just text. Once the stream ends
   * the history query refetches and contains both new rows -- so
   * without this the answer appeared TWICE, with the stored copy
   * sorting above the question that produced it.
   *
   * Filtering history down to this baseline keeps the richer live
   * rendering authoritative until the user moves on.
   */
  baseline: Set<string>;
};

/**
 * Trace chips, built ONLY from fields the event actually carried.
 *
 * Every chip is conditional on its value being non-null. That is the
 * data-honesty rule made structural: `TraceChip` requires a string, so
 * there is no way to pass it "missing" and have it render a dash.
 * A cache hit therefore shows "from cache" and no timings, because
 * nothing was retrieved, reranked or generated for that request.
 */
function Trace({ event }: { event: CitationsEvent }) {
  const chips: React.ReactNode[] = [];

  if (event.cached) {
    chips.push(<TraceChip key="c" label="from" value="cache" tone="accent" />);
  }
  if (event.rewritten_question) {
    chips.push(
      <TraceChip
        key="r"
        label="rewritten"
        value="yes"
        title={`Searched for: ${event.rewritten_question}`}
      />,
    );
  }
  if (event.retrieve_ms !== null) {
    chips.push(<TraceChip key="rt" label="retrieve" value={`${event.retrieve_ms} ms`} />);
  }
  // Suppressed when nothing was retrieved: "rerank 0 ms" is true but
  // reads as an impossibly fast rerank rather than as "there was
  // nothing to rerank".
  if (event.rerank_ms !== null && (event.candidate_count ?? 0) > 0) {
    chips.push(<TraceChip key="rr" label="rerank" value={`${event.rerank_ms} ms`} />);
  }
  if (event.first_token_ms !== null) {
    chips.push(
      <TraceChip
        key="ft"
        label="model first token"
        value={`${event.first_token_ms} ms`}
        title="Time for the model to produce its first token. Excludes retrieval and rerank."
      />,
    );
  }
  if (event.candidate_count !== null) {
    chips.push(<TraceChip key="cc" label="candidates" value={String(event.candidate_count)} />);
  }
  // Confidence only when the score is a calibrated relevance.
  if (event.scores_calibrated && event.confidence !== null) {
    chips.push(
      <TraceChip key="cf" label="confidence" value={event.confidence.toFixed(2)} tone="good" />,
    );
  }
  if (event.used_reranker_fallback) {
    chips.push(<TraceChip key="fb" label="reranker" value="degraded" />);
  }

  if (chips.length === 0) return null;
  return <div className="mb-3 flex flex-wrap gap-1.5">{chips}</div>;
}

function Bubble({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex justify-end">
      <p className="ac-surface max-w-[82%] rounded-card rounded-br-[6px] px-4 py-3 text-[15px] font-medium text-on-ac">
        {children}
      </p>
    </div>
  );
}

function Assistant({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex gap-3">
      <span className="mt-0.5 shrink-0" aria-hidden="true">
        <Logo size={28} />
      </span>
      <div className="min-w-0 flex-1 pt-1">{children}</div>
    </div>
  );
}

export function ChatScreen() {
  const user = useUser();
  const queryClient = useQueryClient();

  // `null` means "whichever is most recent", resolved below. Storing
  // an explicit choice only once the user makes one.
  const [chosenId, setChosenId] = useState<string | null>(null);
  const [live, setLive] = useState<Live | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [activeMarker, setActiveMarker] = useState<number | null>(null);
  const [panelOpen, setPanelOpen] = useState(true);
  const [question, setQuestion] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const conversationsKey = queryKeys.conversations(user.org_id, user.id);

  const { data: conversations } = useQuery({
    queryKey: conversationsKey,
    queryFn: () => apiFetch<Conversation[]>("conversations"),
  });

  // DERIVED, not synced. An effect that copied the latest id into
  // state would render once with no conversation and again with one,
  // and would fight the user's own choice every time the list
  // refetched. No auto-create either: a conversation row per page
  // visit would fill the list with empties.
  const conversationId = chosenId ?? conversations?.[0]?.id ?? null;

  const { data: history } = useQuery({
    queryKey: conversationId
      ? queryKeys.messages(user.org_id, user.id, conversationId)
      : ["messages", "none"],
    queryFn: () => apiFetch<Message[]>(`conversations/${conversationId}/messages`),
    enabled: conversationId !== null,
  });

  const createConversation = useMutation({
    mutationFn: () =>
      apiFetch<Conversation>("conversations", {
        method: "POST",
        body: JSON.stringify({ title: null }),
      }),
    onSuccess: (created) => {
      setChosenId(created.id);
      setLive(null);
      void queryClient.invalidateQueries({ queryKey: conversationsKey });
    },
  });

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [live?.text, history]);

  // Abort any in-flight stream when the component goes away, so a
  // navigation stops paying for tokens nobody will read.
  useEffect(() => () => abortRef.current?.abort(), []);

  async function ask(text: string) {
    let id = conversationId;
    if (!id) {
      const created = await createConversation.mutateAsync();
      id = created.id;
    }

    // The baseline must be COMPLETE or earlier turns vanish from the
    // transcript for as long as the live one is on screen. If the
    // history query has not resolved yet -- asking within a second of
    // a page load, which is entirely normal -- `history` is undefined
    // and a snapshot of it would be an empty set. Resolve it first;
    // fetchQuery returns the cache when it is already there, so this
    // costs a request only in the case that needs one.
    const existing =
      history ??
      (await queryClient.fetchQuery({
        queryKey: queryKeys.messages(user.org_id, user.id, id),
        queryFn: () => apiFetch<Message[]>(`conversations/${id}/messages`),
      }));

    const controller = new AbortController();
    abortRef.current = controller;
    setStreaming(true);
    setActiveMarker(null);
    setLive({
      question: text,
      text: "",
      outcome: null,
      error: null,
      baseline: new Set((existing ?? []).map((m) => m.id)),
    });

    try {
      await streamAnswer(
        id,
        text,
        {
          onToken: (chunk) =>
            setLive((current) =>
              current ? { ...current, text: current.text + chunk } : current,
            ),
          onCitations: (event) =>
            // The authoritative answer REPLACES the streamed text: a
            // marker the validator dropped is still on screen until
            // this lands.
            setLive((current) =>
              current ? { ...current, text: event.answer, outcome: event } : current,
            ),
        },
        controller.signal,
      );
      // The turn is now persisted server-side, so the history query
      // owns it. Invalidate rather than appending by hand.
      void queryClient.invalidateQueries({
        queryKey: queryKeys.messages(user.org_id, user.id, id),
      });
    } catch (caught) {
      if (caught instanceof StreamAborted) {
        setLive((current) =>
          current ? { ...current, error: "You stopped this answer." } : current,
        );
      } else {
        const error = caught as Error & { retryAfterSeconds?: number | null };
        const wait = error.retryAfterSeconds;
        setLive((current) =>
          current
            ? {
                ...current,
                error: wait
                  ? `${error.message} Try again in ${wait}s.`
                  : error.message || "The answer could not be completed.",
              }
            : current,
        );
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }

  function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    const text = question.trim();
    if (!text || streaming) return;
    setQuestion("");
    void ask(text);
  }

  const citations: Citation[] = live?.outcome?.citations ?? [];
  const markers = new Set(citations.map((c) => c.marker));
  // Both the gate and a model-side refusal (safety block, zero valid
  // citations) report kind="refusal"; `gated` alone only catches the
  // first. An assist turn is neither.
  const refused =
    live?.outcome?.kind === "refusal" || live?.outcome?.gated === true;
  const assisted = live?.outcome?.kind === "assist";

  // Turns already persisted. While a live turn is on screen this is
  // frozen to the rows that existed before it, so the exchange is not
  // rendered twice -- see the note on Live.baseline.
  const past = live
    ? (history ?? []).filter((m) => live.baseline.has(m.id))
    : (history ?? []);

  return (
    // flex-col until xl, then row.
    //
    // This was `flex` at every width, so on a phone the Sources panel
    // -- a `w-full shrink-0` sibling -- took the whole width and shoved
    // the conversation off-screen. The panel's own `border-t` and
    // `xl:border-t-0` always intended stacking; the parent never
    // switched.
    //
    // 100dvh, not 100vh: on mobile browsers 100vh is the height WITH
    // the URL bar collapsed, so a 100vh column puts the composer below
    // the fold and the user cannot reach the input without scrolling
    // the page itself.
    <div className="flex h-[calc(100dvh-72px)] min-w-0 flex-col xl:flex-row">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="flex items-center justify-between gap-3 border-b border-line px-5 py-3 sm:px-8">
          <span className="rounded-pill border border-line bg-s1 px-3 py-1 font-mono text-[11px] text-tx2">
            Org only
          </span>
          <Button
            variant="ghost"
            className="h-9 px-3 text-[13px]"
            onClick={() => createConversation.mutate()}
            disabled={streaming}
          >
            New conversation
          </Button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-6 sm:px-8">
          <div className="mx-auto flex w-full max-w-[760px] flex-col gap-6">
            {past.length === 0 && !live && (
              <div className="pt-10">
                <EmptyState title="Ask about your documents">
                  Answers come only from what this organization has uploaded.
                  When nothing relevant is found, you get told so rather than
                  given a guess.
                </EmptyState>
              </div>
            )}

            {past.map((message) =>
              message.role === "user" ? (
                <Bubble key={message.id}>{message.content}</Bubble>
              ) : (
                <Assistant key={message.id}>
                  {message.kind === "error" ? (
                    <InlineBanner tone="error" title="The answer stopped early">
                      {message.content}
                    </InlineBanner>
                  ) : message.kind === "refusal" ? (
                    <RefusalCard text={message.content} />
                  ) : (
                    // `assist` falls through to plain prose on purpose:
                    // a greeting is an ordinary reply, and wrapping it
                    // in a banner would make saying hello look like a
                    // warning.
                    <AnswerMarkdown
                      text={message.content}
                      markers={new Set()}
                      activeMarker={null}
                      onCite={() => {}}
                    />
                  )}
                </Assistant>
              ),
            )}

            {live && (
              <>
                <Bubble>{live.question}</Bubble>
                <Assistant>
                  {live.outcome && !assisted && <Trace event={live.outcome} />}

                  {live.error ? (
                    <InlineBanner tone="error" title="The answer stopped early">
                      {live.error}
                    </InlineBanner>
                  ) : refused ? (
                    <RefusalCard text={live.text} />
                  ) : (
                    <>
                      <div aria-live="polite" aria-busy={streaming}>
                        <AnswerMarkdown
                          text={live.text}
                          markers={markers}
                          activeMarker={activeMarker}
                          onCite={(n) => {
                            setActiveMarker(n);
                            setPanelOpen(true);
                          }}
                        />
                      </div>
                      {streaming && !live.outcome && (
                        <span className="animate-caret inline-block h-[1.1em] w-[2px] translate-y-0.5 bg-ac2" />
                      )}
                      {citations.length > 0 && (
                        <div className="mt-4 flex flex-wrap gap-2">
                          {citations.map((c) => (
                            <SourcePill
                              key={c.marker}
                              n={c.marker}
                              file={c.document}
                              page={c.page}
                              onClick={() => {
                                setActiveMarker(c.marker);
                                setPanelOpen(true);
                              }}
                            />
                          ))}
                        </div>
                      )}
                    </>
                  )}
                </Assistant>
              </>
            )}
            <div ref={endRef} />
          </div>
        </div>

        <form
          onSubmit={onSubmit}
          className="border-t border-line px-5 py-4 sm:px-8"
        >
          <div className="mx-auto flex w-full max-w-[760px] items-center gap-3">
            <label htmlFor="question" className="sr-only">
              Ask about your documents
            </label>
            <input
              id="question"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="Ask about your documents"
              disabled={streaming}
              maxLength={4000}
              autoComplete="off"
              className="h-12 flex-1 rounded-pill border border-line bg-s2 px-5 text-[15px] text-tx placeholder:text-tx3 transition-all duration-200 ease-brand focus:border-ac2 focus:outline-none focus:ring-4 focus:ring-ac-ring disabled:opacity-60"
            />
            {streaming ? (
              <button
                type="button"
                onClick={() => abortRef.current?.abort()}
                className="relative inline-flex h-12 items-center rounded-pill border border-line2 px-5 text-[14px] font-medium text-tx"
              >
                {/* The soft pulsing ring marks "still running". */}
                <span
                  aria-hidden="true"
                  className="animate-ping-dot absolute inset-0 rounded-pill border border-[color:var(--ac)]/40 motion-reduce:hidden"
                />
                Stop
              </button>
            ) : (
              <Button
                type="submit"
                disabled={!question.trim()}
                className="h-12 rounded-pill px-6"
              >
                Ask
              </Button>
            )}
          </div>
        </form>
      </div>

      <SourcesPanel
        citations={citations}
        activeMarker={activeMarker}
        onSelect={setActiveMarker}
        onClose={() => setPanelOpen(false)}
        open={panelOpen}
      />
    </div>
  );
}
