/**
 * Home: ask a question, and see the jobs you already asked.
 */
import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import StatusBadge from "../components/research/StatusBadge";
import { timeAgo } from "../lib/format";
import { apiError, createJob, listJobs } from "../lib/research";
import type { ResearchJob } from "../lib/types";

const EXAMPLES = [
  "How should a team choose between pgvector, Pinecone and FAISS for a production RAG system?",
  "What changed in the EU AI Act in 2026, and what does it require from a startup shipping an LLM product?",
  "Is Postgres LISTEN/NOTIFY a viable replacement for Redis pub/sub at 10k events per second?",
];

export default function Research() {
  const navigate = useNavigate();
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [jobs, setJobs] = useState<ResearchJob[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);

  useEffect(() => {
    listJobs()
      .then(setJobs)
      .catch((err) => setListError(apiError(err)));
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const trimmed = question.trim();
    if (trimmed.length < 10) {
      setError("Please write at least 10 characters - the planner needs something to work with.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const job = await createJob(trimmed);
      navigate(`/jobs/${job.job_id}`);
    } catch (err) {
      setError(apiError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <section>
        <h1 className="text-2xl font-bold">Ask a research question</h1>
        <p className="mt-1 text-sm text-slate-400">
          The planner drafts sub-questions, you approve them (and can edit them), then parallel
          agents search, read and extract evidence. A coverage critic may run one more round if it
          finds a real gap.
        </p>
      </section>

      <form onSubmit={submit} className="rounded-xl border border-slate-800 bg-slate-900 p-6">
        <label htmlFor="question" className="mb-2 block text-xs font-semibold uppercase tracking-wide text-slate-400">
          Your question
        </label>
        <textarea
          id="question"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          rows={3}
          maxLength={500}
          placeholder="e.g. How should a team choose between pgvector, Pinecone and FAISS for a production RAG system?"
          className="w-full resize-y rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
        />
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
          <span className="text-xs text-slate-500">{question.trim().length} / 500</span>
          <button
            type="submit"
            disabled={busy}
            className="rounded bg-indigo-600 px-4 py-2 text-sm font-semibold hover:bg-indigo-500 disabled:opacity-50"
          >
            {busy ? "Planning…" : "Draft a plan"}
          </button>
        </div>
        {error && (
          <p className="mt-3 rounded border border-rose-900 bg-rose-950/60 px-3 py-2 text-sm text-rose-200">
            {error}
          </p>
        )}
        <div className="mt-4 flex flex-wrap gap-2">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              onClick={() => setQuestion(example)}
              className="max-w-full truncate rounded border border-slate-700 px-2 py-1 text-xs text-slate-400 hover:bg-slate-800 hover:text-slate-200"
              title={example}
            >
              {example}
            </button>
          ))}
        </div>
      </form>

      <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
        <h2 className="mb-4 font-semibold">Your research</h2>
        {listError && <p className="text-sm text-rose-300">{listError}</p>}
        {!jobs && !listError && <p className="text-sm text-slate-500">Loading…</p>}
        {jobs?.length === 0 && (
          <p className="text-sm text-slate-500">Nothing yet - ask your first question above.</p>
        )}
        <ul className="divide-y divide-slate-800">
          {(jobs ?? []).map((job) => (
            <li key={job.job_id}>
              <Link
                to={`/jobs/${job.job_id}`}
                className="-mx-2 flex flex-wrap items-center gap-3 rounded px-2 py-3 hover:bg-slate-800/50"
              >
                <StatusBadge status={job.status} />
                <span className="min-w-0 flex-1 truncate text-sm text-slate-200">{job.question}</span>
                <span className="text-xs text-slate-500">
                  {job.notes_count} notes · {job.rounds} round{job.rounds === 1 ? "" : "s"} ·{" "}
                  {timeAgo(job.created_at)}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
