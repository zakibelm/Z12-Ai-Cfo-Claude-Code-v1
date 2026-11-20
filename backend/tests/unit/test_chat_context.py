"""Question du chat : ce qui sert à la recherche, à la mémoire et au cache."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import chat_context
import kb_storage
import main
import security_limits

USER = "21"
H = {"X-User-Id": USER}
CASE = "Vous êtes stagiaire en fiscalité au cabinet Trebbiano et Muska. " * 20
ATTACHED = (f"[Pièce jointe « Etude de cas.pdf »]\n{CASE}\n[Fin de « Etude de cas.pdf »]\n\n"
            "[Pièce jointe « T4.docx » : non lue — aucun texte lisible]\n\n"
            "analyse et réponds aux questions")
ROUTING = 'Analyse cette demande et retourne le plan de workflow JSON :\n\n"analyse et réponds aux questions"'
PLAN = '{"type": "single", "priority": "high", "agents": ["TaxAgent"], "phases": [], "reason": "Cas fiscal."}'


class TestQuestionOnly:
    def test_attachment_text_is_removed_and_the_typed_question_kept(self):
        assert chat_context.has_attachments(ATTACHED)
        assert chat_context.question_only(ATTACHED) == "analyse et réponds aux questions"

    def test_truncated_english_and_legacy_formats(self):
        cut = ("[Pièce jointe « cas.pdf » — extrait : 1 000 caractères sur 90 000, la suite n'est pas fournie]\n"
               "texte du cas\n[Fin de « cas.pdf »]\n\nQuelle est la TPS ?")
        assert chat_context.question_only(cut) == "Quelle est la TPS ?"
        english = '[Attachment "case.pdf"]\ncase text\n[End of "case.pdf"]\n\nWhat is the GST?'
        assert chat_context.question_only(english) == "What is the GST?"
        legacy = "[📎 cas.pdf]\n---\n[Fichier: cas.pdf | 238 KB | application/pdf]\nIndexez via Documents.\n---\nanalyse"
        assert chat_context.has_attachments(legacy) and chat_context.question_only(legacy) == "analyse"

    def test_a_plain_question_is_untouched(self):
        assert not chat_context.has_attachments("Qu'est-ce que la TPS ? [voir note]")
        assert chat_context.question_only("  Qu'est-ce que la TPS ?  ") == "Qu'est-ce que la TPS ?"

    def test_purpose(self):
        assert chat_context.purpose_of("agent", "Qu'est-ce que la TPS ?") == "agent"
        assert chat_context.purpose_of("routing", "x") == "routing"
        assert chat_context.purpose_of("synthesis", "x") == "synthesis"
        assert chat_context.purpose_of("n'importe quoi", "x") == "agent"
        assert chat_context.purpose_of(None, ROUTING) == "routing"          # ancienne interface : reconnue à sa consigne

    def test_cache_key_covers_the_whole_conversation(self):
        a = [{"role": "system", "content": "s1"}, {"role": "user", "content": "Analyse la TPS"}, {"role": "assistant", "content": "…"},
             {"role": "user", "content": "oui"}]
        b = [{"role": "system", "content": "s2"}, {"role": "user", "content": "Analyse la paie"}, {"role": "assistant", "content": "…"},
             {"role": "user", "content": "oui"}]
        assert chat_context.conversation_key(a) != chat_context.conversation_key(b)
        assert chat_context.conversation_key(a) == chat_context.conversation_key([{"role": "system", "content": "autre"}, *a[1:]])


@pytest.fixture
def chat(monkeypatch, tmp_path):
    """/api/chat avec un faux fournisseur ; mémoire, cache et recherche documentaire observés."""
    seen = {"provider": [], "retrieve": [], "memory_read": [], "memory_saved": [], "cache_read": [], "cache_saved": [],
            "answer": "La TPS à remettre est de 4 415 $, soit 5 % des ventes taxables nettes des crédits.", "cache": {}}
    monkeypatch.setattr(kb_storage, "get_conn", lambda: None)
    monkeypatch.setattr(kb_storage, "_DOCS_FILE", tmp_path / "docs.json")
    monkeypatch.setenv("OPENROUTER_API_KEY", "cle-plateforme")
    monkeypatch.setattr(security_limits.llm_limiter, "max_calls", 10_000)

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": seen["answer"]}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.001}}

    class Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, **kwargs):
            seen["provider"].append(kwargs["json"]["messages"])
            return Response()

    async def retrieve(query, user_id, **kwargs):
        seen["retrieve"].append(query)
        return {"sources": [], "withheld": 0, "context": ""}

    async def memory_read(question, agent, user_id="default"):
        seen["memory_read"].append(question)
        return ""

    async def memory_save(session_id, agent, question, answer, user_id="default"):
        seen["memory_saved"].append((question, answer))

    async def cache_read(query, agent, scope=""):
        seen["cache_read"].append(query)
        return seen["cache"].get((query, scope))

    async def cache_save(query, agent, response, scope=""):
        seen["cache_saved"].append(query)
        seen["cache"][(query, scope)] = response

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    monkeypatch.setattr(main, "retrieve", retrieve)
    monkeypatch.setattr(main, "inject_memory_context", memory_read)
    monkeypatch.setattr(main, "save_session_summary", memory_save)
    monkeypatch.setattr(main, "get_cached_response", cache_read)
    monkeypatch.setattr(main, "cache_response", cache_save)
    with TestClient(main.app) as client:
        seen["ask"] = lambda messages, **extra: client.post("/api/chat", headers=H, json={
            "model": "deepseek/deepseek-v4-pro", "messages": [{"role": "system", "content": "Tu es un CPA."}, *messages], **extra})
        yield seen


def _user(text):
    return [{"role": "user", "content": text}]


class TestSpecialistAnswer:
    def test_documents_are_searched_with_the_question_not_the_attached_file(self, chat):
        assert chat["ask"](_user(ATTACHED)).status_code == 200
        assert chat["retrieve"] == ["analyse et réponds aux questions"]
        assert CASE.strip() in chat["provider"][0][-1]["content"]           # le spécialiste, lui, reçoit bien le cas

    def test_nothing_from_a_conversation_with_attachments_is_kept(self, chat):
        """Une pièce jointe du chat ne vit que dans la conversation."""
        chat["ask"](_user(ATTACHED))
        chat["ask"]([*_user(ATTACHED), {"role": "assistant", "content": "Réponse."}, *_user("et pour la TVQ ?")])
        assert chat["memory_saved"] == [] and chat["memory_read"] == []
        assert chat["cache_saved"] == [] and chat["cache_read"] == []

    def test_an_ordinary_question_still_uses_memory(self, chat):
        chat["ask"](_user("Qu'est-ce que la TPS ?"))
        assert chat["memory_read"] == ["Qu'est-ce que la TPS ?"]
        assert chat["memory_saved"][0][0] == "Qu'est-ce que la TPS ?"


class TestOrchestratorCalls:
    def test_a_routing_plan_never_enters_memory_nor_searches_documents(self, chat):
        """Régression : les plans JSON entraient en mémoire, puis les spécialistes répondaient par du JSON."""
        chat["answer"] = PLAN
        assert chat["ask"](_user(ROUTING), purpose="routing").status_code == 200
        assert chat["memory_saved"] == [] and chat["memory_read"] == [] and chat["retrieve"] == []

    def test_an_older_interface_is_recognised_by_its_instruction(self, chat):
        chat["answer"] = PLAN
        chat["ask"](_user(ROUTING))                                         # sans « purpose »
        assert chat["memory_saved"] == [] and chat["retrieve"] == []

    def test_a_synthesis_is_not_memorised_either(self, chat):
        chat["ask"](_user('Demande originale :\n"x"\n\n### Sophie\nAnalyse'), purpose="synthesis")
        assert chat["memory_saved"] == [] and chat["retrieve"] == []


class TestCache:
    def test_a_cached_answer_has_the_same_shape_as_a_provider_answer(self, chat):
        """Régression : une réponse en cache n'avait pas de « choices » et s'affichait « Erreur inattendue. »"""
        first = chat["ask"](_user("Qu'est-ce que la TPS ?")).json()
        again = chat["ask"](_user("Qu'est-ce que la TPS ?")).json()
        assert again["cached"] is True and len(chat["provider"]) == 1
        assert again["choices"][0]["message"]["content"] == first["choices"][0]["message"]["content"]

    def test_the_same_short_reply_in_another_conversation_is_not_served_from_cache(self, chat):
        """Régression : « oui » ressortait la réponse d'un autre « oui »."""
        one = [*_user("Veux-tu le calcul de la TPS ?"), {"role": "assistant", "content": "Je peux le faire."}, *_user("oui")]
        two = [*_user("Veux-tu le calcul de la paie ?"), {"role": "assistant", "content": "Je peux le faire."}, *_user("oui")]
        chat["ask"](one)
        answer = chat["ask"](two).json()
        assert "cached" not in answer and len(chat["provider"]) == 2
        assert all(len(key) == 64 for key in chat["cache_saved"])           # empreinte, jamais le texte de la question
