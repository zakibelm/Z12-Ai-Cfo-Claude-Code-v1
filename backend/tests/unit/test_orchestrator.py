"""Orchestrateur serveur : plan toujours valide, repli signalé, équipe unifiée."""
import json

import httpx
import pytest

from orchestrator import assessor, engine, team


def _client(content=None, raises=False):
    class Response:
        def json(self):
            return {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    class Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            if raises:
                raise httpx.ConnectError("down")
            return Response()
    return Client


@pytest.mark.asyncio
async def test_assessor_falls_back_to_keywords_when_llm_is_down(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _client(raises=True))
    a = await assessor.assess_question("Comment réduire ma TPS et mon impôt ?")
    assert a.fallback and a.warning and a.agents == ["sophie"]


@pytest.mark.asyncio
async def test_assessor_survives_garbage_json_and_unknown_agents(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _client("pas du json du tout"))
    assert (await assessor.assess_question("zzz")).agents == [team.DEFAULT_AGENT]
    monkeypatch.setattr(httpx, "AsyncClient", _client('{"complexity":"complex","agents":["thomas","elena","hacker"]}'))
    a = await assessor.assess_question("Fiscalité")
    assert a.fallback and set(a.agents) <= set(team.AGENT_IDS)


@pytest.mark.asyncio
async def test_assessor_caps_agents(monkeypatch):
    raw = json.dumps({"complexity": "complex", "agents": team.AGENT_IDS, "reasoning": "tout"})
    monkeypatch.setattr(httpx, "AsyncClient", _client(raw))
    a = await assessor.assess_question("Acquisition")
    assert len(a.agents) == team.MAX_AGENTS and not a.fallback


@pytest.mark.asyncio
async def test_warning_event_is_streamed_on_fallback(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _client(raises=True))

    async def fake_agent(agent, question, context):
        return f"réponse {agent}"
    monkeypatch.setattr(engine, "call_agent", fake_agent)
    events = [e async for e in engine.execute_orchestration("TPS", {})]
    types = [json.loads(e[6:])["type"] for e in events if e.startswith("data: {")]
    assert types[0] == "warning" and "assessment" in types and "agent_result" in types


def test_engine_uses_the_unified_team():
    assert not hasattr(engine, "AGENT_PROMPTS")
    assert "Sophie" in team.system_prompt("sophie") and "Patrick" in team.system_prompt("patrick")
