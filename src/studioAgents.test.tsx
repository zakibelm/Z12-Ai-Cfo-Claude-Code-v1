// Studio : l'accueil ne liste pas les spécialistes, seuls ceux à l'œuvre sont affichés,
// et aucune clé personnelle n'est exigée dans le navigateur.
import React from "react";
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";

vi.mock("@sentry/react", () => ({ init: () => {}, captureException: () => {}, ErrorBoundary: ({ children }: any) => children }));

import App from "../App";

(Element.prototype as any).scrollTo = () => {};

const PLAN = JSON.stringify({ type: "single", priority: "normal", agents: ["TaxAgent"], reason: "Question fiscale",
  user_message: "", estimated_seconds: 5, synthesis_needed: false });

function serve(chatCalls: any[]) {
  let chat = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: any, init?: any) => {
    const url = String(input);
    if (url.includes("/auth/local/me")) return { ok: true, status: 200, json: async () => ({ id: "21", email: "client@exemple.ca", name: "Client", is_admin: false }) } as any;
    if (url.includes("/api/chat")) {
      chatCalls.push(init);
      const content = chat++ === 0 ? PLAN : "La TPS est une taxe fédérale de 5 %.";
      return { ok: true, status: 200, json: async () => ({ choices: [{ message: { content } }] }) } as any;
    }
    if (url.includes("/api/budget")) return { ok: true, status: 200, json: async () => ({ unlimited: false, budget_cad: 3, spent_cad: 0, remaining_cad: 3, percent: 0, warning: false, blocked: false, reason: null }) } as any;
    return { ok: true, status: 200, json: async () => ({ documents: [], clients: [], unfiled: 0 }) } as any;
  }));
}

beforeEach(() => { localStorage.clear(); window.history.replaceState(null, "", "/"); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

async function openStudio() {
  const { container } = render(<App />);
  await waitFor(() => expect(container.querySelector(".app")).toBeTruthy());
  await screen.findByText(/Décrivez votre demande : je l'analyse/);
  return container;
}

describe("studio welcome", () => {
  it("greets in one sentence without listing the nine specialists", async () => {
    serve([]);
    const container = await openStudio();
    const thread = container.querySelector(".orch-card")!.textContent || "";
    expect(thread).not.toMatch(/9 spécialistes|Sophie|Alexandre|Patrick/);
    expect(container.querySelectorAll('[data-testid="active-roster"] .agent-row')).toHaveLength(0);
    expect(screen.queryByText("Équipe CPA virtuelle")).toBeNull();           // plus de liste permanente dans le menu
    expect(screen.queryByText(/Configurer votre clé OpenRouter/)).toBeNull();   // la clé est celle de la plateforme
  });

  it("replaces the old welcome when a saved conversation is reopened", async () => {
    const old = "🏢 **Orchestrateur — Bureau CPA Virtuel**\n\nBonjour ! Je coordonne une Équipe de **9 spécialistes CPA** à votre service :\n\n👩 **Sophie** — Fiscaliste";
    localStorage.setItem("z12-conversations", JSON.stringify([{ id: "cv_1", title: "Ancienne discussion TPS", agentId: "TaxAgent", createdAt: 1, updatedAt: 1,
      messages: [{ role: "assistant", isOrchestrator: true, content: old, ts: 1 },
                 { role: "user", content: "Question d'avant", ts: 2 },
                 { role: "assistant", content: "Réponse d'avant", agentId: "TaxAgent", ts: 3 }] }]));
    serve([]);
    const container = await openStudio();
    fireEvent.click(await screen.findByText("Ancienne discussion TPS"));
    await screen.findByText("Réponse d'avant");
    expect(container.textContent).not.toMatch(/9 spécialistes CPA/);
    expect(screen.getByText(/Décrivez votre demande : je l'analyse/)).toBeTruthy();
  });
});

describe("specialists at work", () => {
  it("shows only the assigned specialist, and needs no personal key", async () => {
    const chatCalls: any[] = [];
    serve(chatCalls);
    const container = await openStudio();
    const input = container.querySelector("textarea") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "Qu'est-ce que la TPS ?" } });
    fireEvent.click(container.querySelector(".send-btn") as HTMLElement);

    await screen.findByText(/taxe fédérale de 5 %/);
    expect(screen.queryByText(/Clé API OpenRouter manquante/)).toBeNull();
    expect(chatCalls.length).toBeGreaterThanOrEqual(2);
    for (const call of chatCalls) expect(call.headers["X-API-Key"]).toBeUndefined();

    const rows = Array.from(container.querySelectorAll('[data-testid="active-roster"] .agent-row'));
    expect(rows.map(r => r.getAttribute("title"))).toEqual(["Sophie Mercier"]);
    expect(screen.getByText("Spécialistes sur votre demande")).toBeTruthy();
  });
});
