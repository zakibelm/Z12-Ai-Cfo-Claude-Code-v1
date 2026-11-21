"""Équipe d'agents — SOURCE UNIQUE pour l'orchestrateur serveur.

Les 9 spécialistes ci-dessous correspondent 1:1 aux agents du frontend
(TaxAgent, AuditAgent, …) et réutilisent les prompts de `agent_prompts.py`.
Toute modification de l'équipe se fait ici et nulle part ailleurs.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional

from agent_prompts import AGENT_PROMPTS as _PROMPTS

MAX_AGENTS = 4


@dataclass(frozen=True)
class Specialist:
    id: str            # identifiant serveur (minuscule, sans accent)
    frontend_id: str   # identifiant côté React
    prompt_key: str    # clé dans agent_prompts.AGENT_PROMPTS
    label: str
    domain: str        # description courte injectée dans le prompt d'évaluation
    keywords: tuple    # mots-clés (sans accent) pour le routage de secours


TEAM: Dict[str, Specialist] = {s.id: s for s in [
    Specialist("sophie", "TaxAgent", "Sophie", "Fiscal",
               "fiscalité, impôts, TPS/TVQ, RS&DE, déductions",
               ("impot", "fiscal", "tps", "tvq", "t2", "t1", "deduction", "cra", "revenu quebec", "rs&de", "sred")),
    Specialist("alexandre", "AuditAgent", "Alexandre", "Audit",
               "audit, contrôle interne, risques, normes CPA/IFRS/NCECF",
               ("audit", "controle interne", "risque", "ifrs", "ncecf", "fraude", "verification")),
    Specialist("natalie", "CashFlowAgent", "Natalie", "Trésorerie",
               "trésorerie, cashflow, liquidité, BFR, financement court terme",
               ("tresorerie", "cashflow", "cash flow", "liquidite", "bfr", "fonds de roulement", "marge de credit")),
    Specialist("isabelle", "ComplianceAgent", "Isabelle", "Conformité",
               "Loi 25, PIPEDA, CASL, gouvernance des données",
               ("loi 25", "pipeda", "casl", "donnees personnelles", "vie privee", "conformite", "efvp")),
    Specialist("marc", "FinancialAgent", "Marc", "Analyse financière",
               "états financiers, ratios, benchmarks, diagnostic",
               ("ratio", "etats financiers", "bilan", "rentabilite", "solvabilite", "marge", "diagnostic", "benchmark")),
    Specialist("sarah", "InvestmentAgent", "Sarah", "Investissement / M&A",
               "valorisation DCF, multiples, due diligence, acquisition",
               ("valorisation", "dcf", "acquisition", "m&a", "due diligence", "investissement", "multiple", "capital-risque")),
    Specialist("jeanfrancois", "OCRAgent", "Jean-François", "Extraction documentaire",
               "extraction de données de PDF/images, relevés, factures",
               ("ocr", "extraire", "extraction", "releve bancaire", "facture", "pdf", "numeriser")),
    Specialist("emilie", "VeilleAgent", "Émilie", "Veille réglementaire",
               "nouveautés réglementaires, jurisprudence, tendances",
               ("veille", "nouvelle reglementation", "jurisprudence", "budget federal", "budget du quebec", "changement de loi")),
    Specialist("patrick", "SubventionsAgent", "Patrick", "Subventions",
               "subventions, crédits d'impôt, programmes gouvernementaux",
               ("subvention", "credit d'impot", "pari", "canexport", "investissement quebec", "programme gouvernemental", "cdap")),
]}

# Un agent peut dépendre du résultat d'un autre (exécuté ensuite, avec son contexte).
DEPENDENCIES: Dict[str, str] = {
    "isabelle": "alexandre",
    "sarah": "marc",
    "patrick": "sophie",
}

DEFAULT_AGENT = "marc"  # généraliste financier : repli sûr quand rien ne correspond

AGENT_IDS: List[str] = list(TEAM.keys())


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def system_prompt(agent_id: str) -> str:
    spec = TEAM.get(agent_id)
    if not spec:
        return _PROMPTS["Auto"]
    return _PROMPTS.get(spec.prompt_key) or _PROMPTS["Auto"]


def roster_for_prompt() -> str:
    return "\n".join(f"- {s.id} : {s.domain}" for s in TEAM.values())


def fast_route(question: str, limit: int = MAX_AGENTS) -> List[str]:
    """Routage déterministe hors-ligne (mots-clés). Retourne [] si aucun signal."""
    q = _norm(question)
    scored = []
    for s in TEAM.values():
        hits = sum(1 for k in s.keywords if k in q)
        if hits:
            scored.append((hits, s.id))
    scored.sort(key=lambda x: (-x[0], AGENT_IDS.index(x[1])))
    return [a for _, a in scored[:limit]]


def sanitize_agents(raw, limit: int = MAX_AGENTS) -> List[str]:
    """Filtre les identifiants inconnus, déduplique, borne à `limit`."""
    if not isinstance(raw, list):
        return []
    seen: List[str] = []
    for a in raw:
        if not isinstance(a, str):
            continue
        key = _norm(a).replace("-", "").replace(" ", "")
        if key in TEAM and key not in seen:
            seen.append(key)
    return seen[:limit]


def parse_json_object(raw: str) -> Optional[dict]:
    """Extrait un objet JSON d'une réponse LLM (tolère le texte autour). None si impossible."""
    import json
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, ValueError):
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group())
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None
