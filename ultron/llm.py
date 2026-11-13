"""Model-agnostic LLM layer. Agents call llm.json(...) and never know which provider is behind it.

Switching from the API prototype to the RTX 3060 PC is a config change:
    provider: openai_compatible
    base_url: http://<pc-ip>:11434/v1
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from .db import DB
from .guard import Guard

JSON_RULE = "\n\nRespond with one valid JSON value only. No prose before or after it, no code fences."


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class LLM:
    def __init__(self, cfg: dict, db: DB, guard: Guard):
        self.cfg = cfg
        self.provider = os.getenv("ULTRON_LLM_PROVIDER") or cfg["provider"]
        self.models = cfg["models"]
        self.pricing = cfg.get("pricing") or {}
        self.db = db
        self.guard = guard
        self._client = None

    @property
    def simulated(self) -> bool:
        return self.provider == "mock"

    def _client_obj(self):
        if self._client is None:
            if self.provider == "anthropic":
                import anthropic
                self._client = anthropic.Anthropic()
            elif self.provider == "openai_compatible":
                from openai import OpenAI
                self._client = OpenAI(base_url=self.cfg.get("base_url") or None,
                                      api_key=os.getenv("OPENAI_API_KEY") or "not-needed")
            else:
                raise ValueError(f"Unknown llm.provider '{self.provider}'")
        return self._client

    def _cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        rate = self.pricing.get(model)
        if not rate:
            return 0.0
        return (input_tokens * rate[0] + output_tokens * rate[1]) / 1_000_000

    def complete(self, system: str, prompt: str, *, team: str, agent: str, tier: str = "fast",
                 max_tokens: int = 2000, mock: Callable[[], Any] | Any = None) -> LLMResult:
        self.guard.check(team)
        model = self.models[tier]

        if self.provider == "mock":
            value = mock() if callable(mock) else mock
            text = value if isinstance(value, str) else json.dumps(value)
            in_tok, out_tok = (len(system) + len(prompt)) // 4, len(text) // 4
        elif self.provider == "anthropic":
            resp = self._client_obj().messages.create(
                model=model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            in_tok, out_tok = resp.usage.input_tokens, resp.usage.output_tokens
        else:
            resp = self._client_obj().chat.completions.create(
                model=model, max_tokens=max_tokens,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            )
            text = resp.choices[0].message.content or ""
            usage = resp.usage
            in_tok = usage.prompt_tokens if usage else 0
            out_tok = usage.completion_tokens if usage else 0

        cost = self._cost(model, in_tok, out_tok)
        self.db.insert("llm_spend", team=team, agent=agent, model=model, input_tokens=in_tok,
                       output_tokens=out_tok, cost_usd=cost, simulated=int(self.simulated))
        return LLMResult(text, model, in_tok, out_tok, cost)

    def json(self, system: str, prompt: str, **kw) -> Any:
        """Ask for JSON, parse it, retry once with a nudge if the model wraps it in prose."""
        system = system + JSON_RULE
        last_error = None
        for _ in range(2):
            result = self.complete(system, prompt, **kw)
            try:
                return parse_json(result.text)
            except ValueError as err:
                last_error = err
                prompt = prompt + "\n\nYour last reply was not valid JSON. Reply with the JSON value only."
        raise ValueError(f"{kw.get('agent')}: model did not return valid JSON ({last_error})")


def parse_json(text: str) -> Any:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError("no JSON found")
    start = min(starts)
    closer = "}" if text[start] == "{" else "]"
    end = text.rfind(closer)
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as err:
        raise ValueError(str(err)) from err
