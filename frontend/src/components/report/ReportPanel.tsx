/**
 * The report: generate it, watch it stream, then read it with its audit.
 *
 * Three things this panel insists on showing, because they are the phase's claim:
 * 1. **The text arrives live** - tokens stream in, so a 30-90 s generation is not
 *    a spinner.
 * 2. **Precision is measured, not asserted** - the badge says how many markers
 *    resolved to a real source and which ones did not.
 * 3. **Every citation is a link back to a stored note** - click a `[n]` chip and
 *    land on the source with its claim and verbatim quote.
 */
import { useReport } from "../../hooks/useReport";
import { formatTokens, hostOf, timeAgo } from "../../lib/format";
import type { Citation, ReportValidation } from "../../lib/types";
import Markdown from "./Markdown";

const PHASE_BADGE: Record<string, { label: string; className: string }> = {
  generating: {
    label: "Writing",
    className: "border-sky-800 bg-sky-950 text-sky-300",
  },
  ready: {
    label: "Ready",
    className: "border-emerald-800 bg-emerald-950 text-emerald-300",
  },
  failed: {
    label: "Failed",
    className: "border-rose-800 bg-rose-950 text-rose-300",
  },
};

function Stat({
  label,
  value,
  hint,
  tone = "text-slate-100",
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: string;
}) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`text-lg font-semibold ${tone}`}>{value}</div>
      {hint && <div className="text-[11px] text-slate-500">{hint}</div>}
    </div>
  );
}

function precisionTone(precision: number): string {
  if (precision >= 1) return "text-emerald-300";
  if (precision >= 0.9) return "text-amber-300";
  return "text-rose-300";
}

function coverageTone(coverage: number): string {
  if (coverage >= 0.7) return "text-emerald-300";
  if (coverage >= 0.5) return "text-amber-300";
  return "text-rose-300";
}

function AuditStats({ validation }: { validation: ReportValidation }) {
  const pct = (value: number) => `${Math.round(value * 100)}%`;
  return (
    <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
      <Stat
        label="Citations"
        value={String(validation.valid_markers)}
        hint={`${validation.unused_sources} source${validation.unused_sources === 1 ? "" : "s"} unused`}
      />
      <Stat
        label="Precision"
        value={pct(validation.precision)}
        hint={
          validation.uncited
            ? "no markers in the text"
            : validation.unknown_markers.length
              ? `invented: ${validation.unknown_markers.join(", ")}`
              : "every marker resolves"
        }
        tone={precisionTone(validation.precision)}
      />
      <Stat
        label="Coverage"
        value={pct(validation.coverage)}
        hint={`${validation.cited_sentences}/${validation.sentences} sentences cited`}
        tone={coverageTone(validation.coverage)}
      />
      <Stat
        label="Length"
        value={`${validation.words} w`}
        hint={`${formatTokens(validation.markers)} markers`}
      />
    </div>
  );
}

function SourceList({ citations }: { citations: Citation[] }) {
  if (!citations.length) return null;
  return (
    <div className="mt-6 border-t border-slate-800 pt-4">
      <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-500">
        Sources cited ({citations.length})
      </h3>
      <ol className="space-y-2">
        {citations.map((citation) => (
          <li
            key={citation.note_id}
            id={`cite-${citation.index}`}
            className="scroll-mt-4 rounded-lg border border-slate-800 bg-slate-950/60 p-3"
          >
            <div className="flex gap-3">
              <span className="mt-0.5 flex h-5 min-w-5 items-center justify-center rounded bg-indigo-950 px-1 text-[11px] font-semibold text-indigo-300 ring-1 ring-indigo-800">
                {citation.index}
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-sm text-slate-200">{citation.claim}</p>
                {citation.quote && (
                  <details className="mt-1.5">
                    <summary className="cursor-pointer text-[11px] text-slate-500 hover:text-slate-300">
                      verbatim quote
                    </summary>
                    <blockquote className="mt-1 border-l-2 border-slate-700 pl-3 text-xs italic text-slate-400">
                      “{citation.quote}”
                    </blockquote>
                  </details>
                )}
                <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                  <span className="uppercase tracking-wide">{citation.confidence}</span>
                  <a
                    href={citation.url}
                    target="_blank"
                    rel="noreferrer"
                    className="truncate hover:text-slate-300 hover:underline"
                  >
                    {citation.title || hostOf(citation.url)}
                  </a>
                  <span className="truncate text-slate-600" title={citation.sub_question}>
                    {citation.sub_question}
                  </span>
                </div>
              </div>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

export default function ReportPanel({
  jobId,
  noteCount,
}: {
  jobId: string;
  noteCount: number;
}) {
  const { phase, draft, report, citations, validation, progress, streaming, error, waited, generate, retry } =
    useReport(jobId);
  const badge = PHASE_BADGE[phase];
  const busy = phase === "generating";

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-semibold">Report</h2>
          {badge && (
            <span
              className={`rounded border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide ${badge.className}`}
            >
              {badge.label}
            </span>
          )}
          {validation?.repaired && (
            <span
              title="The first draft drifted off our citation format or was cut off, so a second attempt rewrote it. The better-scoring draft was kept."
              className="rounded border border-amber-800 bg-amber-950 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-amber-300"
            >
              second attempt
            </span>
          )}
          {validation?.truncated && (
            <span
              title="The model hit its token ceiling, so this text may end mid-sentence. Regenerate to try again."
              className="rounded border border-rose-800 bg-rose-950 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-rose-300"
            >
              truncated
            </span>
          )}
          {validation?.uncited && (
            <span
              title="No citation marker in this text resolves to a stored source. The prose is readable but it is not traceable evidence - regenerate to try again."
              className="rounded border border-rose-800 bg-rose-950 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-rose-300"
            >
              no citations
            </span>
          )}
          {progress?.repairing && (
            <span className="animate-pulse text-[11px] text-amber-300">
              fixing citations…
            </span>
          )}
        </div>

        {phase === "ready" || phase === "failed" ? (
          <button
            type="button"
            onClick={() => void generate()}
            className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
          >
            Regenerate
          </button>
        ) : null}
      </div>

      {error && (
        <p className="mb-3 rounded border border-rose-900 bg-rose-950/60 px-3 py-2 text-sm text-rose-200">
          {error}
        </p>
      )}

      {/*
        The loading state has to be honest about duration. The API's database is
        a free-tier instance in another region: it suspends when idle, and the
        first request of the day costs seconds, not milliseconds. A sentence that
        never changes reads as a hang, so this counts.
      */}
      {phase === "loading" && (
        <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-4">
          <p className="text-sm text-slate-400">
            Checking for a report…{" "}
            {waited >= 2 && <span className="tabular-nums text-slate-500">{waited}s</span>}
          </p>
          {waited >= 6 && (
            <p className="mt-1 text-xs text-slate-500">
              Still waiting on the API. The free-tier database suspends when idle, so the first
              request after a break can take a few seconds to wake it.
            </p>
          )}
          {waited >= 12 && (
            <button
              type="button"
              onClick={() => void retry()}
              className="mt-2 rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
            >
              Ask again
            </button>
          )}
        </div>
      )}

      {phase === "none" && !error && (
        <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-4">
          <p className="text-sm text-slate-300">
            No report yet. The writer reads the {noteCount} stored notes, numbers them as sources,
            and streams a cited brief.
          </p>
          <p className="mt-1 text-xs text-slate-500">
            Expect 30-90 seconds: the first token can take a minute while the model starts, then the
            text types itself out. Every claim must carry a <code className="text-slate-400">[n]</code>{" "}
            marker or it is flagged by the audit.
          </p>
          <button
            type="button"
            onClick={() => void generate()}
            disabled={noteCount === 0}
            className="mt-3 rounded bg-indigo-600 px-4 py-2 text-sm font-semibold hover:bg-indigo-500 disabled:opacity-50"
          >
            {noteCount === 0 ? "No notes to write from" : "Generate report"}
          </button>
        </div>
      )}

      {busy && validation === null && progress && (
        <p className="mb-3 text-xs text-slate-500">
          writing from {progress.notes ?? noteCount} notes · {progress.sources ?? "?"} numbered
          sources
        </p>
      )}

      {busy && !draft && (
        <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-4">
          <p className="animate-pulse text-sm text-sky-300">Waiting for the first token…</p>
          <p className="mt-1 text-xs text-slate-500">
            The stream is connected. A cold model can take up to two minutes before it emits text -
            this is provider latency, not a hang.
          </p>
        </div>
      )}

      {(draft || phase === "ready") && (
        <>
          {validation && <AuditStats validation={validation} />}
          <article className="rounded-lg border border-slate-800 bg-slate-950/40 p-4">
            <Markdown markdown={draft} citations={citations} caret={streaming} />
          </article>
          {report && (
            <p className="mt-2 text-[11px] text-slate-500">
              {/* The proxy fails over between providers; naming the model that
                  actually answered makes a silent fallback visible. */}
              {report.model ? `served by ${report.model} · ` : ""}
              {formatTokens(report.tokens_used)} tokens · updated {timeAgo(report.updated_at)}
            </p>
          )}
          <SourceList citations={citations} />
        </>
      )}

      {phase === "failed" && !draft && (
        <div className="rounded-lg border border-rose-900 bg-rose-950/40 p-4">
          <p className="text-sm text-rose-200">
            {report?.error ?? error ?? "Report generation failed."}
          </p>
          <p className="mt-1 text-xs text-slate-400">
            The research is unaffected - the notes are still stored. Regenerate when ready.
          </p>
          <button
            type="button"
            onClick={() => void retry()}
            className="mt-3 rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
          >
            Check again
          </button>
        </div>
      )}

      {/*
        Load failed (timeout, dead connection) but the report may well exist -
        saying "No report yet" here would be a lie, so this state is separate.
      */}
      {phase === "none" && error && (
        <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-4">
          <p className="text-sm text-slate-300">
            Could not read the report. The request failed, so this page cannot tell whether one
            exists.
          </p>
          <button
            type="button"
            onClick={() => void retry()}
            className="mt-3 rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
          >
            Try again
          </button>
        </div>
      )}
    </section>
  );
}
