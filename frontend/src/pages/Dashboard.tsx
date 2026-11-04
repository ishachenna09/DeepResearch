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
}

export default function Dashboard() {
  const [health, setHealth] = useState<Health | null>(null);
  const [llm, setLlm] = useState<Health | null>(null);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    api.get("/healthz").then((r) => setHealth(r.data)).catch(() => setHealth({ overall: "unreachable" }));
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

  const Badge = ({ value }: { value?: string }) => (
    <span
      className={`rounded px-2 py-0.5 text-xs font-semibold ${
        value === "ok" ? "bg-emerald-950 text-emerald-400" : "bg-rose-950 text-rose-400"
      }`}
    >
      {value ?? "unknown"}
    </span>
  );

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-bold">Phase 0 — System Status</h1>

      <section className="rounded-xl border border-slate-800 bg-slate-900 p-6">
        <h2 className="mb-4 font-semibold">Infrastructure</h2>
        <div className="flex flex-wrap gap-6 text-sm">
          <div className="flex items-center gap-2">API <Badge value={health?.api} /></div>
          <div className="flex items-center gap-2">Postgres (Neon) <Badge value={health?.postgres} /></div>
          <div className="flex items-center gap-2">Redis (Upstash) <Badge value={health?.redis} /></div>
        </div>
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
    </div>
  );
}
