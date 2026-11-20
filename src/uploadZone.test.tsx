// Dépôt de documents : bouton « Enregistrer », un fichier par requête, résultat réel affiché.
import React from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { UploadZone, refusalReason, failureMessage, fileSize } from "./components/UploadZone";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const file = (name: string, size = 2048) => {
  const f = new File(["x"], name);
  Object.defineProperty(f, "size", { value: size });
  return f;
};
const choose = (...files: File[]) => fireEvent.change(screen.getByTestId("upload-input"), { target: { files } });
const rows = () => screen.getAllByTestId("upload-row");

function server(answers: Array<{ status?: number; body?: any } | "network">, statuses: any[] = []) {
  const calls: FormData[] = [];
  let polls = 0;
  vi.stubGlobal("XMLHttpRequest", undefined);                               // envoi par fetch dans ces tests
  vi.stubGlobal("fetch", vi.fn(async (url: any, init?: any) => {
    if (init?.method !== "POST") {                                          // relecture de l'état d'un document
      const doc = statuses[Math.min(polls++, statuses.length - 1)];
      return { ok: !!doc, status: doc ? 200 : 404, json: async () => doc ?? {} } as any;
    }
    calls.push(init.body);
    const answer = answers[Math.min(calls.length - 1, answers.length - 1)];
    if (answer === "network") throw new TypeError("Failed to fetch");
    const status = answer.status ?? 200;
    return { ok: status < 400, status, json: async () => answer.body ?? {} } as any;
  }));
  return calls;
}
const indexed = (chunks = 12) => ({ body: { documents: [{ doc_id: "d1", status: "indexed", chunks_count: chunks }] } });

describe("upload rules", () => {
  it("refuses file types and sizes the server would reject, before sending", () => {
    expect(refusalReason({ name: "bilan.pdf", size: 1000 })).toBe("");
    expect(refusalReason({ name: "video.mp4", size: 1000 })).toContain(".mp4");
    expect(refusalReason({ name: "archive.zip", size: 1000 })).toContain("non accepté");
    expect(refusalReason({ name: "gros.pdf", size: 145 * 1048576 })).toBe("");          // vos fichiers de 62 à 145 Mo passent
    expect(refusalReason({ name: "enorme.pdf", size: 501 * 1048576 })).toContain("maximum 500 Mo");
    expect(refusalReason({ name: "vide.pdf", size: 0 })).toBe("Fichier vide.");
    expect(fileSize(1536)).toBe("2 Ko");
    expect(fileSize(3 * 1048576)).toBe("3,0 Mo");
  });

  it("explains server refusals in plain words", () => {
    expect(failureMessage(403, "RAG métier réservé à l'administrateur")).toBe("RAG métier réservé à l'administrateur");
    expect(failureMessage(401, "")).toContain("reconnectez-vous");
    expect(failureMessage(413, "")).toContain("trop volumineux");
    expect(failureMessage(504, "")).toContain("actualisez la liste");
    expect(failureMessage(0, "")).toContain("Connexion interrompue");
  });
});

describe("upload zone", () => {
  it("waits for the Save button, then reports what the server confirmed", async () => {
    const calls = server([indexed(12)]);
    const onAdd = vi.fn();
    render(<UploadZone lang="fr" onAdd={onAdd} contentType="internal_knowledge" destinationLabel="le RAG métier" />);
    expect(screen.queryByTestId("upload-save")).toBeNull();               // rien à enregistrer tant qu'aucun fichier n'est choisi
    choose(file("guide-tps.pdf"));
    expect(screen.getByText("1 document prêt à enregistrer dans le RAG métier")).toBeTruthy();
    expect(calls).toHaveLength(0);                                         // rien n'est envoyé avant le clic
    fireEvent.click(screen.getByTestId("upload-save"));
    await waitFor(() => expect(rows()[0].getAttribute("data-status")).toBe("saved"));
    expect(rows()[0].textContent).toContain("Enregistré et indexé — 12 segments");
    expect(JSON.parse(calls[0].get("bulk_metadata") as string).content_type).toBe("internal_knowledge");
    expect(onAdd).toHaveBeenCalledTimes(1);
  });

  it("sends one file per request, into the selected client folder", async () => {
    const calls = server([indexed(3)]);
    render(<UploadZone lang="fr" contentType="client_data" placement={{ clientId: "c1", projectId: "p2" }} />);
    choose(file("a.pdf"), file("b.xlsx"), file("c.docx"));
    fireEvent.click(screen.getByText("Enregistrer (3)"));
    await waitFor(() => expect(calls).toHaveLength(3));
    for (const body of calls) {
      expect(body.getAll("files")).toHaveLength(1);                        // régression : chaque fichier était renvoyé N fois
      expect(JSON.parse(body.get("bulk_metadata") as string)).toMatchObject({ content_type: "client_data", client_id: "c1", project_id: "p2" });
    }
    expect(calls.map(b => (b.get("files") as File).name).sort()).toEqual(["a.pdf", "b.xlsx", "c.docx"]);
    await waitFor(() => expect(screen.getByText("Terminé.")).toBeTruthy());
  });

  it("never shows a failed upload as indexed, and lets the user retry", async () => {
    const calls = server([{ status: 413, body: { detail: "Fichier trop volumineux (max 25 Mo)" } }, indexed(5)]);
    const onAdd = vi.fn();
    render(<UploadZone lang="fr" onAdd={onAdd} />);
    choose(file("releve.pdf"));
    fireEvent.click(screen.getByTestId("upload-save"));
    await waitFor(() => expect(rows()[0].getAttribute("data-status")).toBe("failed"));
    expect(screen.getByRole("alert").textContent).toBe("Fichier trop volumineux (max 25 Mo)");
    expect(rows()[0].textContent).not.toMatch(/indexé/i);                  // régression : un échec s'affichait « ✅ indexé »
    expect(onAdd).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Réessayer"));
    fireEvent.click(screen.getByTestId("upload-save"));
    await waitFor(() => expect(rows()[0].getAttribute("data-status")).toBe("saved"));
    expect(calls).toHaveLength(2);
  });

  it("says so when the server stored the file but could not read it, or skipped it", async () => {
    server([{ body: { documents: [{ doc_id: "d2", status: "error", chunks_count: 0, status_message: "PDF protégé par mot de passe" }] } },
            { body: { documents: [], count: 0 } }]);
    render(<UploadZone lang="fr" />);
    choose(file("protege.pdf"));
    fireEvent.click(screen.getByTestId("upload-save"));
    await waitFor(() => expect(rows()[0].getAttribute("data-status")).toBe("unreadable"));
    expect(rows()[0].textContent).toContain("aucun texte lisible n'a été trouvé : PDF protégé par mot de passe");
    choose(file("autre.pdf"));
    fireEvent.click(screen.getByTestId("upload-save"));
    await waitFor(() => expect(rows()[0].getAttribute("data-status")).toBe("failed"));
    expect(rows()[0].textContent).toContain("n'a pas enregistré ce fichier");
  });

  it("reports a lost connection or a gateway timeout without pretending success", async () => {
    server(["network", { status: 504 }]);
    render(<UploadZone lang="fr" />);
    choose(file("a.pdf"));
    fireEvent.click(screen.getByTestId("upload-save"));
    await waitFor(() => expect(rows()[0].textContent).toContain("Connexion interrompue"));
    choose(file("b.pdf"));
    fireEvent.click(screen.getByTestId("upload-save"));
    await waitFor(() => expect(rows()[0].textContent).toContain("n'a pas répondu à temps"));
  });

  it("refuses an unsupported file on the spot and keeps the Save button for the others", async () => {
    const calls = server([indexed(2)]);
    render(<UploadZone lang="fr" />);
    choose(file("video.mp4"), file("ok.pdf"));
    expect(rows().map(r => r.getAttribute("data-status")).sort()).toEqual(["ready", "refused"]);
    fireEvent.click(screen.getByTestId("upload-save"));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect((calls[0].get("files") as File).name).toBe("ok.pdf");
  });

  it("lets the same file be chosen again, and a file be removed before saving", () => {
    server([indexed()]);
    render(<UploadZone lang="fr" />);
    const input = screen.getByTestId("upload-input") as HTMLInputElement;
    choose(file("a.pdf"));
    expect(input.value).toBe("");                                          // régression : rechoisir le même fichier ne faisait rien
    fireEvent.click(screen.getByLabelText("Retirer a.pdf"));
    expect(screen.queryByTestId("upload-row")).toBeNull();
    expect(screen.queryByText(/Réinitialiser le RAG/)).toBeNull();
  });
});

describe("background indexing", () => {
  const received = { body: { documents: [{ doc_id: "d9", status: "processing", chunks_count: 0 }] } };

  it("follows a received document until the server says it is indexed", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      server([received], [{ doc_id: "d9", status: "processing" }, { doc_id: "d9", status: "indexed", chunks_count: 240,
        status_message: "310 pages numérisées : seules les 40 premières sont lues par OCR" }]);
      const onAdd = vi.fn();
      render(<UploadZone lang="fr" onAdd={onAdd} />);
      choose(file("grand-livre.pdf", 145 * 1048576));
      fireEvent.click(screen.getByTestId("upload-save"));
      await waitFor(() => expect(rows()[0].getAttribute("data-status")).toBe("indexing"));
      expect(rows()[0].textContent).toContain("Reçu. Indexation en cours sur le serveur");
      expect(screen.getByText(/Vous pouvez quitter cette page/)).toBeTruthy();
      expect((screen.getByTestId("upload-save") as HTMLButtonElement).disabled).toBe(true);
      await vi.advanceTimersByTimeAsync(3100);                               // 1re relecture : encore en cours
      expect(rows()[0].getAttribute("data-status")).toBe("indexing");
      await vi.advanceTimersByTimeAsync(3100);                               // 2e relecture : indexé
      await waitFor(() => expect(rows()[0].getAttribute("data-status")).toBe("saved"));
      expect(rows()[0].textContent).toContain("Enregistré et indexé — 240 segments");
      expect(rows()[0].textContent).toContain("seules les 40 premières sont lues par OCR");   // l'avertissement du serveur est montré
      expect(onAdd).toHaveBeenCalledTimes(2);                                // à la réception, puis au résultat
    } finally { vi.useRealTimers(); }
  });

  it("reports a document the server could not read after indexing", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      server([received], [{ doc_id: "d9", status: "error", chunks_count: 0, status_message: "Indexation interrompue (MemoryError) — relancez « Réindexer »." }]);
      render(<UploadZone lang="fr" />);
      choose(file("scan.pdf"));
      fireEvent.click(screen.getByTestId("upload-save"));
      await waitFor(() => expect(rows()[0].getAttribute("data-status")).toBe("indexing"));
      await vi.advanceTimersByTimeAsync(3100);
      await waitFor(() => expect(rows()[0].getAttribute("data-status")).toBe("unreadable"));
      expect(rows()[0].textContent).toContain("relancez « Réindexer »");
    } finally { vi.useRealTimers(); }
  });
});

describe("partly read documents", () => {
  it("never reports a partly read manual as indexed", async () => {
    server([{ body: { documents: [{ doc_id: "d3", status: "partial", chunks_count: 21,
      status_message: "texte lu sur 136 pages sur 848" }] } }]);
    render(<UploadZone lang="fr" />);
    choose(file("fiscalite-2024.pdf", 124 * 1048576));
    fireEvent.click(screen.getByTestId("upload-save"));
    await waitFor(() => expect(rows()[0].getAttribute("data-status")).toBe("partial"));
    expect(rows()[0].textContent).toContain("Indexé en partie seulement — 21 segments : texte lu sur 136 pages sur 848");
    expect(rows()[0].textContent).not.toContain("Enregistré et indexé");
  });
});

describe("upload progress", () => {
  it("shows the percentage sent while a large file uploads", async () => {
    let live: any;
    class FakeXhr {
      upload: any = {}; status = 0; responseText = "";
      onload: any; onerror: any; onabort: any;
      open() {} send() { live = this; }
    }
    vi.stubGlobal("XMLHttpRequest", FakeXhr as any);
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }) as any));
    render(<UploadZone lang="fr" />);
    choose(file("grand-livre.pdf", 145 * 1048576));
    fireEvent.click(screen.getByTestId("upload-save"));
    await waitFor(() => expect(live).toBeTruthy());
    live.upload.onprogress({ lengthComputable: true, loaded: 58, total: 145 });
    await waitFor(() => expect(rows()[0].textContent).toContain("Envoi au serveur… 40 %"));
    live.status = 413; live.responseText = "<html>413 Request Entity Too Large</html>";   // page d'erreur du serveur web
    live.onload();
    await waitFor(() => expect(rows()[0].getAttribute("data-status")).toBe("failed"));
    expect(rows()[0].textContent).toContain("trop volumineux pour le serveur");
  });
});
