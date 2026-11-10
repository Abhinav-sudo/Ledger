import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import openai
import yaml
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ValidationError
from pydantic_core import to_json

log = logging.getLogger("ledger.llm")

CONFIG = Path(__file__).with_name("models.yaml")
MAX_TOOL_ROUNDS = 5
FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class AllModelsFailed(Exception):
    pass


class Reply[T: BaseModel](NamedTuple):
    value: T
    model: str
    index: int


@dataclass
class Breaker:
    threshold: int
    cooldown_s: float
    failures: int = 0
    opened_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self) -> bool:
        with self.lock:
            if self.opened_at is None:
                return True
            if time.monotonic() - self.opened_at < self.cooldown_s:
                return False
            # Half-open: let this one trial through and keep everyone else out until it reports.
            self.opened_at = time.monotonic()
            return True

    def success(self) -> None:
        with self.lock:
            self.failures = 0
            self.opened_at = None

    def failure(self) -> None:
        with self.lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = time.monotonic()


def _parse[T: BaseModel](content: str | None, schema: type[T]) -> T:
    raw = json.loads(FENCE.sub("", (content or "").strip()), parse_float=Decimal)
    return schema.model_validate(raw)


def _instructions(schema: type[BaseModel]) -> dict[str, str]:
    return {
        "role": "system",
        "content": "Reply with a single JSON object, no prose, matching this JSON schema. "
        'Write every money amount and quantity as a string, for example "1234.50".\n'
        + json.dumps(schema.model_json_schema()),
    }


class ModelRouter:
    def __init__(self, config: dict[str, Any], http_client: httpx.Client | None = None) -> None:
        self.roles: dict[str, list[str]] = config["roles"]
        self.clients: dict[str, openai.OpenAI | None] = {}
        for name, p in config["providers"].items():
            key = os.environ.get(p["api_key_env"]) if "api_key_env" in p else "local"
            base_url = os.environ.get(p.get("base_url_env", ""), p["base_url"])
            self.clients[name] = (
                openai.OpenAI(
                    base_url=base_url,
                    api_key=key,
                    max_retries=0,
                    timeout=90,
                    http_client=http_client,
                )
                if key
                else None
            )
        b = config["breaker"]
        self.breakers = {name: Breaker(b["threshold"], b["cooldown_s"]) for name in self.clients}

    @classmethod
    def from_config(cls, path: Path = CONFIG) -> "ModelRouter":
        return cls(yaml.safe_load(path.read_text()))

    def complete[T: BaseModel](
        self,
        role: str,
        messages: list[dict[str, Any]],
        schema: type[T],
        tools: list[Callable[..., Any]] | None = None,
        start_at: int = 0,
    ) -> Reply[T]:
        chain = self.roles[role]
        messages = [_instructions(schema), *messages]
        for index in range(start_at, len(chain)):
            provider, model = chain[index].split("/", 1)
            client = self.clients[provider]
            if client is None:
                self._log(role, provider, model, None, None, "no api key")
                continue
            if not self.breakers[provider].allow():
                self._log(role, provider, model, None, None, "breaker open")
                continue
            try:
                value = self._attempt(role, provider, model, client, messages, schema, tools)
            except (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError):
                self.breakers[provider].failure()
                continue
            except openai.APIStatusError:
                continue
            self.breakers[provider].success()
            if value is not None:
                return Reply(value, chain[index], index)
        raise AllModelsFailed(f"every model for role {role!r} from index {start_at} failed")

    def _attempt[T: BaseModel](
        self,
        role: str,
        provider: str,
        model: str,
        client: openai.OpenAI,
        messages: list[dict[str, Any]],
        schema: type[T],
        tools: list[Callable[..., Any]] | None,
    ) -> T | None:
        """One model: the tool loop, then one repair turn. None means escalate on schema."""
        messages = list(messages)
        content = self._tool_loop(role, provider, model, client, messages, tools)
        if content is None:
            content = self._call(role, provider, model, client, messages, schema=schema).content
        for repair in (False, True):
            try:
                return _parse(content, schema)
            except (ValueError, ValidationError) as e:
                if repair:
                    self._log(
                        role, provider, model, None, None, f"schema invalid after repair: {e}"
                    )
                    return None
                messages += [
                    {"role": "assistant", "content": content or ""},
                    {
                        "role": "user",
                        "content": f"That reply was invalid: {e}\nSend corrected JSON only.",
                    },
                ]
                content = self._call(role, provider, model, client, messages, schema=schema).content
        return None

    def _tool_loop(
        self,
        role: str,
        provider: str,
        model: str,
        client: openai.OpenAI,
        messages: list[dict[str, Any]],
        tools: list[Callable[..., Any]] | None,
    ) -> str | None:
        if not tools:
            return None
        by_name = {f.__name__: f for f in tools}
        specs = [convert_to_openai_tool(f) for f in tools]
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                msg = self._call(role, provider, model, client, messages, tools=specs)
            except openai.BadRequestError as e:
                # Some models try to answer by calling a tool that does not exist; the
                # structured call that follows gets the answer instead.
                if e.code == "tool_use_failed":
                    return None
                raise
            if not msg.tool_calls:
                return msg.content
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [c.model_dump() for c in msg.tool_calls],
                }
            )
            for call in msg.tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": _run_tool(by_name, call.function.name, call.function.arguments),
                    }
                )
        return None

    def _call(
        self,
        role: str,
        provider: str,
        model: str,
        client: openai.OpenAI,
        messages: list[dict[str, Any]],
        schema: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0}
        if schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
            }
        if tools:
            kwargs["tools"] = tools
        start = time.monotonic()
        try:
            resp = client.chat.completions.create(**kwargs)
        except openai.APIStatusError as e:
            self._log(
                role, provider, model, start, None, f"http {e.status_code} {e.code or ''}".strip()
            )
            raise
        except openai.APIConnectionError as e:
            self._log(role, provider, model, start, None, f"connection: {type(e).__name__}")
            raise
        self._log(role, provider, model, start, resp.usage, None)
        return resp.choices[0].message

    def _log(
        self,
        role: str,
        provider: str,
        model: str,
        start: float | None,
        usage: Any,
        fallback_reason: str | None,
    ) -> None:
        record = {
            "role": role,
            "provider": provider,
            "model": model,
            "latency_ms": round((time.monotonic() - start) * 1000) if start else None,
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None,
            "fallback_reason": fallback_reason,
        }
        log.info(" ".join(f"{k}={v}" for k, v in record.items()), extra={"llm": record})


def _run_tool(by_name: dict[str, Callable[..., Any]], name: str, arguments: str) -> str:
    if name not in by_name:
        return f"error: unknown tool {name!r}"
    try:
        return to_json(by_name[name](**json.loads(arguments or "{}"))).decode()
    except (TypeError, ValueError) as e:
        return f"error: {e}"
