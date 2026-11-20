// Pièces jointes du chat : le serveur lit le texte des fichiers (PDF, Word, Excel, images…),
// sans rien enregistrer, et ce texte est joint à la question pour les spécialistes.

export type Attachment = { name: string; text: string; chars: number; truncated: boolean; error: string | null };

/** Texte de pièces jointes par message, au plus (la conversation entière est limitée par le serveur). */
export const ATTACHMENT_BUDGET = 40000;
/** Taille de l'historique envoyé à un spécialiste, pièces jointes comprises. */
export const HISTORY_BUDGET = 100000;

const TEXT_FILE = /\.(txt|csv|md|json|xml|yaml|yml|log|sql|html|css|tsx|ts|js|py)$/i;

function readLocally(file: File): Promise<Attachment> {
  return new Promise(resolve => {
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result || "");
      resolve({ name: file.name, text, chars: text.length, truncated: false, error: text.trim() ? null : "fichier vide" });
    };
    reader.onerror = () => resolve({ name: file.name, text: "", chars: 0, truncated: false, error: "lecture impossible" });
    reader.readAsText(file, "utf-8");
  });
}

function refusal(status: number, detail: string, fr: boolean): string {
  if (detail) return detail;
  if (status === 401) return fr ? "session expirée : reconnectez-vous" : "session expired: sign in again";
  if (status === 413) return fr ? "fichier trop volumineux pour le chat — déposez-le dans Documents" : "file too large for the chat — upload it in Documents";
  if (status === 429) return fr ? "trop d'envois : attendez une minute" : "too many uploads: wait a minute";
  if (status === 0) return fr ? "connexion interrompue" : "connection lost";
  return fr ? `lecture impossible (erreur ${status})` : `could not be read (error ${status})`;
}

/** Lit les fichiers joints. Ne lève jamais : un fichier illisible porte sa raison dans `error`. */
export async function readAttachments(files: File[], fr = true): Promise<Attachment[]> {
  const local = files.filter(f => TEXT_FILE.test(f.name) || (f.type || "").startsWith("text/"));
  const remote = files.filter(f => !local.includes(f));
  const results = new Map<File, Attachment>();
  for (const file of local) results.set(file, await readLocally(file));

  if (remote.length) {
    const fail = (reason: string) => remote.forEach(f => results.set(f, { name: f.name, text: "", chars: 0, truncated: false, error: reason }));
    try {
      const body = new FormData();
      remote.forEach(f => body.append("files", f));
      const res = await fetch("/api/attachments/extract", { method: "POST", body });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) fail(refusal(res.status, typeof data?.detail === "string" ? data.detail : "", fr));
      else {
        const read = (data.files || []) as any[];
        remote.forEach((f, i) => {
          const r = read[i];
          results.set(f, r
            ? { name: f.name, text: r.text || "", chars: r.chars || 0, truncated: !!r.truncated, error: r.error || null }
            : { name: f.name, text: "", chars: 0, truncated: false, error: fr ? "non lu par le serveur" : "not read by the server" });
        });
      }
    } catch { fail(refusal(0, "", fr)); }
  }
  return files.map(f => results.get(f)!);
}

/** Répartit le budget de texte entre les pièces jointes : les petites entières, les grosses coupées. */
export function allocate(attachments: Attachment[], budget = ATTACHMENT_BUDGET): Attachment[] {
  const readable = attachments.filter(a => !a.error && a.text);
  const share = new Map<Attachment, number>();
  let remaining = budget;
  [...readable].sort((a, b) => a.text.length - b.text.length).forEach((a, i, sorted) => {
    const part = Math.min(a.text.length, Math.floor(remaining / (sorted.length - i)));
    share.set(a, part);
    remaining -= part;
  });
  return attachments.map(a => {
    const part = share.get(a);
    if (part === undefined || part >= a.text.length) return a;
    return { ...a, text: a.text.slice(0, part), truncated: true };
  });
}

/** Texte joint à la question, tel que le lisent les spécialistes. */
export function attachmentsBlock(attachments: Attachment[], fr = true): string {
  return attachments.map(a => {
    if (a.error || !a.text) {
      return fr ? `[Pièce jointe « ${a.name} » : non lue — ${a.error || "aucun texte lisible"}]\n`
                : `[Attachment "${a.name}": not read — ${a.error || "no readable text"}]\n`;
    }
    const cut = a.truncated
      ? (fr ? ` — extrait : ${a.text.length.toLocaleString("fr-CA")} caractères sur ${Math.max(a.chars, a.text.length).toLocaleString("fr-CA")}, la suite n'est pas fournie`
            : ` — excerpt: ${a.text.length.toLocaleString("en-CA")} of ${Math.max(a.chars, a.text.length).toLocaleString("en-CA")} characters, the rest is not provided`)
      : "";
    return (fr ? `[Pièce jointe « ${a.name} »${cut}]` : `[Attachment "${a.name}"${cut}]`)
      + `\n${a.text}\n` + (fr ? `[Fin de « ${a.name} »]\n` : `[End of "${a.name}"]\n`);
  }).join("\n");
}

/** Résumé d'une pièce jointe pour l'affichage (pas de contenu). */
export function attachmentSummary(a: Attachment, fr = true): string {
  if (a.error || !a.text) return fr ? `non lu : ${a.error || "aucun texte lisible"}` : `not read: ${a.error || "no readable text"}`;
  const size = `${a.text.length.toLocaleString(fr ? "fr-CA" : "en-CA")} ${fr ? "caractères lus" : "characters read"}`;
  return a.truncated ? `${size} ${fr ? "(extrait)" : "(excerpt)"}` : size;
}

/**
 * Historique envoyé à un spécialiste : les derniers messages, plus le dernier message qui portait
 * des pièces jointes s'il est sorti de la fenêtre — sinon le cas soumis est oublié au bout de
 * trois échanges. Si l'ensemble dépasse le budget, les pièces jointes les plus anciennes sont
 * remplacées par une note.
 */
export function fitHistory(history: any[], windowSize = 6, budget = HISTORY_BUDGET): any[] {
  const all = history.filter(m => m.role !== "system");
  let kept = all.slice(-windowSize);
  const earlier = all.slice(0, Math.max(0, all.length - windowSize)).filter(m => m.files?.length);
  if (earlier.length) kept = [earlier[earlier.length - 1], ...kept];
  const total = () => kept.reduce((sum, m) => sum + (m.content?.length || 0), 0);
  for (let i = 0; i < kept.length - 1 && total() > budget; i++) {
    const m = kept[i];
    if (!m.files?.length) continue;
    const names = m.files.map((f: any) => f.name).join(", ");
    kept[i] = { ...m, content: `[Pièces jointes retirées du contexte, trop volumineuses : ${names}]\n${m.display ?? ""}` };
  }
  return kept;
}
