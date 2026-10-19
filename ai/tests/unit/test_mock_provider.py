import asyncio
import math

import pytest

from tutor_ai.providers import Message, ModelProvider
from tutor_ai.providers.mock import DEMO_PREFIX, MockProvider, best_sentence, hashed_embedding, tokens

LESSON = (
    "Every time you write down a measurement you create data. A table of such records is called a dataset. "
    "Features are things we can observe, like colour and weight. The label is the answer we want to predict."
)


def run(coro):
    return asyncio.run(coro)


async def collect(stream):
    return "".join([part async for part in stream])


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_mock_satisfies_the_provider_interface():
    assert isinstance(MockProvider(), ModelProvider)


def test_tokens_drop_stopwords_and_punctuation():
    assert tokens("What is the LABEL, in a dataset?") == ["label", "dataset"]


def test_embeddings_are_unit_length_and_deterministic():
    a = hashed_embedding("features and labels", 768)
    assert len(a) == 768
    assert math.isclose(math.sqrt(sum(v * v for v in a)), 1.0, rel_tol=1e-9)
    assert a == hashed_embedding("features and labels", 768)


def test_shared_words_mean_similar_vectors():
    query = hashed_embedding("what is a label in a dataset", 768)
    related = hashed_embedding("The label is the answer we want to predict from the dataset", 768)
    unrelated = hashed_embedding("Photosynthesis happens in the chloroplasts of green leaves", 768)
    assert cosine(query, related) > cosine(query, unrelated) + 0.2


def test_text_without_content_words_still_gives_a_unit_vector():
    vector = hashed_embedding("the of and", 64)
    assert vector[0] == 1.0 and sum(abs(v) for v in vector) == 1.0


def test_embed_keeps_order_and_dimensions():
    provider = MockProvider(dimensions=128)
    vectors = run(provider.embed(["one text", "another text"], kind="document"))
    assert [len(v) for v in vectors] == [128, 128]
    assert vectors[0] == hashed_embedding("one text", 128)


def test_best_sentence_picks_the_most_overlapping_sentence():
    assert best_sentence("what is the label", LESSON) == "The label is the answer we want to predict."
    assert best_sentence("cricket scores", LESSON) is None


def test_chat_quotes_the_lesson_and_asks_a_question():
    messages = [Message("system", LESSON), Message("user", "What is a dataset?")]
    reply = run(collect(MockProvider().chat(messages, max_tokens=200, temperature=0.2)))
    assert reply.startswith(DEMO_PREFIX)
    assert "A table of such records is called a dataset." in reply
    assert reply.endswith("?")


def test_chat_redirects_off_topic_questions():
    messages = [Message("system", LESSON), Message("user", "Who won the cricket world cup?")]
    reply = run(collect(MockProvider().chat(messages, max_tokens=200, temperature=0.2)))
    assert "isn't covered in this lesson" in reply


def test_chat_respects_max_tokens():
    messages = [Message("system", LESSON), Message("user", "What is a dataset?")]
    reply = run(collect(MockProvider().chat(messages, max_tokens=3, temperature=0.2)))
    assert len(reply.split(" ")) == 3


def test_health_and_close():
    provider = MockProvider()
    assert run(provider.health()) is True
    assert run(provider.aclose()) is None


@pytest.mark.parametrize("dimensions", [64, 768, 1536])
def test_any_configured_dimension(dimensions):
    assert len(hashed_embedding("x y z", dimensions)) == dimensions


def test_overview_questions_get_the_opening_passage():
    system = "Rules.\n\n[P1] Units\nSI units are the standard units of measurement. The metre measures length.\n\n[P2] Mass\nMass is in kilograms."
    for question in ("What is this lesson about?", "Explain the main idea", "summarise please"):
        reply = run(
            collect(
                MockProvider().chat(
                    [Message("system", system), Message("user", question)], max_tokens=200, temperature=0.2
                )
            )
        )
        assert '"SI units are the standard units of measurement." [P1]' in reply, question
    off_topic = run(
        collect(
            MockProvider().chat(
                [Message("system", system), Message("user", "Who won the cricket match?")],
                max_tokens=200,
                temperature=0.2,
            )
        )
    )
    assert "isn't covered in this lesson" in off_topic
    no_passages = run(
        collect(
            MockProvider().chat(
                [Message("system", "Rules only."), Message("user", "What is this lesson about?")],
                max_tokens=200,
                temperature=0.2,
            )
        )
    )
    assert "isn't covered in this lesson" in no_passages
