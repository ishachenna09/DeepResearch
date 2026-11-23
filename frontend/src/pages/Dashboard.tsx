import { useEffect, useState } from "react";
import api from "../lib/api";

interface Health {
  status?: string;
  api?: string;
  postgres?: string;
  redis?: string;
  overall?: string;
  served_by?: string;
  reply?: string;
  detail?: string;
  host?: string;
}

interface UsageModel {
  model: string;
  reports: number;
  tokens: number;
  estimated_usd: number;
}

interface Usage {
  scope: string;
  days: number;
  totals: {
    jobs: number;
    notes: number;
    research_tokens: number;
    report_tokens: number;
    tokens: number;
    estimated_usd: number;
  };
  research_tokens_priced_as: string;
  by_model: UsageModel[];
  pricing: { as_of: string; basis: string; output_share: number };
}

const n = (value: number) => value.toLocaleString();
const usd = (value: number) =>
  value === 0 ? "$0.00" : `$${value.toFixed(value < 1 ? 4 : 2)}`;

export default function Dashboard() {
  const [health, setHealth] = useState<Health | null>(null);
  const [llm, setLlm] = useState<Health | null>(null);
  const [langfuse, setLangfuse] = useState<Health | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    api.get("/healthz").then((r) => setHealth(r.data)).catch(() => setHealth({ overall: "unreachable" }));
    api.get("/usage").then((r) => setUsage(r.data)).catch(() => setUsage(null));
    api.get("/health/langfuse").then((r) => setLangfuse(r.data)).catch(() => setLangfuse({ status: "error" }));
  }, []);

  const checkLlm = async () => {
    setChecking(true);
    try {
      const { data } = await api.get("/health/llm");
      setLlm(data);
    } finally {
      setChecking(false);
    }
  };

  const Badge = ({ value }: { value?: string }) => {
    const good = value === "ok" || value === "healthy";
    const neutral = value === "disabled";
    return (
      <span
        className={`rounded px-2 py-0.5 text-xs font-semibold ${
          good
            ? "bg-emerald-950 text-emerald-400"
            : neutral
              ? "bg-slate-800 text-slate-400"
              : "bg-rose-950 text-rose-400"
        }`}
      >
        {value ?? "unknown"}
      </span>
    );
  };

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-bold">System status</h1>

      <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
        <h2 className="mb-4 font-semibold">Infrastructure</h2>
        <div className="flex flex-wrap gap-6 text-sm">
          <div className="flex items-center gap-2">API <Badge value={health?.api} /></div>
          <div className="flex items-center gap-2">Postgres (Neon) <Badge value={health?.postgres} /></div>
          <div className="flex items-center gap-2">Redis (checkpoints + cache) <Badge value={health?.redis} /></div>
        </div>
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
        <div className="mb-4 flex items-baseline justify-between">
          <h2 className="font-semibold">Usage &amp; shadow cost</h2>
          <span className="text-xs text-slate-500">last {usage?.days ?? 30} days</span>
        </div>

        {!usage ? (
          <p className="text-sm text-slate-400">Usage unavailable.</p>
        ) : (
          <>
            <div className="mb-5 grid grid-cols-2 gap-4 sm:grid-cols-4">
              <div className="rounded-lg bg-slate-950 p-4">
                <div className="text-xs text-slate-500">Research jobs</div>
                <div className="mt-1 text-2xl font-bold">{n(usage.totals.jobs)}</div>
                <div className="text-xs text-slate-500">{n(usage.totals.notes)} notes</div>
              </div>
              <div className="rounded-lg bg-slate-950 p-4">
                <div className="text-xs text-slate-500">Tokens</div>
                <div className="mt-1 text-2xl font-bold">{n(usage.totals.tokens)}</div>
                <div className="text-xs text-slate-500">
                  {n(usage.totals.research_tokens)} research · {n(usage.totals.report_tokens)} report
                </div>
              </div>
              <div className="rounded-lg bg-slate-950 p-4">
                <div className="text-xs text-slate-500">Shadow cost</div>
                <div className="mt-1 text-2xl font-bold">{usd(usage.totals.estimated_usd)}</div>
                <div className="text-xs text-slate-500">at list prices, not billed</div>
              </div>
              <div className="rounded-lg bg-slate-950 p-4">
                <div className="text-xs text-slate-500">Free tier</div>
                <div className="mt-1 text-2xl font-bold">$0.00</div>
                <div className="text-xs text-slate-500">what the invoice says</div>
              </div>
            </div>

            {usage.by_model.length > 0 && (
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-800 text-left text-xs uppercase text-slate-500">
                    <th className="py-2">Report model</th>
                    <th className="py-2 text-right">Reports</th>
                    <th className="py-2 text-right">Tokens</th>
                    <th className="py-2 text-right">Est. USD</th>
                  </tr>
                </thead>
                <tbody>
                  {usage.by_model.map((row) => (
                    <tr key={row.model} className="border-b border-slate-800/60">
                      <td className="py-2 font-mono text-xs">{row.model}</td>
                      <td className="py-2 text-right">{row.reports}</td>
                      <td className="py-2 text-right">{n(row.tokens)}</td>
                      <td className="py-2 text-right">{usd(row.estimated_usd)}</td>
                    </tr>
                  ))}
                  <tr>
                    <td className="py-2 text-xs text-slate-500" colSpan={2}>
                      research tokens priced as <span className="font-mono">{usage.research_tokens_priced_as}</span>
                    </td>
                    <td className="py-2 text-right">{n(usage.totals.research_tokens)}</td>
                    <td />
                  </tr>
                </tbody>
              </table>
            )}

            <p className="mt-3 text-xs text-slate-500">
              {usage.pricing.basis}. Price table as of {usage.pricing.as_of}; {" "}
              {Math.round(usage.pricing.output_share * 100)}% output assumption.
              Report tokens are priced by the model that served them.
            </p>
          </>
        )}
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
        <h2 className="mb-2 font-semibold">LLM Gateway (LiteLLM: Gemini → Groq fallback)</h2>
        <button
          onClick={checkLlm}
          disabled={checking}
          className="mb-4 rounded bg-indigo-600 px-4 py-2 text-sm font-semibold hover:bg-indigo-500 disabled:opacity-50"
        >
          {checking ? "Calling LLM…" : "Test LLM call"}
        </button>
        {llm && (
          <pre className="overflow-auto rounded bg-slate-950 p-4 text-xs text-slate-300">
            {JSON.stringify(llm, null, 2)}
          </pre>
        )}
      </section>

      <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
        <div className="mb-2 flex items-center gap-3">
          <h2 className="font-semibold">Tracing (Langfuse)</h2>
          <Badge value={langfuse?.status} />
        </div>
        <p className="text-sm text-slate-400">
          {langfuse?.status === "ok"
            ? `Connected to ${langfuse.host}. Report traces carry the job as session + a “report” tag.`
            : langfuse?.status === "disabled"
              ? "Keys are not set, so tracing is off. Reports still run; they are just not observable."
              : `Tracing is configured but not reachable: ${langfuse?.detail ?? "unknown error"}`}
        </p>
      </section>
    </div>
  );
}
