/**
 * The report: load it, generate it, and follow the token stream.
 *
 * State machine (independent of the job's): `loading -> none | generating ->
 * ready | failed`. `none` is a real state - it means "no report yet", which is
 * why a 404 from GET is not an error.
 *
 * Why the DB is the source of truth and the stream is only the view:
 * every terminal event is followed by one `GET /report`. The stream carries
 * tokens for the typing effect and a `done` summary, but the stored row carries
 * the citations array - so what the reader sees is always what was persisted.
 *
 * Reconnect: on a dropped connection we re-request the same stream with the
 * `Last-Event-ID` of the last token we rendered. On the server that resumes a
 * Redis Stream instead of replaying from 0 - which is the entire reason Phase 5
 * used Streams rather than pub/sub. After 3 failed attempts we stop pretending
 * and poll the row instead (the report still generates without streaming).
 *
 * The load is one shared request (see `getReport`): this effect runs twice under
 * StrictMode and once more on a remount, and each duplicate used to cost a full
 * round trip. `load` is therefore memoised on `jobId` alone, which is why
 * `follow` reads the current report from a ref instead of from state.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import {
  generateReport,
  getReport,
  isNotFound,
  parseSseBlock,
  reportStreamUrl,
} from "../lib/report";
import { apiError } from "../lib/research";
import type { Citation, Report, ReportValidation } from "../lib/types";
import { useAuthStore } from "../store/auth";

export type ReportPhase = "loading" | "none" | "generating" | "ready" | "failed";

export interface ReportProgress {
  sources?: number;
  notes?: number;
  repairing?: boolean;
}

const MAX_STREAM_ATTEMPTS = 3;
const POLL_MS = 4000;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

export function useReport(jobId: string | undefined) {
  const [phase, setPhase] = useState<ReportPhase>("loading");
  const [draft, setDraft] = useState("");
  const [report, setReport] = useState<Report | null>(null);
  const [citations, setCitations] = useState<Citation[]>([]);
  const [validation, setValidation] = useState<ReportValidation | null>(null);
  const [progress, setProgress] = useState<ReportProgress | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** Seconds spent in `loading`, so a slow first load can say so. */
  const [waited, setWaited] = useState(0);

  const mounted = useRef(true);
  const abort = useRef<AbortController | null>(null);
  // The model emits ~1k chunks; keeping the join in a ref avoids rebuilding the
  // whole string from a stale closure on every token.
  const draftRef = useRef("");
  // Latest stored row. Used where reading state would make a callback unstable
  // (and an unstable `follow` would restart the stream on every render).
  const reportRef = useRef<Report | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      abort.current?.abort();
    };
  }, []);

  /** Replace the visible text and remember it for the next token. */
  const setText = useCallback((text: string) => {
    draftRef.current = text;
    setDraft(text);
  }, []);

  /** Load the stored row. `null` means "no report yet". */
  const applyStored = useCallback(
    (stored: Report) => {
      reportRef.current = stored;
      setReport(stored);
      setCitations(stored.citations ?? []);
      setValidation(stored.validation ?? null);
      setText(stored.markdown ?? "");
      setPhase(
        stored.status === "ready"
          ? "ready"
          : stored.status === "generating"
            ? "generating"
            : "failed",
      );
      if (stored.status === "failed") setError(stored.error ?? "Report generation failed.");
    },
    [setText],
  );

  /** After a terminal event: trust the database, not the stream payload. */
  const finalize = useCallback(async () => {
    if (!jobId) return;
    try {
      applyStored(await getReport(jobId));
      setError((current) => (current && current.includes("Live stream") ? current : null));
    } catch (err) {
      if (!mounted.current) return;
      setError(apiError(err));
      setPhase("failed");
    }
  }, [jobId, applyStored]);

  /** Apply one parsed SSE event. Returns true when it was terminal. */
  const applyEvent = useCallback(
    (event: string, data: string): boolean => {
      let payload: Record<string, unknown> | null = null;
      try {
        payload = data ? (JSON.parse(data) as Record<string, unknown>) : null;
      } catch {
        payload = null; // a malformed frame must not kill the stream
      }

      if (event === "token") {
        const text = typeof payload?.text === "string" ? payload.text : "";
        if (text) {
          draftRef.current += text;
          setDraft(draftRef.current);
        }
        return false;
      }
      if (event === "replace") {
        // The second attempt replaced the whole draft; show the corrected version.
        setText(typeof payload?.text === "string" ? payload.text : "");
        return false;
      }
      if (event === "status") {
        setProgress({
          sources: typeof payload?.sources === "number" ? payload.sources : undefined,
          notes: typeof payload?.notes === "number" ? payload.notes : undefined,
          repairing: payload?.status === "repairing",
        });
        return false;
      }
      if (event === "done") return true;
      if (event === "error") {
        setError(typeof payload?.error === "string" ? payload.error : "Report generation failed.");
        return true;
      }
      return false;
    },
    [setText],
  );

  /** Poll the row when streaming is unavailable. */
  const pollUntilDone = useCallback(
    async (signal: AbortSignal) => {
      if (!jobId) return;
      for (;;) {
        if (signal.aborted || !mounted.current) return;
        await sleep(POLL_MS);
        try {
          const stored = await getReport(jobId);
          if (!mounted.current) return;
          if (stored.status !== "generating") {
            applyStored(stored);
            return;
          }
        } catch (err) {
          if (!mounted.current) return;
          setError(apiError(err));
        }
      }
    },
    [jobId, applyStored],
  );

  /** Consume one response body until a terminal event (returns true) or EOF. */
  const consume = useCallback(
    async (response: Response, signal: AbortSignal, cursor: { id: string }) => {
      if (!response.body) return false;
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let terminal = false;

      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
          let cut = buffer.indexOf("\n\n");
          while (cut !== -1) {
            const frame = parseSseBlock(buffer.slice(0, cut));
            buffer = buffer.slice(cut + 2);
            if (frame) {
              if (frame.id) cursor.id = frame.id;
              if (applyEvent(frame.event, frame.data)) {
                terminal = true;
                break;
              }
            }
            cut = buffer.indexOf("\n\n");
          }
          if (terminal || signal.aborted) break;
        }
      } finally {
        // Cancel the body: leaving it open keeps the request (and the server's
        // generator) alive until the TTL expires.
        void reader.cancel().catch(() => undefined);
      }
      return terminal;
    },
    [applyEvent],
  );

  /** Open the stream, resuming from the last id we rendered on reconnect. */
  const follow = useCallback(async () => {
    if (!jobId) return;
    const controller = new AbortController();
    abort.current = controller;
    setStreaming(true);
    const cursor = { id: "" };

    try {
      for (let attempt = 1; attempt <= MAX_STREAM_ATTEMPTS; attempt += 1) {
        if (controller.signal.aborted || !mounted.current) return;
        try {
          const token = useAuthStore.getState().accessToken;
          const response = await fetch(reportStreamUrl(jobId), {
            headers: {
              Accept: "text/event-stream",
              ...(token ? { Authorization: `Bearer ${token}` } : {}),
              ...(cursor.id ? { "Last-Event-ID": cursor.id } : {}),
            },
            signal: controller.signal,
          });

          if (response.status === 401) {
            // The stream cannot trigger the axios refresh interceptor, so the
            // user re-authenticates instead of silently getting a dead stream.
            setError("Your session expired. Sign in again, then regenerate the report.");
            setPhase(reportRef.current ? "ready" : "none");
            return;
          }
          if (!response.ok) throw new Error(`stream returned ${response.status}`);

          const terminal = await consume(response, controller.signal, cursor);
          // Terminal event or a clean EOF: either way the row is the truth.
          await finalize();
          if (terminal) return;
          return;
        } catch (err) {
          if (controller.signal.aborted) return;
          if (attempt === MAX_STREAM_ATTEMPTS) {
            setError(
              `Live stream unavailable (${err instanceof Error ? err.message : String(err)}). ` +
                "Polling instead - the report is still being generated.",
            );
            await pollUntilDone(controller.signal);
            return;
          }
          await sleep(700 * attempt);
        }
      }
    } finally {
      if (mounted.current) setStreaming(false);
    }
  }, [jobId, consume, finalize, pollUntilDone]);

  /**
   * Initial load. Also exposed as `retry`, so a timeout or a genuinely dead
   * connection is recoverable without reloading the page (a reload would drop
   * the in-memory token and log the user out).
   */
  const load = useCallback(async () => {
    if (!jobId) return;
    setPhase("loading");
    setError(null);
    setProgress(null);

    try {
      const stored = await getReport(jobId);
      if (!mounted.current) return;
      applyStored(stored);
      // A report left `generating` by a previous page visit: attach to the
      // stream. The server replays from the start, so nothing is lost.
      if (stored.status === "generating") void follow();
    } catch (err) {
      if (!mounted.current) return;
      if (isNotFound(err)) {
        setPhase("none");
        setText("");
        return;
      }
      setError(apiError(err));
      setPhase("none");
    }
  }, [jobId, applyStored, follow, setText]);

  useEffect(() => {
    void load();
    return () => {
      abort.current?.abort();
    };
  }, [load]);

  // Elapsed seconds while the row is loading: "Checking for a report… (7s)"
  // tells the user the page is working, which a static sentence does not.
  useEffect(() => {
    if (phase !== "loading") {
      setWaited(0);
      return;
    }
    const startedAt = Date.now();
    const id = window.setInterval(() => {
      setWaited(Math.round((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(id);
  }, [phase]);

  const generate = useCallback(async () => {
    if (!jobId) return;
    setError(null);
    setProgress(null);
    setText("");
    try {
      await generateReport(jobId);
      if (!mounted.current) return;
      setPhase("generating");
      void follow();
    } catch (err) {
      if (!mounted.current) return;
      setError(apiError(err));
      setPhase(reportRef.current ? "ready" : "none");
    }
  }, [jobId, follow, setText]);

  return {
    phase,
    draft,
    report,
    citations,
    validation,
    progress,
    streaming,
    error,
    waited,
    generate,
    retry: load,
  };
}
