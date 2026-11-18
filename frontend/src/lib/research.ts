/**
 * Typed wrappers around the research endpoints.
 *
 * Why a module instead of `api.get(...)` in components?
 * One place that knows the paths, the payloads and the error shape - so a
 * rename in `api/app/api/v1/research.py` is a compile error here, not a 404 a
 * user discovers.
 */
import axios from "axios";

import api from "./api";
import type { NoteSearchResponse, NotesResponse, ResearchJob, ResearchPlan } from "./types";

/** FastAPI puts the reason in `detail`; axios puts transport errors elsewhere. */
export function apiError(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const detail = err.response?.data?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      // Pydantic 422: [{loc: [...], msg: "..."}]
      return detail
        .map((item) => (typeof item?.msg === "string" ? item.msg : JSON.stringify(item)))
        .join("; ");
    }
    if (err.code === "ERR_NETWORK") {
      return `Cannot reach the API at ${api.defaults.baseURL}. Is it running?`;
    }
    return err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

export async function listJobs(limit = 20): Promise<ResearchJob[]> {
  const { data } = await api.get<ResearchJob[]>("/research", { params: { limit } });
  return data;
}

export async function createJob(question: string): Promise<ResearchJob> {
  const { data } = await api.post<ResearchJob>("/research", { question });
  return data;
}

export async function getJob(jobId: string): Promise<ResearchJob> {
  const { data } = await api.get<ResearchJob>(`/research/${jobId}`);
  return data;
}

/** Approve (optionally with an edited plan) or reject. Returns the new job state. */
export async function decidePlan(
  jobId: string,
  action: "approve" | "reject",
  plan?: ResearchPlan,
): Promise<ResearchJob> {
  const { data } = await api.post<ResearchJob>(`/research/${jobId}/plan/approve`, { action, plan });
  return data;
}

export async function listNotes(jobId: string, limit = 200): Promise<NotesResponse> {
  const { data } = await api.get<NotesResponse>(`/research/${jobId}/notes`, { params: { limit } });
  return data;
}

export async function searchNotes(
  jobId: string,
  query: string,
  topK = 8,
): Promise<NoteSearchResponse> {
  const { data } = await api.post<NoteSearchResponse>(`/research/${jobId}/notes/search`, {
    query,
    top_k: topK,
  });
  return data;
}
