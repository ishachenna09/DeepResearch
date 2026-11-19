/**
 * A deliberately small markdown renderer for model output.
 *
 * Why not `react-markdown`?
 * The writer emits a known subset (headings, paragraphs, bullets, bold, inline
 * code, `[n]` markers) and nothing else is expected to work. A full parser plus
 * its plugin chain would add ~80 kB and, more importantly, would make it easy to
 * reach for `dangerouslySetInnerHTML` - which is never acceptable on model
 * output. Here every string the model produces is rendered as text.
 *
 * Citations: `[3]` and `[1, 4]` become chips that link to `#cite-3` in the
 * source list below the report. A number that is not in the registry renders in
 * red - that is a hallucinated citation, and hiding it would be lying to the
 * reader about the audit.
 */
import { Fragment, type ReactNode } from "react";

import type { Citation } from "../../lib/types";

/** marker | **bold** | `code` - order matters, first alternative wins. */
const TOKEN_RE = /\[(\d+(?:\s*,\s*\d+)*)\]|\*\*([^*]+)\*\*|`([^`]+)`/g;

type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "list"; ordered: boolean; items: string[] };

function parseBlocks(markdown: string): Block[] {
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;

  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push({ kind: "paragraph", text: paragraph.join(" ") });
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list && list.items.length) blocks.push({ kind: "list", ...list });
    list = null;
  };

  for (const raw of markdown.replace(/\r\n/g, "\n").split("\n")) {
    const trimmed = raw.trim();

    if (!trimmed) {
      flushParagraph();
      flushList();
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(trimmed);
    if (heading) {
      flushParagraph();
      flushList();
      blocks.push({ kind: "heading", level: heading[1].length, text: heading[2] });
      continue;
    }

    const bullet = /^[-*+]\s+(.*)$/.exec(trimmed);
    if (bullet) {
      flushParagraph();
      if (!list || list.ordered) {
        flushList();
        list = { ordered: false, items: [] };
      }
      list.items.push(bullet[1]);
      continue;
    }

    const numbered = /^\d+[.)]\s+(.*)$/.exec(trimmed);
    if (numbered) {
      flushParagraph();
      if (!list || !list.ordered) {
        flushList();
        list = { ordered: true, items: [] };
      }
      list.items.push(numbered[1]);
      continue;
    }

    flushList();
    paragraph.push(trimmed);
  }

  flushParagraph();
  flushList();
  return blocks;
}

function CitationChip({ n, known }: { n: number; known: boolean }) {
  return (
    <a
      href={`#cite-${n}`}
      title={known ? `source ${n}` : `source ${n} does not exist - hallucinated marker`}
      className={`mx-0.5 inline-flex min-w-[1.15rem] items-center justify-center rounded px-1 align-super text-[10px] font-semibold ring-1 transition-colors ${
        known
          ? "bg-indigo-950 text-indigo-300 ring-indigo-800 hover:bg-indigo-800 hover:text-indigo-100"
          : "bg-rose-950 text-rose-300 ring-rose-800"
      }`}
    >
      {n}
    </a>
  );
}

function Inline({ text, known }: { text: string; known: Set<number> }) {
  const parts: ReactNode[] = [];
  let last = 0;

  for (const match of text.matchAll(TOKEN_RE)) {
    const at = match.index ?? 0;
    if (at > last) parts.push(text.slice(last, at));

    if (match[1]) {
      const numbers = match[1].split(",").map((raw) => Number(raw.trim()));
      numbers.forEach((n, i) => {
        parts.push(
          <Fragment key={`c-${at}-${n}-${i}`}>
            {i > 0 && <span className="text-slate-500">,</span>}
            <CitationChip n={n} known={known.has(n)} />
          </Fragment>,
        );
      });
    } else if (match[2]) {
      parts.push(
        <strong key={`b-${at}`} className="font-semibold text-slate-100">
          {match[2]}
        </strong>,
      );
    } else if (match[3]) {
      parts.push(
        <code key={`k-${at}`} className="rounded bg-slate-800 px-1 py-0.5 text-[12px] text-slate-200">
          {match[3]}
        </code>,
      );
    }

    last = at + match[0].length;
  }

  if (last < text.length) parts.push(text.slice(last));
  return <>{parts}</>;
}

const HEADING_CLASS: Record<number, string> = {
  1: "mb-2 mt-5 text-xl font-bold text-slate-100",
  2: "mb-2 mt-5 text-lg font-bold text-slate-100",
  3: "mb-1.5 mt-4 text-sm font-semibold uppercase tracking-wide text-slate-400",
};

export default function Markdown({
  markdown,
  citations,
  caret = false,
}: {
  markdown: string;
  citations: Citation[];
  /** Show a blinking cursor while tokens are still arriving. */
  caret?: boolean;
}) {
  const known = new Set(citations.map((citation) => citation.index));
  const blocks = parseBlocks(markdown);

  return (
    <div className="space-y-2 text-sm leading-relaxed text-slate-300">
      {blocks.map((block, index) => {
        if (block.kind === "heading") {
          const Tag = (block.level >= 3 ? "h4" : block.level === 2 ? "h3" : "h2") as "h2" | "h3" | "h4";
          return (
            <Tag key={index} className={HEADING_CLASS[Math.min(block.level, 3)]}>
              <Inline text={block.text} known={known} />
            </Tag>
          );
        }
        if (block.kind === "list") {
          const ListTag = block.ordered ? "ol" : "ul";
          return (
            <ListTag
              key={index}
              className={`space-y-1.5 pl-5 ${block.ordered ? "list-decimal" : "list-disc"} marker:text-slate-600`}
            >
              {block.items.map((item, i) => (
                <li key={i}>
                  <Inline text={item} known={known} />
                </li>
              ))}
            </ListTag>
          );
        }
        return (
          <p key={index}>
            <Inline text={block.text} known={known} />
          </p>
        );
      })}
      {caret && (
        <span className="inline-block h-4 w-1.5 animate-pulse bg-indigo-400 align-text-bottom" />
      )}
    </div>
  );
}
