"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { useUser } from "@/components/app/UserContext";
import { Card, InlineBanner, Segmented, Skeleton } from "@/components/ui/primitives";
import { Sparkline } from "@/components/ui/Sparkline";
import { ApiError, apiFetch, queryKeys } from "@/lib/api";
import { cx } from "@/lib/cx";
import type { UsageDay, UsageResponse } from "@/lib/types";

type Range = "1" | "7" | "30";

const RANGES: Array<{ value: Range; label: string }> = [
  { value: "1", label: "Today" },
  { value: "7", label: "7 days" },
  { value: "30", label: "30 days" },
];

function sum(rows: UsageDay[], field: keyof UsageDay): number {
  return rows.reduce((total, row) => total + (Number(row[field]) || 0), 0);
}

/** A rate, or null when the denominator is zero. */
function rate(numerator: number, denominator: number): number | null {
  if (denominator <= 0) return null;
  return numerator / denominator;
}

function formatCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 10_000) return `${Math.round(value / 1000)}k`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

/**
 * The delta against the preceding, equally long period.
 *
 * Null when there is no prior period to compare with -- on a one-day
 * range, or when the window does not reach back far enough. A "+0%"
 * in that case would claim a comparison that was never made.
 */
function Delta({ value }: { value: number | null }) {
  if (value === null) return null;
  const up = value >= 0;
  return (
    <span
      className={cx(
        "rounded-pill border px-2 py-0.5 font-mono text-[11px]",
        up
          ? "border-[color:var(--ok)]/30 text-ok-tx"
          : "border-line text-tx2",
      )}
    >
      {up ? "+" : ""}
      {Math.round(value * 100)}%
    </span>
  );
}

function MetricCard({
  label,
  value,
  delta,
  series,
  footnote,
}: {
  label: string;
  value: string;
  delta: number | null;
  series: number[];
  footnote?: string;
}) {
  return (
    <Card>
      <div className="flex items-start justify-between gap-3">
        <p className="text-[13px] text-tx2">{label}</p>
        <Delta value={delta} />
      </div>
      <p className="font-display mt-1.5 text-[28px] font-semibold tracking-display text-tx">
        {value}
      </p>
      {series.length > 1 ? (
        <div className="mt-3 h-[36px]">
          <Sparkline points={series} label={`${label} over the selected range`} />
        </div>
      ) : (
        <p className="mt-3 text-[11.5px] text-tx3">
          Not enough days to show a trend.
        </p>
      )}
      {footnote && <p className="mt-2 text-[11.5px] text-tx3">{footnote}</p>}
    </Card>
  );
}

/** Stacked bars: answered live vs served from cache. */
function QuestionsChart({ rows }: { rows: UsageDay[] }) {
  const max = Math.max(...rows.map((r) => r.ok), 1);
  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="font-display text-[15px] font-semibold text-tx">
          Questions per day
        </h2>
        <div className="flex items-center gap-4 text-[12px] text-tx2">
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-[3px] bg-ac" />
            Answered live
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-[3px] bg-amber" />
            From cache
          </span>
        </div>
      </div>

      <div className="mt-6 flex h-[180px] items-end justify-start gap-1.5 overflow-x-auto">
        {rows.map((row, i) => {
          const cached = Math.min(row.cache_hits, row.ok);
          const livePart = Math.max(0, row.ok - cached);
          const total = livePart + cached;
          return (
            <div
              key={row.day}
              // h-full is load-bearing: the inner bar sizes itself with
              // a PERCENTAGE height, and a percentage against an
              // auto-height parent resolves to zero. The chart
              // rendered blank without it.
              // max-w caps a bar so a range with ONE day renders a bar
              // rather than a wall of colour filling the whole chart;
              // flex-1 still spreads thirty of them evenly.
              className="group flex h-full min-w-[10px] max-w-[56px] flex-1 flex-col items-center justify-end gap-1.5"
              title={`${row.day}: ${livePart} live, ${cached} cached`}
            >
              <div
                className="animate-rise flex w-full flex-col justify-end"
                style={{
                  height: `${(total / max) * 100}%`,
                  animationDelay: `${Math.min(i * 25, 500)}ms`,
                }}
              >
                {cached > 0 && (
                  <div
                    className="w-full rounded-t-[3px] bg-amber"
                    style={{ height: `${(cached / Math.max(total, 1)) * 100}%` }}
                  />
                )}
                {livePart > 0 && (
                  <div
                    className={cx(
                      "w-full bg-ac",
                      cached === 0 && "rounded-t-[3px]",
                    )}
                    style={{ height: `${(livePart / Math.max(total, 1)) * 100}%` }}
                  />
                )}
              </div>
            </div>
          );
        })}
      </div>
      <p className="mt-3 text-[11.5px] text-tx3">
        Cache attribution began when usage metering was added, so questions
        answered before that read as live.
      </p>
    </Card>
  );
}

export function UsageScreen() {
  const user = useUser();
  const [range, setRange] = useState<Range>("7");

  // Twice the window, so the second half can be compared against the
  // first to produce a delta. One request, not two.
  const days = Number(range) * 2;

  const { data, isPending, isError, error } = useQuery({
    queryKey: queryKeys.usage(user.org_id, user.id, days),
    queryFn: () => apiFetch<UsageResponse>(`usage?days=${days}`),
  });

  if (isError) {
    const forbidden = error instanceof ApiError && error.status === 403;
    return (
      <div className="mx-auto w-full max-w-[1100px] px-5 py-10 sm:px-10">
        <InlineBanner
          tone={forbidden ? "info" : "error"}
          title={forbidden ? "Administrators only" : "Could not load usage"}
        >
          {forbidden
            ? "Usage for an organization is visible to its administrators."
            : error instanceof ApiError
              ? error.message
              : "Try again shortly."}
        </InlineBanner>
      </div>
    );
  }

  // The API returns newest first; charts read left to right in time.
  const all = [...(data?.series ?? [])].reverse();
  const window = Number(range);
  const current = all.slice(-window);
  const previous = all.slice(-window * 2, -window);
  // A delta needs a full prior period AND a non-zero base.
  const comparable = previous.length === window && window > 1;

  function delta(field: keyof UsageDay): number | null {
    if (!comparable) return null;
    const before = sum(previous, field);
    if (before === 0) return null;
    return (sum(current, field) - before) / before;
  }

  const requests = sum(current, "requests");
  const tokens = sum(current, "llm_input_tokens") + sum(current, "llm_output_tokens");
  const cacheRate = rate(sum(current, "cache_hits"), requests);
  const refusalRate = rate(sum(current, "refused"), requests);

  const latencies = current
    .map((row) => row.avg_latency_ms)
    .filter((value): value is number => value !== null);
  const avgLatency =
    latencies.length > 0
      ? Math.round(latencies.reduce((a, b) => a + b, 0) / latencies.length)
      : null;

  return (
    <div className="mx-auto w-full max-w-[1100px] px-5 py-8 sm:px-10 sm:py-10">
      <div className="animate-fade-up flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="font-display text-[30px] font-semibold tracking-display text-tx">
            Usage
          </h1>
          <p className="mt-1.5 text-[14px] text-tx2">
            This organization only. Token counts come from the provider&apos;s
            own reporting.
          </p>
        </div>
        <Segmented
          label="Date range"
          value={range}
          onChange={setRange}
          options={RANGES}
        />
      </div>

      {isPending ? (
        <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-[150px] w-full" />
          ))}
        </div>
      ) : requests === 0 ? (
        <div className="animate-fade-up stagger-1 mt-8">
          <InlineBanner tone="info" title="No activity in this range">
            Nothing has been asked in the selected period, so there is nothing
            to chart.
          </InlineBanner>
        </div>
      ) : (
        <>
          <div className="animate-fade-up stagger-1 mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <MetricCard
              label="Questions"
              value={formatCount(requests)}
              delta={delta("requests")}
              series={current.map((r) => r.requests)}
            />
            <MetricCard
              label="Tokens"
              value={formatCount(tokens)}
              delta={delta("llm_output_tokens")}
              series={current.map((r) => r.llm_input_tokens + r.llm_output_tokens)}
              footnote="Prompt plus completion."
            />
            <MetricCard
              label="Cache hit rate"
              value={cacheRate === null ? "—" : `${Math.round(cacheRate * 100)}%`}
              delta={null}
              series={current.map((r) => r.cache_hits)}
            />
            <MetricCard
              label="Refusal rate"
              value={refusalRate === null ? "—" : `${Math.round(refusalRate * 100)}%`}
              delta={null}
              series={current.map((r) => r.refused)}
              footnote="A refusal is a correct outcome, not a failure."
            />
          </div>

          <div className="animate-fade-up stagger-2 mt-4 grid gap-4 lg:grid-cols-[2fr_1fr]">
            <QuestionsChart rows={current} />

            {/*
              This card replaces the design's "Where the time goes"
              three-bar breakdown. That needed per-stage timings
              aggregated over a window; the backend records retrieve,
              rerank and first-token per REQUEST on the answer event,
              and stores only an end-to-end average per day. One real
              number beats three invented ones.
            */}
            <Card>
              <h2 className="font-display text-[15px] font-semibold text-tx">
                Average latency
              </h2>
              <p className="font-display mt-3 text-[34px] font-semibold tracking-display text-tx">
                {avgLatency === null ? "—" : `${formatCount(avgLatency)} ms`}
              </p>
              <p className="mt-1 text-[12.5px] text-tx2">
                End to end, per answered question.
              </p>
              {current.length > 1 && (
                <div className="mt-4 h-[36px]">
                  <Sparkline
                    points={current.map((r) => r.avg_latency_ms ?? 0)}
                    label="Average latency over the selected range"
                  />
                </div>
              )}
              <p className="mt-4 border-t border-line pt-3 text-[11.5px] leading-relaxed text-tx3">
                A mean of daily means, which weights a quiet day the same as a
                busy one. Days with no recorded latency are omitted rather than
                counted as zero.
              </p>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
