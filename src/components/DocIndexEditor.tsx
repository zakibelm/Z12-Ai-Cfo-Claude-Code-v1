// Description et mots-clés d'un document : ce que la recherche des agents lit en priorité.
// Générés à l'indexation, modifiables ici ; l'enregistrement met à jour l'index tout de suite.
import React, { useState } from "react";

export function parseKeywords(text: string): string[] {
  const seen = new Set<string>();
  return text.split(/[,;\n]/).map((k) => k.trim().replace(/\s+/g, " ")).filter((k) => {
    if (!k || seen.has(k.toLowerCase())) return false;
    seen.add(k.toLowerCase());
    return true;
  });
}

export function DocIndexEditor({ docId, description, keywords, lang, onSaved }: {
  docId: string; description: string; keywords: string[]; lang: string; onSaved?: () => void;
}) {
  const fr = lang === "fr";
  const [text, setText] = useState(description || "");
  const [words, setWords] = useState((keywords || []).join(", "));
  const [state, setState] = useState<{ busy: boolean; message: string; error: boolean }>({ busy: false, message: "", error: false });

  const save = async () => {
    setState({ busy: true, message: "", error: false });
    try {
      const res = await fetch("/api/knowledge/" + encodeURIComponent(docId), {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ description: text, keywords: parseKeywords(words) }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.detail || `Erreur ${res.status}`);
      setText(data.description || "");
      setWords((data.keywords || []).join(", "));
      setState({ busy: false, message: fr ? "Enregistré : la recherche des agents est à jour." : "Saved: agent search is up to date.", error: false });
      onSaved?.();
    } catch (e: any) {
      setState({ busy: false, message: e?.message || "Erreur", error: true });
    }
  };

  const label: React.CSSProperties = { fontSize: 10, fontWeight: 500, color: "var(--ink-3)", marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.06em" };
  const field: React.CSSProperties = { width: "100%", boxSizing: "border-box", background: "transparent", border: "1px solid var(--line-2)", borderRadius: 8, padding: "8px 10px", color: "var(--ink)", fontSize: 12, fontFamily: "inherit" };

  return (
    <div data-testid="doc-index-editor" style={{ marginBottom: 12 }}>
      <div style={label}>{fr ? "Description (lue par la recherche des agents)" : "Description (read by agent search)"}</div>
      <textarea aria-label={fr ? "Description du document" : "Document description"} rows={2} maxLength={600}
        value={text} onChange={(e) => setText(e.target.value)} style={{ ...field, resize: "vertical", marginBottom: 8 }} />
      <div style={label}>{fr ? "Mots-clés (séparés par des virgules)" : "Keywords (comma-separated)"}</div>
      <input aria-label={fr ? "Mots-clés du document" : "Document keywords"} value={words}
        onChange={(e) => setWords(e.target.value)} style={{ ...field, marginBottom: 8 }} />
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <button onClick={save} disabled={state.busy}
          style={{ background: "var(--accent-soft)", border: "1px solid var(--accent-line)", borderRadius: 8, padding: "6px 12px", color: "var(--accent)", fontSize: 12, cursor: state.busy ? "wait" : "pointer" }}>
          {state.busy ? (fr ? "Enregistrement…" : "Saving…") : (fr ? "Enregistrer" : "Save")}
        </button>
        {state.message && <span role={state.error ? "alert" : "status"} style={{ fontSize: 12, color: state.error ? "#e5484d" : "var(--ink-2)" }}>{state.message}</span>}
      </div>
    </div>
  );
}
