"""Budget de consommation contre un vrai PostgreSQL (si Z12_TEST_DATABASE_URL est défini)."""
from __future__ import annotations

import os

import pytest

import budget
import kb_storage

TEST_URL = os.getenv("Z12_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="Z12_TEST_DATABASE_URL non défini")


@pytest.fixture
def db(monkeypatch):
    import psycopg

    def drop():
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS cfo_usage, cfo_budgets")

    drop()
    monkeypatch.setattr(kb_storage, "DATABASE_URL", TEST_URL)
    monkeypatch.setenv("ADMIN_USER_IDS", "9")
    monkeypatch.setenv("USD_TO_CAD", "1.25")
    for name in ("TRIAL_BUDGET_CAD", "PLATFORM_MONTHLY_BUDGET_CAD"):
        monkeypatch.delenv(name, raising=False)
    assert budget.ensure_schema() is True
    assert budget.ensure_schema() is True  # idempotent
    yield
    drop()


def test_spending_budgets_and_caps_in_postgres(db, monkeypatch):
    import psycopg
    from fastapi import HTTPException

    budget.record("21", "modele/a", 0.40, "chat", 1000, 200)
    budget.record("21", "modele/b", 0.000012, "orchestrate", 10, 5, estimated=True)
    budget.record("22", "modele/a", 4.0, "chat")
    assert round(budget.spent_usd("21"), 6) == 0.400012
    assert budget.status("21")["spent_cad"] == 0.5 and budget.status("21")["blocked"] is False
    budget.check("21")

    # Compte au-delà de son budget d'essai : bloqué, puis débloqué par l'administrateur.
    with pytest.raises(HTTPException) as refused:
        budget.check("22")
    assert refused.value.status_code == 402
    budget.set_account_budget("22", 10.0, "9")
    budget.set_account_budget("22", 12.5, "9")                # mise à jour, pas de doublon
    assert budget.account_budget_cad("22") == 12.5
    budget.check("22")
    budget.set_account_budget("22", None, "9")
    assert budget.account_budget_cad("22") == 3.0

    usage = budget.usage_by_account()
    assert usage["21"]["calls"] == 2 and usage["22"]["calls"] == 1 and usage["21"]["last_used"]

    # Plafond mensuel : seules les dépenses du mois comptent.
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        conn.execute("INSERT INTO cfo_usage (user_id, cost_usd, created_at) VALUES ('30', 900, NOW() - interval '40 days')")
    assert budget.platform_open() is True
    monkeypatch.setenv("PLATFORM_MONTHLY_BUDGET_CAD", "5")
    assert budget.platform_open() is False                    # 4,40 USD -> 5,50 $ CA ce mois-ci
    with pytest.raises(HTTPException):
        budget.check("9")                                     # l'administrateur aussi
    assert budget.status("30")["blocked"] is True
