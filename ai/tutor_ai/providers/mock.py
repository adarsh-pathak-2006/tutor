"""Deterministic provider for tests, CI and the hosted demo: no network, no key, instant (design §4).

Embeddings are *hashed bag-of-words* vectors: each word is hashed to a position (and a sign) in the vector, and the
result is normalised to unit length. Texts that share words therefore get similar vectors, so retrieval, off-topic
detection and citations behave realistically without a model. The same text always gives the same vector.

Chat is rule-based: it finds the passage sentence that shares the most words with the student's message and, by mode,
quotes it (explain), asks about it (guide) or only points to it (hint), citing the passage label. Replies are clearly
marked as demo replies.
"""

import hashlib
import math
import re
from collections.abc import AsyncIterator

from .base import EmbeddingKind, Message

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
STOPWORDS = frozenset(
    "a an and are as at be by for from has have how i in is it its of on or that the this to was what when where which "
    "who why will with you your do does did can".split()
)
DEMO_PREFIX = "(Demo tutor) "
# The prompt builder labels passages "[P1] heading\n text"; the mock reads only those blocks when present, so it
# never quotes the tutor's own rules back to the student.
_PASSAGE_BLOCK = re.compile(r"^\[(P\d+)\][^\n]*\n(.*?)(?=\n\n\[P\d+\]|\Z)", re.MULTILINE | re.DOTALL)


def tokens(text: str) -> list[str]:
    return [w for w in (m.group(0).lower() for m in _WORD.finditer(text)) if w not in STOPWORDS and len(w) > 1]


def hashed_embedding(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    for word in tokens(text):
        digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:  # no content words: a fixed unit vector keeps the maths well-defined
        vector[0] = 1.0
        return vector
    return [v / norm for v in vector]


def best_sentence(question: str, context: str) -> str | None:
    wanted = set(tokens(question))
    best, best_score = None, 0
    for sentence in (s.strip() for s in _SENTENCE.split(context) if s.strip()):
        score = len(wanted & set(tokens(sentence)))
        if score > best_score:
            best, best_score = sentence, score
    return best


def _best_passage_sentence(question: str, system: str) -> tuple[str | None, str | None]:
    blocks: list[tuple[str | None, str]] = list(_PASSAGE_BLOCK.findall(system)) or [(None, system)]
    wanted = set(tokens(question))
    best: tuple[str | None, str | None] = (None, None)
    best_score = 0
    for label, text in blocks:
        sentence = best_sentence(question, text)
        score = len(wanted & set(tokens(sentence))) if sentence else 0
        if score > best_score:
            best, best_score = (label, sentence), score
    return best


def _mode(system: str) -> str:
    if "Mode: hint." in system:
        return "hint"
    if "Mode: guide." in system:
        return "socratic"
    return "explain"


# Questions that only ask what the lesson is about share no words with its passages; they get its opening passage.
OVERVIEW_WORDS = frozenset(
    "lesson about topic chapter explain summary summarise summarize overview learn learning teach tell me main idea "
    "start begin please give brief quick whole".split()
)


def _opening_sentence(system: str) -> tuple[str | None, str | None]:
    blocks: list[tuple[str | None, str]] = list(_PASSAGE_BLOCK.findall(system))
    if not blocks:
        return None, None
    label, text = blocks[0]
    sentence = next((s.strip() for s in _SENTENCE.split(text) if s.strip()), None)
    return label, sentence


class MockProvider:
    name = "mock"
    chat_model = "mock-tutor-v1"
    embedding_model = "mock-hashed-bow-v1"

    def __init__(self, dimensions: int = 768) -> None:
        self.dimensions = dimensions

    async def chat(self, messages: list[Message], *, max_tokens: int, temperature: float) -> AsyncIterator[str]:
        question = next((m.content for m in reversed(messages) if m.role == "user"), "")
        system = "\n".join(m.content for m in messages if m.role == "system")
        label, found = _best_passage_sentence(question, system)
        asked = set(tokens(question))
        if not found and asked and asked <= OVERVIEW_WORDS:
            label, found = _opening_sentence(system)
        cite = f" [{label}]" if label else ""
        mode = _mode(system)
        if not found:
            reply = f"{DEMO_PREFIX}That isn't covered in this lesson. Shall we go back to what the lesson is about?"
        elif mode == "hint":
            reply = (
                f"{DEMO_PREFIX}Hint: look again at this part of the lesson{cite}. Which idea in it fits your question?"
            )
        elif mode == "socratic":
            reply = (
                f'{DEMO_PREFIX}Read this line from the lesson: "{found}"{cite} '
                "What does it tell you about your question?"
            )
        else:
            reply = f'{DEMO_PREFIX}The lesson says: "{found}"{cite} What do you think that means for your question?'
        words = reply.split(" ")
        for i, word in enumerate(words[: max(max_tokens, 1)]):
            yield word if i == 0 else " " + word

    async def embed(self, texts: list[str], *, kind: EmbeddingKind) -> list[list[float]]:
        return [hashed_embedding(t, self.dimensions) for t in texts]

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None
