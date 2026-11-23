import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import api from "../lib/api";
import { formatTokens, timeAgo } from "../lib/format";
import { useAuthStore } from "../store/auth";

interface AdminOverview {
  generated_at: string;
  window: { days: number; from: string; to: string };
  users: { total: number; active: number; new: number };
  jobs: {
    total: number;
    by_status: Record<string, number>;
    failed: number;
    notes: number;
    tokens: number;
  };
  reports: {
    total: number;
    by_status: Record<string, number>;
    ready: number;
    failed: number;
    success_rate: number | null;
    tokens: number;
  };
  tokens: { research: number; report: number; total: number };
  cost: {
    estimated_usd: number;
    research_usd: number;
    report_usd: number;
    research_tokens_priced_as: string;
    pricing: { as_of: string; basis: string; output_share: number };
  };
  by_day: { day: string; jobs: number; reports: number; tokens: number }[];
  by_model: {
    model: string;
    reports: number;
    ready: number;
    failed: number;
    tokens: number;
    estimated_usd: number;
  }[];
  top_users: {
    user_id: string;
    email: string;
    jobs: number;
    reports: number;
    notes: number;
    tokens: number;
  }[];
  recent_failures: {
    kind: "job" | "report";
    id: string;
    job_id: string;
    question: string;
    error: string | null;
    created_at: string;
  }[];
}

const usd = (value: number) =>
  value === 0 ? "$0.00" : `$${value.toFixed(value < 1 ? 4 : 2)}`;

function Card({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-lg bg-slate-950 p-4">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-bold">{value}</div>
      {hint && <div className="text-xs text-slate-500">{hint}</div>}
    </div>
  );
}

export default function Admin() {
  const user = useAuthStore((state) => state.user);
  const [days, setDays] = useState(30);
  const [data, setData] = useState<AdminOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (user?.role !== "admin") {
      setLoading(false);
      return;
    }
    let live = true;
    setLoading(true);
    setError(null);
    api
      .get<AdminOverview>("/admin/overview", { params: { days }, timeout: 30_000 })
      .then((response) => live && setData(response.data))
      .catch(() => live && setError("Could not load the admin overview."))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [days, user?.role]);

  if (user?.role !== "admin") {
    return (
      <div className="rounded-xl border border-slate-800 bg-slate-900 p-8 text-center">
        <h1 className="text-lg font-bold">Admin only</h1>
        <p className="mt-2 text-sm text-slate-400">
          The overview shows every user's questions and cost. Your role is{" "}
          <span className="uppercase text-indigo-400">{user?.role ?? "unknown"}</span>.
        </p>
      </div>
    );
  }

  const maxTokens = Math.max(1, ...(data?.by_day.map((day) => day.tokens) ?? [1]));

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold">Admin overview</h1>
          <p className="text-sm text-slate-400">
            One snapshot of every user, job and token in the window.
          </p>
        </div>
        <div className="flex items-center gap-2 text-sm">
          <select
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
            className="rounded border border-slate-700 bg-slate-900 px-3 py-1.5"
          >
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </select>
        </div>
      </div>

      {loading && !data ? (
        <p className="text-sm text-slate-400">Loading…</p>
      ) : error ? (
        <p className="text-sm text-rose-400">{error}</p>
      ) : data ? (
        <>
          <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
              <Card label="Users" value={String(data.users.total)} hint={`${data.users.active} active`} />
              <Card label="Jobs" value={String(data.jobs.total)} hint={`${data.jobs.notes.toLocaleString()} notes`} />
              <Card
                label="Reports"
                value={`${data.reports.ready}/${data.reports.total}`}
                hint="ready / generated"
              />
              <Card label="Tokens" value={formatTokens(data.tokens.total)} hint={`${formatTokens(data.tokens.research)} research`} />
              <Card label="Shadow cost" value={usd(data.cost.estimated_usd)} hint="at list prices" />
              <Card
                label="Report success"
                value={data.reports.success_rate === null ? "—" : `${Math.round(data.reports.success_rate * 100)}%`}
                hint={`${data.reports.failed} failed`}
              />
            </div>
            <p className="mt-4 text-xs text-slate-500">
              Window {days} days · generated {timeAgo(data.generated_at)} · failed jobs and
              reports are counted, because they burned tokens too.
            </p>
          </section>

          <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
            <h2 className="mb-1 font-semibold">Activity</h2>
            <p className="mb-5 text-xs text-slate-500">
              Tokens per day (research + report). Hover a bar for the split.
            </p>
            {data.by_day.length === 0 ? (
              <p className="text-sm text-slate-400">No activity in this window.</p>
            ) : (
              <div className="flex h-32 items-end gap-1">
                {data.by_day.map((day) => (
                  <div
                    key={day.day}
                    className="group flex h-full flex-1 items-end"
                    title={`${day.day}: ${day.jobs} job(s), ${day.reports} report(s), ${day.tokens.toLocaleString()} tokens`}
                  >
                    <div
                      className="w-full rounded-t bg-indigo-600/70 transition group-hover:bg-indigo-500"
                      style={{ height: `${Math.max(2, (day.tokens / maxTokens) * 100)}%` }}
                    />
                  </div>
                ))}
              </div>
            )}
            {data.by_day.length > 0 && (
              <div className="mt-2 flex justify-between text-xs text-slate-500">
                <span>{data.by_day[0].day}</span>
                <span>{data.by_day[data.by_day.length - 1].day}</span>
              </div>
            )}
          </section>

          <div className="grid gap-6 lg:grid-cols-2">
            <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
              <h2 className="mb-4 font-semibold">By model</h2>
              {data.by_model.length === 0 ? (
                <p className="text-sm text-slate-400">No reports in this window.</p>
              ) : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-slate-800 text-left text-xs uppercase text-slate-500">
                      <th className="py-2">Model</th>
                      <th className="py-2 text-right">Ready</th>
                      <th className="py-2 text-right">Failed</th>
                      <th className="py-2 text-right">Tokens</th>
                      <th className="py-2 text-right">Est.</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_model.map((row) => (
                      <tr key={row.model} className="border-b border-slate-800/60">
                        <td className="py-2 font-mono text-xs">{row.model}</td>
                        <td className="py-2 text-right">{row.ready}</td>
                        <td className={`py-2 text-right ${row.failed ? "text-rose-400" : ""}`}>
                          {row.failed}
                        </td>
                        <td className="py-2 text-right">{row.tokens.toLocaleString()}</td>
                        <td className="py-2 text-right">{usd(row.estimated_usd)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>

            <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
              <h2 className="mb-4 font-semibold">Top users by tokens</h2>
              {data.top_users.length === 0 ? (
                <p className="text-sm text-slate-400">No accounts were active in this window.</p>
              ) : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-slate-800 text-left text-xs uppercase text-slate-500">
                      <th className="py-2">User</th>
                      <th className="py-2 text-right">Jobs</th>
                      <th className="py-2 text-right">Reports</th>
                      <th className="py-2 text-right">Tokens</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.top_users.map((row) => (
                      <tr key={row.user_id} className="border-b border-slate-800/60">
                        <td className="py-2 text-xs">{row.email}</td>
                        <td className="py-2 text-right">{row.jobs}</td>
                        <td className="py-2 text-right">{row.reports}</td>
                        <td className="py-2 text-right">{row.tokens.toLocaleString()}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
          </div>

          <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
            <h2 className="mb-4 font-semibold">Recent failures</h2>
            {data.recent_failures.length === 0 ? (
              <p className="text-sm text-emerald-400">No failures in this window.</p>
            ) : (
              <ul className="space-y-3">
                {data.recent_failures.map((failure) => (
                  <li
                    key={`${failure.kind}-${failure.id}`}
                    className="rounded-lg border border-slate-800 bg-slate-950 p-3"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <Link
                        to={`/jobs/${failure.job_id}`}
                        className="text-sm font-medium hover:text-indigo-300"
                      >
                        {failure.question}
                      </Link>
                      <span className="shrink-0 rounded bg-rose-950 px-2 py-0.5 text-xs font-semibold text-rose-400">
                        {failure.kind}
                      </span>
                    </div>
                    <p className="mt-1 text-xs text-slate-400">{failure.error || "no error recorded"}</p>
                    <p className="mt-1 text-xs text-slate-600">{timeAgo(failure.created_at)}</p>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <p className="text-xs text-slate-500">
            {data.cost.pricing.basis}. Research tokens are priced as{" "}
            <span className="font-mono">{data.cost.research_tokens_priced_as}</span>; report
            tokens by the model that served them. Price table as of{" "}
            {data.cost.pricing.as_of}.
          </p>
        </>
      ) : null}
    </div>
  );
}
