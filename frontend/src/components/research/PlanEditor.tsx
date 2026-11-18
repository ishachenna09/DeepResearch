/**
 * The HITL gate: edit the planner's plan, then approve or reject.
 *
 * This is the surface where a human can *reduce* coverage - the exact case the
 * Phase 4 critic exists for. So the budget field carries the measured cost of a
 * round next to it: editing 6 angles down to 3 is a legitimate choice, but
 * starving the budget is not.
 *
 * Why chips for angles/constraints instead of a comma-separated text field?
 * The planner emits items that CONTAIN commas ("Performance (latency,
 * throughput, recall)"). Joining and re-splitting mangled them and made the
 * form claim "edited" on load. Arrays in state, arrays on the wire - no
 * round-trip, no ambiguity.
 */
import { useMemo, useState } from "react";

import { apiError, decidePlan } from "../../lib/research";
import type { ResearchJob, ResearchPlan } from "../../lib/types";

const MIN_SUB_QUESTIONS = 3;
const MAX_SUB_QUESTIONS = 7;
const MIN_BUDGET = 50_000;
const MAX_BUDGET = 400_000;

interface Props {
  job: ResearchJob;
  onDone: (job: ResearchJob) => void;
}

function samePlan(a: ResearchPlan, b: ResearchPlan): boolean {
  return (
    JSON.stringify(a.sub_questions) === JSON.stringify(b.sub_questions) &&
    JSON.stringify(a.angles) === JSON.stringify(b.angles) &&
    JSON.stringify(a.constraints) === JSON.stringify(b.constraints) &&
    a.budget_tokens === b.budget_tokens
  );
}

/** Add/remove editor for a list of short strings. */
function ChipEditor({
  label,
  items,
  onChange,
  placeholder,
}: {
  label: string;
  items: string[];
  onChange: (next: string[]) => void;
  placeholder: string;
}) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const value = draft.trim();
    if (!value) return;
    onChange([...items, value]);
    setDraft("");
  };
  return (
    <div>
      <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-400">
        {label}
      </label>
      <div className="mb-2 flex flex-wrap gap-1.5">
        {items.map((item, index) => (
          <span
            key={`${item}-${index}`}
            className="inline-flex items-center gap-1 rounded border border-slate-700 bg-slate-800 px-2 py-0.5 text-xs text-slate-300"
          >
            {item}
            <button
              type="button"
              onClick={() => onChange(items.filter((_, i) => i !== index))}
              className="text-slate-500 hover:text-rose-300"
              title={`Remove "${item}"`}
            >
              ✕
            </button>
          </span>
        ))}
        {items.length === 0 && <span className="text-xs text-slate-600">none</span>}
      </div>
      <div className="flex gap-2">
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();   // do not submit the plan
              add();
            }
          }}
          placeholder={placeholder}
          className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm outline-none focus:border-indigo-500"
        />
        <button
          type="button"
          onClick={add}
          className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
        >
          Add
        </button>
      </div>
    </div>
  );
}

export default function PlanEditor({ job, onDone }: Props) {
  const original = job.plan as ResearchPlan;

  const [subQuestions, setSubQuestions] = useState<string[]>(original.sub_questions);
  const [angles, setAngles] = useState<string[]>(original.angles);
  const [constraints, setConstraints] = useState<string[]>(original.constraints);
  const [budget, setBudget] = useState<number>(original.budget_tokens);
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const draft: ResearchPlan = useMemo(
    () => ({
      question: job.question,
      sub_questions: subQuestions.map((q) => q.trim()).filter(Boolean),
      angles,
      constraints,
      budget_tokens: Number(budget) || 0,
    }),
    [angles, budget, constraints, job.question, subQuestions],
  );

  const invalidReason = useMemo(() => {
    if (draft.sub_questions.length < MIN_SUB_QUESTIONS) {
      return `Keep at least ${MIN_SUB_QUESTIONS} sub-questions (you have ${draft.sub_questions.length}).`;
    }
    if (draft.sub_questions.length > MAX_SUB_QUESTIONS) {
      return `At most ${MAX_SUB_QUESTIONS} sub-questions.`;
    }
    if (draft.budget_tokens < MIN_BUDGET || draft.budget_tokens > MAX_BUDGET) {
      return `Budget must be between ${MIN_BUDGET / 1000}k and ${MAX_BUDGET / 1000}k tokens.`;
    }
    return null;
  }, [draft]);

  const edited = !samePlan(draft, original);

  const submit = async (action: "approve" | "reject") => {
    if (action === "reject" && !window.confirm("Reject this plan? The job ends without notes.")) {
      return;
    }
    setBusy(action);
    setError(null);
    try {
      const next = await decidePlan(job.job_id, action, action === "approve" ? draft : undefined);
      onDone(next);
    } catch (err) {
      setError(apiError(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="rounded-xl border border-amber-900/60 bg-slate-900 p-6">
      <div className="mb-1 flex flex-wrap items-center justify-between gap-3">
        <h2 className="font-semibold text-amber-200">Review the research plan</h2>
        {edited && (
          <span className="rounded border border-amber-800 bg-amber-950 px-2 py-0.5 text-xs font-semibold text-amber-300">
            edited
          </span>
        )}
      </div>
      <p className="mb-5 text-sm text-slate-400">
        The planner proposed these angles. Edit freely - the researcher runs exactly what you
        approve, and the coverage critic will flag anything the plan leaves out.
      </p>

      <label className="mb-2 block text-xs font-semibold uppercase tracking-wide text-slate-400">
        Sub-questions ({draft.sub_questions.length})
      </label>
      <div className="mb-3 space-y-2">
        {subQuestions.map((question, index) => (
          <div key={index} className="flex items-start gap-2">
            <span className="mt-2 w-5 text-right text-xs text-slate-500">{index + 1}</span>
            <textarea
              value={question}
              rows={2}
              onChange={(event) => {
                const next = [...subQuestions];
                next[index] = event.target.value;
                setSubQuestions(next);
              }}
              className="w-full resize-y rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
            />
            <button
              type="button"
              onClick={() => setSubQuestions(subQuestions.filter((_, i) => i !== index))}
              disabled={subQuestions.length <= MIN_SUB_QUESTIONS}
              className="mt-0.5 rounded border border-slate-700 px-2 py-2 text-xs text-slate-400 hover:bg-slate-800 disabled:opacity-30"
              title={
                subQuestions.length <= MIN_SUB_QUESTIONS
                  ? `A plan needs at least ${MIN_SUB_QUESTIONS}`
                  : "Remove this sub-question"
              }
            >
              ✕
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        onClick={() => setSubQuestions([...subQuestions, ""])}
        disabled={subQuestions.length >= MAX_SUB_QUESTIONS}
        className="mb-5 rounded border border-slate-700 px-3 py-1 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-30"
      >
        + Add sub-question
      </button>

      <div className="mb-5 grid gap-4 sm:grid-cols-2">
        <ChipEditor
          label="Angles"
          items={angles}
          onChange={setAngles}
          placeholder="e.g. migration cost"
        />
        <ChipEditor
          label="Constraints"
          items={constraints}
          onChange={setConstraints}
          placeholder="e.g. self-hosted only"
        />
      </div>

      <div className="mb-6">
        <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-slate-400">
          Token budget
        </label>
        <input
          type="number"
          value={budget}
          min={MIN_BUDGET}
          max={MAX_BUDGET}
          step={10_000}
          onChange={(event) => setBudget(Number(event.target.value))}
          className="w-40 rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-indigo-500"
        />
        <p className="mt-1 text-xs text-slate-500">
          Measured: ~26k tokens per research round, so two rounds land near 55k. The budget is a
          brake, not a target - it stops a runaway loop, it does not buy more research.
        </p>
      </div>

      {error && (
        <p className="mb-4 rounded border border-rose-900 bg-rose-950/60 px-3 py-2 text-sm text-rose-200">
          {error}
        </p>
      )}
      {invalidReason && !error && <p className="mb-4 text-sm text-amber-300">{invalidReason}</p>}

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => void submit("approve")}
          disabled={!!invalidReason || busy !== null}
          className="rounded bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-500 disabled:opacity-40"
        >
          {busy === "approve" ? "Starting research…" : "Approve & research"}
        </button>
        <button
          type="button"
          onClick={() => void submit("reject")}
          disabled={busy !== null}
          className="rounded border border-rose-900 px-4 py-2 text-sm font-semibold text-rose-300 hover:bg-rose-950 disabled:opacity-40"
        >
          {busy === "reject" ? "Rejecting…" : "Reject plan"}
        </button>
        <button
          type="button"
          onClick={() => {
            setSubQuestions(original.sub_questions);
            setAngles(original.angles);
            setConstraints(original.constraints);
            setBudget(original.budget_tokens);
            setError(null);
          }}
          disabled={!edited || busy !== null}
          className="ml-auto rounded border border-slate-700 px-3 py-2 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-30"
        >
          Reset to planner's plan
        </button>
      </div>
    </section>
  );
}
