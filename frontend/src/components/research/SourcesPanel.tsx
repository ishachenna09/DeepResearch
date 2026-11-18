/**
 * What the researcher actually read, grouped by round.
 *
 * Provenance is a product feature, not debug output: `cached` proves the Redis
 * page cache did its job, `injected` proves the guardrail fired, and a `stage`
 * + `error` pair is the difference between "we never found it" and "the site
 * said 403".
 */
import { hostOf } from "../../lib/format";
import type { Source } from "../../lib/types";

function Badge({ children, tone }: { children: string; tone: string }) {
  return <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${tone}`}>{children}</span>;
}

function Row({ source }: { source: Source }) {
  const failed = Boolean(source.error);
  return (
    <li className="border-t border-slate-800 py-2 text-sm first:border-t-0">
      <div className="flex flex-wrap items-center gap-2">
        {source.url ? (
          <a
            href={source.url}
            target="_blank"
            rel="noreferrer"
            className={`truncate font-medium hover:underline ${failed ? "text-slate-400" : "text-slate-200"}`}
          >
            {source.title || hostOf(source.url)}
          </a>
        ) : (
          <span className="text-slate-400">(no url)</span>
        )}
        {source.cached && <Badge tone="bg-slate-800 text-slate-400">cache</Badge>}
        {source.injected && <Badge tone="bg-amber-950 text-amber-300">injection flagged</Badge>}
        {typeof source.chars === "number" && (
          <span className="text-[11px] text-slate-500">{source.chars} chars</span>
        )}
        {source.sub_question && (
          <span className="ml-auto max-w-[45%] truncate text-[11px] text-slate-500" title={source.sub_question}>
            {source.sub_question}
          </span>
        )}
      </div>
      {source.error && (
        <p className="mt-1 text-xs text-rose-300">
          {source.stage ?? "error"}: {source.error}
        </p>
      )}
    </li>
  );
}

export default function SourcesPanel({ sources }: { sources: Source[] }) {
  if (sources.length === 0) {
    return (
      <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
        <h2 className="mb-2 font-semibold">Sources</h2>
        <p className="text-sm text-slate-500">No pages fetched yet.</p>
      </section>
    );
  }

  const rounds = new Map<number, Source[]>();
  for (const source of sources) {
    const round = Number(source.round ?? 1);
    rounds.set(round, [...(rounds.get(round) ?? []), source]);
  }

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="font-semibold">Sources</h2>
        <span className="text-xs text-slate-500">
          {sources.filter((s) => s.url && !s.error).length} read ·{" "}
          {sources.filter((s) => s.error).length} failed
        </span>
      </div>
      <div className="space-y-5">
        {[...rounds.keys()].sort((a, b) => a - b).map((round) => {
          const items = rounds.get(round) ?? [];
          return (
            <div key={round}>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
                Round {round} {round === 1 ? "· approved plan" : "· gap-filling"}
              </h3>
              <ul>
                {items.map((source, index) => (
                  <Row key={`${source.url ?? "none"}-${index}`} source={source} />
                ))}
              </ul>
            </div>
          );
        })}
      </div>
    </section>
  );
}
