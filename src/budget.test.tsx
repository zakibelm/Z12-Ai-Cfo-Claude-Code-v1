import React from "react";
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { BudgetBadge, money, refreshBudget } from "./components/BudgetBadge";
import { BudgetsPanel } from "./components/BudgetsPanel";
import { callOpenRouter } from "./utils/workflow";
import { MyUsagePanel } from "./components/SettingsView";

beforeEach(() => { localStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const base = { currency: "CAD", unlimited: false, budget_cad: 3, spent_cad: 1.2, remaining_cad: 1.8, percent: 40, warning: false, blocked: false, reason: null };
function serve(budget: any) {
  const state = { budget };
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => state.budget }) as any));
  return state;
}

describe("trial budget indicator", () => {
  it("formats amounts in Canadian dollars", () => {
    expect(money(1.2)).toBe("1,20 $");
    expect(money(null)).toBe("0,00 $");
    expect(money(3, "en")).toBe("$3.00");
  });

  it("shows spent over granted, and follows each answer", async () => {
    const state = serve(base);
    render(<BudgetBadge lang="fr" />);
    expect((await screen.findByTestId("budget-badge")).textContent).toBe("Essai : 1,20 $ / 3,00 $");
    state.budget = { ...base, spent_cad: 2.6, remaining_cad: 0.4, percent: 87, warning: true };
    refreshBudget();
    await waitFor(() => expect(screen.getByTestId("budget-badge").textContent).toBe("Essai : 2,60 $ / 3,00 $"));
    expect(screen.getByRole("status").getAttribute("title")).toContain("0,40 $");
  });

  it("says clearly when the trial is used up or the platform is paused", async () => {
    serve({ ...base, spent_cad: 3.1, remaining_cad: 0, percent: 100, blocked: true, reason: "account" });
    render(<BudgetBadge lang="fr" />);
    expect((await screen.findByTestId("budget-badge")).textContent).toContain("Budget d'essai épuisé (3,00 $)");
    cleanup();
    serve({ ...base, unlimited: true, budget_cad: null, blocked: true, reason: "platform" });
    render(<BudgetBadge lang="fr" />);
    expect((await screen.findByTestId("budget-badge")).textContent).toContain("budget de la plateforme atteint");
  });

  it("shows nothing for an account without a limit", async () => {
    const mock = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ ...base, unlimited: true, budget_cad: null }) }) as any);
    vi.stubGlobal("fetch", mock);
    render(<BudgetBadge lang="fr" />);
    await waitFor(() => expect(mock).toHaveBeenCalled());
    expect(screen.queryByTestId("budget-badge")).toBeNull();
  });
});

describe("refused requests", () => {
  it("show the server's explanation instead of a bare error code", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 402,
      json: async () => ({ detail: "Votre budget d'essai de 3,00 $ est épuisé. Contactez-nous pour poursuivre." }) }) as any));
    await expect(callOpenRouter("m", "s", [{ role: "user", content: "q" }], "")).rejects.toThrow("budget d'essai de 3,00 $");
  });
});

describe("administrator budgets panel", () => {
  const overview = {
    default_budget_cad: 3, usd_to_cad: 1.4,
    platform: { month_spent_cad: 12.5, cap_cad: 150, blocked: false },
    accounts: [
      { user_id: "9", email: "admin@exemple.ca", is_admin: true, calls: 40, unlimited: true, budget_cad: null, spent_cad: 9, percent: 0, warning: false, blocked: false },
      { user_id: "21", email: "client@exemple.ca", is_admin: false, calls: 12, unlimited: false, budget_cad: 3, spent_cad: 3.5, percent: 100, warning: false, blocked: true },
    ],
  };

  it("lists real spending per account and the platform total", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => overview }) as any));
    render(<BudgetsPanel lang="fr" />);
    expect(await screen.findByText("client@exemple.ca")).toBeTruthy();
    expect(screen.getByText("3,50 $ / 3,00 $ (100 %)")).toBeTruthy();
    expect(screen.getByText("admin@exemple.ca (administrateur)")).toBeTruthy();
    expect(screen.getByText("9,00 $ · sans limite")).toBeTruthy();
    expect(screen.getByTestId("budgets-panel").textContent).toContain("12,50 $ / 150,00 $");
    expect(screen.getAllByText("Fixer")).toHaveLength(1); // pas de plafond à régler pour l'administrateur
  });

  it("raises one account's budget, accepting a comma as decimal separator", async () => {
    const calls: any[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: any, init?: any) => {
      calls.push({ url: String(url), init });
      return { ok: true, status: 200, json: async () => overview } as any;
    }));
    render(<BudgetsPanel lang="fr" />);
    fireEvent.change(await screen.findByLabelText("Budget de client@exemple.ca"), { target: { value: "10,50" } });
    fireEvent.click(screen.getByText("Fixer"));
    await waitFor(() => expect(calls.some(c => c.init?.method === "PUT")).toBe(true));
    const put = calls.find(c => c.init?.method === "PUT");
    expect(put.url).toBe("/api/admin/budgets/21");
    expect(JSON.parse(put.init.body)).toEqual({ budget_cad: 10.5 });
  });

  it("refuses an invalid amount without calling the server", async () => {
    const mock = vi.fn(async () => ({ ok: true, status: 200, json: async () => overview }) as any);
    vi.stubGlobal("fetch", mock);
    render(<BudgetsPanel lang="fr" />);
    fireEvent.change(await screen.findByLabelText("Budget de client@exemple.ca"), { target: { value: "abc" } });
    fireEvent.click(screen.getByText("Fixer"));
    expect((await screen.findByRole("alert")).textContent).toBe("Montant invalide");
    expect(mock).toHaveBeenCalledTimes(1); // seulement le chargement initial
  });
});

describe("my usage (ordinary user)", () => {
  it("shows what the account spent, its budget and what is left", async () => {
    serve(base);
    render(<MyUsagePanel lang="fr" />);
    expect((await screen.findByTestId("usage-spent")).textContent).toBe("1,20 $");
    expect(screen.getByTestId("usage-budget").textContent).toBe("3,00 $");
    expect(screen.getByTestId("usage-remaining").textContent).toBe("1,80 $");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("warns near the limit and says clearly when the trial is used up", async () => {
    const state = serve({ ...base, spent_cad: 2.6, remaining_cad: 0.4, percent: 87, warning: true });
    render(<MyUsagePanel lang="fr" />);
    expect((await screen.findByRole("status")).textContent).toContain("Il vous reste 0,40 $");
    state.budget = { ...base, spent_cad: 3.1, remaining_cad: 0, percent: 100, blocked: true, reason: "account" };
    refreshBudget();
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("budget d'essai est épuisé"));
  });

  it("does not reveal the platform budget when the platform is paused", async () => {
    serve({ ...base, blocked: true, reason: "platform" });
    render(<MyUsagePanel lang="fr" />);
    const text = (await screen.findByTestId("my-usage-panel")).textContent || "";
    expect(text).toContain("momentanément suspendu");
    expect(text).not.toMatch(/150|plateforme/);
  });
});
