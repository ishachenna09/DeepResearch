/**
 * Live progress + the critic's verdict.
 *
 * Everything shown here comes from fields the backend already records
 * (`rounds`, `tokens_used`, `sources[].round`, `critique`), so the UI explains
 * *why* a run stopped instead of only that it stopped.
 */
import { elapsedSince, formatTokens, timeAgo } from "../../lib/format";
import type { ResearchJob } from "../../lib/types";

const STOP_REASONS: Record<string, { label: string; tone: string; hint: string }> = {
  sufficient: {
    label: "Answer covered",
    tone: "border-emerald-800 bg-emerald-950/60 text-emerald-200",
    hint: "The critic judged the evidence sufficient and closed the loop early.",
  },
  no_gaps: {
    label: "No new questions",
    tone: "border-slate-700 bg-slate-800/60 text-slate-200",
    hint: "Coverage was imperfect, but the critic could not name a genuinely new question.",
  },
  max_rounds: {
    label: "Round cap reached",
    tone: "border-amber-800 bg-amber-950/60 text-amber-200",
    hint: "The gap-filling loop is capped at 2 rounds by design - each round is a real search + LLM cost.",
  },
  budget: {
    label: "Token budget reached",
    tone: "border-amber-800 bg-amber-950/60 text-amber-200",
    hint: "Spending passed 90% of the plan's budget, so the loop stopped before starting another round.",
  },
  no_progress: {
    label: "Last round added nothing new",
    tone: "border-rose-800 bg-rose-950/60 text-rose-200",
    hint: "The repeat round found fewer than 3 new notes - the search space is exhausted.",
  },
  critic_error: {
    label: "Critic failed",
    tone: "border-rose-800 bg-rose-950/60 text-rose-200",
    hint: "No usable verdict was produced, so the loop stopped rather than research blind.",
  },
  no_notes: {
    label: "No notes extracted",
    tone: "border-rose-800 bg-rose-950/60 text-rose-200",
    hint: "Pages were fetched but nothing could be extracted - check the source errors below.",
  },
};

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/60 px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="text-lg font-semibold text-slate-100">{value}</div>
      {hint && <div className="text-[11px] text-slate-500">{hint}</div>}
    </div>
  );
}

export default function RunProgress({ job }: { job: ResearchJob }) {
  const budget = job.plan?.budget_tokens ?? 0;
  const pct = budget > 0 ? Math.min(100, Math.round((job.tokens_used / budget) * 100)) : 0;
  const barTone = pct >= 90 ? "bg-rose-500" : pct >= 70 ? "bg-amber-500" : "bg-emerald-500";

  const sources = job.sources ?? [];
  const okPages = sources.filter((source) => source.url && !source.error);
  const byRound = new Map<number, number>();
  for (const page of okPages) {
    const round = Number(page.round ?? 1);
    byRound.set(round, (byRound.get(round) ?? 0) + 1);
  }
  const roundsSeen = [...byRound.keys()].sort((a, b) => a - b);
  const roundCount = Math.max(job.rounds || 1, roundsSeen[roundsSeen.length - 1] ?? 1);
  const critique = job.critique;
  const stop = critique?.stop_reason ? STOP_REASONS[critique.stop_reason] : null;

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-semibold">Run</h2>
        <span className="text-xs text-slate-500">
          started {timeAgo(job.created_at)}
          {job.status === "researching" && ` · ${elapsedSince(job.created_at)} elapsed`}
        </span>
      </div>

      <div className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Tile
          label="Rounds"
          value={`${roundCount} / 2`}
          hint={roundCount > 1 ? "gap-filling fired" : "initial fan-out only"}
        />
        <Tile label="Notes" value={String(job.notes_count)} hint="stored, deduped" />
        <Tile label="Pages" value={String(okPages.length)} hint={`${byRound.size} round(s) sourced`} />
        <Tile
          label="Tokens"
          value={formatTokens(job.tokens_used)}
          hint={budget ? `of ${formatTokens(budget)}` : "no budget set"}
        />
      </div>

      {budget > 0 && (
        <div className="mb-5">
          <div className="mb-1 flex justify-between text-xs text-slate-500">
            <span>token usage</span>
            <span>{pct}% of the plan's budget</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded bg-slate-800">
            <div className={`h-full ${barTone}`} style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}

      <div className="mb-5 flex flex-wrap items-center gap-2">
        {Array.from({ length: roundCount }, (_, i) => i + 1).map((round) => {
          const pages = byRound.get(round) ?? 0;
          const running = job.status === "researching" && round === roundCount && pages < 1;
          return (
            <span
              key={round}
              className={`rounded border px-2.5 py-1 text-xs ${
                running
                  ? "animate-pulse border-sky-800 bg-sky-950 text-sky-300"
                  : "border-slate-700 bg-slate-800 text-slate-300"
              }`}
            >
              Round {round} · {pages} page{pages === 1 ? "" : "s"}
            </span>
          );
        })}
        {job.status === "researching" && (
          <span className="text-xs text-slate-500">
            searching, reading and extracting - the page updates every 2 s
          </span>
        )}
      </div>

      {stop && critique && (
        <div className={`rounded-lg border px-4 py-3 ${stop.tone}`}>
          <div className="flex flex-wrap items-center gap-2 text-sm font-semibold">
            <span>{stop.label}</span>
            <span className="font-mono text-[11px] opacity-70">{critique.stop_reason}</span>
            <span className="text-[11px] font-normal opacity-70">
              reviewed after round {critique.round} · {critique.notes_new} new notes in that round
            </span>
          </div>
          <p className="mt-1 text-sm opacity-90">{stop.hint}</p>
          <p className="mt-2 text-sm italic opacity-80">“{critique.reason}”</p>
        </div>
      )}
    </section>
  );
}
