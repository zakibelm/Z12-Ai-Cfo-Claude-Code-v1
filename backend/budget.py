"""Budget de consommation, indépendant du modèle choisi.

Chaque appel à un modèle est compté à son coût réel (en dollars, tel que renvoyé par le
fournisseur), pas en nombre de questions : une question à un modèle haut de gamme peut coûter
cent fois plus qu'une question à un modèle économique.

Deux plafonds, vérifiés AVANT l'appel :
- par compte : un budget d'essai total (TRIAL_BUDGET_CAD, 3 $ par défaut), modifiable compte
  par compte par l'administrateur. Les administrateurs n'y sont pas soumis ;
- pour toute la plateforme : un plafond mensuel (PLATFORM_MONTHLY_BUDGET_CAD, 150 $ par
  défaut), filet de sécurité qui s'applique à tout le monde.

Le coût exact n'est connu qu'à la fin d'une réponse : un compte peut donc dépasser son
plafond du montant d'une seule réponse. Si le fournisseur ne renvoie pas de coût, il est
estimé à partir des jetons avec un tarif volontairement élevé — jamais compté à zéro.
"""
from __future__ import annotations

import datetime as dt
import os
from contextvars import ContextVar

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import kb_storage

# Utilisateur de la requête en cours, posé par `security_limits.llm_user`.
current_user: ContextVar[str | None] = ContextVar("z12_budget_user", default=None)

WARN_RATIO = 0.8
MAX_ACCOUNT_BUDGET_CAD = 10_000.0

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS cfo_usage (
        id          BIGSERIAL PRIMARY KEY,
        user_id     TEXT NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        kind        TEXT,
        model       TEXT,
        cost_usd    NUMERIC(14, 8) NOT NULL DEFAULT 0,
        tokens_in   INTEGER,
        tokens_out  INTEGER,
        estimated   BOOLEAN NOT NULL DEFAULT FALSE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cfo_usage_user ON cfo_usage (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_cfo_usage_created ON cfo_usage (created_at)",
    """CREATE TABLE IF NOT EXISTS cfo_budgets (
        user_id     TEXT PRIMARY KEY,
        budget_cad  NUMERIC(12, 2) NOT NULL,
        updated_by  TEXT,
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
]


# ─── Réglages ────────────────────────────────────────────────────────────────

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def trial_budget_cad() -> float:
    """Budget d'essai par compte. Une valeur négative désactive la limite par compte."""
    return _env_float("TRIAL_BUDGET_CAD", 3.0)


def platform_budget_cad() -> float:
    """Plafond mensuel de la plateforme. Une valeur négative le désactive."""
    return _env_float("PLATFORM_MONTHLY_BUDGET_CAD", 150.0)


def usd_to_cad() -> float:
    rate = _env_float("USD_TO_CAD", 1.40)
    return rate if rate > 0 else 1.40


def _is_admin(user_id: str | None) -> bool:
    from security_limits import is_admin

    return is_admin(user_id)


# ─── Coût d'une réponse ──────────────────────────────────────────────────────

def usage_param_enabled() -> bool:
    """OPENROUTER_USAGE_PARAM=0 retire la demande de coût des requêtes (le coût est alors estimé)."""
    return os.getenv("OPENROUTER_USAGE_PARAM", "1").strip().lower() not in ("0", "false", "non", "off")


def with_usage(body: dict) -> dict:
    """Demande au fournisseur de renvoyer le coût réel de l'appel."""
    return {**body, "usage": {"include": True}} if usage_param_enabled() else dict(body)


def without_usage(body: dict) -> dict:
    return {k: v for k, v in body.items() if k != "usage"}


def cost_of(data: dict | None) -> tuple[float, int, int, bool]:
    """(coût en USD, jetons en entrée, jetons en sortie, estimé ?) d'une réponse du fournisseur."""
    usage = (data or {}).get("usage") if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}

    def _int(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    tokens_in, tokens_out = _int(usage.get("prompt_tokens")), _int(usage.get("completion_tokens"))
    cost = usage.get("cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        return float(cost), tokens_in, tokens_out, False
    estimate = (tokens_in * _env_float("BUDGET_FALLBACK_USD_PER_MTOK_IN", 3.0)
                + tokens_out * _env_float("BUDGET_FALLBACK_USD_PER_MTOK_OUT", 15.0)) / 1_000_000
    return estimate, tokens_in, tokens_out, True


def estimate_from_text(prompt: str, answer: str) -> dict:
    """Réponse sans décompte (flux coupé) : jetons estimés à quatre caractères par jeton."""
    return {"usage": {"prompt_tokens": len(prompt or "") // 4 + 1, "completion_tokens": len(answer or "") // 4 + 1}}


# ─── Stockage ────────────────────────────────────────────────────────────────

def _usage_file():
    return kb_storage._DOCS_FILE.parent / "usage.json"


def _budgets_file():
    return kb_storage._DOCS_FILE.parent / "budgets.json"


def ensure_schema() -> bool:
    conn = kb_storage.get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            for statement in SCHEMA:
                cur.execute(statement)
            conn.commit()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Tables de budget indisponibles : {type(e).__name__}")
        conn.rollback()
        return False
    finally:
        conn.close()


def record(user_id: str | None, model: str | None, cost_usd: float, kind: str,
           tokens_in: int = 0, tokens_out: int = 0, estimated: bool = False) -> None:
    """Enregistre une dépense. Ne lève jamais : une panne de comptage ne casse pas la réponse."""
    user_id = str(user_id or "inconnu")
    try:
        conn = kb_storage.get_conn()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO cfo_usage (user_id, kind, model, cost_usd, tokens_in, tokens_out, estimated)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (user_id, kind, (model or "")[:200], cost_usd, tokens_in, tokens_out, estimated),
                    )
                    conn.commit()
                return
            finally:
                conn.close()
        with kb_storage._lock:
            rows = kb_storage._load(_usage_file())
            rows.append({"user_id": user_id, "kind": kind, "model": model, "cost_usd": cost_usd,
                         "tokens_in": tokens_in, "tokens_out": tokens_out, "estimated": estimated,
                         "created_at": dt.datetime.now(dt.timezone.utc).isoformat()})
            kb_storage._save(_usage_file(), rows)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Dépense non enregistrée : {type(e).__name__}")


def meter(data: dict | None, model: str | None, kind: str, user_id: str | None = None) -> float:
    """Compte le coût d'une réponse du fournisseur pour l'utilisateur de la requête en cours."""
    cost, tokens_in, tokens_out, estimated = cost_of(data)
    if cost <= 0 and not tokens_in and not tokens_out:
        return 0.0
    record(user_id or current_user.get(), model, cost, kind, tokens_in, tokens_out, estimated)
    return cost


def _month_start() -> dt.datetime:
    now = dt.datetime.now(dt.timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def spent_usd(user_id: str | None = None, since: dt.datetime | None = None) -> float:
    """Dépense totale (USD) d'un compte, ou de tous les comptes si `user_id` est None."""
    conn = kb_storage.get_conn()
    if conn:
        try:
            clauses, params = [], []
            if user_id is not None:
                clauses.append("user_id = %s")
                params.append(str(user_id))
            if since is not None:
                clauses.append("created_at >= %s")
                params.append(since)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            with conn.cursor() as cur:
                cur.execute(f"SELECT COALESCE(SUM(cost_usd), 0) AS total FROM cfo_usage {where}", params)
                row = cur.fetchone()
            return float(row["total"] if isinstance(row, dict) else row[0])
        finally:
            conn.close()
    total = 0.0
    for row in kb_storage._load(_usage_file()):
        if user_id is not None and row.get("user_id") != str(user_id):
            continue
        if since is not None and dt.datetime.fromisoformat(row["created_at"]) < since:
            continue
        total += float(row.get("cost_usd") or 0)
    return total


def account_budget_cad(user_id: str) -> float:
    """Budget du compte : celui fixé par l'administrateur, sinon le budget d'essai par défaut."""
    conn = kb_storage.get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT budget_cad FROM cfo_budgets WHERE user_id = %s", (str(user_id),))
                row = cur.fetchone()
            if row:
                return float(row["budget_cad"] if isinstance(row, dict) else row[0])
        finally:
            conn.close()
    else:
        for row in kb_storage._load(_budgets_file()):
            if row.get("user_id") == str(user_id):
                return float(row["budget_cad"])
    return trial_budget_cad()


def set_account_budget(user_id: str, budget_cad: float | None, updated_by: str) -> None:
    """Fixe le budget d'un compte ; None le remet au budget d'essai par défaut."""
    conn = kb_storage.get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                if budget_cad is None:
                    cur.execute("DELETE FROM cfo_budgets WHERE user_id = %s", (str(user_id),))
                else:
                    cur.execute(
                        """INSERT INTO cfo_budgets (user_id, budget_cad, updated_by) VALUES (%s, %s, %s)
                           ON CONFLICT (user_id) DO UPDATE SET budget_cad = EXCLUDED.budget_cad,
                               updated_by = EXCLUDED.updated_by, updated_at = NOW()""",
                        (str(user_id), budget_cad, updated_by),
                    )
                conn.commit()
            return
        finally:
            conn.close()
    with kb_storage._lock:
        rows = [r for r in kb_storage._load(_budgets_file()) if r.get("user_id") != str(user_id)]
        if budget_cad is not None:
            rows.append({"user_id": str(user_id), "budget_cad": budget_cad, "updated_by": updated_by})
        kb_storage._save(_budgets_file(), rows)


# ─── Vérification ────────────────────────────────────────────────────────────

def status(user_id: str) -> dict:
    """État du budget d'un compte, en dollars canadiens."""
    rate = usd_to_cad()
    spent = round(spent_usd(user_id) * rate, 4)
    unlimited = _is_admin(user_id)
    budget = account_budget_cad(user_id)
    if budget < 0:
        unlimited = True
    platform_cap = platform_budget_cad()
    platform_spent = round(spent_usd(None, _month_start()) * rate, 4)
    platform_blocked = platform_cap >= 0 and platform_spent >= platform_cap
    account_blocked = not unlimited and spent >= budget
    percent = 0 if unlimited or budget <= 0 else min(100, round(100 * spent / budget))
    return {
        "currency": "CAD", "unlimited": unlimited,
        "budget_cad": None if unlimited else round(budget, 2),
        "spent_cad": round(spent, 2),
        "remaining_cad": None if unlimited else round(max(0.0, budget - spent), 2),
        "percent": percent,
        "warning": not unlimited and not account_blocked and budget > 0 and spent >= WARN_RATIO * budget,
        "blocked": account_blocked or platform_blocked,
        "reason": "platform" if platform_blocked else ("account" if account_blocked else None),
    }


def _money(value: float) -> str:
    return f"{value:.2f}".replace(".", ",") + " $"


def check(user_id: str) -> None:
    """402 si le budget du compte ou le plafond de la plateforme est atteint. Appelé avant
    tout appel à un modèle. Une panne du comptage ne bloque pas le service."""
    try:
        state = status(user_id)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Vérification du budget impossible : {type(e).__name__}")
        return
    if state["reason"] == "platform":
        raise HTTPException(402, "Le budget mensuel de la plateforme est atteint. Le service reprendra le mois prochain "
                                 "ou dès que l'administrateur l'aura relevé.")
    if state["reason"] == "account":
        raise HTTPException(402, f"Votre budget d'essai de {_money(state['budget_cad'])} est épuisé. "
                                 "Contactez-nous pour poursuivre.")


def platform_open() -> bool:
    """Faux quand le plafond mensuel de la plateforme est atteint (canaux sans compte, ex. WhatsApp)."""
    try:
        cap = platform_budget_cad()
        return cap < 0 or spent_usd(None, _month_start()) * usd_to_cad() < cap
    except Exception:  # noqa: BLE001
        return True


def allowed(user_id: str) -> bool:
    """Version sans exception de `check`, pour les traitements de fond (indexation)."""
    try:
        check(user_id)
        return True
    except HTTPException:
        return False


# ─── Routes ──────────────────────────────────────────────────────────────────

def _admin_dep():
    from security_limits import admin_user

    return admin_user


def _user_dep():
    from auth import get_current_user_id

    return get_current_user_id


router = APIRouter(tags=["budget"])


class BudgetBody(BaseModel):
    budget_cad: float | None = None


@router.get("/api/budget")
async def my_budget(user_id: str = Depends(_user_dep())):
    return status(user_id)


def _emails() -> dict[str, str]:
    """Courriels des comptes, pour l'écran de l'administrateur (au mieux)."""
    try:
        import auth_local

        with auth_local._conn() as c, c.cursor() as cur:
            cur.execute("SELECT id, email, is_active FROM local_users")
            return {str(r["id"]): r["email"] for r in cur.fetchall() if r.get("is_active", True)}
    except Exception:  # noqa: BLE001
        return {}


def usage_by_account() -> dict[str, dict]:
    conn = kb_storage.get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT user_id, COALESCE(SUM(cost_usd), 0) AS cost, COUNT(*) AS calls,
                                      MAX(created_at) AS last_used FROM cfo_usage GROUP BY user_id""")
                rows = cur.fetchall()
            return {r["user_id"]: {"cost": float(r["cost"]), "calls": int(r["calls"]),
                                   "last_used": r["last_used"].isoformat() if r["last_used"] else None} for r in rows}
        finally:
            conn.close()
    result: dict[str, dict] = {}
    for row in kb_storage._load(_usage_file()):
        entry = result.setdefault(row["user_id"], {"cost": 0.0, "calls": 0, "last_used": None})
        entry["cost"] += float(row.get("cost_usd") or 0)
        entry["calls"] += 1
        entry["last_used"] = max(entry["last_used"] or "", row.get("created_at") or "") or None
    return result


@router.get("/api/admin/budgets")
async def budgets_list(_: str = Depends(_admin_dep())):
    rate = usd_to_cad()
    usage, emails = usage_by_account(), _emails()
    accounts = []
    for user_id in sorted(set(usage) | set(emails), key=lambda u: (not u.isdigit(), int(u) if u.isdigit() else 0, u)):
        state = status(user_id)
        accounts.append({
            "user_id": user_id, "email": emails.get(user_id), "is_admin": _is_admin(user_id),
            "calls": usage.get(user_id, {}).get("calls", 0), "last_used": usage.get(user_id, {}).get("last_used"),
            **{k: state[k] for k in ("unlimited", "budget_cad", "spent_cad", "remaining_cad", "percent", "warning")},
            "blocked": state["reason"] == "account",
        })
    cap = platform_budget_cad()
    month = round(spent_usd(None, _month_start()) * rate, 2)
    return {
        "accounts": accounts,
        "default_budget_cad": trial_budget_cad(),
        "platform": {"month_spent_cad": month, "cap_cad": None if cap < 0 else cap,
                     "blocked": cap >= 0 and month >= cap},
        "usd_to_cad": rate,
    }


@router.put("/api/admin/budgets/{user_id}")
async def budgets_set(user_id: str, body: BudgetBody, admin_id: str = Depends(_admin_dep())):
    if body.budget_cad is not None and not (0 <= body.budget_cad <= MAX_ACCOUNT_BUDGET_CAD):
        raise HTTPException(400, f"Budget entre 0 et {MAX_ACCOUNT_BUDGET_CAD:.0f} $")
    if len(user_id) > 64:
        raise HTTPException(400, "Identifiant de compte invalide")
    set_account_budget(user_id, body.budget_cad, admin_id)
    return status(user_id)
