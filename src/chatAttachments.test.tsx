// Pièces jointes du chat : le contenu des fichiers arrive aux spécialistes.
import React from "react";
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";

vi.mock("@sentry/react", () => ({ init: () => {}, captureException: () => {}, ErrorBoundary: ({ children }: any) => children }));

import App from "../App";
import { allocate, attachmentsBlock, fitHistory, readAttachments, ATTACHMENT_BUDGET } from "./utils/attachments";

(Element.prototype as any).scrollTo = () => {};

const att = (name: string, length: number, error: string | null = null) =>
  ({ name, text: error ? "" : "x".repeat(length), chars: length, truncated: false, error });

beforeEach(() => { localStorage.clear(); window.history.replaceState(null, "", "/"); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("attachment text budget", () => {
  it("keeps small files whole and cuts only the large ones", () => {
    const [small, large, failed] = allocate([att("a.xlsx", 2000), att("b.pdf", 90000), att("c.docx", 0, "illisible")], 40000);
    expect(small.text.length).toBe(2000);
    expect(small.truncated).toBe(false);
    expect(large.text.length).toBe(38000);
    expect(large.truncated).toBe(true);
    expect(failed.error).toBe("illisible");
  });

  it("tells the specialists what was read, what was cut and what could not be read", () => {
    const block = attachmentsBlock(allocate([att("cas.pdf", 90000), att("T4.docx", 0, "aucun texte lisible")], 1000))
      .replace(/[\u202f\u00a0]/g, " ");                                   // séparateur de milliers du français
    expect(block).toContain("[Pièce jointe « cas.pdf » — extrait : 1 000 caractères sur 90 000, la suite n'est pas fournie]");
    expect(block).toContain("[Fin de « cas.pdf »]");
    expect(block).toContain("[Pièce jointe « T4.docx » : non lue — aucun texte lisible]");
    expect(block).not.toContain("Indexez via Documents");
  });

  it("keeps the submitted case in context after several exchanges", () => {
    const history: any[] = [{ role: "user", content: "CAS " + "x".repeat(500) + "\nAnalysez", display: "Analysez", files: [{ name: "cas.pdf" }] }];
    for (let i = 0; i < 5; i++) history.push({ role: "assistant", content: `réponse ${i}` }, { role: "user", content: `question ${i}` });
    const kept = fitHistory(history, 6);
    expect(kept).toHaveLength(7);
    expect(kept[0].files[0].name).toBe("cas.pdf");                        // régression : le cas sortait du contexte au 4e échange
    expect(kept[6].content).toBe("question 4");
  });

  it("drops the oldest attachment text rather than exceed the server limit", () => {
    const history = [
      { role: "user", content: "A".repeat(70000), display: "premier envoi", files: [{ name: "vieux.pdf" }] },
      { role: "assistant", content: "ok" },
      { role: "user", content: "B".repeat(40000), display: "second envoi", files: [{ name: "neuf.xlsx" }] },
    ];
    const kept = fitHistory(history, 6, 100000);
    expect(kept[0].content).toBe("[Pièces jointes retirées du contexte, trop volumineuses : vieux.pdf]\npremier envoi");
    expect(kept[2].content.length).toBe(40000);
  });
});

describe("reading attachments", () => {
  it("reports a refused upload on every file instead of pretending", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 413, json: async () => ({}) }) as any));
    const [a, b] = await readAttachments([new File(["x"], "a.pdf"), new File(["x"], "b.docx")]);
    expect(a.error).toContain("trop volumineux pour le chat");
    expect(b.error).toBe(a.error);
  });

  it("reads plain text files in the browser, without the server", async () => {
    const mock = vi.fn();
    vi.stubGlobal("fetch", mock);
    const [notes] = await readAttachments([new File(["Ventes : 184 500 $"], "notes.txt", { type: "text/plain" })]);
    expect(notes.text).toBe("Ventes : 184 500 $");
    expect(mock).not.toHaveBeenCalled();
  });
});

describe("studio with attached files", () => {
  const CASE_TEXT = "Boulangerie Lévesque inc. — Question 1 : calculez la TPS et la TVQ à remettre.";
  const PLAN = JSON.stringify({ type: "single", priority: "normal", agents: ["TaxAgent"], reason: "Fiscalité", user_message: "", synthesis_needed: false });

  function serve(chat: any[], extract: any[]) {
    vi.stubGlobal("fetch", vi.fn(async (input: any, init?: any) => {
      const url = String(input);
      if (url.includes("/auth/local/me")) return { ok: true, status: 200, json: async () => ({ id: "21", email: "client@exemple.ca", is_admin: false }) } as any;
      if (url.includes("/api/attachments/extract")) {
        extract.push(init.body);
        return { ok: true, status: 200, json: async () => ({ files: [
          { filename: "Etude de cas.pdf", text: CASE_TEXT, chars: CASE_TEXT.length, truncated: false, error: null },
          { filename: "Mon travail .xlsx", text: "| Poste | Montant |\n| TPS perçue | 9225 |", chars: 40, truncated: false, error: null },
          { filename: "T4.docx", text: "", chars: 0, truncated: false, error: "aucun texte lisible" },
        ] }) } as any;
      }
      if (url.includes("/api/chat")) {
        chat.push(JSON.parse(init.body));
        const content = chat.length === 1 ? PLAN : "La TPS à remettre est de 4 415 $.";
        return { ok: true, status: 200, json: async () => ({ choices: [{ message: { content } }] }) } as any;
      }
      if (url.includes("/api/budget")) return { ok: true, status: 200, json: async () => ({ unlimited: true, blocked: false }) } as any;
      return { ok: true, status: 200, json: async () => ({ documents: [], clients: [], unfiled: 0 }) } as any;
    }));
  }

  it("sends the files' content to the specialist and shows only their names", async () => {
    const chat: any[] = [], extract: FormData[] = [];
    serve(chat, extract);
    const { container } = render(<App />);
    await screen.findByText(/Décrivez votre demande : je l'analyse/);
    const files = [new File(["%PDF"], "Etude de cas.pdf"), new File(["PK"], "Mon travail .xlsx"), new File(["PK"], "T4.docx")];
    fireEvent.change(screen.getByTestId("attach-input"), { target: { files } });
    const chip = await screen.findByTestId("attach-chip");
    expect(chip.textContent).toContain("3 fichiers — 1 non lu");
    expect(extract[0].getAll("files")).toHaveLength(3);

    fireEvent.change(container.querySelector("textarea")!, { target: { value: "analyse et répondre aux questions" } });
    fireEvent.click(container.querySelector(".send-btn") as HTMLElement);
    await screen.findByText(/4 415 \$/);

    // Le spécialiste (2e appel) reçoit le texte du cas et du classeur, une seule fois.
    const sent = chat[1].messages.map((m: any) => m.content).join("\n");
    expect(sent).toContain(CASE_TEXT);
    expect(sent).toContain("TPS perçue | 9225");
    expect(sent).toContain("[Pièce jointe « T4.docx » : non lue — aucun texte lisible]");
    expect(sent).not.toContain("Indexez via Documents");
    expect(sent.split(CASE_TEXT)).toHaveLength(2);
    expect(chat[1].messages.filter((m: any) => m.role === "user" && m.content.endsWith("analyse et répondre aux questions"))).toHaveLength(1);

    // La bulle affiche les noms et l'état de lecture, pas le contenu des fichiers.
    const bubble = container.querySelector(".msg-user-bubble")!;
    expect(bubble.textContent).toContain("Etude de cas.pdf");
    expect(bubble.textContent).toContain("T4.docx — non lu : aucun texte lisible");
    expect(bubble.textContent).toContain("analyse et répondre aux questions");
    expect(bubble.textContent).not.toContain("Boulangerie Lévesque");
    expect(screen.queryByTestId("attach-chip")).toBeNull();               // pièces jointes consommées
  });

  it("leaves room for the question within the per-message budget", () => {
    expect(ATTACHMENT_BUDGET).toBeLessThanOrEqual(40000);
  });
});
