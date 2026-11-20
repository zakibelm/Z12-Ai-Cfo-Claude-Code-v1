import React from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { DocIndexEditor, parseKeywords } from "./components/DocIndexEditor";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("document description and keywords", () => {
  it("parses keywords: trims, splits, removes blanks and duplicates", () => {
    expect(parseKeywords(" TPS,  tvq ; tps\ncoût   de revient,, ")).toEqual(["TPS", "tvq", "coût de revient"]);
    expect(parseKeywords("")).toEqual([]);
  });

  it("saves the description and keywords, then shows what the server kept", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ description: "Guide des ratios.", keywords: ["ratio", "liquidité"] }) }) as any);
    vi.stubGlobal("fetch", fetchMock);
    const onSaved = vi.fn();
    render(<DocIndexEditor docId="d 1" description="Ancien" keywords={["ratio"]} lang="fr" onSaved={onSaved} />);
    fireEvent.change(screen.getByLabelText("Description du document"), { target: { value: "Guide des ratios." } });
    fireEvent.change(screen.getByLabelText("Mots-clés du document"), { target: { value: "ratio, liquidité, ratio" } });
    fireEvent.click(screen.getByText("Enregistrer"));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0] as any;
    expect(url).toBe("/api/knowledge/d%201");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body)).toEqual({ description: "Guide des ratios.", keywords: ["ratio", "liquidité"] });
    expect((screen.getByLabelText("Mots-clés du document") as HTMLInputElement).value).toBe("ratio, liquidité");
    expect(screen.getByRole("status").textContent).toContain("Enregistré");
  });

  it("shows the server's reason when saving fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 404, json: async () => ({ detail: "Document non trouvé" }) }) as any));
    const onSaved = vi.fn();
    render(<DocIndexEditor docId="d1" description="" keywords={[]} lang="fr" onSaved={onSaved} />);
    fireEvent.click(screen.getByText("Enregistrer"));
    expect((await screen.findByRole("alert")).textContent).toContain("Document non trouvé");
    expect(onSaved).not.toHaveBeenCalled();
  });
});
