"""Deterministic post-check of the model's output, one sentence at a time (REQ-096, REQ-097, REQ-098).

Each stage is a pure function over a sentence and the bundle. A sentence is released to the student
only after it passed every stage, and no stage can retract text already released, so every check
is decidable on one sentence.
"""
