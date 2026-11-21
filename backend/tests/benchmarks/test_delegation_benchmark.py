"""Benchmark de délégation (hors ligne) : la question est-elle routée vers le bon agent ?

Mesure le routage de secours déterministe (`team.fast_route`), celui qui protège l'utilisateur
quand l'évaluateur IA tombe. Seuil minimal : 85 % de précision, 0 agent inconnu.
"""
import json
from pathlib import Path

from orchestrator import team
from orchestrator.assessor import build_assessment

DATA = json.loads((Path(__file__).parent / "delegation_dataset.json").read_text(encoding="utf-8"))
THRESHOLD = 0.85


def test_fast_route_accuracy():
    ok = 0
    misses = []
    for case in DATA:
        routed = team.fast_route(case["q"])
        if set(case["expect"]) <= set(routed):
            ok += 1
        else:
            misses.append((case["q"], case["expect"], routed))
    accuracy = ok / len(DATA)
    print(f"\n[benchmark délégation] {ok}/{len(DATA)} = {accuracy:.0%}")
    assert accuracy >= THRESHOLD, f"précision {accuracy:.0%} < {THRESHOLD:.0%} ; ratés : {misses}"


def test_never_returns_unknown_or_too_many_agents():
    for case in DATA:
        routed = team.fast_route(case["q"])
        assert set(routed) <= set(team.AGENT_IDS)
        assert len(routed) <= team.MAX_AGENTS


def test_assessment_is_always_valid():
    a = build_assessment(["hacker", "sophie", "sophie", "alexandre", "natalie", "marc", "sarah"], "complex", "x")
    assert len(a.agents) <= team.MAX_AGENTS and a.agents[0] == "sophie"
    assert build_assessment([], "weird", "").agents == [team.DEFAULT_AGENT]
    assert build_assessment(["sophie", "marc"], "simple", "").complexity.value == "medium"


def test_dependencies_are_ordered_after_their_parent():
    a = build_assessment(["isabelle", "alexandre"], "medium", "")
    assert ("isabelle", "alexandre") in a.sequential_chain
    assert "isabelle" not in a.parallel_group and "alexandre" in a.parallel_group


def test_every_agent_has_a_real_prompt_and_frontend_id():
    seen = set()
    for s in team.TEAM.values():
        assert team.system_prompt(s.id) != team.system_prompt("inconnu"), s.id
        assert s.frontend_id not in seen
        seen.add(s.frontend_id)
