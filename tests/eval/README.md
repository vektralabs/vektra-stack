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

Both files share the same 55 questions (21 factual, 15 reasoning, 10 multi-chunk,
9 adversarial without ground truth; 37 IT / 18 EN). Entry shape:
`{id, question, expected_keywords, namespace, category, language}`. Relevance is
keyword-based (`expected_keywords`, diacritic-insensitive substring match), so it
is chunking-independent and survives reingestion.

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

## Adding questions

Append JSONL entries with a unique `id`, the target `namespace`, and 1-3
`expected_keywords` that only appear in the passages that truly answer the
question. Adversarial entries (expected refusal) omit `expected_keywords` and are
reported separately (`has_ground_truth: false`).
