/**
 * The evidence: atomic notes with a verbatim quote and a link back to the page.
 *
 * Two ways to read them:
 * - browse, grouped by sub-question (what the researcher found per angle)
 * - semantic search (what the researcher found *about a claim*), which is the
 *   Phase 3 kNN endpoint and the reason vectors live in Postgres
 */
import { useEffect, useMemo, useState } from "react";

import { hostOf } from "../../lib/format";
import { apiError, listNotes, searchNotes } from "../../lib/research";
import type { Note } from "../../lib/types";

const CONFIDENCE_TONE: Record<string, string> = {
  high: "bg-emerald-950 text-emerald-300",
  medium: "bg-slate-800 text-slate-300",
  low: "bg-amber-950 text-amber-300",
};

function NoteCard({ note }: { note: Note }) {
  return (
    <li className="rounded-lg border border-slate-800 bg-slate-950/60 p-3">
      <p className="text-sm text-slate-100">{note.note}</p>
      {note.quote && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-slate-400 hover:text-slate-200">
            verbatim quote
          </summary>
          <blockquote className="mt-1 border-l-2 border-slate-700 pl-3 text-xs italic text-slate-400">
            “{note.quote}”
          </blockquote>
        </details>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
        <span className={`rounded px-1.5 py-0.5 font-semibold ${CONFIDENCE_TONE[note.confidence] ?? CONFIDENCE_TONE.medium}`}>
          {note.confidence}
        </span>
        <a href={note.url} target="_blank" rel="noreferrer" className="hover:text-slate-300 hover:underline">
          {note.title || hostOf(note.url)}
        </a>
        {typeof note.score === "number" && (
          <span className="ml-auto font-mono text-slate-400">cos {note.score.toFixed(3)}</span>
        )}
      </div>
    </li>
  );
}

export default function NotesPanel({ jobId }: { jobId: string }) {
  const [notes, setNotes] = useState<Note[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<Note[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    listNotes(jobId)
      .then((response) => {
        if (cancelled) return;
        setNotes(response.notes);
        setTotal(response.count);
      })
      .catch((err) => !cancelled && setError(apiError(err)));
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  const grouped = useMemo(() => {
    const groups = new Map<string, Note[]>();
    for (const note of notes ?? []) {
      groups.set(note.sub_question, [...(groups.get(note.sub_question) ?? []), note]);
    }
    return [...groups.entries()].sort((a, b) => b[1].length - a[1].length);
  }, [notes]);

  const runSearch = async () => {
    const trimmed = query.trim();
    if (trimmed.length < 3) {
      setError("Search needs at least 3 characters.");
      return;
    }
    setSearching(true);
    setError(null);
    try {
      const response = await searchNotes(jobId, trimmed);
      setResults(response.results);
    } catch (err) {
      setError(apiError(err));
    } finally {
      setSearching(false);
    }
  };

  if (error && !notes) {
    return (
      <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
        <h2 className="mb-2 font-semibold">Evidence</h2>
        <p className="text-sm text-rose-300">{error}</p>
      </section>
    );
  }
  if (!notes) {
    return (
      <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
        <h2 className="mb-2 font-semibold">Evidence</h2>
        <p className="text-sm text-slate-500">Loading notes…</p>
      </section>
    );
  }

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-semibold">
          Evidence <span className="text-sm font-normal text-slate-500">({total} notes)</span>
        </h2>
        <div className="flex items-center gap-2">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => event.key === "Enter" && void runSearch()}
            placeholder="semantic search, e.g. hnsw recall"
            className="w-56 rounded border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm outline-none focus:border-indigo-500"
          />
          <button
            type="button"
            onClick={() => void runSearch()}
            disabled={searching}
            className="rounded bg-indigo-600 px-3 py-1.5 text-sm font-semibold hover:bg-indigo-500 disabled:opacity-50"
          >
            {searching ? "…" : "Search"}
          </button>
        </div>
      </div>

      {error && <p className="mb-3 text-sm text-rose-300">{error}</p>}

      {results ? (
        <div>
          <div className="mb-3 flex items-center justify-between text-xs text-slate-500">
            <span>
              {results.length} nearest note{results.length === 1 ? "" : "s"} to “{query.trim()}”
            </span>
            <button
              type="button"
              onClick={() => setResults(null)}
              className="rounded border border-slate-700 px-2 py-1 hover:bg-slate-800"
            >
              Clear search
            </button>
          </div>
          <ul className="space-y-2">
            {results.map((note) => (
              <NoteCard key={note.id} note={note} />
            ))}
          </ul>
        </div>
      ) : (
        <div className="space-y-5">
          {grouped.map(([subQuestion, items]) => (
            <div key={subQuestion}>
              <h3 className="mb-2 text-xs font-semibold text-slate-400">
                {subQuestion} <span className="text-slate-600">· {items.length}</span>
              </h3>
              <ul className="space-y-2">
                {items.map((note) => (
                  <NoteCard key={note.id} note={note} />
                ))}
              </ul>
            </div>
          ))}
          {grouped.length === 0 && <p className="text-sm text-slate-500">No notes stored.</p>}
        </div>
      )}
    </section>
  );
}
