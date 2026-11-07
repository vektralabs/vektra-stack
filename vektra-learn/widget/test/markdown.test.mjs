// Ordered list rendering (issue #164).
//
// A list whose items are separated by blank lines used to close and reopen one
// <ol> per item, so every item rendered as "1." and the number written in the
// source was discarded. These tests pin the rendered HTML.
import { test } from "node:test";
import assert from "node:assert/strict";
import { renderMarkdown } from "../src/markdown.js";

const count = (html, token) => html.split(token).length - 1;

test("blank lines between ordered items keep one list and its numbering", () => {
  const html = renderMarkdown("1. First step\n\n2. Second step\n\n3. Third step");
  assert.equal(count(html, "<ol>"), 1);
  assert.equal(count(html, "</ol>"), 1);
  assert.equal(count(html, "<li>"), 3);
  assert.ok(!html.includes("<ol start="));
});

test("the first source number becomes the <ol> start", () => {
  const html = renderMarkdown("3. Third\n4. Fourth\n5. Fifth");
  assert.ok(html.includes('<ol start="3">'));
  assert.equal(count(html, "<li>"), 3);
});

test("a marker that breaks the sequence is kept with value", () => {
  const html = renderMarkdown("1. One\n2. Two\n5. Five\n6. Six");
  assert.ok(html.includes('<li value="5">Five</li>'));
  assert.ok(html.includes("<li>Six</li>"));
});

test("unordered lists keep their markers across blank lines", () => {
  const html = renderMarkdown("- alpha\n\n- beta");
  assert.equal(count(html, "<ul>"), 1);
  assert.equal(count(html, "<li>"), 2);
});

test("a paragraph after a list still closes it", () => {
  const html = renderMarkdown("1. one\n\ntext after");
  assert.equal(count(html, "</ol>"), 1);
  assert.ok(html.includes("<p>text after</p>"));
});

test("switching from ordered to unordered closes the ordered list", () => {
  const html = renderMarkdown("1. one\n\n- bullet");
  assert.ok(html.includes("</ol>"));
  assert.ok(html.includes("<ul>"));
});

test("a fenced code block keeps its content and is not read as a list", () => {
  const html = renderMarkdown(
    "Before\n\n```js\n1. not a list\n2. still code\n```\n\nAfter",
  );
  assert.ok(html.includes("<pre><code>1. not a list"));
  assert.ok(!html.includes("<ol>"));
  assert.ok(html.includes("<p>Before</p>"));
  assert.ok(html.includes("<p>After</p>"));
});
