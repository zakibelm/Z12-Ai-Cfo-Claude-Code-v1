"""Modèles retirés par le fournisseur : bascule sur le modèle de repli."""
import pytest
from fastapi.testclient import TestClient

import kb_storage
import main
import model_fallback
import security_limits

RETIRED = "deepseek/deepseek-r1"
GONE = ('{"error":{"message":"deepseek/deepseek-r1 is no longer available on OpenRouter: '
        'no provider serves this model anymore.","code":404}}')
QUESTION = {"model": RETIRED, "use_rag": False, "messages": [
    {"role": "system", "content": "Tu es un CPA."}, {"role": "user", "content": "Bonjour"}]}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_storage, "get_conn", lambda: None)
    monkeypatch.setattr(kb_storage, "_DOCS_FILE", tmp_path / "docs.json")
    monkeypatch.setenv("OPENROUTER_API_KEY", "cle-plateforme")
    monkeypatch.setenv("ADMIN_USER_IDS", "9")
    monkeypatch.delenv("FALLBACK_MODEL", raising=False)
    monkeypatch.setattr(security_limits.llm_limiter, "max_calls", 10_000)
    model_fallback.reset()
    yield TestClient(main.app)
    model_fallback.reset()


@pytest.fixture
def provider(monkeypatch):
    """Faux fournisseur : 404 pour tout modèle de `dead`, 200 sinon."""
    state = {"dead": {RETIRED}, "models": []}

    class Response:
        def __init__(self, model):
            self.status_code = 404 if model in state["dead"] else 200
            self.text = GONE if self.status_code == 404 else ""
            self._model = model

        def json(self):
            return {"choices": [{"message": {"content": f"Réponse de {self._model}"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001}}

    class Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, **kwargs):
            state["models"].append(kwargs["json"]["model"])
            return Response(kwargs["json"]["model"])

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    return state


H = {"X-User-Id": "9"}


def test_a_retired_model_no_longer_blocks_the_studio(api, provider):
    """Régression : « requête refusée » parce que deepseek/deepseek-r1 n'est plus servi."""
    answer = api.post("/api/chat", headers=H, json=QUESTION)
    assert answer.status_code == 200
    body = answer.json()
    assert body["choices"][0]["message"]["content"] == "Réponse de deepseek/deepseek-v4-pro"
    assert body["model_substituted"] is True and body["model_requested"] == RETIRED
    assert provider["models"] == [RETIRED, "deepseek/deepseek-v4-pro"]


def test_the_retired_model_is_remembered_and_not_called_again(api, provider):
    api.post("/api/chat", headers=H, json=QUESTION)
    api.post("/api/chat", headers=H, json=QUESTION)
    assert provider["models"] == [RETIRED, "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-pro"]
    assert model_fallback.known_unavailable() == [RETIRED]


def test_a_working_model_is_left_alone(api, provider):
    answer = api.post("/api/chat", headers=H, json={**QUESTION, "model": "anthropic/claude-sonnet-4.5"}).json()
    assert "model_substituted" not in answer and provider["models"] == ["anthropic/claude-sonnet-4.5"]


def test_spending_is_charged_to_the_model_actually_used(api, provider):
    import budget

    api.post("/api/chat", headers=H, json=QUESTION)
    rows = kb_storage._load(budget._usage_file())
    assert [r["model"] for r in rows] == ["deepseek/deepseek-v4-pro"]


def test_fallback_model_is_configurable_and_never_loops(api, provider, monkeypatch):
    monkeypatch.setenv("FALLBACK_MODEL", "mon/repli")
    provider["dead"].add("mon/repli")
    refused = api.post("/api/chat", headers=H, json=QUESTION)
    assert refused.status_code == 502 and provider["models"] == [RETIRED, "mon/repli"]   # un seul nouvel essai
    assert model_fallback.known_unavailable() == [RETIRED]                                # le repli n'est jamais écarté


def test_other_provider_errors_are_not_mistaken_for_a_retired_model():
    assert model_fallback.is_unavailable_response(404, "") is True
    assert model_fallback.is_unavailable_response(400, "x is not a valid model ID") is True
    assert model_fallback.is_unavailable_response(400, "No endpoints found for x") is True
    for status, text in ((400, "unknown parameter: usage"), (401, "invalid key"), (402, "insufficient credits"),
                         (429, "rate limited"), (500, "boom"), (200, "")):
        assert model_fallback.is_unavailable_response(status, text) is False


@pytest.mark.asyncio
async def test_agent_kernel_falls_back_too(api, provider, monkeypatch):
    import z_kernel

    monkeypatch.setattr(z_kernel, "OPENROUTER_API_KEY", "k")
    answer = await z_kernel.call_llm(RETIRED, "système", "question", sensibilite="public")
    assert answer == "Réponse de deepseek/deepseek-v4-pro"


def test_no_retired_model_left_in_the_configuration():
    assert RETIRED not in model_fallback.configured_models()
    assert not any(m.startswith("deepseek/deepseek-r1:") for m in model_fallback.configured_models())
    assert model_fallback.fallback_model() in model_fallback.configured_models()


@pytest.mark.asyncio
async def test_startup_check_flags_models_missing_from_the_catalog(api, monkeypatch):
    import httpx

    class Response:
        status_code = 200

        def json(self):
            return {"data": [{"id": "deepseek/deepseek-v4-pro"}, {"id": "anthropic/claude-3.5-sonnet"}]}

    class Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    missing = await model_fallback.check_catalog()
    assert "deepseek/deepseek-r1-0528" in missing and "deepseek/deepseek-v4-pro" not in missing
    assert model_fallback.resolve("deepseek/deepseek-r1-0528") == "deepseek/deepseek-v4-pro"
    assert model_fallback.resolve("anthropic/claude-3.5-sonnet") == "anthropic/claude-3.5-sonnet"

    class Down(Client):
        async def get(self, *a, **k): raise httpx.ConnectError("réseau coupé")

    model_fallback.reset()
    monkeypatch.setattr(httpx, "AsyncClient", Down)
    assert await model_fallback.check_catalog() == []          # catalogue illisible : rien n'est écarté
    assert model_fallback.known_unavailable() == []
