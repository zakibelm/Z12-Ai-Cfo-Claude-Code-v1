// Porte d'entree : presentation -> connexion / inscription -> Studio.
import React from "react";
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";

vi.mock("@sentry/react", () => ({ init: () => {}, captureException: () => {}, ErrorBoundary: ({ children }: any) => children }));
const goToLanding = vi.fn();
vi.mock("./utils/nav", async (orig) => ({ ...(await orig<any>()), goToLanding: () => goToLanding() }));

import App from "../App";
import { authRouteFromPath } from "./utils/nav";

// jsdom n'implemente pas scrollTo.
(Element.prototype as any).scrollTo = () => {};

function mockFetch(session: { ok: boolean; body?: unknown }, calls: any[] = []) {
  let signedIn = session.ok;
  return vi.fn(async (input: any, init?: any) => {
    const url = String(input);
    calls.push({ url, init });
    if (url.includes("/auth/local/me")) {
      return { ok: signedIn, status: signedIn ? 200 : 401, json: async () => session.body ?? { id: "9", email: "nouveau@exemple.ca" } } as any;
    }
    if (url.includes("/auth/local/register") || url.includes("/auth/local/login")) {
      signedIn = true;
      return { ok: true, status: 200, json: async () => ({ ok: true }) } as any;
    }
    return { ok: false, status: 401, json: async () => ({}), text: async () => "" } as any;
  });
}

beforeEach(() => { goToLanding.mockClear(); window.history.replaceState(null, "", "/"); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("auth routes", () => {
  it("recognises /login and /register only", () => {
    expect(authRouteFromPath("/login")).toBe("login");
    expect(authRouteFromPath("/register/")).toBe("register");
    expect(authRouteFromPath("/")).toBeNull();
    expect(authRouteFromPath("/landing.html")).toBeNull();
  });
});

describe("entry flow", () => {
  it("sends a visitor without a session to the landing page, never the Studio", async () => {
    vi.stubGlobal("fetch", mockFetch({ ok: false }));
    render(<App />);
    await waitFor(() => expect(goToLanding).toHaveBeenCalledTimes(1));
    expect(document.querySelector(".roster-foot")).toBeNull();
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  it("shows the sign-in form on /login", async () => {
    window.history.replaceState(null, "", "/login");
    vi.stubGlobal("fetch", mockFetch({ ok: false }));
    render(<App />);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Connexion" })).toBeTruthy());
    expect(goToLanding).not.toHaveBeenCalled();
    expect(document.querySelector(".roster-foot")).toBeNull();
  });

  it("shows the sign-up form on /register and opens the Studio after registering", async () => {
    window.history.replaceState(null, "", "/register");
    const calls: any[] = [];
    vi.stubGlobal("fetch", mockFetch({ ok: false }, calls));
    render(<App />);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Créer un compte" })).toBeTruthy());

    fireEvent.change(screen.getByLabelText("Adresse courriel"), { target: { value: "nouveau@exemple.ca" } });
    fireEvent.change(screen.getByLabelText("Mot de passe"), { target: { value: "court" } });
    fireEvent.click(screen.getByRole("button", { name: "Créer mon compte" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("10 caractères"));
    expect(calls.some(c => c.url.includes("/register"))).toBe(false);

    // Inscription sur invitation : sans code, rien n'est envoyé au serveur.
    fireEvent.change(screen.getByLabelText("Mot de passe"), { target: { value: "motdepasse-solide" } });
    fireEvent.click(screen.getByRole("button", { name: "Créer mon compte" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("code d'invitation"));
    expect(calls.some(c => c.url.endsWith("/auth/local/register"))).toBe(false);

    fireEvent.change(screen.getByLabelText("Code d'invitation"), { target: { value: " abcd-efgh-jklm " } });
    fireEvent.click(screen.getByRole("button", { name: "Créer mon compte" }));
    await waitFor(() => expect(document.querySelector(".roster-foot")).not.toBeNull());
    expect(window.location.pathname).toBe("/");
    const sent = calls.find(c => c.url.endsWith("/auth/local/register"));
    expect(JSON.parse(sent.init.body)).toEqual({ email: "nouveau@exemple.ca", password: "motdepasse-solide", invite_code: "abcd-efgh-jklm" });
  });

  it("fills the code from an invitation link", async () => {
    window.history.replaceState(null, "", "/register?code=WXYZ-2345-ABCD");
    vi.stubGlobal("fetch", mockFetch({ ok: false }));
    render(<App />);
    const field = (await screen.findByLabelText("Code d'invitation")) as HTMLInputElement;
    expect(field.value).toBe("WXYZ-2345-ABCD");
  });

  it("asks for no code when the server allows open registration", async () => {
    window.history.replaceState(null, "", "/register");
    const calls: any[] = [];
    const base = mockFetch({ ok: false }, calls);
    vi.stubGlobal("fetch", vi.fn(async (input: any, init?: any) => {
      if (String(input).includes("/registration-mode")) return { ok: true, status: 200, json: async () => ({ mode: "open" }) } as any;
      return base(input, init);
    }));
    render(<App />);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Créer un compte" })).toBeTruthy());
    await waitFor(() => expect(screen.queryByLabelText("Code d'invitation")).toBeNull());
    fireEvent.change(screen.getByLabelText("Adresse courriel"), { target: { value: "libre@exemple.ca" } });
    fireEvent.change(screen.getByLabelText("Mot de passe"), { target: { value: "motdepasse-solide" } });
    fireEvent.click(screen.getByRole("button", { name: "Créer mon compte" }));
    await waitFor(() => expect(document.querySelector(".roster-foot")).not.toBeNull());
    const sent = calls.find(c => c.url.endsWith("/auth/local/register"));
    expect(JSON.parse(sent.init.body)).toEqual({ email: "libre@exemple.ca", password: "motdepasse-solide" });
  });

  it("opens the Studio for a valid session and shows the session user", async () => {
    vi.stubGlobal("fetch", mockFetch({ ok: true, body: { id: "5", email: "admin@cfo.local", name: "Admin CFO" } }));
    render(<App />);
    await waitFor(() => expect(document.querySelector(".roster-foot")).not.toBeNull());
    expect(goToLanding).not.toHaveBeenCalled();
    expect(document.querySelector(".user-dot")?.textContent).toBe("AC");
    expect(document.body.textContent).not.toContain("Belmeddah Zakaria");
  });
});

describe("business knowledge (RAG métier) visibility", () => {
  async function openDocuments(isAdmin: boolean) {
    vi.stubGlobal("fetch", mockFetch({ ok: true, body: { id: "9", email: "u@exemple.ca", name: "U", is_admin: isAdmin } }));
    const { container } = render(<App />);
    await waitFor(() => expect(container.querySelector(".app")).toBeTruthy());
    const nav = Array.from(container.querySelectorAll("button, a, [role=button], .nav-item, li, div"))
      .find((el) => el.textContent?.trim() === "Documents" || el.getAttribute("title") === "Documents") as HTMLElement;
    expect(nav).toBeTruthy();
    fireEvent.click(nav);
    await screen.findByText(/RAG client — dossiers clients/);
  }

  it("is hidden from an ordinary user, who only gets client folders", async () => {
    await openDocuments(false);
    expect(screen.queryByText(/RAG métier — sources de connaissance/)).toBeNull();
    expect(screen.queryByText(/sources métier/i)).toBeNull();
    expect(screen.getByTestId("client-folders")).toBeTruthy();
  });

  it("is shown to the administrator", async () => {
    await openDocuments(true);
    expect(screen.getByText(/RAG métier — sources de connaissance/)).toBeTruthy();
  });
});

describe("administrator status stays up to date", () => {
  it("shows the administrator label and the tab once the server grants the right, without reloading", async () => {
    // Page chargée alors que le serveur ne connaissait pas encore le statut ; il le renvoie ensuite.
    localStorage.clear(); // on part du Studio, pas de l'écran Documents mémorisé par un test précédent
    let admin = false;
    vi.stubGlobal("fetch", vi.fn(async (url: any) => {
      const path = String(url);
      if (path.includes("/auth/local/me")) {
        return { ok: true, status: 200, json: async () => ({ id: "9", email: "u@exemple.ca", name: "U", is_admin: admin }) } as any;
      }
      return { ok: true, status: 200, json: async () => ({ documents: [], clients: [], unfiled: 0 }) } as any;
    }));
    const { container } = render(<App />);
    await waitFor(() => expect(container.querySelector(".app")).toBeTruthy());
    expect(screen.queryByTestId("admin-badge")).toBeNull();
    admin = true;
    const nav = Array.from(container.querySelectorAll("button, a, [role=button], .nav-item, li, div"))
      .find((el) => el.textContent?.trim() === "Documents" || el.getAttribute("title") === "Documents") as HTMLElement;
    fireEvent.click(nav);
    expect(await screen.findByText(/RAG métier — sources de connaissance/)).toBeTruthy();
    expect(await screen.findByTestId("admin-badge")).toBeTruthy();
  });
});

describe("settings view", () => {
  // Régression : l'écran Paramètres plantait au rendu (« auth is not defined »).
  async function openSettings(isAdmin: boolean) {
    localStorage.clear();
    vi.stubGlobal("fetch", vi.fn(async (url: any) => {
      const path = String(url);
      if (path.includes("/auth/local/me")) return { ok: true, status: 200, json: async () => ({ id: "9", email: "u@exemple.ca", name: "U", is_admin: isAdmin }) } as any;
      if (path.includes("/api/admin/invitations")) return { ok: true, status: 200, json: async () => ({ mode: "invite", invitations: [] }) } as any;
      if (path.includes("/api/admin/budgets")) return { ok: true, status: 200, json: async () => ({ accounts: [], default_budget_cad: 3, platform: { month_spent_cad: 0, cap_cad: 150, blocked: false } }) } as any;
      return { ok: true, status: 200, json: async () => ({ documents: [], clients: [], unfiled: 0 }) } as any;
    }));
    const { container } = render(<App />);
    await waitFor(() => expect(container.querySelector(".app")).toBeTruthy());
    const labels = ["Paramètres", "Réglages", "Settings"];
    const nav = Array.from(container.querySelectorAll("button, a, [role=button], .nav-item, li, div"))
      .find((el) => labels.includes(el.textContent?.trim() || "") || labels.includes(el.getAttribute("title") || "")) as HTMLElement;
    expect(nav).toBeTruthy();
    fireEvent.click(nav);
    return container;
  }

  it("opens for the administrator on the invitations tab, with budgets one tab away", async () => {
    await openSettings(true);
    expect(await screen.findByTestId("invitations-panel")).toBeTruthy();
    expect(screen.queryByTestId("budgets-panel")).toBeNull();      // une seule section à l'écran à la fois
    fireEvent.click(screen.getByTestId("settings-tab-budgets"));
    expect(await screen.findByTestId("budgets-panel")).toBeTruthy();
    expect(screen.queryByTestId("invitations-panel")).toBeNull();
  });

  // Régression : les panneaux d'administration étaient rendus à côté de la page, comme cellules
  // de la grille de l'application ; ils recouvraient le menu et le titre « Paramètres ».
  it("keeps the administrator panels inside the settings page", async () => {
    const container = await openSettings(true);
    const panel = await screen.findByTestId("invitations-panel");
    const page = container.querySelector('main[data-screen-label="Settings"]') as HTMLElement;
    expect(page.contains(panel)).toBe(true);
    expect(page.querySelector(".page-body")!.contains(panel)).toBe(true);
    const app = container.querySelector(".app") as HTMLElement;
    expect(Array.from(app.children).filter(el => el.tagName !== "STYLE")).toHaveLength(2);   // menu + page
    fireEvent.click(screen.getByTestId("settings-tab-budgets"));
    expect(page.contains(await screen.findByTestId("budgets-panel"))).toBe(true);
  });

  it("shows the real model names and readable text", async () => {
    const container = await openSettings(true);
    fireEvent.click(await screen.findByTestId("settings-tab-models"));
    const selects = Array.from(container.querySelectorAll("select.set-select")) as HTMLSelectElement[];
    expect(selects.length).toBeGreaterThan(0);
    for (const select of selects) {
      const values = Array.from(select.options).map(o => o.value);
      expect(new Set(values).size).toBe(values.length);                       // aucun modèle en double
      expect(select.value).toBe("deepseek/deepseek-v4-pro");
    }
    const text = container.querySelector('main[data-screen-label="Settings"]')!.textContent || "";
    expect(text).not.toMatch(/Claude Sonnet 4\.5 par défaut/);              // le défaut réel est DeepSeek
    expect(text).not.toMatch(/ à \d+ (modèles|fournisseurs)/);              // ancien texte mal encodé
    fireEvent.click(screen.getByTestId("settings-tab-general"));
    expect(screen.getByText("Tester la connexion")).toBeTruthy();
  });

  // Un utilisateur ne voit que sa propre consommation ; le reste est réservé à l'administration.
  it("shows an ordinary user their own usage and nothing else", async () => {
    const container = await openSettings(false);
    expect(await screen.findByTestId("my-usage-panel")).toBeTruthy();
    for (const hidden of ["invitations-panel", "budgets-panel", "settings-tab-access", "settings-tab-budgets",
                          "settings-tab-models", "settings-tab-general"]) {
      expect(screen.queryByTestId(hidden)).toBeNull();
    }
    const page = container.querySelector('main[data-screen-label="Settings"]') as HTMLElement;
    expect(page.querySelectorAll("select, input")).toHaveLength(0);          // ni modèles, ni clé
    expect(page.textContent).not.toMatch(/OpenRouter|deepseek/i);
    expect(screen.queryByText(/Erreur de démarrage/)).toBeNull();
    const asked = (globalThis.fetch as any).mock.calls.map((c: any[]) => String(c[0]));
    expect(asked.some((u: string) => u.includes("/api/admin/"))).toBe(false); // aucune route d'administration appelée
  });

  it("does not show the administrator a personal usage tab", async () => {
    await openSettings(true);
    await screen.findByTestId("invitations-panel");
    expect(screen.queryByTestId("settings-tab-usage")).toBeNull();
  });
});
