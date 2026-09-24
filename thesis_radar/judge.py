"""The Judge interface, the live Jev judge, and a scriptable fake for tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

# HTTP statuses that will fail the same way on every retry: the request itself is at fault.
PERMANENT_STATUSES = frozenset({400, 404, 413, 422})
# HTTP statuses that mean no request can succeed until the user fixes something (key, plan).
FATAL_STATUSES = frozenset({401, 402, 403})


@dataclass(frozen=True)
class JudgeRequest:
    state: dict[str, Any]
    questions: dict[str, dict[str, Any]]
    model: str

    def payload(self) -> dict[str, Any]:
        return {"model": self.model, "state": self.state, "questions": self.questions}


@dataclass(frozen=True)
class JudgeResult:
    answers: dict[str, dict[str, Any]]
    model: str
    input_tokens: int | None
    request_id: str | None = None


class JudgeError(Exception):
    """A request produced no usable answers. `retryable` is False when retrying cannot help."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class JudgeFatal(JudgeError):
    """No request can succeed (for example, a rejected API key); stop the whole run."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class Judge(Protocol):
    async def __aenter__(self) -> Judge: ...

    async def __aexit__(self, *exc_info: object) -> None: ...

    async def judge(self, request: JudgeRequest) -> JudgeResult: ...


def estimate_tokens_for(request: JudgeRequest) -> int:
    blob = json.dumps({"state": request.state, "questions": request.questions}, ensure_ascii=False)
    return max(1, len(blob) // 4)


def normalize_answer(answer: Any) -> dict[str, Any]:
    try:
        data = answer.model_dump() if hasattr(answer, "model_dump") else dict(answer)
        kind = data.get("type")
        if kind == "noul":
            return {"type": "noul", "noul": float(data["noul"])}
        if kind in ("choice", "score"):
            normalized: dict[str, Any] = {
                "type": kind,
                "probabilities": {str(key): float(value) for key, value in data["probabilities"].items()},
                "confidence": float(data["confidence"]),
            }
            if kind == "choice":
                normalized["choice"] = str(data["choice"])
            else:
                normalized["score"] = float(data["score"])
            return normalized
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise JudgeError(f"malformed answer: {exc!r}", retryable=False) from exc
    raise JudgeError(f"unknown answer type: {kind!r}", retryable=False)


def _request_id(response: Any) -> str | None:
    try:
        value = response.request_id
    except Exception:  # the header is optional audit data; never fail a judgment over it
        return None
    return value if isinstance(value, str) and value else None


def _check_complete(request: JudgeRequest, answers: Mapping[str, Any]) -> None:
    missing = sorted(set(request.questions) - set(answers))
    if missing:
        raise JudgeError(f"response is missing answers for: {', '.join(missing)}", retryable=False)


def classify_error(exc: BaseException) -> JudgeError:
    """Map any exception from the SDK or transport to a JudgeError with the right retry semantics."""
    if isinstance(exc, JudgeError):
        return exc
    status = getattr(exc, "status", None)
    message = f"{type(exc).__name__}: {exc}"
    if isinstance(status, int) and status in FATAL_STATUSES:
        return JudgeFatal(message)
    if isinstance(status, int) and status in PERMANENT_STATUSES:
        return JudgeError(message, retryable=False)
    return JudgeError(message, retryable=True)


class JevJudge:
    """Calls TypeSafe's System One API. Use as `async with JevJudge() as judge`."""

    def __init__(self, *, timeout: float = 120.0, client_factory: Callable[[], Any] | None = None) -> None:
        self._timeout = timeout
        self._factory = client_factory
        self._client: Any = None

    async def __aenter__(self) -> JevJudge:
        if self._factory is not None:
            self._client = self._factory()
        else:
            from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

            self._client = AsyncTypeSafeClient(
                timeout=self._timeout,
                retry=RetryPolicy(max_retries=3, http_statuses={408, 429, 500, 502, 503, 504, 529}, timeout=180.0),
            )
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc_info)
            self._client = None

    async def judge(self, request: JudgeRequest) -> JudgeResult:
        if self._client is None:
            raise RuntimeError("JevJudge must be used with 'async with'")
        try:
            response = await self._client.system_one(
                state=request.state, questions=request.questions, model=request.model
            )
        except Exception as exc:  # the SDK raises TypeSafeError subclasses; transports may raise others
            raise classify_error(exc) from exc
        answers = {name: normalize_answer(answer) for name, answer in response.answers.items()}
        _check_complete(request, answers)
        usage = getattr(response, "usage", None)
        return JudgeResult(
            answers=answers, model=response.model, input_tokens=getattr(usage, "input_tokens", None),
            request_id=_request_id(response),
        )


class FakeJudge:
    """Answers from a Python function; for tests and offline runs."""

    def __init__(self, respond: Callable[[JudgeRequest], Mapping[str, Any]], *, model: str = "jev-fake") -> None:
        self._respond = respond
        self.model = model
        self.requests: list[JudgeRequest] = []

    async def __aenter__(self) -> FakeJudge:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def judge(self, request: JudgeRequest) -> JudgeResult:
        self.requests.append(request)
        answers = {name: normalize_answer(answer) for name, answer in self._respond(request).items()}
        _check_complete(request, answers)
        return JudgeResult(answers=answers, model=self.model, input_tokens=estimate_tokens_for(request))
