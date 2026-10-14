# RAG evaluation harness (TECH-002)

Two-stage evaluation against a running Vektra stack. Both scripts are pure HTTP
clients: they require `VEKTRA_API_URL` and `VEKTRA_API_KEY` in the environment.

```bash
make eval-retrieval    # /api/v1/search (no LLM) - hit rate, MRR, precision@k
make eval-e2e          # /api/v1/query (full pipeline + LLM) - grounded rate, latency
# extra args:
make eval-retrieval EVAL_ARGS="--dataset tests/eval/dataset-full.jsonl --output tests/eval/results_retrieval_full.jsonl"
```

Results are written as JSONL next to the datasets (`results_*.jsonl`, gitignored:
record aggregates in the active `.s2s/plans/` file and in vektra-internal).

## Datasets

| File | Namespace | Corpus |
|------|-----------|--------|
| `dataset.jsonl` | `default` | Excerpt corpus, 12 chunks: `costituzione_italiana.md` v2 (6), `udhr_excerpts.md` v2 (4), `sample.pdf` (2). Hit rate saturates here; useful for smoke/regression, not for tuning. |
| `dataset-full.jsonl` | `eval-full` | Full-document corpus, 78 chunks: clean full Italian Constitution (72) + official UDHR English PDF (6). Same 55 questions, discriminative metrics. |
| `dataset-textbook.jsonl` | `eval-textbook` | Textbook corpus (TECH-005 collection 1), 449 child chunks + 113 parents: one long Italian legal textbook. 60 questions, Italian. **Ground truth pending human spot-check: do not gate a release on it yet** (see "Validation status" below). |

`dataset.jsonl` and `dataset-full.jsonl` share the same 55 questions (21 factual,
15 reasoning, 10 multi-chunk, 9 adversarial without ground truth; 37 IT / 18 EN).
Entry shape: `{id, question, expected_keywords, namespace, category, language}`
(`dataset-textbook.jsonl` adds `source_section`, which the harness ignores and which
exists so a human can audit where each question came from). Relevance is
keyword-based (`expected_keywords`, diacritic-insensitive substring match), so it
is chunking-independent and survives reingestion.

### Which one to use

The excerpt corpus saturates: its hit rate is near 100% regardless of configuration,
because 20 retrieved candidates cover most of a 12-chunk corpus. Use it as a smoke
test, never to compare configurations. `eval-full` discriminates but its documents are
short, self-contained legal articles, so it understates anything that depends on
surrounding context (parent chunk expansion in particular). `eval-textbook` is the hard
bench: long argumentative prose whose chunks are ambiguous on their own.

First measured baseline on `eval-textbook` (MiniLM, dual chunking, top_k=5): hit rate
78.4%, MRR 0.5873 (factual 79%, multi-chunk 92%, reasoning 67%). Compare with
`eval-full` under the same configuration: 82.6% / 0.7029. The textbook is harder, which
is the point.

## Corpus provenance (eval-full)

- `costituzione-full-clean.md`: full text of the Italian Constitution converted
  from Wikisource (`https://it.wikisource.org/api/rest_v1/page/html/Costituzione_della_Repubblica_italiana`,
  CC BY-SA), metadata header stripped. Kept outside the repo at
  `/mnt/ai/datasets/vektra-eval/` on the dev machine.
- `udhr-en-ohchr.pdf`: official OHCHR English UDHR
  (`https://www.ohchr.org/sites/default/files/UDHR/Documents/UDHR_Translations/eng.pdf`).
- Do NOT use the senato.it combined PDF (`costituzione.pdf`, 506 pages): its print
  layout breaks pdfplumber extraction (fused words like
  `COSTITUZIONEDELLAREPUBBLICAITALIANA`, preserved hyphenation) and invalidates
  keyword ground truth. Measured impact: IT hit rate 72% garbled vs 88% clean on
  the same questions. Worth keeping in mind as a future "dirty extraction" test
  case, but never as a retrieval-quality corpus.

To rebuild the corpus: download the two sources, then
`scripts/ingest.sh <file> eval-full` for each.

## Corpus provenance (eval-textbook)

Carlo Smuraglia, *Diritto penale del lavoro*, Milano University Press 2025 (reissue of
the 1980 Cedam text), DOI `10.54103/milanoup.236`. **Licence CC BY-SA 4.0**, so the text
may be redistributed and modified with attribution under the same licence.

```bash
curl -L -o textbook.pdf \
  "https://libri.unimi.it/index.php/milanoup/catalog/download/236/841/2256?inline=1"
# sha256 ef43d9cf11f52d8f7f17c97c24ee4bbb9cdcb2836de12ebce94340cb15fb3ec5
uv run --with pdfplumber python scripts/eval_clean_textbook.py textbook.pdf textbook-clean.md
scripts/ingest.sh textbook-clean.md eval-textbook
```

Why this book: 13 chapters, numbered sections, prose with dense implicit
cross-references ("le considerazioni suesposte", "come si e' detto"), no formulas and no
tables, so the corpus stresses retrieval rather than PDF extraction. The cleaning script
classifies text by font size (body 11pt, footnotes 9pt, running headers 10pt, headings
14/18pt), drops footnotes and headers, repairs end-of-line hyphenation, and preserves the
chapter/section structure as markdown headings, which is what the dual chunker turns into
parent chunks. It removes the citation apparatus on purpose: footnotes in a legal treatise
are dense reference lists that would pollute the chunks.

Licensing note for anyone adding a corpus: prefer CC BY or CC BY-SA. Sources carrying an
**ND** (NoDerivatives) clause cannot be used, because a cleaned and chunked corpus is a
derivative work. Most Italian university lecture notes are CC BY-NC-ND for this reason, and
OpenStax advertises CC BY while its live per-book metadata says CC BY-NC-SA. Check the
licence on the actual artefact, not the marketing page.

## Ground truth methodology

Applies to any new dataset. Keyword ground truth is only as good as its construction, and a
bad dataset fails silently: it produces plausible numbers that mean nothing.

1. **Author questions from full documents, not from chunks.** Reading a chunk and writing a
   question about it guarantees the question is answerable from that chunk, which biases the
   whole dataset towards trivially retrievable passages.
2. **Every keyword must be individually discriminative.** The harness scores a chunk as a hit
   if *any one* keyword appears in it (case- and diacritic-insensitive substring). A generic
   term therefore marks wrong chunks as hits. Use verbatim multi-word phrases lifted from the
   source passage, and verify each one occurs in the intended section and nowhere else.
3. **Adversarial questions must be verified non-answerable**, and not just by grep: the corpus
   may cover the topic elsewhere in different words. An adversarial question the corpus can
   actually answer punishes the system for being right.
4. **Run the closed-book control.** Put every question to the answering LLM with no context and
   an instruction to answer only if certain. Any question it answers correctly from parametric
   memory does not measure retrieval: drop it or rewrite it to depend on the source (for
   example by anchoring it to the author's own argument). On `eval-textbook`: 0 expected
   keywords leaked, 53/60 answered "NON SO".
5. **Spot-check a stratified sample by hand.** Machine checks cannot see a question that a
   single section already answers although it is labelled multi-chunk, nor an adversarial
   question the corpus covers under a synonym.

## Validation status

| Dataset | Machine-validated | Closed-book control | Human spot-check |
|---------|-------------------|---------------------|------------------|
| `dataset.jsonl` | partial | not run | no |
| `dataset-full.jsonl` | partial | not run | no |
| `dataset-textbook.jsonl` | yes | yes (0 leaks) | **pending** |

`dataset-textbook.jsonl` is published so the evaluation can be reproduced and reviewed, but
its ground truth has not been reviewed by a human yet. Until it has, treat its numbers as
indicative and do not gate a release on them. The closed-book control should also be run
retroactively on the two older datasets.

## Adding questions

Append JSONL entries with a unique `id`, the target `namespace`, and 1-3
`expected_keywords` that only appear in the passages that truly answer the
question. Adversarial entries (expected refusal) omit `expected_keywords` and are
reported separately (`has_ground_truth: false`).
