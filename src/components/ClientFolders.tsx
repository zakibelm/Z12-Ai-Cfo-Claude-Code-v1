// Arborescence du RAG client : un dossier par client, des sous-dossiers par projet.
// Le dossier sélectionné devient le dossier actif des agents.
import React, { useState } from "react";
import type { ActiveFolder } from "../utils/activeFolder";

export type ProjectNode = { project_id: string; name: string; documents: number };
export type ClientNode = { client_id: string; name: string; documents: number; projects: ProjectNode[] };
export type FolderTree = { clients: ClientNode[]; unfiled: number };

async function call(url: string, method: string, body?: unknown): Promise<any> {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.detail || `Erreur ${res.status}`);
  return data;
}

export function ClientFolders({ lang, tree, selected, showUnfiled, onSelect, onShowUnfiled, onChanged }: {
  lang: string; tree: FolderTree; selected: ActiveFolder | null; showUnfiled: boolean;
  onSelect: (folder: ActiveFolder | null) => void; onShowUnfiled: () => void; onChanged: () => void;
}) {
  const fr = lang === "fr";
  const [newClient, setNewClient] = useState("");
  const [newProject, setNewProject] = useState("");
  const [error, setError] = useState("");

  const run = async (action: () => Promise<void>) => {
    setError("");
    try { await action(); onChanged(); } catch (e: any) { setError(e?.message || "Erreur"); }
  };

  const addClient = () => run(async () => {
    const name = newClient.trim();
    if (!name) return;
    const created = await call("/api/clients", "POST", { name });
    setNewClient("");
    onSelect({ clientId: created.client_id, clientName: created.name });
  });

  const addProject = () => run(async () => {
    const name = newProject.trim();
    if (!name || !selected) return;
    const created = await call(`/api/clients/${encodeURIComponent(selected.clientId)}/projects`, "POST", { name });
    setNewProject("");
    onSelect({ clientId: selected.clientId, clientName: selected.clientName, projectId: created.project_id, projectName: created.name });
  });

  const rename = (folderId: string, current: string) => run(async () => {
    const name = window.prompt(fr ? "Nouveau nom" : "New name", current);
    if (!name || !name.trim() || name.trim() === current) return;
    const renamed = await call(`/api/folders/${encodeURIComponent(folderId)}`, "PATCH", { name: name.trim() });
    if (selected?.clientId === folderId) onSelect({ ...selected, clientName: renamed.name });
    else if (selected?.projectId === folderId) onSelect({ ...selected, projectName: renamed.name });
  });

  const remove = (folderId: string, name: string, isClient: boolean) => run(async () => {
    if (!window.confirm(fr ? `Supprimer le dossier « ${name} » ?` : `Delete folder "${name}"?`)) return;
    await call(`/api/folders/${encodeURIComponent(folderId)}`, "DELETE");
    if (!selected) return;
    if (isClient && selected.clientId === folderId) onSelect(null);
    else if (selected.projectId === folderId) onSelect({ clientId: selected.clientId, clientName: selected.clientName });
  });

  const row = (active: boolean, indent: number): React.CSSProperties => ({
    display: "flex", alignItems: "center", gap: 6, padding: `7px 10px 7px ${10 + indent}px`, cursor: "pointer",
    borderRadius: 8, background: active ? "var(--accent-soft)" : "transparent", color: active ? "var(--ink)" : "var(--ink-2)", fontSize: 12.5,
  });
  const iconBtn: React.CSSProperties = { background: "transparent", border: "none", color: "var(--ink-3)", cursor: "pointer", fontSize: 11, padding: "0 2px" };
  const field: React.CSSProperties = { flex: 1, minWidth: 0, background: "transparent", border: "1px solid var(--line-2)", borderRadius: 8, padding: "6px 9px", color: "var(--ink)", fontSize: 12, outline: "none" };
  const addBtn: React.CSSProperties = { background: "var(--accent-soft)", border: "1px solid var(--accent-line)", borderRadius: 8, padding: "6px 10px", color: "var(--accent)", fontSize: 12, cursor: "pointer", whiteSpace: "nowrap" };
  const count = (n: number) => <span style={{ marginLeft: "auto", fontSize: 10, color: "var(--ink-3)", fontFamily: "'DM Mono',monospace" }}>{n}</span>;

  return (
    <div data-testid="client-folders" style={{ border: "1px solid var(--line)", borderRadius: 12, padding: 10, marginBottom: 12 }}>
      <div style={{ fontSize: 10, fontWeight: 500, color: "var(--ink-3)", textTransform: "uppercase", letterSpacing: "0.07em", padding: "2px 6px 8px" }}>
        {fr ? "Dossiers clients" : "Client folders"}
      </div>

      {tree.clients.length === 0 && (
        <div style={{ padding: "6px 8px 10px", fontSize: 12, color: "var(--ink-3)" }}>
          {fr ? "Aucun dossier client. Créez-en un pour y déposer des documents." : "No client folder yet. Create one to add documents."}
        </div>
      )}

      {tree.clients.map((client) => {
        const clientActive = !showUnfiled && selected?.clientId === client.client_id;
        return (
          <div key={client.client_id}>
            <div style={row(clientActive && !selected?.projectId, 0)} onClick={() => onSelect({ clientId: client.client_id, clientName: client.name })}>
              <span>📁</span><span style={{ fontWeight: 500 }}>{client.name}</span>
              {count(client.documents)}
              <button style={iconBtn} title={fr ? "Renommer" : "Rename"} onClick={(e) => { e.stopPropagation(); rename(client.client_id, client.name); }}>✎</button>
              <button style={iconBtn} title={fr ? "Supprimer" : "Delete"} onClick={(e) => { e.stopPropagation(); remove(client.client_id, client.name, true); }}>🗑</button>
            </div>
            {clientActive && client.projects.map((project) => (
              <div key={project.project_id} style={row(selected?.projectId === project.project_id, 18)}
                onClick={() => onSelect({ clientId: client.client_id, clientName: client.name, projectId: project.project_id, projectName: project.name })}>
                <span>📂</span><span>{project.name}</span>
                {count(project.documents)}
                <button style={iconBtn} title={fr ? "Renommer" : "Rename"} onClick={(e) => { e.stopPropagation(); rename(project.project_id, project.name); }}>✎</button>
                <button style={iconBtn} title={fr ? "Supprimer" : "Delete"} onClick={(e) => { e.stopPropagation(); remove(project.project_id, project.name, false); }}>🗑</button>
              </div>
            ))}
            {clientActive && (
              <div style={{ display: "flex", gap: 6, padding: "4px 10px 8px 28px" }}>
                <input style={field} value={newProject} onChange={(e) => setNewProject(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") addProject(); }}
                  placeholder={fr ? "Nouveau projet (ex. Audit 2025)" : "New project (e.g. Audit 2025)"} aria-label={fr ? "Nom du projet" : "Project name"} />
                <button style={addBtn} onClick={addProject}>{fr ? "+ Projet" : "+ Project"}</button>
              </div>
            )}
          </div>
        );
      })}

      {tree.unfiled > 0 && (
        <div style={row(showUnfiled, 0)} onClick={onShowUnfiled}>
          <span>🗂</span><span>{fr ? "À classer" : "To file"}</span>{count(tree.unfiled)}
        </div>
      )}

      <div style={{ display: "flex", gap: 6, padding: "8px 6px 2px", borderTop: "1px solid var(--line)", marginTop: 6 }}>
        <input style={field} value={newClient} onChange={(e) => setNewClient(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") addClient(); }}
          placeholder={fr ? "Nouveau client (ex. Les Entreprises Boréal)" : "New client"} aria-label={fr ? "Nom du client" : "Client name"} />
        <button style={addBtn} onClick={addClient}>{fr ? "+ Client" : "+ Client"}</button>
      </div>
      {error && <div role="alert" style={{ color: "#e5484d", fontSize: 12, padding: "6px 8px 0" }}>{error}</div>}
    </div>
  );
}
