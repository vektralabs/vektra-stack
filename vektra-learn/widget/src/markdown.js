/**
 * Minimal Markdown to HTML renderer for chat messages.
 * Covers the most common patterns in LLM responses.
 * Output is sanitized: no raw HTML passthrough, only generated tags.
 *
 * Supported: **bold**, *italic*, `inline code`, ```code blocks```,
 * [links](url), # headings (h3-h4), - unordered lists, 1. ordered lists.
 */

/**
 * Render a Markdown string to sanitized HTML.
 * @param {string} text - raw Markdown
 * @returns {string} - HTML string (safe to assign to innerHTML)
 */
export function renderMarkdown(text) {
  if (!text) return "";

  // Escape HTML entities first (XSS prevention)
  let html = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  // Code blocks (``` ... ```) - must be processed before inline patterns
  html = html.replace(
    /```(\w*)\n([\s\S]*?)```/g,
    (_, lang, code) => `<pre><code>${code.trimEnd()}</code></pre>`
  );

  // Split into lines for block-level processing
  const lines = html.split("\n");
  const output = [];
  let inList = null; // "ul" | "ol" | null
  let inPre = false;

  let paraLines = []; // accumulate consecutive text lines into one <p>

  function flushParagraph() {
    if (paraLines.length > 0) {
      output.push(`<p>${paraLines.join(" ")}</p>`);
      paraLines = [];
    }
  }

  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];

    // Track <pre> blocks: pass through without block-level processing
    if (line.includes("<pre>")) {
      flushParagraph();
      if (inList) {
        output.push(`</${inList}>`);
        inList = null;
      }
      inPre = true;
      output.push(line);
      if (line.includes("</pre>")) inPre = false;
      continue;
    }
    if (inPre) {
      output.push(line);
      if (line.includes("</pre>")) inPre = false;
      continue;
    }

    // Headings
    const headingMatch = line.match(/^(#{1,4})\s+(.+)$/);
    if (headingMatch) {
      flushParagraph();
      if (inList) {
        output.push(`</${inList}>`);
        inList = null;
      }
      const level = Math.min(headingMatch[1].length + 2, 6); // # -> h3, ## -> h4
      output.push(`<h${level}>${renderInline(headingMatch[2])}</h${level}>`);
      continue;
    }

    // Unordered list items
    const ulMatch = line.match(/^[\s]*[-*]\s+(.+)$/);
    if (ulMatch) {
      flushParagraph();
      if (inList !== "ul") {
        if (inList) output.push(`</${inList}>`);
        output.push("<ul>");
        inList = "ul";
      }
      output.push(`<li>${renderInline(ulMatch[1])}</li>`);
      continue;
    }

    // Ordered list items
    const olMatch = line.match(/^[\s]*\d+\.\s+(.+)$/);
    if (olMatch) {
      flushParagraph();
      if (inList !== "ol") {
        if (inList) output.push(`</${inList}>`);
        output.push("<ol>");
        inList = "ol";
      }
      output.push(`<li>${renderInline(olMatch[1])}</li>`);
      continue;
    }

    // Close open list if this line is not a list item
    if (inList) {
      output.push(`</${inList}>`);
      inList = null;
    }

    // Empty line -> paragraph break
    if (line.trim() === "") {
      flushParagraph();
      output.push("");
      continue;
    }

    // Regular text line: accumulate for paragraph grouping
    paraLines.push(renderInline(line));
  }

  // Flush remaining paragraph and close any open list
  flushParagraph();
  if (inList) {
    output.push(`</${inList}>`);
  }

  return output.join("\n");
}

/**
 * Render inline Markdown patterns (bold, italic, code, links).
 * @param {string} text - single line, HTML-escaped
 * @returns {string}
 */
function renderInline(text) {
  return (
    text
      // Inline code (must be before bold/italic to avoid conflicts)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      // Bold
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      // Italic
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      // Links (only http/https to prevent javascript: XSS)
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, url) => {
        if (/^https?:\/\//i.test(url)) {
          const safeUrl = url.replace(/"/g, "&quot;");
          return `<a href="${safeUrl}" target="_blank" rel="noopener">${label}</a>`;
        }
        return `${label} (${url})`;
      })
  );
}
