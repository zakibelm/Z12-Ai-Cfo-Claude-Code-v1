// RAG client : dossiers de clients, sous-dossiers de projets, dossier actif transmis aux agents.
import React from "react";
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";

import { ClientFolders } from "./components/ClientFolders";
import { activeFolderLabel, activeFolderParams, getActiveFolder, setActiveFolder } from "./utils/activeFolder";
import { callOpenRouter } from "./utils/workflow";

const TREE = {
  unfiled: 2,
  clients: [
    { client_id: "c1", name: "Boréal", documents: 3, projects: [{ project_id: "p1", name: "Audit 2025", documents: 1 }] },
    { client_id: "c2", name: "Nordik", documents: 0, projects: [] },
  ],
};

function ok(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as any;
}

beforeEach(() => { localStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("active folder", () => {
  it("is empty by default and sends nothing to the agents", () => {
    expect(getActiveFolder()).toBeNull();
    expect(activeFolderParams()).toEqual({});
  });

  it("stores a client, then a client and project", () => {
    setActiveFolder({ clientId: "c1", clientName: "Boréal" });
    expect(activeFolderParams()).toEqual({ client_id: "c1" });
    expect(activeFolderLabel(getActiveFolder())).toBe("Boréal");
    setActiveFolder({ clientId: "c1", clientName: "Boréal", projectId: "p1", projectName: "Audit 2025" });
    expect(activeFolderParams()).toEqual({ client_id: "c1", project_id: "p1" });
    expect(activeFolderLabel(getActiveFolder())).toBe("Boréal › Audit 2025");
    setActiveFolder(null);
    expect(activeFolderParams()).toEqual({});
  });

  it("ignores a corrupted stored value", () => {
    localStorage.setItem("z12-active-folder", "{pas du json");
    expect(getActiveFolder()).toBeNull();
    localStorage.setItem("z12-active-folder", JSON.stringify({ clientName: "sans identifiant" }));
    expect(getActiveFolder()).toBeNull();
  });
});

describe("chat requests", () => {
  const bodyOf = (call: any) => JSON.parse(call[1].body);

  it("carry the active client folder", async () => {
    setActiveFolder({ clientId: "c1", clientName: "Boréal", projectId: "p1", projectName: "Audit 2025" });
    const fetchMock = vi.fn(async () => ok({ choices: [{ message: { content: "réponse" } }] }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await callOpenRouter("m", "s", [{ role: "user", content: "q" }], "")).toBe("réponse");
    expect(bodyOf(fetchMock.mock.calls[0])).toMatchObject({ client_id: "c1", project_id: "p1" });
  });

  it("carry no folder when none is active", async () => {
    const fetchMock = vi.fn(async () => ok({ choices: [{ message: { content: "réponse" } }] }));
    vi.stubGlobal("fetch", fetchMock);
    await callOpenRouter("m", "s", [{ role: "user", content: "q" }], "");
    expect(bodyOf(fetchMock.mock.calls[0])).not.toHaveProperty("client_id");
  });

  it("forget a folder the server no longer knows and answer without client data", async () => {
    setActiveFolder({ clientId: "supprime", clientName: "Ancien" });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(ok({ detail: "Dossier client non trouve" }, 404))
      .mockResolvedValueOnce(ok({ choices: [{ message: { content: "réponse" } }] }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await callOpenRouter("m", "s", [{ role: "user", content: "q" }], "")).toBe("réponse");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(bodyOf(fetchMock.mock.calls[1])).not.toHaveProperty("client_id");
    expect(getActiveFolder()).toBeNull();
  });
});

describe("client folder tree", () => {
  function mount(selected: any = null, handlers: any = {}) {
    const props = { onSelect: vi.fn(), onShowUnfiled: vi.fn(), onChanged: vi.fn(), ...handlers };
    render(<ClientFolders lang="fr" tree={TREE} selected={selected} showUnfiled={false} {...props} />);
    return props;
  }

  it("lists clients, and the projects of the selected client only", () => {
    mount({ clientId: "c1", clientName: "Boréal" });
    expect(screen.getByText("Boréal")).toBeTruthy();
    expect(screen.getByText("Nordik")).toBeTruthy();
    expect(screen.getByText("Audit 2025")).toBeTruthy();
    expect(screen.getByText("À classer")).toBeTruthy();
    cleanup();
    mount(null);
    expect(screen.queryByText("Audit 2025")).toBeNull();
  });

  it("selects a client, then a project inside it", () => {
    const props = mount({ clientId: "c1", clientName: "Boréal" });
    fireEvent.click(screen.getByText("Nordik"));
    expect(props.onSelect).toHaveBeenLastCalledWith({ clientId: "c2", clientName: "Nordik" });
    fireEvent.click(screen.getByText("Audit 2025"));
    expect(props.onSelect).toHaveBeenLastCalledWith({ clientId: "c1", clientName: "Boréal", projectId: "p1", projectName: "Audit 2025" });
  });

  it("creates a client folder and selects it", async () => {
    const fetchMock = vi.fn(async () => ok({ client_id: "c3", name: "Groupe Tremblay" }, 201));
    vi.stubGlobal("fetch", fetchMock);
    const props = mount(null);
    fireEvent.change(screen.getByLabelText("Nom du client"), { target: { value: "  Groupe Tremblay " } });
    fireEvent.click(screen.getByText("+ Client"));
    await waitFor(() => expect(props.onSelect).toHaveBeenCalledWith({ clientId: "c3", clientName: "Groupe Tremblay" }));
    const [url, init] = fetchMock.mock.calls[0] as any;
    expect(url).toBe("/api/clients");
    expect(JSON.parse(init.body)).toEqual({ name: "Groupe Tremblay" });
    expect(props.onChanged).toHaveBeenCalled();
  });

  it("creates a project inside the selected client", async () => {
    const fetchMock = vi.fn(async () => ok({ project_id: "p2", client_id: "c1", name: "Acquisition" }, 201));
    vi.stubGlobal("fetch", fetchMock);
    const props = mount({ clientId: "c1", clientName: "Boréal" });
    fireEvent.change(screen.getByLabelText("Nom du projet"), { target: { value: "Acquisition" } });
    fireEvent.click(screen.getByText("+ Projet"));
    await waitFor(() => expect(props.onSelect).toHaveBeenCalledWith({ clientId: "c1", clientName: "Boréal", projectId: "p2", projectName: "Acquisition" }));
    expect((fetchMock.mock.calls[0] as any)[0]).toBe("/api/clients/c1/projects");
  });

  it("shows the server's reason when a folder is refused", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ok({ detail: "Un dossier porte déjà ce nom" }, 400)));
    const props = mount(null);
    fireEvent.change(screen.getByLabelText("Nom du client"), { target: { value: "Boréal" } });
    fireEvent.click(screen.getByText("+ Client"));
    expect((await screen.findByRole("alert")).textContent).toContain("Un dossier porte déjà ce nom");
    expect(props.onSelect).not.toHaveBeenCalled();
  });

  it("does not create a folder with an empty name", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    mount(null);
    fireEvent.click(screen.getByText("+ Client"));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
