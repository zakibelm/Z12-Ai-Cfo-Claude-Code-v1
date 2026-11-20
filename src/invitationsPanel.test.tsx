import React from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { InvitationsPanel, invitationLink } from "./components/InvitationsPanel";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const ACTIVE = { id: 2, label: "Cabinet Tremblay", status: "active", created_at: "2026-11-12T10:00:00+00:00",
  expires_at: "2026-11-26T10:00:00+00:00", used_at: null, used_by: null };
const USED = { ...ACTIVE, id: 1, label: "Boréal", status: "used", used_by: "client@exemple.ca" };

function server(list: any[]) {
  const calls: any[] = [];
  const mock = vi.fn(async (url: any, init?: any) => {
    calls.push({ url: String(url), init });
    if (init?.method === "POST") return { ok: true, status: 201, json: async () => ({ ...ACTIVE, id: 3, label: "Nordik", code: "WXYZ-2345-ABCD" }) } as any;
    if (init?.method === "DELETE") return { ok: true, status: 200, json: async () => ({ revoked: 2 }) } as any;
    return { ok: true, status: 200, json: async () => ({ mode: "invite", invitations: list }) } as any;
  });
  vi.stubGlobal("fetch", mock);
  return calls;
}

describe("invitations panel", () => {
  it("lists invitations with their status and who used them", async () => {
    server([ACTIVE, USED]);
    render(<InvitationsPanel lang="fr" />);
    expect(await screen.findByText("Cabinet Tremblay")).toBeTruthy();
    expect(screen.getByText("Boréal — client@exemple.ca")).toBeTruthy();
    expect(screen.getByText("Active")).toBeTruthy();
    expect(screen.getByText("Utilisée")).toBeTruthy();
    expect(screen.getAllByText("Révoquer")).toHaveLength(1); // seule une invitation active se révoque
    expect(screen.getByText(/Inscription sur invitation/)).toBeTruthy();
  });

  it("creates an invitation and shows the code and its link once", async () => {
    const calls = server([]);
    render(<InvitationsPanel lang="fr" />);
    fireEvent.change(screen.getByLabelText("Destinataire de l'invitation"), { target: { value: " Nordik " } });
    fireEvent.change(screen.getByLabelText("Durée de validité"), { target: { value: "30" } });
    fireEvent.click(screen.getByText("Créer une invitation"));
    expect((await screen.findByTestId("invitation-code")).textContent).toBe("WXYZ-2345-ABCD");
    expect(screen.getByText(invitationLink("WXYZ-2345-ABCD"))).toBeTruthy();
    const post = calls.find(c => c.init?.method === "POST");
    expect(post.url).toBe("/api/admin/invitations");
    expect(JSON.parse(post.init.body)).toEqual({ label: "Nordik", days: 30 });
  });

  it("revokes an active invitation", async () => {
    const calls = server([ACTIVE]);
    render(<InvitationsPanel lang="fr" />);
    fireEvent.click(await screen.findByText("Révoquer"));
    await waitFor(() => expect(calls.some(c => c.init?.method === "DELETE" && c.url === "/api/admin/invitations/2")).toBe(true));
  });

  it("builds a sign-up link that carries the code", () => {
    expect(invitationLink("AB CD")).toBe(`${window.location.origin}/register?code=AB%20CD`);
  });
});
