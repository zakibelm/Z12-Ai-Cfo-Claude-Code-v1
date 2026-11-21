import os
from dataclasses import dataclass, field
from enum import Enum
from typing import List

import httpx

import budget
from . import team
from .team import DEPENDENCIES, MAX_AGENTS  # noqa: F401  (réexportés pour compatibilité)

ORCHESTRATOR_MODEL = os.getenv("ORCHESTRATOR_MODEL", "deepseek/deepseek-v4-pro")


class Complexity(str, Enum):
    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"


# Conservé pour compatibilité : dérivé de la source unique.
AGENTS_AVAILABLE = team.AGENT_IDS

ASSESSOR_PROMPT = """Tu es l'orchestrateur du cabinet financier Z12 AI CFO Suite.
Analyse cette question et retourne UNIQUEMENT un JSON valide (aucun texte autour).
Question : {question}
Agents disponibles :
{roster}
Retourne : {{"complexity": "simple|medium|complex", "agents": ["id1"], "reasoning": "texte court"}}
Règles : simple=1 agent, medium=2-3 agents, complex=question stratégique majeure (acquisition, restructuration, financement). Maximum {max_agents} agents. Utilise uniquement les identifiants listés."""


@dataclass
class AssessmentResult:
    complexity: Complexity
    agents: List[str]
    parallel_group: List[str]
    sequential_chain: List[tuple]
    reasoning: str
    fallback: bool = False
    warning: str = ""


def build_assessment(agents: List[str], complexity: str, reasoning: str,
                     fallback: bool = False, warning: str = "") -> AssessmentResult:
    """Construit un plan cohérent : agents valides, bornés, dépendances ordonnées."""
    agents = team.sanitize_agents(agents) or [team.DEFAULT_AGENT]
    try:
        cx = Complexity(complexity)
    except ValueError:
        cx = Complexity.SIMPLE if len(agents) == 1 else Complexity.MEDIUM
    if len(agents) == 1:
        cx = Complexity.SIMPLE
    elif cx == Complexity.SIMPLE:
        cx = Complexity.MEDIUM  # plusieurs agents => jamais « simple »
    parallel = [a for a in agents if a not in DEPENDENCIES or DEPENDENCIES[a] not in agents]
    sequential = [(a, DEPENDENCIES[a]) for a in agents if a in DEPENDENCIES and DEPENDENCIES[a] in agents]
    return AssessmentResult(cx, agents, parallel, sequential, reasoning, fallback, warning)


def _fallback(question: str, why: str) -> AssessmentResult:
    routed = team.fast_route(question)
    agents = routed or [team.DEFAULT_AGENT]
    return build_assessment(
        agents, "simple" if len(agents) == 1 else "medium",
        f"fallback: {why}", fallback=True,
        warning="Routage automatique de secours utilisé (l'évaluateur IA n'a pas répondu correctement).",
    )


async def assess_question(question: str, user_id=None) -> AssessmentResult:
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "HTTP-Referer": "https://cfo.optigenius.pro", "X-Title": "Z12 AI CFO Suite"},
                json=budget.with_usage({"model": ORCHESTRATOR_MODEL, "max_tokens": 200,
                      "messages": [{"role": "user", "content": ASSESSOR_PROMPT.format(
                          question=question, roster=team.roster_for_prompt(), max_agents=MAX_AGENTS)}]}),
            )
        data = response.json()
        budget.meter(data, ORCHESTRATOR_MODEL, "orchestrate", user_id)
        raw = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[WARN] Assessor error: {e}")
        return _fallback(question, "appel évaluateur échoué")
    parsed = team.parse_json_object(raw)
    if not parsed:
        return _fallback(question, "JSON illisible")
    agents = team.sanitize_agents(parsed.get("agents"))
    if not agents:
        return _fallback(question, "aucun agent valide")
    return build_assessment(agents, str(parsed.get("complexity", "")), str(parsed.get("reasoning", ""))[:300])
