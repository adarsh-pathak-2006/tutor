"""One tutor turn, as a stream of events (design §3 "Tutor turn", §6, §7).

Events, in order: `meta` → any number of `delta` → exactly one `final`; or an `error` instead of the rest.
Django relays deltas to the browser and stores only the `final` event (its `text` is authoritative: when the output
check replaces a reply, `final.text` differs from the deltas and `final.replaced` is true).

Steps:
1. Input safety (safety.py): harmful, risk-to-life and prompt-injection messages get a fixed reply without any model
   call; personal data is scrubbed before any model sees it.
2. Context: the pinned passages (reloaded by id) or, for a new chat or a re-indexed lesson, freshly pinned ones.
3. On later turns, a message far from the pinned passages triggers one course-wide search; if nothing relevant is
   found the student gets a gentle redirect without a model call.
4. The prompt (prompt_builder.py) is streamed from the provider through a StreamGuard; a quiz answer or harmful text
   stops the stream before it reaches the student.
5. The final event carries the full text, citations, safety results and timings.

The AI service never learns who the student is (a pseudonymous learner_ref only) and never logs message text.
"""

import asyncio
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from . import retrieval
from .prompt_builder import BuiltPrompt, Mode, PromptLimits, Turn, build_prompt
from .prompts import PromptRegistry
from .providers.base import ModelProvider, ProviderError
from .retrieval import Passage
from .safety import OK, InputDecision, SafetyPolicy, StreamGuard, Verdict, grounding_score
from .settings import Settings

logger = logging.getLogger("tutor_ai.tutor")

TEMPERATURE = 0.3
_CITATION = re.compile(r"\[(P\d+)\]")

_CONVERSATIONAL_PHRASES = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "no",
        "nope",
        "ok",
        "okay",
        "sure",
        "thanks",
        "thank you",
        "continue",
        "go on",
        "tell me more",
        "explain more",
        "give an example",
        "what next",
        "i understand",
        "i got it",
        "i see",
        "makes sense",
        "understood",
        "got it",
        "please continue",
        "next",
        "more",
        "cool",
        "alright",
        "all right",
        "great",
        "yes please",
        "no thanks",
    }
)


def is_conversational_continuation(text: str) -> bool:
    cleaned = re.sub(r"[^\w\s]", "", text.strip().lower())
    words = cleaned.split()
    if not words:
        return False
    if cleaned in _CONVERSATIONAL_PHRASES:
        return True
    if len(words) <= 3 and " ".join(words) in _CONVERSATIONAL_PHRASES:
        return True
    if len(words) <= 2 and all(w in _CONVERSATIONAL_PHRASES or w in {"please", "so", "and"} for w in words):
        return True
    return False


Event = tuple[str, dict[str, Any]]


@dataclass(frozen=True)
class LessonRef:
    id: uuid.UUID
    version_id: uuid.UUID
    course_id: uuid.UUID
    title: str


@dataclass(frozen=True)
class TurnRequest:
    learner_ref: str
    chat_id: uuid.UUID
    class_number: int | None
    lesson: LessonRef
    mode: Mode
    message: str
    history: list[Turn] = field(default_factory=list)
    pinned_chunk_ids: list[uuid.UUID] = field(default_factory=list)
    open_quiz: bool = False
    protected_answers: list[str] = field(default_factory=list)
    max_reply_tokens: int = 250


@dataclass
class _Context:
    passages: list[Passage]
    pinned_ids: list[uuid.UUID]
    repinned: bool = False
    extra_retrieval: bool = False
    off_topic: bool = False
    similarity: float | None = None


class TutorService:
    def __init__(
        self,
        engine: AsyncEngine,
        provider: ModelProvider,
        prompts: PromptRegistry,
        policy: SafetyPolicy,
        settings: Settings,
    ) -> None:
        self.engine = engine
        self.provider = provider
        self.prompts = prompts
        self.policy = policy
        self.settings = settings
        self.minimum = retrieval.min_similarity(settings.min_similarity, provider.name)

    # -- public ------------------------------------------------------------------------------------------------

    async def turn(self, request: TurnRequest) -> AsyncIterator[Event]:
        started = time.monotonic()
        decision = self.policy.check_input(request.message)
        mode: Mode = "hint" if request.open_quiz else request.mode
        if decision.action == "block":
            yield "meta", self._meta(request, pinned=request.pinned_chunk_ids, mode=mode, model=None)
            yield (
                "final",
                self._final(request, started, text=decision.reply or "", decision=decision, output=OK, blocked=True),
            )
            self._log(request, started, "blocked", decision.verdict)
            return

        context = await self._context(request, decision.text)
        if context is None:
            yield "error", {"code": "lesson_not_indexed", "message": "This lesson isn't ready for the tutor yet."}
            return
        if context.off_topic:
            yield "meta", self._meta(request, pinned=context.pinned_ids, mode=mode, model=None)
            text = self.policy.reply("off_topic", lesson_title=request.lesson.title)
            yield (
                "final",
                self._final(request, started, text=text, decision=decision, output=OK, context=context, off_topic=True),
            )
            self._log(request, started, "off_topic", decision.verdict)
            return

        prompt = self._build(request, decision.text, context.passages)
        yield "meta", self._meta(request, pinned=context.pinned_ids, mode=prompt.mode, model=self.provider.chat_model)
        guard = StreamGuard(self.policy, request.protected_answers)
        if decision.notice:
            notice = decision.notice + "\n\n"
            yield "delta", {"text": notice}
        else:
            notice = ""
        try:
            async for chunk in self._stream(prompt, request, guard):
                yield "delta", {"text": chunk}
        except ProviderError as exc:
            logger.warning("tutor turn failed", extra={"chat_id": str(request.chat_id), "code": exc.code})
            yield "error", {"code": exc.code, "message": "The tutor is unavailable right now."}
            return
        except TimeoutError:
            logger.warning("tutor turn timed out", extra={"chat_id": str(request.chat_id)})
            yield "error", {"code": "timeout", "message": "The tutor took too long to answer."}
            return

        replaced = guard.problem is not None
        if replaced:
            fallback = "quiz_fallback" if guard.problem == "quiz_answer" else "output_fallback"
            body = self.policy.reply(fallback)
        else:
            tail = guard.finish()
            if tail:
                yield "delta", {"text": tail}
            body = guard.text.strip()
        grounding = (
            grounding_score(body, [p.text for p in context.passages] + [decision.text]) if not replaced else None
        )
        yield (
            "final",
            self._final(
                request,
                started,
                text=notice + body,
                decision=decision,
                output=guard.verdict,
                context=context,
                prompt=prompt,
                replaced=replaced,
                grounding=grounding,
                reply_text=body,
            ),
        )
        self._log(request, started, "replaced" if replaced else "answered", decision.verdict)

    # -- steps -------------------------------------------------------------------------------------------------

    async def _context(self, request: TurnRequest, message: str) -> _Context | None:
        model = self.provider.embedding_model
        budget = self.settings.context_tokens
        pinned: list[Passage] = []
        if request.pinned_chunk_ids:
            async with self.engine.connect() as conn:
                pinned = await retrieval.passages_by_id(conn, request.pinned_chunk_ids, model)
        if not pinned or len(pinned) < len(request.pinned_chunk_ids):  # new chat, or the lesson was re-indexed
            fresh = await retrieval.pin_context(
                self.engine,
                self.provider,
                lesson_id=request.lesson.id,
                course_id=request.lesson.course_id,
                query=message,
                budget_tokens=budget,
                related_k=self.settings.retrieval_top_k,
                minimum=self.minimum,
                max_cache_entries=self.settings.query_cache_max_entries,
            )
            if not fresh:
                return None
            return _Context(fresh, [p.chunk_id for p in fresh], repinned=bool(request.pinned_chunk_ids))

        context = _Context(pinned, [p.chunk_id for p in pinned])
        if is_conversational_continuation(message):
            context.similarity = 1.0
            return context

        vector = await retrieval.get_query_embedding(
            self.provider, message, max_entries=self.settings.query_cache_max_entries
        )
        async with self.engine.connect() as conn:
            context.similarity = await retrieval.best_similarity(conn, context.pinned_ids, vector, model)
            if context.similarity is not None and context.similarity >= self.minimum:
                return context
            extra = await retrieval.search(
                conn,
                vector,
                course_id=request.lesson.course_id,
                model=model,
                k=self.settings.retrieval_top_k,
                minimum=self.minimum,
            )
        already = set(context.pinned_ids)
        new = [p for p in extra if p.chunk_id not in already]
        if not extra:
            context.off_topic = True
        else:  # this turn only: the pinned prefix stays the same for the next turns
            context.passages = pinned + new
            context.extra_retrieval = True
        return context

    def _build(self, request: TurnRequest, message: str, passages: list[Passage]) -> BuiltPrompt:
        limits = PromptLimits(
            context_tokens=self.settings.context_tokens,
            history_turns=self.settings.history_turns,
            history_tokens=self.settings.history_tokens,
            reply_tokens=min(request.max_reply_tokens, self.settings.reply_tokens),
        )
        return build_prompt(
            self.prompts.active("tutor"),
            mode=request.mode,
            class_number=request.class_number,
            lesson_title=request.lesson.title,
            passages=passages,
            history=request.history,
            message=message,
            limits=limits,
            open_quiz=request.open_quiz,
        )

    async def _stream(self, prompt: BuiltPrompt, request: TurnRequest, guard: StreamGuard) -> AsyncIterator[str]:
        """Model output that passed the guard. Raises TimeoutError when the whole reply takes longer than the turn
        timeout (a per-chunk wait, so the deadline never cancels anything outside this generator)."""
        reply_tokens = min(request.max_reply_tokens, self.settings.reply_tokens)
        stream = self.provider.chat(prompt.messages, max_tokens=reply_tokens, temperature=TEMPERATURE)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.turn_timeout_seconds
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    delta = await asyncio.wait_for(anext(stream), remaining)
                except StopAsyncIteration:
                    break
                released = guard.feed(delta)
                if released:
                    yield released
                if guard.problem is not None:
                    break  # stop generating: nothing more of this reply will be shown
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()

    # -- events ------------------------------------------------------------------------------------------------

    def _meta(self, request: TurnRequest, *, pinned: list[uuid.UUID], mode: Mode, model: str | None) -> dict[str, Any]:
        return {
            "chat_id": str(request.chat_id),
            "model": model,
            "provider": self.provider.name,
            "prompt_version": self.prompts.active("tutor").ref,
            "safety_version": self.policy.ref,
            "mode": mode,
            "pinned_chunk_ids": [str(i) for i in pinned],
        }

    def _final(
        self,
        request: TurnRequest,
        started: float,
        *,
        text: str,
        decision: InputDecision,
        output: Verdict,
        context: _Context | None = None,
        prompt: BuiltPrompt | None = None,
        blocked: bool = False,
        off_topic: bool = False,
        replaced: bool = False,
        grounding: float | None = None,
        reply_text: str = "",
    ) -> dict[str, Any]:
        citations = []
        if prompt is not None and not replaced:
            headings = {p.chunk_id: p.heading for p in context.passages} if context else {}
            for label in dict.fromkeys(_CITATION.findall(reply_text)):
                chunk_id = prompt.passage_ids.get(label)
                if chunk_id is not None:
                    citations.append({"label": label, "chunk_id": str(chunk_id), "heading": headings.get(chunk_id, "")})
        flags = []
        if decision.verdict.flagged:
            flags.append({"stage": "input", **decision.verdict.as_dict()})
        if output.flagged:
            flags.append({"stage": "output", **output.as_dict()})
        ungrounded = grounding is not None and grounding < self.settings.grounding_min
        if ungrounded:
            flags.append({"stage": "output", "category": "ungrounded", "severity": "low", "flagged": False})
        return {
            "text": text.strip(),
            "message": decision.text,  # the student's message as stored: personal data already hidden
            "blocked": blocked,
            "replaced": replaced,
            "off_topic": off_topic,
            "citations": citations,
            "safety": {
                "input": decision.verdict.as_dict(),
                "output": output.as_dict(),
                "personal_data": list(decision.personal_data),
                "grounding": None if grounding is None else round(grounding, 3),
            },
            "flags": flags,
            "context": {
                "pinned_chunk_ids": [str(i) for i in context.pinned_ids] if context else [],
                "repinned": bool(context and context.repinned),
                "extra_retrieval": bool(context and context.extra_retrieval),
                "similarity": None if not context or context.similarity is None else round(context.similarity, 3),
            },
            "usage": {
                "prompt_tokens_estimate": prompt.estimated_tokens if prompt else 0,
                "reply_tokens_estimate": len(reply_text.split()) if reply_text else 0,
                "history_used": prompt.history_used if prompt else 0,
                "history_dropped": prompt.history_dropped if prompt else 0,
                "cost": 0.0,  # local and mock models; the hosted provider reports real cost
            },
            "latency_ms": int((time.monotonic() - started) * 1000),
            "model": self.provider.chat_model if prompt else None,
            "prompt_version": self.prompts.active("tutor").ref,
            "safety_version": self.policy.ref,
            "mode": prompt.mode if prompt else request.mode,
        }

    def _log(self, request: TurnRequest, started: float, outcome: str, verdict: Verdict) -> None:
        logger.info(
            "tutor turn",
            extra={
                "chat_id": str(request.chat_id),
                "learner_ref": request.learner_ref,
                "outcome": outcome,
                "input_category": verdict.category,
                "latency_ms": int((time.monotonic() - started) * 1000),
            },
        )
