// Dépôt de documents dans le RAG : on choisit les fichiers, on vérifie la liste, on clique
// « Enregistrer ». Chaque fichier est envoyé seul, l'un après l'autre, avec sa progression.
// Le serveur répond dès que le fichier est reçu, puis l'indexe en arrière-plan : l'état est
// relu jusqu'au résultat réel (indexé, illisible, ou refusé avec la raison du serveur).
// Rien n'est marqué « indexé » tant que le serveur ne l'a pas confirmé.
import React, { useCallback, useEffect, useRef, useState } from "react";

// Mêmes règles que le serveur (backend/utils/security.py et security_limits.py).
export const ALLOWED_EXTENSIONS = ["pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls", "csv", "txt", "json", "xml", "md",
  "log", "png", "jpg", "jpeg", "bmp", "tiff", "gif", "rtf", "odt", "ods", "odp"];
export const MAX_FILE_MB = 500;
const POLL_MS = 3000;

type Status = "ready" | "sending" | "indexing" | "saved" | "partial" | "unreadable" | "failed" | "refused";
type Item = { id: string; file: File; name: string; size: number; status: Status; message: string;
  startedAt?: number; percent?: number; docId?: string };
type Answer = { status: number; data: any };

/** Envoie le formulaire en suivant la progression. Sans XMLHttpRequest, repli sur fetch. */
export function postWithProgress(url: string, body: FormData, onProgress: (percent: number) => void): Promise<Answer> {
  if (typeof XMLHttpRequest === "undefined") {
    return fetch(url, { method: "POST", body }).then(async res => ({ status: res.status, data: await res.json().catch(() => ({})) }));
  }
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.upload.onprogress = e => { if (e.lengthComputable && e.total > 0) onProgress(Math.min(100, Math.round(100 * e.loaded / e.total))); };
    xhr.onload = () => {
      let data: any = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch { /* réponse non JSON (page d'erreur du serveur web) */ }
      resolve({ status: xhr.status, data });
    };
    xhr.onerror = () => reject(new TypeError("network"));
    xhr.onabort = () => reject(new TypeError("aborted"));
    xhr.send(body);
  });
}

export function fileSize(bytes: number): string {
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1).replace(".", ",") + " Mo";
  return Math.max(1, Math.round(bytes / 1024)) + " Ko";
}

function extensionOf(name: string): string {
  return name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
}

/** Raison pour laquelle un fichier ne peut pas être envoyé, ou "" s'il peut l'être. */
export function refusalReason(file: { name: string; size: number }, fr = true): string {
  const ext = extensionOf(file.name);
  if (!ALLOWED_EXTENSIONS.includes(ext)) {
    return fr ? `Type de fichier non accepté (.${ext || "?"}).` : `File type not accepted (.${ext || "?"}).`;
  }
  if (file.size === 0) return fr ? "Fichier vide." : "Empty file.";
  if (file.size > MAX_FILE_MB * 1048576) {
    return fr ? `Fichier trop volumineux (${fileSize(file.size)}, maximum ${MAX_FILE_MB} Mo).`
              : `File too large (${fileSize(file.size)}, maximum ${MAX_FILE_MB} MB).`;
  }
  return "";
}

/** Message lisible pour un refus du serveur ou une panne de réseau. */
export function failureMessage(status: number, detail: string, fr = true): string {
  if (detail) return detail;
  if (status === 401) return fr ? "Session expirée : reconnectez-vous, puis réessayez." : "Session expired: sign in again, then retry.";
  if (status === 403) return fr ? "Vous n'avez pas le droit de déposer un document ici." : "You are not allowed to upload here.";
  if (status === 413) return fr ? "Fichier trop volumineux pour le serveur." : "File too large for the server.";
  if (status === 429) return fr ? "Trop d'envois d'un coup : attendez une minute, puis réessayez." : "Too many uploads at once: wait a minute, then retry.";
  if (status === 502 || status === 503 || status === 504) {
    return fr ? "Le serveur n'a pas répondu à temps. Le document est peut-être encore en cours d'indexation : actualisez la liste dans une minute avant de le renvoyer."
              : "The server did not answer in time. The document may still be indexing: refresh the list in a minute before sending it again.";
  }
  if (status === 0) return fr ? "Connexion interrompue pendant l'envoi. Vérifiez votre réseau, puis réessayez." : "Connection lost during upload. Check your network, then retry.";
  return fr ? `Le serveur a refusé le document (erreur ${status}).` : `The server refused the document (error ${status}).`;
}

export function UploadZone({ lang, onAdd, contentType = "client_data", placement = null, destinationLabel = "" }: any) {
  const fr = lang === "fr";
  const inputRef = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [queue, setQueue] = useState<Item[]>([]);
  const [busy, setBusy] = useState(false);
  const [, tick] = useState(0);

  const patch = (id: string, change: Partial<Item>) => setQueue(prev => prev.map(q => q.id === id ? { ...q, ...change } : q));
  const indexing = queue.filter(q => q.status === "indexing");
  const indexingKey = indexing.map(q => q.docId).join(",");

  // Pendant un envoi ou une indexation, le temps écoulé s'affiche.
  useEffect(() => {
    if (!busy && !indexingKey) return;
    const timer = window.setInterval(() => tick(n => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [busy, indexingKey]);

  /** Résultat final d'un document, tel que le serveur le rapporte. */
  const settle = (id: string, doc: any) => {
    const chunks = Number(doc.chunks_count ?? doc.chunks ?? 0);
    if (doc.status === "error" || !chunks) {
      patch(id, { status: "unreadable", message: (fr ? "Enregistré, mais aucun texte lisible n'a été trouvé" : "Stored, but no readable text was found")
        + (doc.status_message ? ` : ${doc.status_message}` : ".") });
    } else if (doc.status === "partial") {
      // Le serveur n'a lu qu'une partie du document : on le dit, avec ce qu'il a lu.
      patch(id, { status: "partial", message: (fr ? `Indexé en partie seulement — ${chunks} segment${chunks > 1 ? "s" : ""}` : `Only partly indexed — ${chunks} chunk${chunks > 1 ? "s" : ""}`)
        + (doc.status_message ? ` : ${doc.status_message}` : ".") });
    } else {
      patch(id, { status: "saved", message: (fr ? `Enregistré et indexé — ${chunks} segment${chunks > 1 ? "s" : ""}` : `Saved and indexed — ${chunks} chunk${chunks > 1 ? "s" : ""}`)
        + (doc.status_message ? (fr ? `. À noter : ${doc.status_message}` : `. Note: ${doc.status_message}`) : "") });
    }
    onAdd?.(doc);
  };

  // L'indexation se fait en arrière-plan sur le serveur : son état est relu jusqu'au résultat.
  useEffect(() => {
    if (!indexingKey) return;
    let stopped = false;
    const check = async () => {
      for (const item of indexing) {
        try {
          const res = await fetch("/api/knowledge/" + encodeURIComponent(item.docId!));
          if (stopped) return;
          if (res.status === 404) { patch(item.id, { status: "failed", message: fr ? "Le document a été supprimé pendant son indexation." : "The document was deleted while indexing." }); continue; }
          if (!res.ok) continue;                       // panne passagère : on relira
          const doc = await res.json();
          if (!stopped && doc.status !== "processing") settle(item.id, doc);
        } catch { /* réseau coupé un instant : on relira */ }
      }
    };
    const timer = window.setInterval(check, POLL_MS);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [indexingKey]);

  const addFiles = useCallback((list: FileList | File[] | null) => {
    const files = Array.from(list || []);
    // Le champ est vidé : choisir de nouveau le même fichier doit le reproposer.
    if (inputRef.current) inputRef.current.value = "";
    if (!files.length) return;
    setQueue(prev => [
      ...files.map((file, i) => {
        const reason = refusalReason(file, fr);
        return { id: `${Date.now()}-${i}-${file.name}`, file, name: file.name, size: file.size,
                 status: (reason ? "refused" : "ready") as Status, message: reason };
      }),
      ...prev,
    ]);
  }, [fr]);

  const sendOne = async (item: Item): Promise<void> => {
    patch(item.id, { status: "sending", message: "", startedAt: Date.now(), percent: 0 });
    const body = new FormData();
    body.append("files", item.file);
    // Type de contenu : distingue le RAG métier des dossiers client. Le serveur valide la valeur.
    body.append("bulk_metadata", JSON.stringify({ domaine: "Financial", sensibilite: "professionnel", content_type: contentType,
      ...(placement?.clientId ? { client_id: placement.clientId } : {}),
      ...(placement?.projectId ? { project_id: placement.projectId } : {}) }));
    try {
      const { status, data } = await postWithProgress("/api/knowledge/ingest", body, percent => patch(item.id, { percent }));
      if (status < 200 || status >= 300) {
        patch(item.id, { status: "failed", message: failureMessage(status, typeof data?.detail === "string" ? data.detail : "", fr) });
        return;
      }
      const doc = (data.documents || data.results || [])[0];
      if (!doc) {
        patch(item.id, { status: "failed", message: fr ? "Le serveur n'a pas enregistré ce fichier (type non accepté)." : "The server did not store this file (type not accepted)." });
        return;
      }
      if (doc.status === "processing" && doc.doc_id) {
        // Reçu par le serveur : l'indexation continue en arrière-plan, même si cette page est fermée.
        patch(item.id, { status: "indexing", docId: doc.doc_id, startedAt: Date.now(), message: "" });
        onAdd?.(doc);
        return;
      }
      settle(item.id, doc);
    } catch {
      patch(item.id, { status: "failed", message: failureMessage(0, "", fr) });
    }
  };

  const save = async () => {
    if (busy) return;
    const todo = queue.filter(q => q.status === "ready");
    if (!todo.length) return;
    setBusy(true);
    // Un fichier à la fois : chaque résultat est connu, et une requête ne porte qu'un document.
    for (const item of [...todo].reverse()) await sendOne(item);
    setBusy(false);
  };

  const retry = (id: string) => patch(id, { status: "ready", message: "" });
  const remove = (id: string) => setQueue(prev => prev.filter(q => q.id !== id));
  const clearDone = () => setQueue(prev => prev.filter(q => q.status === "ready" || q.status === "sending" || q.status === "indexing"));

  const ready = queue.filter(q => q.status === "ready").length;
  const finished = queue.some(q => q.status !== "ready" && q.status !== "sending" && q.status !== "indexing");
  const tone: Record<Status, string> = { ready: "var(--ink-3)", sending: "var(--accent)", indexing: "var(--accent)", saved: "#10B981", partial: "#d99a00",
    unreadable: "#d99a00", failed: "#e5484d", refused: "#e5484d" };
  const where = destinationLabel ? (fr ? ` dans ${destinationLabel}` : ` to ${destinationLabel}`) : "";

  return (
    <section data-testid="upload-zone" style={{ marginTop: 14 }}>
      <div role="button" tabIndex={0} aria-label={fr ? "Choisir des documents" : "Choose documents"}
        onDrop={e => { e.preventDefault(); setDrag(false); addFiles(e.dataTransfer.files); }}
        onDragOver={e => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
        onClick={() => inputRef.current?.click()}
        onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); inputRef.current?.click(); } }}
        style={{ border: `2px dashed ${drag ? "var(--accent)" : "var(--line-2)"}`, background: drag ? "var(--accent-soft)" : "transparent",
                 borderRadius: 14, padding: "22px 20px", textAlign: "center", cursor: "pointer" }}>
        <div style={{ fontSize: 14, fontWeight: 500, color: "var(--ink)", marginBottom: 6 }}>
          {fr ? "Choisir des documents ou les glisser ici" : "Choose documents or drop them here"}
        </div>
        <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
          {fr ? `PDF, Word, Excel, PowerPoint, CSV, texte, images — ${MAX_FILE_MB} Mo maximum par fichier`
              : `PDF, Word, Excel, PowerPoint, CSV, text, images — ${MAX_FILE_MB} MB maximum per file`}
        </div>
        <input ref={inputRef} data-testid="upload-input" type="file" multiple style={{ display: "none" }}
          accept={ALLOWED_EXTENSIONS.map(e => "." + e).join(",")} onChange={e => addFiles(e.target.files)} />
      </div>

      {queue.length > 0 && (
        <div style={{ border: "1px solid var(--line)", borderRadius: 12, marginTop: 10, overflow: "hidden" }}>
          <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", padding: "12px 14px", borderBottom: "1px solid var(--line)" }}>
            <span style={{ flex: 1, minWidth: 160, fontSize: 13, color: "var(--ink-2)" }}>
              {busy ? (fr ? "Envoi en cours… gardez cette page ouverte jusqu'à la fin de l'envoi." : "Uploading… keep this page open until the upload ends.")
                : indexing.length ? (fr ? `${indexing.length} document${indexing.length > 1 ? "s" : ""} reçu${indexing.length > 1 ? "s" : ""}, indexation en cours sur le serveur. Vous pouvez quitter cette page.`
                                        : `${indexing.length} document${indexing.length > 1 ? "s" : ""} received, indexing on the server. You can leave this page.`)
                : ready ? (fr ? `${ready} document${ready > 1 ? "s" : ""} prêt${ready > 1 ? "s" : ""} à enregistrer${where}` : `${ready} document${ready > 1 ? "s" : ""} ready to save${where}`)
                : (fr ? "Terminé." : "Done.")}
            </span>
            {finished && !busy && <button className="btn" onClick={clearDone}>{fr ? "Effacer la liste" : "Clear list"}</button>}
            <button className="btn btn-primary" data-testid="upload-save" onClick={save} disabled={busy || !ready}
              style={{ padding: "9px 18px", opacity: busy || !ready ? 0.5 : 1 }}>
              {busy ? (fr ? "Envoi…" : "Uploading…") : (fr ? `Enregistrer${ready > 1 ? ` (${ready})` : ""}` : `Save${ready > 1 ? ` (${ready})` : ""}`)}
            </button>
          </div>

          {queue.map(item => {
            const active = item.status === "sending" || item.status === "indexing";
            const seconds = active && item.startedAt ? Math.floor((Date.now() - item.startedAt) / 1000) : 0;
            const elapsed = seconds >= 60 ? `${Math.floor(seconds / 60)} min ${seconds % 60} s` : `${seconds} s`;
            const text = item.status === "ready" ? (fr ? "Prêt — cliquez « Enregistrer »" : "Ready — click “Save”")
              : item.status === "sending"
                ? (fr ? `Envoi au serveur… ${item.percent ?? 0} %` : `Uploading… ${item.percent ?? 0}%`)
              : item.status === "indexing"
                ? (fr ? `Reçu. Indexation en cours sur le serveur… ${elapsed}` : `Received. Indexing on the server… ${elapsed}`)
                  + (seconds >= 30 ? (fr ? " (un gros document ou un document numérisé peut prendre plusieurs minutes)" : " (a large or scanned document can take several minutes)") : "")
                : item.message;
            return (
              <div key={item.id} data-testid="upload-row" data-status={item.status}
                style={{ display: "flex", gap: 10, alignItems: "flex-start", padding: "11px 14px", borderBottom: "1px solid var(--line)" }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
                    <span style={{ fontSize: 13, color: "var(--ink)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{item.name}</span>
                    <span style={{ fontSize: 11, color: "var(--ink-3)", flexShrink: 0 }}>{fileSize(item.size)}</span>
                  </div>
                  <div role={item.status === "failed" || item.status === "refused" ? "alert" : "status"}
                    style={{ fontSize: 12, color: tone[item.status], marginTop: 3, lineHeight: 1.4 }}>{text}</div>
                  {item.status === "sending" && (
                    <div className="set-meter" style={{ marginTop: 6 }}><span style={{ width: `${item.percent ?? 0}%` }} /></div>
                  )}
                </div>
                {item.status === "failed" && !busy && <button className="btn" onClick={() => retry(item.id)}>{fr ? "Réessayer" : "Retry"}</button>}
                {!active && !busy && (
                  <button className="btn" aria-label={(fr ? "Retirer " : "Remove ") + item.name} onClick={() => remove(item.id)} style={{ padding: "6px 10px" }}>✕</button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
