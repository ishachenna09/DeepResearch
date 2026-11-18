import type { JobStatus } from "../../lib/types";

const STYLES: Record<JobStatus, { label: string; className: string }> = {
  planning: { label: "Planning", className: "border-indigo-800 bg-indigo-950 text-indigo-300" },
  awaiting_approval: { label: "Needs approval", className: "border-amber-800 bg-amber-950 text-amber-300" },
  // Legacy row: approved by a pre-Phase-4 build that then died before recording
  // `researching`. Nothing will ever advance it, so it is styled as inert.
  approved: { label: "Approved (legacy)", className: "border-slate-700 bg-slate-800 text-slate-300" },
  researching: { label: "Researching", className: "border-sky-800 bg-sky-950 text-sky-300" },
  notes_ready: { label: "Notes ready", className: "border-emerald-800 bg-emerald-950 text-emerald-300" },
  rejected: { label: "Rejected", className: "border-slate-700 bg-slate-800 text-slate-300" },
  failed: { label: "Failed", className: "border-rose-800 bg-rose-950 text-rose-300" },
};

const FALLBACK = { label: "Unknown", className: "border-slate-700 bg-slate-800 text-slate-300" };

export default function StatusBadge({ status }: { status: JobStatus }) {
  const style = STYLES[status] ?? FALLBACK;
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-xs font-semibold ${style.className}`}
    >
      {status === "researching" && (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-sky-400" aria-hidden />
      )}
      {style.label}
    </span>
  );
}
