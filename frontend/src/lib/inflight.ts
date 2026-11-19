/**
 * Share one in-flight request between callers with the same key.
 *
 * Why this exists: React 18 StrictMode mounts every effect twice in
 * development, and a route re-entry mounts the panel again while the first
 * request is still open. Measured in this app: four identical
 * `GET /report` calls per page load, each ~1.6 s, all returning the same body.
 * Against a free-tier database in another region that latency is the whole
 * page-load budget, so the duplicates are not cosmetic.
 *
 * The entry is dropped as soon as the promise settles, so this is NOT a cache:
 * a later call always performs a real request and can never serve a stale row.
 * That matters for `finalize()`, which must read what was just persisted.
 */
const inflight = new Map<string, Promise<unknown>>();

export function shared<T>(key: string, start: () => Promise<T>): Promise<T> {
  const existing = inflight.get(key) as Promise<T> | undefined;
  if (existing) return existing;

  const request = start().finally(() => {
    inflight.delete(key);
  });
  inflight.set(key, request);
  return request;
}

/** Forget a keyed request, e.g. before a mutation that invalidates it. */
export function invalidate(key: string): void {
  inflight.delete(key);
}
