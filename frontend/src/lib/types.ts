/**
 * API types, mirrored from api/app/schemas/{research,notes}.py.
 *
 * Kept hand-written (not generated) on purpose for now: the shapes change every
 * phase and a generator would be one more moving part. When the API stabilises
 * (Phase 8), swap this for `openapi-typescript` output - the import sites stay
 * identical.
 */

/**
 * `approved` is a legacy value: early builds wrote it when a human approved and
 * then ran the graph in-process, so a crash left the row stuck there forever.
 * The current flow writes `researching` before returning (fire-and-poll), so
 * `approved` only exists on old rows - it is treated as "in flight, will never
 * finish", not as a live status.
 */
export type JobStatus =
  | "planning"
  | "awaiting_approval"
  | "approved"
  | "researching"
  | "notes_ready"
  | "rejected"
  | "failed";

/** Statuses after which the server will never change the job again. */
export const TERMINAL_STATUSES: JobStatus[] = ["notes_ready", "rejected", "failed"];

/** Statuses that mean "work is happening right now" → poll fast. */
export const ACTIVE_STATUSES: JobStatus[] = ["planning", "researching"];

export interface ResearchPlan {
  question: string;
  sub_questions: string[];
  angles: string[];
  constraints: string[];
  budget_tokens: number;
}

/** One fetched page. Errors and successes share this shape, separated by `stage`. */
export interface Source {
  sub_question?: string;
  /** 1 = initial fan-out, 2 = gap-filling round (Phase 4). */
  round?: number;
  url?: string;
  title?: string;
  chars?: number;
  http_status?: number;
  injected?: boolean;
  cached?: boolean;
  signals?: string[];
  stage?: "search" | "fetch" | "extract";
  error?: string;
}

/** The critic's verdict for the last reviewed round. */
export interface Critique {
  sufficient: boolean;
  reason: string;
  stop_reason: string | null;
  round: number;
  tokens_used: number;
  budget_tokens: number;
  notes_new: number;
  gaps_proposed?: number;
  gaps_kept?: number;
}

export interface ResearchJob {
  job_id: string;
  question: string;
  plan: ResearchPlan | null;
  status: JobStatus;
  notes_count: number;
  error?: string | null;
  sources?: Source[] | null;
  rounds: number;
  tokens_used: number;
  critique?: Critique | null;
  created_at: string;
  updated_at: string;
}

export interface Note {
  id: string;
  sub_question: string;
  url: string;
  title?: string | null;
  note: string;
  quote?: string | null;
  confidence: "low" | "medium" | "high" | string;
  signals?: string[] | null;
  created_at: string;
  /** Only present on semantic-search hits: 1 - cosine distance. */
  score?: number;
}

export interface NotesResponse {
  job_id: string;
  status: JobStatus;
  count: number;
  notes: Note[];
}

export interface NoteSearchResponse {
  job_id: string;
  query: string;
  count: number;
  results: Note[];
}
