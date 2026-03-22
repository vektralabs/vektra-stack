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

  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];

    // Skip lines inside <pre> blocks (already processed)
    if (line.includes("<pre>") || line.includes("</pre>")) {
      if (inList) {
        output.push(`</${inList}>`);
        inList = null;
      }
      output.push(line);
      continue;
    }

    // Headings
    const headingMatch = line.match(/^(#{1,4})\s+(.+)$/);
    if (headingMatch) {
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
      output.push("");
      continue;
    }

    // Regular paragraph line
    output.push(`<p>${renderInline(line)}</p>`);
  }

  // Close any open list
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
      // Links
      .replace(
        /\[([^\]]+)\]\(([^)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener">$1</a>'
      )
  );
}
