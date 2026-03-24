#!/usr/bin/env python3
"""End-to-end RAG evaluation harness (TECH-002).

Reads a JSONL dataset, calls the Vektra query API for each question,
and computes answer quality metrics. Optionally uses RAGAS for
faithfulness and answer relevancy scoring.

Usage:
    python scripts/eval_e2e.py [OPTIONS]

Requires VEKTRA_API_URL and VEKTRA_API_KEY environment variables.
For RAGAS metrics, install: uv pip install ragas datasets
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
DEFAULT_OUTPUT = "tests/eval/results_e2e.jsonl"


@dataclass
class E2EResult:
    id: str
    question: str
    category: str
    language: str
    answer: str | None
    no_relevant_context: bool
    num_sources: int
    source_scores: list[float] = field(default_factory=list)
    duration_ms: float = 0.0
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


def evaluate_question(client: httpx.Client, entry: dict, top_k: int) -> E2EResult:
    """Run a single RAG query and collect the response."""
    question_id = entry["id"]
    question = entry["question"]
    namespace = entry.get("namespace", "default")
    category = entry.get("category", "unknown")
    language = entry.get("language", "unknown")

    t0 = time.monotonic()
    try:
        resp = client.post(
            "/api/v1/query",
            json={
                "question": question,
                "namespace": namespace,
                "top_k": top_k,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return E2EResult(
            id=question_id,
            question=question,
            category=category,
            language=language,
            answer=None,
            no_relevant_context=True,
            num_sources=0,
            error=str(e),
        )

    duration_ms = (time.monotonic() - t0) * 1000
    sources = data.get("sources", [])

    return E2EResult(
        id=question_id,
        question=question,
        category=category,
        language=language,
        answer=data.get("answer"),
        no_relevant_context=data.get("no_relevant_context", False),
        num_sources=len(sources),
        source_scores=[s["score"] for s in sources],
        duration_ms=duration_ms,
    )


def print_summary(results: list[E2EResult], elapsed_s: float) -> None:
    total = len(results)
    errors = sum(1 for r in results if r.error)
    evaluated = total - errors

    if evaluated == 0:
        print("No questions evaluated successfully.")
        return

    valid = [r for r in results if r.error is None]
    answered = sum(1 for r in valid if r.answer is not None)
    no_context = sum(1 for r in valid if r.no_relevant_context)
    avg_sources = sum(r.num_sources for r in valid) / evaluated

    durations = sorted(r.duration_ms for r in valid)
    p50 = durations[len(durations) // 2]
    p95 = durations[int(len(durations) * 0.95)]

    print(f"\n{'=' * 60}")
    print("End-to-end evaluation results")
    print(f"{'=' * 60}")
    print(f"Questions:         {total} ({errors} errors)")
    print(f"Duration:          {elapsed_s:.1f}s total")
    print("")
    print(f"Answered:          {answered}/{evaluated} ({answered / evaluated:.0%})")
    print(f"No context:        {no_context}/{evaluated}")
    print(f"Avg sources:       {avg_sources:.1f}")
    print(f"Latency p50:       {p50:.0f}ms")
    print(f"Latency p95:       {p95:.0f}ms")

    # Breakdown by category
    categories = sorted(set(r.category for r in valid))
    if len(categories) > 1:
        print("\nBy category:")
        for cat in categories:
            cat_results = [r for r in valid if r.category == cat]
            cat_answered = sum(1 for r in cat_results if r.answer is not None)
            print(
                f"  {cat:<15} answered={cat_answered}/{len(cat_results)}  n={len(cat_results)}"
            )

    print(f"{'=' * 60}")
    print("\nNote: RAGAS metrics (faithfulness, answer relevancy) require")
    print("ground_truth_answer in the dataset and RAGAS installed.")
    print("Install: uv pip install ragas datasets")


def save_results(results: list[E2EResult], output_path: str) -> None:
    with open(output_path, "w") as f:
        for r in results:
            row = {
                "id": r.id,
                "question": r.question,
                "category": r.category,
                "language": r.language,
                "answer": r.answer,
                "no_relevant_context": r.no_relevant_context,
                "num_sources": r.num_sources,
                "source_scores": r.source_scores,
                "duration_ms": round(r.duration_ms, 1),
            }
            if r.error:
                row["error"] = r.error
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Vektra end-to-end evaluation harness")
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
        "--top-k", type=int, default=5, help="Top-k for query (default: 5)"
    )
    args = parser.parse_args()

    api_url = os.environ.get("VEKTRA_API_URL")
    api_key = os.environ.get("VEKTRA_API_KEY")
    if not api_key:
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
    print(f"API: {api_url}  top_k={args.top_k}")

    client = httpx.Client(
        base_url=api_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=120.0,  # LLM calls can be slow
    )

    results: list[E2EResult] = []
    t0 = time.monotonic()
    for i, entry in enumerate(dataset):
        result = evaluate_question(client, entry, args.top_k)
        results.append(result)
        status = "OK" if result.answer else ("ERR" if result.error else "NO_CTX")
        answer_preview = (result.answer or "")[:60].replace("\n", " ")
        print(
            f"  [{i + 1}/{len(dataset)}] {entry['id']} {status} {result.duration_ms:.0f}ms  {answer_preview}"
        )

    elapsed = time.monotonic() - t0
    client.close()

    print_summary(results, elapsed)
    save_results(results, args.output)
    print(f"\nResults saved to {args.output}")


if __name__ == "__main__":
    main()
