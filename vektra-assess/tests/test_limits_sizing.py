"""M5.1: the REQ-083 defaults, sized on a scripted reference 120-minute session.

The reference runs the real ``DiscussionTurnRunner`` for 60 turns (one message every 2 minutes),
400-character messages (the eval set's questions are far shorter: 400 stands for an argued
answer), replies of about 350 characters, the default model window, tokens estimated at 3.5
characters per token, and sums the runner's own ``TurnUsage``. The defaults must keep their
margins over it (docs/reference/configuration.md, "Sizing of the REQ-083 limits").
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from fakes import RecordingAuditSink, RecordingSafeguard, RecordingTraceWriter
from test_llm_runner import _visible_bundle

from vektra_assess.config import VektraAssessSettings
from vektra_assess.llm import DiscussionTurnRunner, StudentTurn, TurnResult
from vektra_assess.llm.types import PriorTurn
from vektra_testkit.recorders import RecordingLLMProvider

ROOT = Path(__file__).resolve().parents[2]
PROBES = ROOT / "tests" / "eval" / "assess" / "probes"
TURNS = 60  # 120 minutes, one message every 2 minutes
MESSAGE_CHARS = 400
REPLY = (
    "Capisco. Nella [Domanda 1] il commento del docente indica che manca il passaggio "
    "intermedio. "
) * 4


class CharTokens(RecordingLLMProvider):
    def count_tokens(self, text: str, model: str) -> int:
        return math.ceil(len(text) / 3.5)


def eval_questions() -> list[str]:
    out = []
    for path in sorted(PROBES.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            text = json.loads(line).get("text")
            if isinstance(text, str):
                out.append(text)
    return out


async def reference_tokens(settings: VektraAssessSettings) -> int:
    runner = DiscussionTurnRunner(
        llm=CharTokens([REPLY.strip()] * TURNS),
        safeguard=RecordingSafeguard(),
        trace_writer=RecordingTraceWriter(),
        settings=settings,
        audit=RecordingAuditSink(),
    )
    questions = eval_questions()
    bundle = _visible_bundle()
    history: list[PriorTurn] = []
    total = 0
    for i in range(TURNS):
        q = questions[i % len(questions)]
        text = ((q + " ") * (MESSAGE_CHARS // len(q) + 1))[:MESSAGE_CHARS]
        result = None
        async for item in runner.run_stream(
            bundle, StudentTurn(text, i + 1, tuple(history))
        ):
            if isinstance(item, TurnResult):
                result = item
        assert result is not None
        total += result.usage.prompt_tokens + result.usage.completion_tokens
        history += [PriorTurn("student", text), PriorTurn("assistant", result.text)]
    return total


def test_max_message_chars_is_far_above_the_eval_questions_and_fits_the_prompt() -> (
    None
):
    s = VektraAssessSettings()
    longest = max(len(q) for q in eval_questions())
    assert s.max_message_chars >= 20 * longest
    # one message is at most a small share of the prompt budget (3.5 chars per token)
    assert s.max_message_chars / 3.5 <= (s.llm_context_window - s.llm_max_tokens) / 8


def test_messages_per_minute_leave_room_over_the_reference_pace() -> None:
    assert VektraAssessSettings().messages_per_min >= 2 * (TURNS / 120)


async def test_the_token_budget_covers_the_reference_session_with_margin() -> None:
    s = VektraAssessSettings.model_validate({"VEKTRA_ASSESS_LLM_MODEL": "m"})
    used = await reference_tokens(s)
    assert 300_000 <= used <= 450_000, used  # the reference itself has not drifted
    assert s.session_token_budget >= 1.25 * used
    assert s.session_token_budget >= TURNS * s.llm_context_window  # 60 full windows
    # and it still stops a client hammering at the rate limit within the cap
    per_turn = s.llm_context_window
    assert s.session_token_budget < 120 * s.messages_per_min * per_turn
