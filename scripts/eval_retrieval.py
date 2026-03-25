#!/usr/bin/env python3
"""Retrieval-only evaluation harness (TECH-002).

Reads a JSONL dataset, calls the Vektra search API for each question,
and computes retrieval quality metrics. No LLM calls are made.

Usage:
    python scripts/eval_retrieval.py [OPTIONS]

Requires VEKTRA_API_URL and VEKTRA_API_KEY environment variables.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

DEFAULT_DATASET = "tests/eval/dataset.jsonl"
DEFAULT_OUTPUT = "tests/eval/results_retrieval.jsonl"


@dataclass
class QuestionResult:
    id: str
    question: str
    category: str
    language: str
    hit: bool  # at least one chunk matches expected keywords
    reciprocal_rank: float  # 1/rank of first relevant chunk (0 if no hit)
    precision_at_k: float  # relevant chunks / total chunks
    num_retrieved: int
    num_relevant: int
    has_ground_truth: bool = True  # False for entries without expected_keywords
    scores: list[float] = field(default_factory=list)
    error: str | None = None


def load_dataset(path: str) -> list[dict]:
    entries = []
    with open(path) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"Warning: skipping line {line_num}: {e}", file=sys.stderr)
    return entries


def chunk_matches_keywords(text: str, keywords: list[str]) -> bool:
    """Check if a chunk's text contains any of the expected keywords (case-insensitive)."""
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def evaluate_question(
    client: httpx.Client,
    entry: dict,
    top_k: int,
    search_mode: str,
    use_query: bool = False,
) -> QuestionResult:
    """Run a single search/query and compute retrieval metrics.

    When use_query=True, calls /api/v1/query (includes reranker) and
    evaluates on the returned sources. Otherwise calls /api/v1/search
    (raw vector search, no reranker).
    """
    question_id = entry["id"]
    question = entry["question"]
    keywords = entry.get("expected_keywords", [])
    namespace = entry.get("namespace", "default")
    category = entry.get("category", "unknown")
    language = entry.get("language", "unknown")

    try:
        if use_query:
            resp = client.post(
                "/api/v1/query",
                json={
                    "question": question,
                    "namespace": namespace,
                    "top_k": top_k,
                },
            )
        else:
            resp = client.post(
                "/api/v1/search",
                json={
                    "query": question,
                    "namespace": namespace,
                    "top_k": top_k,
                    "search_mode": search_mode,
                },
            )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return QuestionResult(
            id=question_id,
            question=question,
            category=category,
            language=language,
            hit=False,
            reciprocal_rank=0.0,
            precision_at_k=0.0,
            num_retrieved=0,
            num_relevant=0,
            has_ground_truth=False,
            error=str(e),
        )

    # /api/v1/query returns "sources", /api/v1/search returns "results"
    results = data.get("sources") or data.get("results") or []
    scores = [r["score"] for r in results]

    if not keywords:
        # No ground truth: can only report retrieval count and scores
        return QuestionResult(
            id=question_id,
            question=question,
            category=category,
            language=language,
            hit=False,
            reciprocal_rank=0.0,
            precision_at_k=0.0,
            num_retrieved=len(results),
            num_relevant=0,
            has_ground_truth=False,
            scores=scores,
        )

    # Compute which chunks are relevant
    # /api/v1/search uses "text_snippet", /api/v1/query uses "snippet"
    relevant_mask = [
        chunk_matches_keywords(r.get("text_snippet") or r.get("snippet", ""), keywords)
        for r in results
    ]
    num_relevant = sum(relevant_mask)
    hit = num_relevant > 0

    # MRR: reciprocal rank of first relevant chunk
    reciprocal_rank = 0.0
    for i, is_relevant in enumerate(relevant_mask):
        if is_relevant:
            reciprocal_rank = 1.0 / (i + 1)
            break

    # Precision@k
    precision_at_k = num_relevant / len(results) if results else 0.0

    return QuestionResult(
        id=question_id,
        question=question,
        category=category,
        language=language,
        hit=hit,
        reciprocal_rank=reciprocal_rank,
        precision_at_k=precision_at_k,
        num_retrieved=len(results),
        num_relevant=num_relevant,
        scores=scores,
    )


def print_summary(results: list[QuestionResult], elapsed_s: float) -> None:
    total = len(results)
    errors = sum(1 for r in results if r.error)
    evaluated = total - errors

    if evaluated == 0:
        print("No questions evaluated successfully.")
        return

    valid = [r for r in results if r.error is None]
    # Separate scored (have ground truth keywords) from unscored (adversarial)
    scored = [r for r in valid if r.has_ground_truth]
    unscored = [r for r in valid if not r.has_ground_truth]

    print(f"\n{'=' * 60}")
    print("Retrieval evaluation results")
    print(f"{'=' * 60}")
    print(
        f"Questions:      {total} ({errors} errors, {len(unscored)} without ground truth)"
    )
    print(f"Duration:       {elapsed_s:.1f}s ({elapsed_s / total:.2f}s/query)")

    if scored:
        hit_rate = sum(r.hit for r in scored) / len(scored)
        mrr = sum(r.reciprocal_rank for r in scored) / len(scored)
        avg_precision = sum(r.precision_at_k for r in scored) / len(scored)
        avg_retrieved = sum(r.num_retrieved for r in scored) / len(scored)
        avg_relevant = sum(r.num_relevant for r in scored) / len(scored)

        print(f"\nScored ({len(scored)} questions with ground truth):")
        print(f"  Hit rate:       {hit_rate:.1%}")
        print(f"  MRR:            {mrr:.4f}")
        print(f"  Avg precision:  {avg_precision:.4f}")
        print(f"  Avg retrieved:  {avg_retrieved:.1f}")
        print(f"  Avg relevant:   {avg_relevant:.1f}")
    else:
        print("\nNo scored questions (all entries lack ground truth keywords).")

    # Breakdown by category (scored only)
    if scored:
        categories = sorted(set(r.category for r in scored))
        if len(categories) > 1:
            print("\n  By category:")
            for cat in categories:
                cat_results = [r for r in scored if r.category == cat]
                cat_hit = sum(r.hit for r in cat_results) / len(cat_results)
                cat_mrr = sum(r.reciprocal_rank for r in cat_results) / len(cat_results)
                print(
                    f"    {cat:<15} hit={cat_hit:.0%}  mrr={cat_mrr:.4f}  n={len(cat_results)}"
                )

    # Breakdown by language (scored only)
    if scored:
        languages = sorted(set(r.language for r in scored))
        if len(languages) > 1:
            print("\n  By language:")
            for lang in languages:
                lang_results = [r for r in scored if r.language == lang]
                lang_hit = sum(r.hit for r in lang_results) / len(lang_results)
                lang_mrr = sum(r.reciprocal_rank for r in lang_results) / len(
                    lang_results
                )
                print(
                    f"    {lang:<15} hit={lang_hit:.0%}  mrr={lang_mrr:.4f}  n={len(lang_results)}"
                )

    # Score distribution
    all_scores = [s for r in valid for s in r.scores]
    if all_scores:
        all_scores.sort()
        p25 = all_scores[len(all_scores) // 4]
        p50 = all_scores[len(all_scores) // 2]
        p75 = all_scores[3 * len(all_scores) // 4]
        print(
            f"\nScore distribution: min={all_scores[0]:.4f} p25={p25:.4f} p50={p50:.4f} p75={p75:.4f} max={all_scores[-1]:.4f}"
        )

    print(f"{'=' * 60}")


def save_results(results: list[QuestionResult], output_path: str) -> None:
    with open(output_path, "w") as f:
        for r in results:
            row = {
                "id": r.id,
                "question": r.question,
                "category": r.category,
                "language": r.language,
                "hit": r.hit,
                "reciprocal_rank": r.reciprocal_rank,
                "precision_at_k": r.precision_at_k,
                "num_retrieved": r.num_retrieved,
                "num_relevant": r.num_relevant,
                "has_ground_truth": r.has_ground_truth,
                "scores": r.scores,
            }
            if r.error:
                row["error"] = r.error
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Vektra retrieval evaluation harness")
    parser.add_argument(
        "--dataset",
        default=DEFAULT_DATASET,
        help=f"Path to JSONL dataset (default: {DEFAULT_DATASET})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Path for JSONL results output (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--top-k", type=int, default=5, help="Top-k for search (default: 5)"
    )
    parser.add_argument(
        "--search-mode",
        default="hybrid",
        choices=["dense", "sparse", "hybrid"],
        help="Search mode (default: hybrid)",
    )
    parser.add_argument(
        "--use-query",
        action="store_true",
        help="Use /api/v1/query (with reranker) instead of /api/v1/search",
    )
    args = parser.parse_args()

    api_url = os.environ.get("VEKTRA_API_URL")
    api_key = os.environ.get("VEKTRA_API_KEY")
    if not api_key:
        # Try .api-key file
        api_key_file = Path(".api-key")
        if api_key_file.exists():
            api_key = api_key_file.read_text().strip()

    if not api_url or not api_key:
        print(
            "Error: VEKTRA_API_URL and VEKTRA_API_KEY must be set.",
            file=sys.stderr,
        )
        sys.exit(1)

    dataset = load_dataset(args.dataset)
    if not dataset:
        print(f"Error: no entries in {args.dataset}", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(dataset)} questions from {args.dataset}")
    mode_info = (
        "query (with reranker)" if args.use_query else f"search mode={args.search_mode}"
    )
    print(f"API: {api_url}  top_k={args.top_k}  {mode_info}")

    timeout = 120.0 if args.use_query else 30.0
    client = httpx.Client(
        base_url=api_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )

    results: list[QuestionResult] = []
    t0 = time.monotonic()
    for i, entry in enumerate(dataset):
        result = evaluate_question(
            client, entry, args.top_k, args.search_mode, use_query=args.use_query
        )
        results.append(result)
        status = "HIT" if result.hit else ("ERR" if result.error else "MISS")
        print(f"  [{i + 1}/{len(dataset)}] {entry['id']} {status}", end="")
        if result.scores:
            print(f"  scores={[round(s, 3) for s in result.scores[:3]]}", end="")
        print()

    elapsed = time.monotonic() - t0
    client.close()

    print_summary(results, elapsed)
    save_results(results, args.output)
    print(f"\nResults saved to {args.output}")


if __name__ == "__main__":
    main()
