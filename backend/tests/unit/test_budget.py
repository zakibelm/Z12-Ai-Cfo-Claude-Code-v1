"""Budget de consommation : comptage au coût réel, plafonds par compte et pour la plateforme."""
import pytest
from fastapi.testclient import TestClient

import budget
import kb_storage
import main
import security_limits

ADMIN, USER, OTHER = "9", "21", "22"
QUESTION = {"model": "anthropic/claude-sonnet-4.5", "use_rag": False, "messages": [
    {"role": "system", "content": "Tu es un CPA."}, {"role": "user", "content": "Bonjour"}]}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_storage, "get_conn", lambda: None)
    monkeypatch.setattr(kb_storage, "_DOCS_FILE", tmp_path / "docs.json")
    monkeypatch.setenv("ADMIN_USER_IDS", ADMIN)
    monkeypatch.setenv("OPENROUTER_API_KEY", "cle-plateforme")
    monkeypatch.setenv("USD_TO_CAD", "1.25")
    for name in ("TRIAL_BUDGET_CAD", "PLATFORM_MONTHLY_BUDGET_CAD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(security_limits.llm_limiter, "max_calls", 10_000)
    return TestClient(main.app)


@pytest.fixture
def provider(monkeypatch):
    """Faux fournisseur : chaque appel coûte `state['cost']` USD."""
    state = {"cost": 0.40, "calls": 0, "bodies": []}

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "Réponse."}}],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "cost": state["cost"]}}

    class Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, **kwargs):
            state["calls"] += 1
            state["bodies"].append(kwargs.get("json"))
            return Response()

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    return state


def _h(user):
    return {"X-User-Id": user}


def _ask(api, user, **extra):
    return api.post("/api/chat", headers=_h(user), json={**QUESTION, **extra})


class TestCost:
    def test_real_cost_is_used_when_the_provider_returns_it(self):
        assert budget.cost_of({"usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0123}}) == (0.0123, 10, 5, False)

    def test_missing_cost_is_estimated_never_free(self, monkeypatch):
        monkeypatch.delenv("BUDGET_FALLBACK_USD_PER_MTOK_IN", raising=False)
        monkeypatch.delenv("BUDGET_FALLBACK_USD_PER_MTOK_OUT", raising=False)
        cost, tokens_in, tokens_out, estimated = budget.cost_of({"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}})
        assert (cost, tokens_in, tokens_out, estimated) == (18.0, 1_000_000, 1_000_000, True)

    def test_malformed_answers_do_not_crash(self):
        for data in (None, {}, {"usage": None}, {"usage": "x"}, {"usage": {"cost": "gratuit", "prompt_tokens": "abc"}},
                     {"usage": {"cost": -5}}, {"usage": {"cost": True}}):
            cost, *_ = budget.cost_of(data)
            assert cost >= 0

    def test_request_asks_the_provider_for_the_cost(self):
        assert budget.with_usage({"model": "m"}) == {"model": "m", "usage": {"include": True}}


class TestAccountBudget:
    def test_default_trial_budget_is_three_dollars(self, api):
        state = api.get("/api/budget", headers=_h(USER)).json()
        assert state == {"currency": "CAD", "unlimited": False, "budget_cad": 3.0, "spent_cad": 0.0,
                         "remaining_cad": 3.0, "percent": 0, "warning": False, "blocked": False, "reason": None}

    def test_spending_is_counted_in_dollars_whatever_the_model(self, api, provider):
        assert _ask(api, USER).status_code == 200                       # 0,40 USD -> 0,50 $ CA
        assert _ask(api, USER, model="deepseek/deepseek-v4-pro").status_code == 200
        state = api.get("/api/budget", headers=_h(USER)).json()
        assert state["spent_cad"] == 1.0 and state["remaining_cad"] == 2.0 and state["percent"] == 33
        assert provider["bodies"][0]["usage"] == {"include": True}
        # Les autres comptes ne sont pas touchés.
        assert api.get("/api/budget", headers=_h(OTHER)).json()["spent_cad"] == 0.0

    def test_warning_at_80_percent_then_blocked_before_the_call(self, api, provider):
        for _ in range(5):                                              # 5 x 0,50 = 2,50 $ CA (83 %)
            assert _ask(api, USER).status_code == 200
        state = api.get("/api/budget", headers=_h(USER)).json()
        assert state["warning"] is True and state["blocked"] is False
        assert _ask(api, USER).status_code == 200                       # 3,00 $ CA : budget atteint
        calls = provider["calls"]
        refused = _ask(api, USER)
        assert refused.status_code == 402 and "3,00 $" in refused.json()["detail"]
        assert provider["calls"] == calls                               # rien n'a été envoyé au fournisseur
        assert api.get("/api/budget", headers=_h(USER)).json()["blocked"] is True
        # Un autre compte continue de travailler.
        assert _ask(api, OTHER).status_code == 200

    def test_every_model_route_is_blocked_once_the_budget_is_spent(self, api):
        budget.record(USER, "m", 10.0, "chat")
        assert api.post("/api/orchestrate", headers=_h(USER), json={"question": "Analyse"}).status_code == 402
        assert api.post("/query", headers=_h(USER), json={"query": "x", "agent": "sophie"}).status_code == 402
        assert api.post("/stream-query", headers=_h(USER), json={"query": "x", "agent": "sophie"}).status_code == 402
        # Les écrans qui n'appellent pas de modèle restent accessibles.
        assert api.get("/api/knowledge/list", headers=_h(USER)).status_code == 200
        assert api.get("/api/budget", headers=_h(USER)).status_code == 200

    def test_admin_raises_one_account_without_changing_the_others(self, api, provider):
        budget.record(USER, "m", 5.0, "chat")
        assert _ask(api, USER).status_code == 402
        raised = api.put(f"/api/admin/budgets/{USER}", headers=_h(ADMIN), json={"budget_cad": 10})
        assert raised.status_code == 200 and raised.json()["budget_cad"] == 10.0 and raised.json()["blocked"] is False
        assert _ask(api, USER).status_code == 200
        assert api.get("/api/budget", headers=_h(OTHER)).json()["budget_cad"] == 3.0
        # Retour au budget d'essai par défaut.
        assert api.put(f"/api/admin/budgets/{USER}", headers=_h(ADMIN), json={"budget_cad": None}).json()["budget_cad"] == 3.0

    def test_budget_edits_are_admin_only_and_validated(self, api):
        assert api.put(f"/api/admin/budgets/{USER}", headers=_h(USER), json={"budget_cad": 999}).status_code == 403
        assert api.get("/api/admin/budgets", headers=_h(USER)).status_code == 403
        for bad in (-1, 10_001):
            assert api.put(f"/api/admin/budgets/{USER}", headers=_h(ADMIN), json={"budget_cad": bad}).status_code == 400

    def test_admin_is_not_limited_by_the_trial_budget(self, api, provider):
        budget.record(ADMIN, "m", 50.0, "chat")
        assert _ask(api, ADMIN).status_code == 200
        state = api.get("/api/budget", headers=_h(ADMIN)).json()
        assert state["unlimited"] is True and state["budget_cad"] is None and state["blocked"] is False

    def test_a_personal_api_key_is_not_charged_to_the_platform(self, api, provider, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY")
        assert api.post("/api/chat", headers={**_h(USER), "X-API-Key": "cle-du-client"}, json=QUESTION).status_code == 200
        assert api.get("/api/budget", headers=_h(USER)).json()["spent_cad"] == 0.0

    def test_default_budget_is_configurable(self, api, monkeypatch):
        monkeypatch.setenv("TRIAL_BUDGET_CAD", "5")
        assert api.get("/api/budget", headers=_h(USER)).json()["budget_cad"] == 5.0
        monkeypatch.setenv("TRIAL_BUDGET_CAD", "pas-un-nombre")
        assert api.get("/api/budget", headers=_h(USER)).json()["budget_cad"] == 3.0


class TestPlatformBudget:
    def test_platform_cap_stops_everyone_including_the_admin(self, api, provider, monkeypatch):
        monkeypatch.setenv("PLATFORM_MONTHLY_BUDGET_CAD", "2")
        budget.record(OTHER, "m", 1.0, "chat")                          # 1,25 $ CA
        assert _ask(api, USER).status_code == 200                       # +0,50 = 1,75 $ CA
        assert _ask(api, USER).status_code == 200                       # +0,50 = 2,25 $ CA : plafond atteint
        calls = provider["calls"]
        for user in (USER, ADMIN):
            refused = _ask(api, user)
            assert refused.status_code == 402 and "plateforme" in refused.json()["detail"]
        assert provider["calls"] == calls
        assert budget.platform_open() is False

    def test_previous_months_do_not_count_toward_the_platform_cap(self, api, monkeypatch):
        import datetime as dt
        import json

        monkeypatch.setenv("PLATFORM_MONTHLY_BUDGET_CAD", "2")
        old = (budget._month_start() - dt.timedelta(days=3)).isoformat()
        budget._usage_file().write_text(json.dumps([{"user_id": OTHER, "cost_usd": 500.0, "created_at": old}]))
        assert budget.platform_open() is True
        # ... mais ils comptent toujours dans le budget d'essai du compte, qui est un total.
        assert api.get("/api/budget", headers=_h(OTHER)).json()["blocked"] is True

    def test_admin_overview(self, api, provider):
        _ask(api, USER)
        budget.record(OTHER, "m", 4.0, "chat")
        data = api.get("/api/admin/budgets", headers=_h(ADMIN)).json()
        by_id = {a["user_id"]: a for a in data["accounts"]}
        assert by_id[USER]["spent_cad"] == 0.5 and by_id[USER]["calls"] == 1 and by_id[USER]["blocked"] is False
        assert by_id[OTHER]["blocked"] is True and by_id[OTHER]["percent"] == 100
        assert data["default_budget_cad"] == 3.0 and data["usd_to_cad"] == 1.25
        assert data["platform"] == {"month_spent_cad": 5.5, "cap_cad": 150.0, "blocked": False}


class TestResilience:
    def test_a_metering_failure_never_breaks_the_answer(self, api, provider, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("disque plein")

        monkeypatch.setattr(kb_storage, "_save", boom)
        assert _ask(api, USER).status_code == 200

    def test_a_budget_check_failure_does_not_block_the_service(self, api, provider, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("base indisponible")

        monkeypatch.setattr(budget, "status", boom)
        assert _ask(api, USER).status_code == 200

    def test_empty_answers_are_not_recorded(self, api):
        assert budget.meter({"choices": []}, "m", "chat", USER) == 0.0
        assert budget.usage_by_account() == {}


class TestOtherCallSites:
    """L'orchestrateur, les agents et la description d'images sont comptés eux aussi."""

    @pytest.fixture
    def fake_httpx(self, monkeypatch):
        import httpx

        class Response:
            status_code = 200
            text = ""

            def json(self):
                return {"choices": [{"message": {"content": '{"description": "Un graphique.", "mots_cles": []}'}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.02}}

        class Client:
            def __init__(self, *a, **k): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return Response()

        monkeypatch.setattr(httpx, "AsyncClient", Client)

    @pytest.mark.asyncio
    async def test_orchestrator_agents_charge_the_requesting_account(self, api, fake_httpx):
        from orchestrator import engine

        await engine.call_agent("sophie", "Question", {"user_id": USER, "api_key": "k"})
        token = budget.current_user.set(USER)
        try:
            await engine.synthesize("Question", {"sophie": "Analyse"})
        finally:
            budget.current_user.reset(token)
        usage = budget.usage_by_account()[USER]
        assert usage["calls"] == 2 and round(usage["cost"], 4) == 0.04

    @pytest.mark.asyncio
    async def test_agent_kernel_calls_are_charged(self, api, fake_httpx, monkeypatch):
        import z_kernel

        monkeypatch.setattr(z_kernel, "OPENROUTER_API_KEY", "k")
        token = budget.current_user.set(USER)
        try:
            await z_kernel.call_llm("m", "système", "question", sensibilite="public")
        finally:
            budget.current_user.reset(token)
        assert budget.usage_by_account()[USER]["calls"] == 1

    @pytest.mark.asyncio
    async def test_image_descriptions_are_charged_to_the_document_owner(self, api, fake_httpx, monkeypatch, tmp_path):
        from services import image_description

        monkeypatch.setenv("IMAGE_DESCRIPTION_MODEL", "modele/vision")
        image = tmp_path / "g.png"
        image.write_bytes(b"png")
        described = await image_description.describe_image(image, ADMIN)
        assert described["description"] == "Un graphique."
        usage = budget.usage_by_account()[ADMIN]
        assert usage["calls"] == 1 and round(usage["cost"], 4) == 0.02

    def test_stream_without_usage_is_estimated(self):
        data = budget.estimate_from_text("a" * 4000, "b" * 400)
        cost, tokens_in, tokens_out, estimated = budget.cost_of(data)
        assert estimated and tokens_in == 1001 and tokens_out == 101 and cost > 0


class TestProviderCompatibility:
    def test_chat_retries_without_the_cost_request_when_the_provider_rejects_it(self, api, monkeypatch):
        """Un fournisseur qui refuse le paramètre de coût ne bloque pas la réponse."""
        seen = []

        class Response:
            text = "unknown parameter: usage"

            def __init__(self, ok):
                self.status_code = 200 if ok else 400

            def json(self):
                return {"choices": [{"message": {"content": "Réponse."}}],
                        "usage": {"prompt_tokens": 1000, "completion_tokens": 200}}

        class Client:
            def __init__(self, *a, **k): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

            async def post(self, url, **kwargs):
                seen.append("usage" in kwargs["json"])
                return Response(ok="usage" not in kwargs["json"])

        monkeypatch.setattr(main.httpx, "AsyncClient", Client)
        assert _ask(api, USER).status_code == 200
        assert seen == [True, False]
        # Sans coût renvoyé, la dépense est estimée : jamais comptée à zéro.
        assert api.get("/api/budget", headers=_h(USER)).json()["spent_cad"] > 0

    def test_cost_request_can_be_switched_off(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_USAGE_PARAM", "0")
        assert budget.with_usage({"model": "m"}) == {"model": "m"}
        monkeypatch.setenv("OPENROUTER_USAGE_PARAM", "1")
        assert budget.with_usage({"model": "m"})["usage"] == {"include": True}
        assert budget.without_usage({"model": "m", "usage": {"include": True}}) == {"model": "m"}

    def test_a_real_provider_refusal_is_still_reported(self, api, monkeypatch):
        class Response:
            status_code = 401
            text = "invalid key"

        class Client:
            def __init__(self, *a, **k): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return Response()

        monkeypatch.setattr(main.httpx, "AsyncClient", Client)
        refused = _ask(api, USER)
        assert refused.status_code == 401 and "fournisseur" in refused.json()["detail"]
