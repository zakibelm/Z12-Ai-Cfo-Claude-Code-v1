"""Modèles « à réflexion » : la longueur autorisée couvre la réflexion et la réponse."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

import budget
import chat_context
import kb_storage
import main
import security_limits

USER = "21"
H = {"X-User-Id": USER}
ROUTING = 'Analyse cette demande et retourne le plan de workflow JSON :\n\n"analyse le cas"'


def _ok(content, finish="stop", cost=0.004):
    return 200, {"choices": [{"message": {"content": content}, "finish_reason": finish}],
                 "usage": {"prompt_tokens": 7000, "completion_tokens": 4000, "cost": cost}}


@pytest.fixture
def provider(monkeypatch, tmp_path):
    """Faux fournisseur : `script` donne les réponses dans l'ordre ; `bodies` les requêtes reçues."""
    state = {"script": [_ok("Réponse.")], "bodies": [], "raise": None}
    monkeypatch.setattr(kb_storage, "get_conn", lambda: None)
    monkeypatch.setattr(kb_storage, "_DOCS_FILE", tmp_path / "docs.json")
    monkeypatch.setenv("OPENROUTER_API_KEY", "cle-plateforme")
    monkeypatch.setenv("USD_TO_CAD", "1.40")
    monkeypatch.setattr(security_limits.llm_limiter, "max_calls", 10_000)

    class Response:
        def __init__(self, status, data):
            self.status_code, self._data = status, data
            self.text = data if isinstance(data, str) else ""

        def json(self):
            return self._data

    class Client:
        def __init__(self, *a, **k):
            state["timeout"] = k.get("timeout")
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, **kwargs):
            if state["raise"]:
                raise state["raise"]
            state["bodies"].append(kwargs["json"])
            status, data = state["script"][min(len(state["bodies"]) - 1, len(state["script"]) - 1)]
            return Response(status, data)

    async def nothing(*a, **k):
        return None

    async def no_context(*a, **k):
        return {"sources": [], "withheld": 0, "context": ""}

    async def no_memory(*a, **k):
        return ""

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    monkeypatch.setattr(main, "retrieve", no_context)
    monkeypatch.setattr(main, "inject_memory_context", no_memory)
    monkeypatch.setattr(main, "save_session_summary", nothing)
    monkeypatch.setattr(main, "get_cached_response", nothing)
    monkeypatch.setattr(main, "cache_response", nothing)
    with TestClient(main.app) as client:
        state["ask"] = lambda text="Analyse le cas fiscal.", **extra: client.post("/api/chat", headers=H, json={
            "model": "deepseek/deepseek-v4-pro", "messages": [{"role": "user", "content": text}], **extra})
        yield state


def test_a_specialist_may_use_twelve_thousand_tokens():
    assert security_limits.clamp_max_tokens(12000) == 12000
    assert security_limits.clamp_max_tokens(10**9) == security_limits.MAX_TOKENS_CAP == 16000


def test_a_specialist_keeps_its_reasoning_and_its_length(provider):
    assert provider["ask"](max_tokens=12000).status_code == 200
    body = provider["bodies"][0]
    assert "reasoning" not in body and body["max_tokens"] == 12000
    assert provider["timeout"] >= 200                                     # une longue réponse prend plus d'une minute


def test_the_routing_plan_is_made_without_reasoning(provider):
    """Régression : le plan n'arrivait pas, et un cas fiscal partait chez l'analyste financier."""
    provider["script"] = [_ok('{"type":"single","agents":["TaxAgent"]}')]
    assert provider["ask"](ROUTING, purpose="routing", max_tokens=1500).status_code == 200
    assert provider["bodies"][0]["reasoning"] == {"effort": "none"}


def test_a_provider_that_refuses_the_reasoning_setting_still_answers(provider):
    provider["script"] = [(400, "reasoning.effort none is not supported by this model"), _ok('{"type":"single","agents":["TaxAgent"]}')]
    answer = provider["ask"](ROUTING, purpose="routing")
    assert answer.status_code == 200
    assert "reasoning" in provider["bodies"][0] and "reasoning" not in provider["bodies"][1]


def test_an_answer_lost_to_reasoning_is_asked_again_without_it(provider):
    """Régression : « Le modèle a atteint sa limite de longueur avant d'écrire sa réponse. »"""
    provider["script"] = [_ok("", finish="length", cost=0.010), _ok("Le revenu imposable est de 84 300 $.", cost=0.003)]
    answer = provider["ask"](max_tokens=12000)
    assert answer.status_code == 200
    assert answer.json()["choices"][0]["message"]["content"] == "Le revenu imposable est de 84 300 $."
    assert len(provider["bodies"]) == 2 and provider["bodies"][1]["reasoning"] == {"effort": "none"}
    # Les deux appels sont payés : les deux sont comptés au budget du compte.
    usage = budget.usage_by_account()[USER]
    assert usage["calls"] == 2 and round(usage["cost"], 4) == 0.013


def test_no_endless_retry_when_the_second_answer_is_empty_too(provider):
    provider["script"] = [_ok("", finish="length")]
    answer = provider["ask"]()
    assert answer.status_code == 200 and len(provider["bodies"]) == 2
    assert chat_context.cut_before_answering(answer.json())


def test_a_normal_or_merely_long_answer_is_not_asked_twice(provider):
    provider["script"] = [_ok("Début d'une longue analyse", finish="length")]
    provider["ask"]()
    assert len(provider["bodies"]) == 1


def test_a_slow_provider_gives_a_clear_message(provider):
    provider["raise"] = httpx.ReadTimeout("trop long")
    answer = provider["ask"]()
    assert answer.status_code == 504 and "trop de temps" in answer.json()["detail"]


def test_helpers():
    assert chat_context.reasoning_for("routing") == {"effort": "none"}
    assert chat_context.reasoning_for("agent") is None and chat_context.reasoning_for("synthesis") is None
    assert chat_context.answer_text({"choices": [{"message": {"content": None}}]}) == ""
    for broken in (None, {}, {"choices": []}, {"choices": [{}]}):
        assert chat_context.cut_before_answering(broken) is False
