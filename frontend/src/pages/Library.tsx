import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { downloadReport, listReports } from "../lib/library";
import { timeAgo } from "../lib/format";
import type { ReportPage, ReportStatus } from "../lib/types";

const PAGE_SIZE = 10;

const STATUS_STYLE: Record<ReportStatus, string> = {
  ready: "bg-emerald-950 text-emerald-400",
  generating: "bg-amber-950 text-amber-400",
  failed: "bg-rose-950 text-rose-400",
};

function StatusBadge({ status }: { status: ReportStatus }) {
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-semibold ${STATUS_STYLE[status]}`}>
      {status}
    </span>
  );
}

export default function Library() {
  const [page, setPage] = useState<ReportPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<"" | ReportStatus>("");
  const [sort, setSort] = useState<"recent" | "oldest" | "tokens">("recent");
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    listReports({ limit: PAGE_SIZE, offset, q, status, sort })
      .then((data) => {
        if (live) setPage(data);
      })
      .catch(() => {
        if (live) setError("Could not load the library.");
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [offset, q, status, sort]);

  const submitSearch = (event: FormEvent) => {
    event.preventDefault();
    setOffset(0);
    setQ(qInput.trim());
  };

  const clearFilters = () => {
    setQInput("");
    setQ("");
    setStatus("");
    setSort("recent");
    setOffset(0);
  };

  const download = async (reportId: string, format: "md" | "pdf") => {
    const key = `${reportId}:${format}`;
    setBusy(key);
    setNotice(null);
    setError(null);
    try {
      const filename = await downloadReport(reportId, format);
      setNotice(`Downloaded ${filename}`);
    } catch {
      setError(`The ${format.toUpperCase()} export failed. Try regenerating the report.`);
    } finally {
      setBusy(null);
    }
  };

  const filtered = Boolean(q || status || sort !== "recent");
  const first = page && page.items.length ? page.offset + 1 : 0;
  const last = page ? page.offset + page.items.length : 0;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold">Report library</h1>
          <p className="text-sm text-slate-400">
            Every cited brief you have generated, with the audit that shipped it.
          </p>
        </div>
        {page && (
          <span className="text-sm text-slate-500">
            {page.total} report{page.total === 1 ? "" : "s"}
          </span>
        )}
      </div>

      <form onSubmit={submitSearch} className="flex flex-wrap items-center gap-2">
        <input
          value={qInput}
          onChange={(event) => setQInput(event.target.value)}
          placeholder="Search the question…"
          className="min-w-56 flex-1 rounded border border-slate-700 bg-slate-900 px-3 py-2 text-sm outline-none focus:border-indigo-500"
        />
        <button
          type="submit"
          className="rounded bg-indigo-600 px-4 py-2 text-sm font-semibold hover:bg-indigo-500"
        >
          Search
        </button>
        <select
          value={status}
          onChange={(event) => {
            setStatus(event.target.value as "" | ReportStatus);
            setOffset(0);
          }}
          className="rounded border border-slate-700 bg-slate-900 px-3 py-2 text-sm"
        >
          <option value="">All statuses</option>
          <option value="ready">Ready</option>
          <option value="generating">Generating</option>
          <option value="failed">Failed</option>
        </select>
        <select
          value={sort}
          onChange={(event) => {
            setSort(event.target.value as "recent" | "oldest" | "tokens");
            setOffset(0);
          }}
          className="rounded border border-slate-700 bg-slate-900 px-3 py-2 text-sm"
        >
          <option value="recent">Newest first</option>
          <option value="oldest">Oldest first</option>
          <option value="tokens">Most tokens</option>
        </select>
        {filtered && (
          <button
            type="button"
            onClick={clearFilters}
            className="rounded border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800"
          >
            Clear
          </button>
        )}
      </form>

      {notice && <p className="text-sm text-emerald-400">{notice}</p>}
      {error && <p className="text-sm text-rose-400">{error}</p>}

      {loading && !page ? (
        <p className="text-sm text-slate-400">Loading…</p>
      ) : page && page.items.length === 0 ? (
        <div className="rounded-xl border border-slate-800 bg-slate-900 p-8 text-center">
          <p className="font-medium">
            {filtered ? "No reports match those filters." : "No reports yet."}
          </p>
          <p className="mt-1 text-sm text-slate-400">
            {filtered ? (
              "Try a broader search or clear the filters."
            ) : (
              <>
                Finish a research job, then generate its report from the{" "}
                <Link to="/" className="text-indigo-400 hover:underline">
                  research page
                </Link>
                .
              </>
            )}
          </p>
        </div>
      ) : (
        <div className={`space-y-3 ${loading ? "opacity-60" : ""}`}>
          {page?.items.map((item) => (
            <article
              key={item.report_id}
              className="rounded-xl border border-slate-800 bg-slate-900 p-4"
            >
              <div className="flex items-start justify-between gap-4">
                <Link
                  to={`/jobs/${item.job_id}`}
                  className="font-medium leading-snug hover:text-indigo-300"
                >
                  {item.question}
                </Link>
                <StatusBadge status={item.status} />
              </div>

              <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
                <span>{item.words.toLocaleString()} words</span>
                <span>{item.citations} sources</span>
                <span>precision {item.precision.toFixed(2)}</span>
                <span>coverage {item.coverage.toFixed(2)}</span>
                <span>{item.tokens_used.toLocaleString()} tokens</span>
                <span className="font-mono">{item.model ?? "unknown model"}</span>
                <span>{timeAgo(item.updated_at)}</span>
              </div>

              {item.error && (
                <p className="mt-2 line-clamp-2 text-xs text-rose-400">{item.error}</p>
              )}

              <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
                <Link
                  to={`/jobs/${item.job_id}`}
                  className="rounded border border-slate-700 px-3 py-1.5 hover:bg-slate-800"
                >
                  Open report
                </Link>
                <button
                  onClick={() => download(item.report_id, "md")}
                  disabled={item.status !== "ready" || busy === `${item.report_id}:md`}
                  className="rounded border border-slate-700 px-3 py-1.5 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {busy === `${item.report_id}:md` ? "Building…" : "Markdown"}
                </button>
                <button
                  onClick={() => download(item.report_id, "pdf")}
                  disabled={item.status !== "ready" || busy === `${item.report_id}:pdf`}
                  className="rounded border border-slate-700 px-3 py-1.5 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {busy === `${item.report_id}:pdf` ? "Building…" : "PDF"}
                </button>
                {item.status !== "ready" && (
                  <span className="text-xs text-slate-500">exports need a ready report</span>
                )}
              </div>
            </article>
          ))}
        </div>
      )}

      {page && page.total > PAGE_SIZE && (
        <div className="flex items-center justify-between text-sm text-slate-400">
          <span>
            {first}–{last} of {page.total}
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              disabled={offset === 0}
              className="rounded border border-slate-700 px-3 py-1.5 hover:bg-slate-800 disabled:opacity-40"
            >
              Previous
            </button>
            <button
              onClick={() => setOffset(offset + PAGE_SIZE)}
              disabled={last >= page.total}
              className="rounded border border-slate-700 px-3 py-1.5 hover:bg-slate-800 disabled:opacity-40"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
