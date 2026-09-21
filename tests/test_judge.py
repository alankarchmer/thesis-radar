import asyncio
from types import SimpleNamespace

import pytest
import typesafe_sdk

from helpers import noul
from thesis_radar.judge import (
    FakeJudge, JevJudge, JudgeError, JudgeRequest, estimate_tokens_for, normalize_answer,
)

REQUEST = JudgeRequest(
    state={"passage": "Inventory rose."},
    questions={"new_info": {"type": "noul", "instructions": "New?"}},
    model="jev-1.13.0",
)


def test_normalize_answer_accepts_dicts_and_models():
    assert normalize_answer(noul(0.25)) == {"type": "noul", "noul": 0.25}
    assert normalize_answer({"type": "score", "score": 1.5, "probabilities": {0: 0.5, 3: 0.5}, "confidence": 0.3}) == {
        "type": "score", "score": 1.5, "probabilities": {"0": 0.5, "3": 0.5}, "confidence": 0.3,
    }
    model = SimpleNamespace(
        model_dump=lambda: {"type": "choice", "choice": "a", "probabilities": {"a": 0.9, "b": 0.1}, "confidence": 0.8}
    )
    assert normalize_answer(model)["choice"] == "a"
    with pytest.raises(JudgeError):
        normalize_answer({"type": "essay"})


def test_estimate_tokens_is_positive():
    assert estimate_tokens_for(REQUEST) > 0


def test_fake_judge_records_requests_and_checks_answers():
    judge = FakeJudge(lambda request: {"new_info": noul(0.9)})

    async def go():
        async with judge:
            return await judge.judge(REQUEST)

    result = asyncio.run(go())
    assert result.answers == {"new_info": {"type": "noul", "noul": 0.9}}
    assert judge.requests == [REQUEST]
    with pytest.raises(JudgeError, match="missing answers"):
        asyncio.run(FakeJudge(lambda request: {}).judge(REQUEST))


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.calls, self.exited = [], False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        self.exited = True

    async def system_one(self, *, state, questions, model):
        self.calls.append((state, questions, model))
        if self.error is not None:
            raise self.error
        return self.response


def test_jev_judge_maps_the_response():
    client = FakeClient(
        response=SimpleNamespace(
            answers={"new_info": noul(0.7)}, model="jev-1.13.0", usage=SimpleNamespace(input_tokens=321),
            request_id="req_9",
        )
    )

    async def go():
        async with JevJudge(client_factory=lambda: client) as judge:
            return await judge.judge(REQUEST)

    result = asyncio.run(go())
    assert (result.answers, result.model, result.input_tokens) == (
        {"new_info": {"type": "noul", "noul": 0.7}}, "jev-1.13.0", 321,
    )
    assert result.request_id == "req_9"
    assert client.calls == [(REQUEST.state, REQUEST.questions, "jev-1.13.0")]
    assert client.exited


def test_jev_judge_wraps_sdk_errors():
    client = FakeClient(error=typesafe_sdk.TypeSafeError("service unavailable"))

    async def go():
        async with JevJudge(client_factory=lambda: client) as judge:
            await judge.judge(REQUEST)

    with pytest.raises(JudgeError, match="service unavailable"):
        asyncio.run(go())


def test_jev_judge_requires_async_with():
    with pytest.raises(RuntimeError, match="async with"):
        asyncio.run(JevJudge(client_factory=FakeClient).judge(REQUEST))
