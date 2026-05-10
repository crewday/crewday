import type { ReactElement, ReactNode } from "react";

interface ChatMessageBodyProps {
  body: string;
  className: string;
}

type InlineToken =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "strong"; children: InlineToken[] }
  | { kind: "em"; children: InlineToken[] }
  | { kind: "link"; text: string; link: SafeLink | null };

type MarkdownBlock =
  | { kind: "paragraph"; lines: string[] }
  | { kind: "list"; items: string[] }
  | { kind: "code"; code: string }
  | { kind: "heading"; level: HeadingLevel; text: string };

type HeadingLevel = 1 | 2 | 3 | 4 | 5 | 6;

interface SafeLink {
  href: string;
  external: boolean;
}

const TOKEN_MARKERS = ["`", "**", "*", "["];
const SAFE_LINK_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);
const FALLBACK_ORIGIN = "http://localhost";

export default function ChatMessageBody({
  body,
  className,
}: ChatMessageBodyProps): ReactElement {
  // code-health: ignore[ccn,nloc] Lizard's TSX parser attributes the module's helper functions to this tiny component wrapper.
  const blocks = parseBlocks(body);

  return (
    <div className={`${className} chat-markdown`}>
      {blocks.map((block, index) => renderBlock(block, index))}
    </div>
  );
}

function parseBlocks(source: string): MarkdownBlock[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: MarkdownBlock[] = [];
  let paragraph: string[] = [];
  let listItems: string[] = [];
  let codeLines: string[] = [];
  let inFence = false;

  const flushParagraph = () => {
    if (paragraph.length === 0) return;
    blocks.push({ kind: "paragraph", lines: paragraph });
    paragraph = [];
  };
  const flushList = () => {
    if (listItems.length === 0) return;
    blocks.push({ kind: "list", items: listItems });
    listItems = [];
  };

  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      if (inFence) {
        blocks.push({ kind: "code", code: codeLines.join("\n") });
        codeLines = [];
        inFence = false;
      } else {
        flushParagraph();
        flushList();
        inFence = true;
      }
      continue;
    }

    if (inFence) {
      codeLines.push(line);
      continue;
    }

    const headingMatch = listItems.length === 0 ? line.match(/^(#{1,6})\s+(.+)$/) : null;
    const headingText = headingMatch?.[2] ? normalizeHeadingText(headingMatch[2]) : "";
    if (headingMatch?.[1] && headingText) {
      flushParagraph();
      blocks.push({
        kind: "heading",
        level: headingMatch[1].length as HeadingLevel,
        text: headingText,
      });
      continue;
    }

    const listMatch = line.match(/^\s*[-*+]\s+(.+)$/);
    if (listMatch?.[1]) {
      flushParagraph();
      listItems.push(listMatch[1]);
      continue;
    }

    if (line.trim() === "") {
      flushParagraph();
      flushList();
      continue;
    }

    flushList();
    paragraph.push(line);
  }

  if (inFence) blocks.push({ kind: "code", code: codeLines.join("\n") });
  flushParagraph();
  flushList();

  return blocks.length > 0 ? blocks : [{ kind: "paragraph", lines: [""] }];
}

function renderBlock(block: MarkdownBlock, index: number): ReactElement {
  if (block.kind === "code") {
    return (
      <pre key={index} className="chat-markdown__code">
        <code>{block.code}</code>
      </pre>
    );
  }

  if (block.kind === "list") {
    return (
      <ul key={index} className="chat-markdown__list">
        {block.items.map((item, itemIndex) => (
          <li key={itemIndex}>{renderInlineTokens(parseInline(item), "li")}</li>
        ))}
      </ul>
    );
  }

  if (block.kind === "heading") {
    const HeadingTag = headingTagFor(block.level);
    return (
      <HeadingTag
        key={index}
        className={`chat-markdown__heading chat-markdown__heading--level-${block.level}`}
      >
        {renderInlineTokens(parseInline(block.text), `h-${index}`)}
      </HeadingTag>
    );
  }

  return (
    <p key={index} className="chat-markdown__paragraph">
      {block.lines.map((line, lineIndex) => (
        <span key={lineIndex}>
          {lineIndex > 0 && <br />}
          {renderInlineTokens(parseInline(line), `p-${lineIndex}`)}
        </span>
      ))}
    </p>
  );
}

function headingTagFor(level: HeadingLevel): "h3" | "h4" | "h5" | "h6" {
  if (level <= 2) return "h3";
  if (level === 3) return "h4";
  if (level === 4) return "h5";
  return "h6";
}

function normalizeHeadingText(source: string): string {
  return source.replace(/\s+#{1,}\s*$/u, "").trimEnd();
}

function parseInline(source: string): InlineToken[] {
  const tokens: InlineToken[] = [];
  let index = 0;

  while (index < source.length) {
    const next = nextMarkerIndex(source, index);
    if (next > index) {
      tokens.push({ kind: "text", text: source.slice(index, next) });
      index = next;
      continue;
    }

    const codeEnd = source[index] === "`" ? source.indexOf("`", index + 1) : -1;
    if (codeEnd > index + 1) {
      tokens.push({ kind: "code", text: source.slice(index + 1, codeEnd) });
      index = codeEnd + 1;
      continue;
    }

    if (source.startsWith("**", index)) {
      const end = source.indexOf("**", index + 2);
      if (end > index + 2) {
        tokens.push({ kind: "strong", children: parseInline(source.slice(index + 2, end)) });
        index = end + 2;
        continue;
      }
    }

    if (source[index] === "*") {
      const end = source.indexOf("*", index + 1);
      if (end > index + 1) {
        tokens.push({ kind: "em", children: parseInline(source.slice(index + 1, end)) });
        index = end + 1;
        continue;
      }
    }

    if (source[index] === "[") {
      const labelEnd = source.indexOf("](", index + 1);
      const hrefEnd = labelEnd === -1 ? -1 : source.indexOf(")", labelEnd + 2);
      if (labelEnd > index + 1 && hrefEnd > labelEnd + 2) {
        const text = source.slice(index + 1, labelEnd);
        const link = safeLink(source.slice(labelEnd + 2, hrefEnd).trim());
        tokens.push({ kind: "link", text, link });
        index = hrefEnd + 1;
        continue;
      }
    }

    tokens.push({ kind: "text", text: source[index] ?? "" });
    index += 1;
  }

  return tokens;
}

function nextMarkerIndex(source: string, start: number): number {
  const indexes = TOKEN_MARKERS.map((marker) => source.indexOf(marker, start)).filter(
    (index) => index >= 0,
  );
  return indexes.length === 0 ? source.length : Math.min(...indexes);
}

function safeLink(rawHref: string): SafeLink | null {
  try {
    const baseOrigin =
      typeof window === "undefined" ? FALLBACK_ORIGIN : window.location.origin;
    const parsed = new URL(rawHref, baseOrigin);
    if (!SAFE_LINK_PROTOCOLS.has(parsed.protocol)) return null;
    return {
      href: rawHref,
      external: parsed.protocol !== "mailto:" && parsed.origin !== baseOrigin,
    };
  } catch {
    return null;
  }
}

function renderInlineTokens(tokens: InlineToken[], keyPrefix: string): ReactNode[] {
  return tokens.map((token, index) => {
    const key = `${keyPrefix}-${index}`;
    if (token.kind === "code") {
      return <code key={key}>{token.text}</code>;
    }
    if (token.kind === "strong") {
      return <strong key={key}>{renderInlineTokens(token.children, key)}</strong>;
    }
    if (token.kind === "em") {
      return <em key={key}>{renderInlineTokens(token.children, key)}</em>;
    }
    if (token.kind === "link" && token.link) {
      const externalAttrs = token.link.external
        ? { target: "_blank", rel: "noopener noreferrer" }
        : {};
      return (
        <a key={key} href={token.link.href} {...externalAttrs}>
          {token.text}
        </a>
      );
    }
    if (token.kind === "link") {
      return <span key={key}>{token.text}</span>;
    }
    return token.text;
  });
}
