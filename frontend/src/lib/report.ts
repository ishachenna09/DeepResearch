/**
 * Report endpoints + the SSE frame parser.
 *
 * Why `fetch` and not `EventSource`?
 * `EventSource` cannot send an `Authorization` header, so the common workaround
 * puts the JWT in the query string - where it is logged by every proxy between
 * here and the API and kept in browser history. Instead we do an authenticated
 * `fetch`, read `response.body` as a stream, and parse frames ourselves. That
 * also lets a reconnect send `Last-Event-ID` explicitly, which is exactly what
 * the Redis Stream on the server side is designed to answer.
 *
 * Why hand-parse SSE?
 * The format is four lines of spec (`event:`, `data:`, `id:`, comments) and the
 * server is ours. A library would hide the one detail that matters here: the
 * `id:` of each token, which is the resume cursor.
 */
import axios from "axios";

import api from "./api";
import { invalidate, shared } from "./inflight";
import type { Report } from "./types";

/**
 * Axios defaults to no timeout at all (`0` = wait forever). One stalled socket
 * - a suspended database, a half-open connection through a proxy - and the
 * panel sits on "Checking for a report…" indefinitely, which is
 * indistinguishable from a crash. 20 s is ~10x the p99 measured against this
 * deployment, so a timeout here means something is genuinely broken, and the
 * user gets an error with a Retry instead of an eternal skeleton.
 */
const GET_TIMEOUT_MS = 20_000;

/**
 * Shared in-flight GET per job.
 *
 * React 18 StrictMode mounts every effect twice in development, and a route
 * re-entry can mount the panel again while the first request is still open.
 * Measured in the browser: four identical `GET /report` calls per page load,
 * each ~2 s, all with the same body. Sharing the promise makes it one request,
 * and every caller gets the same result (or the same error).
 *
 * Only *in-flight* requests are shared; the entry is dropped as soon as the
 * promise settles, so `finalize()` after a stream always reads fresh data
 * rather than replaying a cached row.
 */
export function getReport(jobId: string): Promise<Report> {
  return shared(`report:${jobId}`, () =>
    api
      .get<Report>(`/research/${jobId}/report`, { timeout: GET_TIMEOUT_MS })
      .then((response) => response.data),
  );
}

/** Starts generation (or attaches to one already running). Returns the row. */
export async function generateReport(jobId: string): Promise<Report> {
  // A pre-generation GET may still be in flight; it would resolve with the old
  // row and overwrite the "generating" state we are about to show.
  invalidate(`report:${jobId}`);
  const { data } = await api.post<Report>(`/research/${jobId}/report`);
  return data;
}

/**
 * Absolute URL for the stream. `fetch` needs the same base as axios; deriving it
 * from `api.defaults.baseURL` keeps `frontend/.env.local` the single source.
 */
export function reportStreamUrl(jobId: string): string {
  const base = api.defaults.baseURL ?? "";
  return `${base}/research/${jobId}/report/stream`;
}

/** 404 means "no report yet" - which is a normal state, not an error. */
export function isNotFound(err: unknown): boolean {
  return axios.isAxiosError(err) && err.response?.status === 404;
}

export interface SseEvent {
  /** Redis entry id; the cursor for `Last-Event-ID`. */
  id?: string;
  event: string;
  data: string;
}

/**
 * One SSE block (everything between two blank lines) -> an event.
 * `\r` is already normalised away by the reader loop.
 */
export function parseSseBlock(block: string): SseEvent | null {
  let event = "message";
  let id: string | undefined;
  const data: string[] = [];

  for (const line of block.split("\n")) {
    // ": connected", ": waiting" - heartbeat comments, not events.
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "id") id = value;
    else if (field === "data") data.push(value);
  }

  if (data.length === 0) return null;
  // Multiple `data:` lines are one payload, joined by newlines (per spec).
  return { id, event, data: data.join("\n") };
}
