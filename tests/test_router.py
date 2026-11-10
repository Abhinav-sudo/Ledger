import json
from collections import Counter
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from ledger.llm.router import AllModelsFailed, ModelRouter


class Answer(BaseModel):
    word: str


def _completion(content: str | None = None, tool_calls: list[dict[str, Any]] | None = None) -> dict:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


class Script:
    """Serves a queue of responses per model; anything unscripted is a 500."""

    def __init__(self, **queues: list[httpx.Response | dict]) -> None:
        self.queues = queues
        self.hits: Counter[str] = Counter()
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        model = body["model"]
        self.hits[model] += 1
        queue = self.queues.get(model) or [httpx.Response(500)]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)


def _router(script: Script, threshold: int = 2, cooldown_s: float = 3600) -> ModelRouter:
    config = {
        "breaker": {"threshold": threshold, "cooldown_s": cooldown_s},
        "providers": {
            "p1": {"base_url": "http://p1/v1", "api_key_env": "P1_KEY"},
            "p2": {"base_url": "http://p2/v1", "api_key_env": "P2_KEY"},
            "nokey": {"base_url": "http://p3/v1", "api_key_env": "UNSET_KEY"},
        },
        "roles": {"r": ["nokey/c", "p1/a", "p2/b"]},
    }
    return ModelRouter(config, http_client=httpx.Client(transport=httpx.MockTransport(script)))


@pytest.fixture(autouse=True)
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("P1_KEY", "k")
    monkeypatch.setenv("P2_KEY", "k")
    monkeypatch.delenv("UNSET_KEY", raising=False)


def _ask(router: ModelRouter, **kwargs: Any) -> Any:
    return router.complete("r", [{"role": "user", "content": "hi"}], Answer, **kwargs)


def test_429_falls_back_to_next_provider() -> None:
    script = Script(a=[httpx.Response(429)], b=[_completion('{"word": "ok"}')])
    reply = _ask(_router(script))
    assert (reply.value.word, reply.model, reply.index) == ("ok", "p2/b", 2)
    assert script.hits["c"] == 0


def test_breaker_trips_then_half_opens() -> None:
    script = Script(a=[httpx.Response(429)], b=[_completion('{"word": "ok"}')])
    router = _router(script, threshold=2)
    for _ in range(3):
        _ask(router)
    assert script.hits["a"] == 2

    router.breakers["p1"].cooldown_s = 0
    script.queues["a"] = [_completion('{"word": "back"}')]
    assert _ask(router).value.word == "back"
    assert router.breakers["p1"].opened_at is None


def test_schema_failure_repairs_once_then_escalates() -> None:
    script = Script(a=[_completion('{"wrong": 1}')], b=[_completion('{"word": "ok"}')])
    reply = _ask(_router(script))
    assert reply.model == "p2/b"
    assert script.hits["a"] == 2


def test_repair_turn_can_succeed() -> None:
    script = Script(a=[_completion("not json"), _completion('```json\n{"word": "fixed"}\n```')])
    reply = _ask(_router(script))
    assert (reply.value.word, reply.model) == ("fixed", "p1/a")


def test_all_failing_raises() -> None:
    with pytest.raises(AllModelsFailed):
        _ask(_router(Script()))


def test_tool_loop_runs_only_given_tools() -> None:
    calls: list[str] = []

    def lookup(po_number: str) -> dict[str, str]:
        """Look up a PO."""
        calls.append(po_number)
        return {"status": "open"}

    tool_call = {
        "id": "t1",
        "type": "function",
        "function": {"name": "lookup", "arguments": '{"po_number": "PO-1"}'},
    }
    script = Script(a=[_completion(None, [tool_call]), _completion('{"word": "done"}')])
    reply = _ask(_router(script), tools=[lookup])
    assert reply.value.word == "done"
    assert calls == ["PO-1"]
    assert script.bodies[0]["tools"][0]["function"]["name"] == "lookup"
    assert script.bodies[1]["messages"][-1]["content"] == '{"status":"open"}'


def test_tool_use_failure_falls_through_to_structured_answer() -> None:
    def lookup(po_number: str) -> dict[str, str]:
        """Look up a PO."""
        return {}

    refused = httpx.Response(
        400,
        json={"error": {"message": "tool 'json' not in request.tools", "code": "tool_use_failed"}},
    )
    script = Script(a=[refused, _completion('{"word": "answered"}')])
    reply = _ask(_router(script), tools=[lookup])
    assert (reply.value.word, reply.model) == ("answered", "p1/a")
    final = script.bodies[-1]
    assert "tools" not in final
    assert final["response_format"]["type"] == "json_schema"
    assert final["response_format"]["json_schema"]["name"] == "Answer"
