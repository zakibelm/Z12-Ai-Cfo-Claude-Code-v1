// Appels au serveur : nature de l'appel, longueur des réponses, cache, plans JSON.
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { analyzeWorkflow, callOpenRouter, executeWorkflow, looksLikeRoutingPlan, synthesizeResults } from "./utils/workflow";

const PLAN = '{"type":"single","priority":"high","agents":["TaxAgent"],"phases":[],"reason":"Reprise de l\'analyse du cas fiscal."}';
const reply = (content: any, extra: any = {}) => ({ ok: true, status: 200, json: async () => ({ choices: [{ message: { content }, ...extra }] }) }) as any;

function serve(answers: any[]) {
  const bodies: any[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: any, init?: any) => {
    if (!String(url).includes("/api/chat")) return { ok: true, status: 200, json: async () => ({}) } as any;
    bodies.push(JSON.parse(init.body));
    return answers[Math.min(bodies.length - 1, answers.length - 1)];
  }));
  return bodies;
}

beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe("call purpose and answer length", () => {
  it("gives specialists room for a full analysis and marks orchestrator calls", async () => {
    const bodies = serve([reply(PLAN), reply("Analyse."), reply("Synthèse.")]);
    await analyzeWorkflow("Analyse le cas", [{ role: "user", content: "Analyse le cas" }], "fr", "");
    await callOpenRouter("deepseek/deepseek-v4-pro", "Tu es un CPA.", [{ role: "user", content: "q" }], "");
    await synthesizeResults([{ name: "Sophie", title: "Fiscaliste", reply: "a" }, { name: "Marc", title: "Analyste", reply: "b" }], "q", {}, "fr", "", {});
    expect(bodies.map(b => [b.purpose, b.max_tokens])).toEqual([["routing", 1500], ["agent", 12000], ["synthesis", 8000]]);
  });

  it("sends only the start of an attached file to the orchestrator", async () => {
    const bodies = serve([reply(PLAN)]);
    const history = [{ role: "user", content: "[Pièce jointe « cas.pdf »]\n" + "x".repeat(20000) + "\n[Fin de « cas.pdf »]\n\nAnalyse", display: "Analyse", files: [{ name: "cas.pdf" }] }];
    await analyzeWorkflow("Analyse", history, "fr", "");
    const sent = bodies[0].messages.map((m: any) => m.content).join("");
    expect(sent.length).toBeLessThan(12000);                               // le prompt de l'orchestrateur + un extrait
    expect(sent).toContain("[Pièce jointe « cas.pdf »]");
  });

  it("says when an answer was cut by the length limit", async () => {
    serve([reply("Début de l'analyse", { finish_reason: "length" })]);
    const text = await callOpenRouter("m", "s", [{ role: "user", content: "q" }], "");
    expect(text).toContain("Début de l'analyse");
    expect(text).toContain("Écrivez « continue »");
  });
});

describe("answers the interface used to show as « Erreur inattendue. »", () => {
  it("reads an answer served from the server cache", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ content: "Réponse du cache.", cached: true }) }) as any));
    expect(await callOpenRouter("m", "s", [{ role: "user", content: "oui" }], "")).toBe("Réponse du cache.");
  });

  it("explains an empty answer instead of a bare error", async () => {
    serve([reply("")]);
    await expect(callOpenRouter("m", "s", [{ role: "user", content: "q" }], "")).rejects.toThrow("n'a renvoyé aucun texte");
    serve([reply(null, { finish_reason: "length" })]);
    await expect(callOpenRouter("m", "s", [{ role: "user", content: "q" }], "")).rejects.toThrow("limite de longueur");
  });
});

describe("a specialist never answers with a routing plan", () => {
  it("recognises a plan, fenced or not", () => {
    expect(looksLikeRoutingPlan(PLAN)).toBe(true);
    expect(looksLikeRoutingPlan("```json\n" + PLAN + "\n```")).toBe(true);
    expect(looksLikeRoutingPlan("La TPS est de 5 %.")).toBe(false);
    expect(looksLikeRoutingPlan('{"taux": 0.05}')).toBe(false);
  });

  it("asks again once, then shows the written answer", async () => {
    const bodies = serve([reply(PLAN), reply("Le revenu imposable de M. Laporte-Paquette est de 84 300 $.")]);
    const [result] = await executeWorkflow({ type: "single", agents: ["TaxAgent"] }, "reprends l'analyse",
      [{ role: "user", content: "reprends l'analyse" }], {}, "", "fr", () => {});
    expect(result.reply).toContain("84 300 $");
    expect(bodies).toHaveLength(2);
    expect(bodies[1].messages.at(-1).content).toContain("Ne renvoie pas de JSON");
  });

  it("reports a clear error if the specialist insists", async () => {
    serve([reply(PLAN)]);
    const [result] = await executeWorkflow({ type: "single", agents: ["TaxAgent"] }, "q", [{ role: "user", content: "q" }], {}, "", "fr", () => {});
    expect(result.status).toBe("error");
    expect(result.reply).toContain("n'a pas produit de réponse rédigée");
    expect(result.reply).not.toContain('"agents"');
  });

  it("uses the model set for the agent even without a browser key", async () => {
    const bodies = serve([reply("ok")]);
    await executeWorkflow({ type: "single", agents: ["TaxAgent"] }, "q", [{ role: "user", content: "q" }],
      { TaxAgent: { model: "deepseek/deepseek-r1-0528" } }, "", "fr", () => {});
    expect(bodies[0].model).toBe("deepseek/deepseek-r1-0528");
  });
});
