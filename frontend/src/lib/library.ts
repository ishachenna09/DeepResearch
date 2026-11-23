/**
 * Report library + exports.
 *
 * Why download as a blob instead of `<a href="/reports/.../export.pdf">`?
 * The API authenticates with a Bearer header; a plain link sends no header, and
 * the alternative (a token in the query string) writes the JWT into browser
 * history and every proxy log. Fetch the bytes with axios, then hand the
 * browser a short-lived object URL.
 */
import api from "./api";
import type { ReportPage } from "./types";

export interface LibraryQuery {
  limit?: number;
  offset?: number;
  /** Free text over the research question. */
  q?: string;
  status?: "" | "generating" | "ready" | "failed";
  sort?: "recent" | "oldest" | "tokens";
}

export function listReports(query: LibraryQuery): Promise<ReportPage> {
  const { status, ...rest } = query;
  return api
    .get<ReportPage>("/reports", {
      // The API names the status filter `status`; `rest` keeps empty values out.
      params: { ...rest, ...(status ? { status } : {}) },
      timeout: 20_000,
    })
    .then((response) => response.data);
}

function filenameFrom(header: unknown, fallback: string): string {
  if (typeof header !== "string") return fallback;
  const match = /filename="?([^";]+)"?/.exec(header);
  return match?.[1] ?? fallback;
}

/** Fetch one export and trigger the browser's download. Returns the filename. */
export async function downloadReport(
  reportId: string,
  format: "md" | "pdf",
): Promise<string> {
  const response = await api.get<Blob>(`/reports/${reportId}/export.${format}`, {
    responseType: "blob",
    // A PDF render is ~0.2 s, but the first call also loads ReportLab. 60 s is
    // generous without letting a broken download hang the UI forever.
    timeout: 60_000,
  });
  const filename = filenameFrom(
    response.headers["content-disposition"],
    `deepresearch-report.${format}`,
  );
  const url = URL.createObjectURL(response.data);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Revoking immediately can cancel the download in some browsers.
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  return filename;
}
