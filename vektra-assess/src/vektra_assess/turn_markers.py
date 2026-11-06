"""Fixed, system-written turn texts (never LLM output).

``INTERRUPTED_REPLY`` is the bot turn appended (kind ``interrupted``) when a reply could not be
delivered: the provider failed, an internal error, or the reply was cut (REQ-070). Appending it
through ``assess_append_turn`` clears ``pending_reply_since``, so the student can write again.
The API turn driver writes it today; the grace cut (plan M3.3) and the worker's
``reconcile_reply`` (plan M3.6) write the same text. It is deliberately NOT a shipped string of
``vektra_assess.llm``: those are part of the prompt version, and this text never reaches a model
as an instruction.
"""

from __future__ import annotations

INTERRUPTED_REPLY = "[risposta interrotta]"
