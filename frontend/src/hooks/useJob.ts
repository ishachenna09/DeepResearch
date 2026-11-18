/**
 * Polling hook for one job.
 *
 * The API is poll-based by design right now (approve returns in ~3 s and a run
 * takes 1-4 min, so awaiting it would block the request). Phase 5 adds SSE for
 * the report stream; this hook is the seam that will be replaced then, which is
 * exactly why it lives in `hooks/` and not inside the page.
 *
 * Cadence: 2 s while work is happening, 15 s while paused at the approval gate
 * (the only thing that changes it is our own click), stop on terminal status.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { apiError, getJob } from "../lib/research";
import {
  ACTIVE_STATUSES,
  TERMINAL_STATUSES,
  type JobStatus,
  type ResearchJob,
} from "../lib/types";

const FAST_MS = 2000;
const PAUSED_MS = 15000;

function isTerminal(status: JobStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

/**
 * Only `planning` and `researching` actually move without us doing anything.
 * Everything else (the approval gate, a legacy `approved` row) can only change
 * because of our own click, so polling it every 2 s is pure waste.
 */
function delayFor(status: JobStatus): number {
  return ACTIVE_STATUSES.includes(status) ? FAST_MS : PAUSED_MS;
}

export function useJob(jobId: string | undefined) {
  const [job, setJob] = useState<ResearchJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Refs, not state: the loop must not restart every time the job object changes.
  const timer = useRef<number | null>(null);
  const cancelled = useRef(false);

  const clearTimer = () => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  };

  const fetchOnce = useCallback(
    async (schedule: boolean) => {
      if (!jobId) return;
      try {
        const fresh = await getJob(jobId);
        if (cancelled.current) return;
        setJob(fresh);
        setError(null);
        clearTimer();
        if (schedule && !isTerminal(fresh.status)) {
          timer.current = window.setTimeout(() => void fetchOnce(true), delayFor(fresh.status));
        }
      } catch (err) {
        if (cancelled.current) return;
        setError(apiError(err));
        clearTimer();
        // Back off on errors instead of hammering a down API.
        if (schedule) timer.current = window.setTimeout(() => void fetchOnce(true), 5000);
      } finally {
        if (!cancelled.current) setLoading(false);
      }
    },
    [jobId],
  );

  useEffect(() => {
    cancelled.current = false;
    setLoading(true);
    setJob(null);
    void fetchOnce(true);
    return () => {
      cancelled.current = true;
      clearTimer();
    };
  }, [fetchOnce]);

  /** Manual refresh (also used right after approve/reject to restart polling). */
  const refresh = useCallback(async () => {
    await fetchOnce(true);
  }, [fetchOnce]);

  /** Accept a job the server just returned, then resume polling for the rest of the run. */
  const applyServerJob = useCallback(
    (next: ResearchJob) => {
      setJob(next);
      setError(null);
      clearTimer();
      if (!isTerminal(next.status)) {
        timer.current = window.setTimeout(() => void fetchOnce(true), delayFor(next.status));
      }
    },
    [fetchOnce],
  );

  return { job, error, loading, refresh, applyServerJob };
}
