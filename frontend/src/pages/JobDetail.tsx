/**
 * One job, from draft plan to stored evidence.
 *
 * The page is a state machine mirroring the API's status machine:
 *   planning           -> skeleton ("the planner is thinking")
 *   awaiting_approval  -> PlanEditor (the HITL gate)
 *   researching        -> live RunProgress (polled every 2 s)
 *   notes_ready        -> verdict + sources + evidence
 *   rejected / failed  -> a terminal explanation, no fake progress
 */
import { Link, useParams } from "react-router-dom";

import NotesPanel from "../components/research/NotesPanel";
import PlanEditor from "../components/research/PlanEditor";
import RunProgress from "../components/research/RunProgress";
import SourcesPanel from "../components/research/SourcesPanel";
import StatusBadge from "../components/research/StatusBadge";
import ReportPanel from "../components/report/ReportPanel";
import { useJob } from "../hooks/useJob";
import { timeAgo } from "../lib/format";
import type { JobStatus } from "../lib/types";

/** Every status this page has an explicit section for. */
const HANDLED_STATUSES: JobStatus[] = [
  "planning",
  "awaiting_approval",
  "researching",
  "notes_ready",
  "rejected",
  "failed",
];

export default function JobDetail() {
  const { jobId } = useParams<{ jobId: string }>();
  const { job, error, loading, refresh, applyServerJob } = useJob(jobId);

  if (!jobId) {
    return <p className="text-sm text-rose-300">No job id in the URL.</p>;
  }

  if (loading && !job) {
    return <p className="text-sm text-slate-500">Loading job…</p>;
  }

  if (error && !job) {
    return (
      <div className="space-y-3">
        <Link to="/" className="text-sm text-indigo-400 hover:underline">
          ← All research
        </Link>
        <p className="rounded border border-rose-900 bg-rose-950/60 px-3 py-2 text-sm text-rose-200">
          {error}
        </p>
      </div>
    );
  }

  if (!job) return null;

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <Link to="/" className="text-sm text-indigo-400 hover:underline">
          ← All research
        </Link>
        <button
          type="button"
          onClick={() => void refresh()}
          className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-400 hover:bg-slate-800"
        >
          Refresh
        </button>
      </div>

      <header className="rounded-xl border border-slate-800 bg-slate-900 p-6">
        <div className="mb-2 flex flex-wrap items-center gap-3">
          <StatusBadge status={job.status} />
          <span className="font-mono text-[11px] text-slate-500">{job.job_id}</span>
          <span className="text-xs text-slate-500">created {timeAgo(job.created_at)}</span>
        </div>
        <h1 className="text-xl font-bold leading-snug">{job.question}</h1>
      </header>

      {error && (
        <p className="rounded border border-amber-900 bg-amber-950/60 px-3 py-2 text-sm text-amber-200">
          Polling hiccup (retrying): {error}
        </p>
      )}

      {job.status === "planning" && (
        <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
          <p className="text-sm text-slate-400">
            The planner is drafting sub-questions. This page refreshes itself.
          </p>
        </section>
      )}

      {job.status === "awaiting_approval" && job.plan && (
        <PlanEditor job={job} onDone={applyServerJob} />
      )}

      {(job.status === "researching" || job.status === "notes_ready") && <RunProgress job={job} />}

      {/* The report sits above the raw evidence: it is the answer, the notes are the proof. */}
      {job.status === "notes_ready" && (
        <ReportPanel jobId={job.job_id} noteCount={job.notes_count} />
      )}

      {job.status === "notes_ready" && <NotesPanel jobId={job.job_id} />}

      {job.status === "notes_ready" && <SourcesPanel sources={job.sources ?? []} />}

      {job.status === "researching" && (job.sources?.length ?? 0) > 0 && (
        <SourcesPanel sources={job.sources ?? []} />
      )}

      {job.status === "rejected" && (
        <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
          <h2 className="mb-2 font-semibold">Plan rejected</h2>
          <p className="text-sm text-slate-400">
            Nothing was searched and no notes were stored. Ask a new question to start over.
          </p>
        </section>
      )}

      {job.status === "failed" && (
        <section className="rounded-xl border border-rose-900 bg-rose-950/40 p-6">
          <h2 className="mb-2 font-semibold text-rose-200">Run failed</h2>
          <p className="text-sm text-rose-200/90">{job.error ?? "No error detail was recorded."}</p>
          <p className="mt-2 text-sm text-slate-400">
            The job is terminal - re-asking the question creates a fresh run.
          </p>
        </section>
      )}

      {/*
        Safety net: the API's status is a plain string, so a row written by an
        older build (or a status added server-side first) must degrade into an
        explanation instead of an empty page.
      */}
      {!HANDLED_STATUSES.includes(job.status) && (
        <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
          <h2 className="mb-2 font-semibold">Nothing to show for “{job.status}”</h2>
          <p className="text-sm text-slate-400">
            This job was written by an older version of the API and never reached a status this
            UI knows how to render. Re-asking the question creates a fresh run.
          </p>
          {job.notes_count > 0 && (
            <p className="mt-2 text-sm text-slate-400">
              It does have {job.notes_count} stored notes - they are still readable through the
              notes API.
            </p>
          )}
        </section>
      )}
    </div>
  );
}
