#!/usr/bin/env python3
"""
Clean-extract "Diritto penale del lavoro" (Carlo Smuraglia, Milano University
Press 2025, CC BY-SA 4.0) from its PDF text layer into a structured markdown
corpus for RAG evaluation.

Run with:
    uv run --with pdfplumber python scripts/eval_clean_textbook.py <input.pdf> <output.md>

Source PDF (CC BY-SA 4.0, free download):
    https://libri.unimi.it/index.php/milanoup/catalog/download/236/841/2256?inline=1
    sha256 ef43d9cf11f52d8f7f17c97c24ee4bbb9cdcb2836de12ebce94340cb15fb3ec5

Method (measured, not guessed -- see the printed report):
  - Font-size based classification of every text line on every page:
      body prose    ~= 11.0pt  (dominant size, ~604k chars)
      footnotes     ~=  9.0pt  (~304k chars) -> dropped entirely
      running header~= 10.0pt, first line on the page, top < 60pt -> dropped
      headings      >= 13pt (14pt = numbered sub-section, 18pt = chapter/
                     front-matter section) -> converted to markdown headings
      superscript footnote markers ~6.3-6.5pt embedded mid-body -> stripped
      small-caps continuation letters ~7.3-7.4pt embedded mid-body (Italian
                     legal-citation convention, e.g. "Mazzoni" typeset as a
                     normal "M" + reduced-size "azzoni") -> KEPT, merged
                     into the surrounding word (dropping them would corrupt
                     names inline in body prose).
  - Paragraph reconstruction uses the first-line indent (x0 ~= 87.9pt vs the
    flush margin x0 ~= 76.5pt) as the paragraph-break signal.
  - End-of-line hyphenation is repaired using a self-referential heuristic:
    any "word-word" hyphen pair that appears INLINE (not at a line break)
    anywhere in the body is treated as a genuine compound and its hyphen is
    preserved; a hyphen at a line break that never appears inline elsewhere
    is treated as a soft (typesetting) break and is removed on join.
  - Front matter (cover, colophon, licence, Piano dell'opera, Comitato
    Scientifico/Editoriale, book-level Sommario/TOC) is dropped by skipping
    everything before the "Presentazione" heading.
  - The per-chapter in-body "Sommario" paragraph (an outline of the coming
    sub-sections, printed right after each chapter title) is dropped: it
    duplicates the sub-section headings and is not prose.
  - The back-matter "Bibliografia essenziale" (a ~13-page author-sorted
    citation list, no prose) is dropped: extraction stops at that heading,
    treated the same as footnotes (dense citation apparatus).
"""

import re
import sys
from collections import Counter

import pdfplumber

PDF_PATH = sys.argv[1] if len(sys.argv) > 1 else "smuraglia-diritto-penale-lavoro.pdf"
OUTPUT_MD = (
    sys.argv[2] if len(sys.argv) > 2 else "smuraglia-diritto-penale-lavoro-clean.md"
)

BOOK_TITLE = "Diritto penale del lavoro"

ROMAN_MAP = {
    "I": 1,
    "II": 2,
    "III": 3,
    "IV": 4,
    "V": 5,
    "VI": 6,
    "VII": 7,
    "VIII": 8,
    "IX": 9,
    "X": 10,
    "XI": 11,
    "XII": 12,
    "XIII": 13,
    "XIV": 14,
    "XV": 15,
}

BODY_SIZE_LO, BODY_SIZE_HI = 10.9, 11.1  # body prose ~11.0pt
FOOTNOTE_SIZE_LO, FOOTNOTE_SIZE_HI = 8.9, 9.1  # footnotes ~9.0pt
HEADER_SIZES = (10.0, 10.5)  # running header line
HEADER_TOP_MAX = 60.0  # header always at top of page
HEADING_MIN_SIZE = 13.0  # 14pt / 18pt headings
SMALLCAPS_MIN_SIZE = 7.0  # keep >= this within body lines
INDENT_X0_THRESHOLD = 82.0  # between flush(76.5) and indent(87.9)

FRONT_MATTER_HEADING = "Presentazione"
BIBLIOGRAPHY_HEADING_PREFIX = "Bibliografia essenziale"

TOP_LEVEL_SECTIONS = {"Presentazione", "Premessa al VI volume", "Introduzione"}


# ---------------------------------------------------------------------------
# Low-level PDF line grouping
# ---------------------------------------------------------------------------


def group_lines(chars, tol=4):
    """Cluster chars into visual lines by proximity of their 'top' coordinate.

    Uses interval-merge clustering (not fixed rounding) so that a single
    visual line whose glyphs have slightly different baselines (e.g. mixed
    font sizes on one line, observed on the Capitolo IV heading) is still
    grouped as one line, while distinct lines (13pt+ apart) stay separate.
    """
    chars = sorted(chars, key=lambda c: c["top"])
    clusters = []
    cur, cur_max = [], None
    for c in chars:
        if cur and c["top"] > cur_max + tol:
            clusters.append(cur)
            cur, cur_max = [], None
        cur.append(c)
        cur_max = c["top"] if cur_max is None else max(cur_max, c["top"])
    if cur:
        clusters.append(cur)
    clusters.sort(key=lambda lc: min(c["top"] for c in lc))
    return clusters


def line_text(chars, min_size=0.0):
    """Concatenate chars left-to-right, dropping any char smaller than min_size."""
    kept = [c for c in chars if c["size"] >= min_size]
    return "".join(c["text"] for c in sorted(kept, key=lambda c: c["x0"]))


def has_size_near(chars, lo, hi):
    return any(lo <= c["size"] <= hi for c in chars)


def dominant_size(chars):
    sizes = Counter(round(c["size"], 1) for c in chars)
    return sizes.most_common(1)[0][0]


# ---------------------------------------------------------------------------
# Pass 1: find genuine inline hyphenated compounds (self-referential lexicon)
# ---------------------------------------------------------------------------

WORD_RE = r"[A-Za-zÀ-ÖØ-öø-ÿ]+"
INLINE_COMPOUND_RE = re.compile(rf"({WORD_RE})-({WORD_RE})")
TRAILING_HYPHEN_WORD_RE = re.compile(rf"({WORD_RE})-\s*$")
LEADING_WORD_RE = re.compile(rf"^({WORD_RE})")


def find_known_compounds(pdf, start_idx, end_idx):
    """Scan body-size lines for '-' that is NOT the last character of the
    line (i.e. not a line-wrap artifact): those are unambiguous genuine
    hyphenated compounds. Build a lowercase set of 'word-word' pairs.
    """
    known = set()
    for pno in range(start_idx, end_idx):
        page = pdf.pages[pno]
        if not page.chars:
            continue
        for lc in group_lines(page.chars):
            if not has_size_near(lc, BODY_SIZE_LO, BODY_SIZE_HI):
                continue
            text = line_text(lc, min_size=SMALLCAPS_MIN_SIZE)
            stripped = text.rstrip()
            if stripped.endswith("-"):
                # this hyphen IS at line end -> not usable for pass 1
                body_wo_trailing = stripped[:-1]
            else:
                body_wo_trailing = stripped
            for m in INLINE_COMPOUND_RE.finditer(body_wo_trailing):
                known.add(f"{m.group(1).lower()}-{m.group(2).lower()}")
    return known


# ---------------------------------------------------------------------------
# Pass 2: locate structural boundaries (front matter end / bibliography start)
# ---------------------------------------------------------------------------


def find_heading_blocks(pdf, start_idx, end_idx):
    """Yield (page_idx, text) for every consecutive run of heading-size
    (>=13pt) lines on pages [start_idx, end_idx)."""
    for pno in range(start_idx, end_idx):
        page = pdf.pages[pno]
        if not page.chars:
            continue
        lines = group_lines(page.chars)
        block = []
        for lc in lines:
            if (
                has_size_near(lc, 13.0, 100.0)
                or max(c["size"] for c in lc) >= HEADING_MIN_SIZE
            ):
                block.append(lc)
            else:
                if block:
                    text = " ".join(line_text(ln, min_size=10.0) for ln in block)
                    yield pno, re.sub(r"\s+", " ", text).strip()
                    block = []
        if block:
            text = " ".join(line_text(ln, min_size=10.0) for ln in block)
            yield pno, re.sub(r"\s+", " ", text).strip()


def locate_boundaries(pdf):
    n = len(pdf.pages)
    body_start = None
    body_end = None
    for pno, text in find_heading_blocks(pdf, 0, n):
        if body_start is None and text.startswith(FRONT_MATTER_HEADING):
            body_start = pno
        if body_start is not None and text.startswith(BIBLIOGRAPHY_HEADING_PREFIX):
            body_end = pno
            break
    if body_start is None:
        raise RuntimeError(
            f"Could not find front-matter boundary heading {FRONT_MATTER_HEADING!r}"
        )
    if body_end is None:
        raise RuntimeError(
            f"Could not find back-matter boundary heading {BIBLIOGRAPHY_HEADING_PREFIX!r}"
        )
    return body_start, body_end


# ---------------------------------------------------------------------------
# Pass 3: main extraction
# ---------------------------------------------------------------------------


class Stats:
    def __init__(self):
        self.pages_processed = 0
        self.headers_removed = 0
        self.footnote_blocks_removed = 0
        self.footnote_lines_removed = 0
        self.footnote_words_removed = 0
        self.stray_marker_lines_dropped = 0
        self.sommario_paragraphs_dropped = 0
        self.sommario_words_dropped = 0
        self.headings = []  # (level, text)
        self.body_words_kept = 0
        self.hyphen_joins_soft = 0
        self.hyphen_joins_compound = 0
        self.unclassified_lines = []


def roman_to_title(text):
    m = re.match(r"^Capitolo\s+([IVXLC]+)\.?\s*(.*)$", text, re.IGNORECASE)
    if not m:
        return None, None
    roman, title = m.group(1), m.group(2).strip()
    title = re.sub(
        r"\s*\*\s*$", "", title
    )  # drop trailing footnote-marker asterisk if any survived
    return roman, title


def repair_hyphen_join(prev_text, next_text, known_compounds, stats):
    """Join prev_text (ends with '-') and next_text, deciding whether the
    hyphen is a genuine compound (kept) or a soft line-break (removed)."""
    m = TRAILING_HYPHEN_WORD_RE.search(prev_text)
    m2 = LEADING_WORD_RE.match(next_text)
    if not m or not m2:
        # can't identify a clean word pair; default to keeping the hyphen as-is
        return prev_text.rstrip() + next_text.lstrip()
    word_before, word_after = m.group(1), m2.group(1)
    key = f"{word_before.lower()}-{word_after.lower()}"
    if key in known_compounds:
        stats.hyphen_joins_compound += 1
        return prev_text.rstrip() + next_text.lstrip()
    else:
        stats.hyphen_joins_soft += 1
        return prev_text.rstrip()[:-1] + next_text.lstrip()


def extract_body(pdf, body_start, body_end, known_compounds, stats):
    """Returns a list of markdown blocks (strings), in reading order."""

    blocks = []  # finished output: ("h2"/"h3"/"p", text)
    para_lines = []  # accumulator for the current paragraph
    heading_lines = []  # accumulator for the current heading block
    in_heading = False
    current_chapter_num = None  # arabic chapter number for ### N.M numbering
    just_started_chapter = False  # true right after emitting a ## Capitolo heading

    def flush_paragraph():
        nonlocal para_lines
        if not para_lines:
            return
        text = para_lines[0]
        for nxt in para_lines[1:]:
            if text.rstrip().endswith("-"):
                text = repair_hyphen_join(text, nxt, known_compounds, stats)
            else:
                text = text.rstrip() + " " + nxt.lstrip()
        text = re.sub(r"[ \t]+", " ", text).strip()
        para_lines = []
        return text

    def emit_paragraph():
        nonlocal just_started_chapter
        text = flush_paragraph()
        if not text:
            return
        if just_started_chapter and re.match(r"^Sommario\b", text):
            stats.sommario_paragraphs_dropped += 1
            stats.sommario_words_dropped += len(text.split())
            just_started_chapter = False
            return
        just_started_chapter = False
        blocks.append(("p", text))
        stats.body_words_kept += len(text.split())

    def emit_heading():
        nonlocal heading_lines, current_chapter_num, just_started_chapter
        if not heading_lines:
            return
        text = heading_lines[0]
        for nxt in heading_lines[1:]:
            if text.rstrip().endswith("-"):
                text = repair_hyphen_join(text, nxt, known_compounds, stats)
            else:
                text = text.rstrip() + " " + nxt.lstrip()
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"\s*\*\s*$", "", text)
        heading_lines = []

        roman, title = roman_to_title(text)
        if roman:
            arabic = ROMAN_MAP.get(roman.upper())
            current_chapter_num = arabic
            heading_text = f"Capitolo {roman} - {title}"
            blocks.append(("h2", heading_text))
            stats.headings.append(("h2", heading_text))
            just_started_chapter = True
            return

        if any(text.startswith(s) for s in TOP_LEVEL_SECTIONS):
            blocks.append(("h2", text))
            stats.headings.append(("h2", text))
            current_chapter_num = None
            just_started_chapter = False
            return

        m = re.match(r"^(\d+)\.\s*(.*)$", text)
        if m:
            num, title = int(m.group(1)), m.group(2).strip()
            if current_chapter_num is not None:
                heading_text = f"{current_chapter_num}.{num} {title}"
            else:
                heading_text = f"{num} {title}"
            blocks.append(("h3", heading_text))
            stats.headings.append(("h3", heading_text))
            return

        stats.unclassified_lines.append(text)
        blocks.append(("h2", text))
        stats.headings.append(("h2 (UNCLASSIFIED)", text))

    for pno in range(body_start, body_end):
        page = pdf.pages[pno]
        if not page.chars:
            continue
        stats.pages_processed += 1
        lines = group_lines(page.chars)

        idx = 0
        # drop running header: first line on page, small size, near top
        if lines:
            first = lines[0]
            if (
                min(c["top"] for c in first) < HEADER_TOP_MAX
                and round(dominant_size(first), 1) in HEADER_SIZES
            ):
                idx = 1
                stats.headers_removed += 1

        page_lines = lines[idx:]
        for li, lc in enumerate(page_lines):
            if has_size_near(lc, BODY_SIZE_LO, BODY_SIZE_HI):
                # BODY line (possibly with small-caps decoration / stray markers)
                if in_heading:
                    emit_heading()
                    in_heading = False
                text = line_text(lc, min_size=SMALLCAPS_MIN_SIZE)
                if not text.strip():
                    continue
                x0 = min(c["x0"] for c in lc if c["size"] >= SMALLCAPS_MIN_SIZE)
                # An indented line only starts a genuine new paragraph if the
                # text accumulated so far actually ended a sentence. This
                # guards against hanging-indent list continuations (e.g. a
                # lettered sub-list "a. ... \n    di controllo ...") whose
                # wrapped second line is indented further than the flush
                # margin but is NOT a new paragraph -- confirmed by measurement
                # (page idx 162: "a. norme che tendono a rendere possibile ed
                # effettiva l'attività di vigilanza e" / "di controllo (e
                # quindi ..." wrongly split without this guard).
                prev_complete = (not para_lines) or para_lines[-1].rstrip().endswith(
                    (".", "?", "!", ":", ";", "»", "”", ")")
                )
                is_new_paragraph = x0 > INDENT_X0_THRESHOLD and prev_complete
                if is_new_paragraph and para_lines:
                    emit_paragraph()
                para_lines.append(text)
            elif max(c["size"] for c in lc) >= HEADING_MIN_SIZE:
                # HEADING line
                if para_lines:
                    emit_paragraph()
                in_heading = True
                heading_lines.append(line_text(lc, min_size=10.0))
            elif has_size_near(lc, FOOTNOTE_SIZE_LO, FOOTNOTE_SIZE_HI):
                # footnote block begins: drop this line and everything else on the page
                # (confirmed by measurement: footnotes never carry over to the
                # next page and no page starts mid-footnote, so the remainder
                # of the page's lines are all footnote content too).
                # NOTE: do NOT flush para_lines here. The footnote apparatus sits
                # at the bottom of the page, visually separate from body flow; the
                # body paragraph in progress may legitimately continue on the next
                # page's first line. Flushing here would wrongly split a sentence
                # into two markdown paragraphs (found via manual page comparison,
                # see e.g. "...dell'autorità" / "amministrativa, ..." on pages
                # idx 294-295).
                if in_heading:
                    emit_heading()
                    in_heading = False
                stats.footnote_blocks_removed += 1
                remainder = page_lines[li:]
                stats.footnote_lines_removed += len(remainder)
                for rlc in remainder:
                    stats.footnote_words_removed += len(line_text(rlc).split())
                break
            else:
                # tiny stray content only (superscript footnote marker clustered alone)
                stats.stray_marker_lines_dropped += 1
                continue

    if in_heading:
        emit_heading()
    if para_lines:
        emit_paragraph()

    return blocks


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_markdown(blocks):
    out = [f"# {BOOK_TITLE}", ""]
    for kind, text in blocks:
        if kind == "h2":
            out.append(f"## {text}")
            out.append("")
        elif kind == "h3":
            out.append(f"### {text}")
            out.append("")
        else:
            out.append(text)
            out.append("")
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    stats = Stats()
    with pdfplumber.open(PDF_PATH) as pdf:
        n_pages = len(pdf.pages)
        print(f"Total PDF pages: {n_pages}")

        body_start, body_end = locate_boundaries(pdf)
        print(
            f"Front matter dropped: pages 0-{body_start - 1} (0-indexed), "
            f"{body_start} pages"
        )
        print(
            f"Body range: pages {body_start}-{body_end - 1} (0-indexed), "
            f"{body_end - body_start} pages"
        )
        print(
            f"Back matter dropped from page {body_end} onward "
            f"({n_pages - body_end} pages: Bibliografia essenziale + colophon)"
        )

        print(
            "\nBuilding self-referential hyphenated-compound lexicon "
            "from inline (non-line-break) hyphens..."
        )
        known_compounds = find_known_compounds(pdf, body_start, body_end)
        print(f"Known inline compounds found: {len(known_compounds)}")

        print("\nExtracting body...")
        blocks = extract_body(pdf, body_start, body_end, known_compounds, stats)

    md = render_markdown(blocks)
    with open(OUTPUT_MD, "w", encoding="utf-8") as f:
        f.write(md)

    total_words = sum(len(t.split()) for k, t in blocks if k == "p")

    print("\n=== EXTRACTION REPORT ===")
    print(f"Pages processed (body): {stats.pages_processed}")
    print(f"Running headers removed: {stats.headers_removed}")
    print(
        f"Footnote blocks removed: {stats.footnote_blocks_removed} "
        f"({stats.footnote_lines_removed} lines, "
        f"~{stats.footnote_words_removed} words)"
    )
    print(f"Stray superscript-marker lines dropped: {stats.stray_marker_lines_dropped}")
    print(
        f"Per-chapter 'Sommario' paragraphs dropped: {stats.sommario_paragraphs_dropped} "
        f"(~{stats.sommario_words_dropped} words)"
    )
    print(
        f"Hyphen line-breaks joined as SOFT (hyphen removed): {stats.hyphen_joins_soft}"
    )
    print(f"Hyphen line-breaks kept as GENUINE COMPOUND: {stats.hyphen_joins_compound}")
    print(f"Headings detected: {len(stats.headings)}")
    for level, text in stats.headings:
        print(f"  {level:>18}: {text}")
    if stats.unclassified_lines:
        print(
            f"UNCLASSIFIED heading-size blocks (needs manual review): "
            f"{len(stats.unclassified_lines)}"
        )
        for t in stats.unclassified_lines:
            print(f"  ??? {t}")
    print(f"\nFinal body word count: {total_words}")
    print(
        f"Estimated chunks at ~350 words/chunk (~500 tokens IT): "
        f"{round(total_words / 350)}"
    )
    print(f"\nOutput written to: {OUTPUT_MD}")


if __name__ == "__main__":
    main()
