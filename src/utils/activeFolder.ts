// Dossier client (et projet) actif : celui dont les agents peuvent lire les documents.
// Le serveur vérifie toujours que le dossier appartient à l'utilisateur ; ce module
// ne fait que mémoriser le choix dans le navigateur.

export type ActiveFolder = {
  clientId: string;
  clientName: string;
  projectId?: string | null;
  projectName?: string | null;
};

const KEY = "z12-active-folder";
export const ACTIVE_FOLDER_EVENT = "z12-active-folder-changed";

export function getActiveFolder(): ActiveFolder | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const value = JSON.parse(raw);
    return value && typeof value.clientId === "string" && value.clientId ? value : null;
  } catch {
    return null;
  }
}

export function setActiveFolder(folder: ActiveFolder | null): void {
  try {
    if (folder) localStorage.setItem(KEY, JSON.stringify(folder));
    else localStorage.removeItem(KEY);
  } catch { /* stockage indisponible : le choix vaut pour la page en cours */ }
  try { window.dispatchEvent(new CustomEvent(ACTIVE_FOLDER_EVENT, { detail: folder })); } catch { /* hors navigateur */ }
}

/** Champs à joindre à une requête d'agent (vide si aucun dossier actif). */
export function activeFolderParams(): { client_id?: string; project_id?: string } {
  const folder = getActiveFolder();
  if (!folder) return {};
  return folder.projectId
    ? { client_id: folder.clientId, project_id: folder.projectId }
    : { client_id: folder.clientId };
}

/** Libellé court : « Boréal » ou « Boréal › Audit 2025 ». */
export function activeFolderLabel(folder: ActiveFolder | null): string {
  if (!folder) return "";
  return folder.projectName ? `${folder.clientName} › ${folder.projectName}` : folder.clientName;
}
