// Invitations (administrateur) : créer un code à usage unique, le transmettre, le révoquer.
import React, { useCallback, useEffect, useState } from "react";

type Invitation = { id: number; label: string | null; status: "active" | "used" | "expired" | "revoked";
  created_at: string; expires_at: string; used_at: string | null; used_by: string | null };

const STATUS_FR: Record<string, string> = { active: "Active", used: "Utilisée", expired: "Expirée", revoked: "Révoquée" };
const MODE_FR: Record<string, string> = {
  invite: "sur invitation", open: "ouverte à tous", closed: "fermée",
};

export function invitationLink(code: string): string {
  return `${window.location.origin}/register?code=${encodeURIComponent(code)}`;
}

export function InvitationsPanel({ lang }: { lang: string }) {
  const fr = lang === "fr";
  const [items, setItems] = useState<Invitation[]>([]);
  const [mode, setMode] = useState("invite");
  const [label, setLabel] = useState("");
  const [days, setDays] = useState(14);
  const [created, setCreated] = useState<{ code: string; label: string | null } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/admin/invitations");
      if (!res.ok) return;
      const data = await res.json();
      setItems(data.invitations || []);
      setMode(data.mode || "invite");
    } catch { /* la liste reste telle quelle */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  const create = async () => {
    setBusy(true); setError("");
    try {
      const res = await fetch("/api/admin/invitations", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: label.trim() || null, days }) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.detail || `Erreur ${res.status}`);
      setCreated({ code: data.code, label: data.label });
      setLabel("");
      load();
    } catch (e: any) { setError(e?.message || "Erreur"); }
    setBusy(false);
  };

  const revoke = async (id: number) => {
    setError("");
    const res = await fetch(`/api/admin/invitations/${id}`, { method: "DELETE" });
    if (!res.ok) { const d = await res.json().catch(() => ({})); setError(d?.detail || `Erreur ${res.status}`); }
    load();
  };

  const copy = (text: string) => { try { navigator.clipboard?.writeText(text); } catch { /* copie manuelle */ } };
  const field: React.CSSProperties = { background: "var(--bg)", border: "1px solid var(--line-2)", borderRadius: 8, padding: "9px 12px", color: "var(--ink)", fontSize: 13, fontFamily: "inherit" };
  const button: React.CSSProperties = { background: "var(--accent-soft)", border: "1px solid var(--accent-line)", borderRadius: 8, padding: "7px 12px", color: "var(--accent)", fontSize: 12.5, cursor: "pointer" };

  return (
    <section data-testid="invitations-panel" className="set-card">
      <div className="set-h">{fr ? "Inviter un client" : "Invite a client"}</div>
      <div className="set-sub">
        {fr ? `Inscription ${MODE_FR[mode] || mode}. Chaque code sert une seule fois.` : `Registration mode: ${mode}. Each code works once.`}
      </div>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 12 }}>
        <input aria-label={fr ? "Destinataire de l'invitation" : "Invitation recipient"} value={label} onChange={e => setLabel(e.target.value)}
          placeholder={fr ? "Pour qui ? (ex. Cabinet Tremblay)" : "For whom?"} style={{ ...field, flex: 1, minWidth: 180 }} />
        <select aria-label={fr ? "Durée de validité" : "Validity"} value={days} onChange={e => setDays(Number(e.target.value))} style={field}>
          {[7, 14, 30, 90].map(d => <option key={d} value={d}>{d} {fr ? "jours" : "days"}</option>)}
        </select>
        <button className="btn btn-primary" onClick={create} disabled={busy} style={{ padding: "9px 16px" }}>{fr ? "Créer une invitation" : "Create invitation"}</button>
      </div>

      {created && (
        <div role="status" style={{ border: "1px solid var(--accent-line)", background: "var(--accent-soft)", borderRadius: 10, padding: 12, marginBottom: 12 }}>
          <div style={{ fontSize: 12, color: "var(--ink-2)", marginBottom: 6 }}>
            {fr ? "Copiez ce code maintenant : il ne sera plus affiché." : "Copy this code now: it will not be shown again."}
          </div>
          <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <code data-testid="invitation-code" style={{ fontSize: 16, letterSpacing: "0.1em", color: "var(--ink)" }}>{created.code}</code>
            <button onClick={() => copy(created.code)} style={button}>{fr ? "Copier le code" : "Copy code"}</button>
            <button onClick={() => copy(invitationLink(created.code))} style={button}>{fr ? "Copier le lien" : "Copy link"}</button>
          </div>
          <div style={{ fontSize: 11, color: "var(--ink-3)", marginTop: 6, wordBreak: "break-all" }}>{invitationLink(created.code)}</div>
        </div>
      )}
      {error && <div role="alert" style={{ color: "#e5484d", fontSize: 12, marginBottom: 10 }}>{error}</div>}

      <div className="set-h" style={{ marginTop: 20, marginBottom: 8 }}>{fr ? "Invitations envoyées" : "Sent invitations"}</div>
      {items.length === 0
        ? <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{fr ? "Aucune invitation pour l'instant." : "No invitation yet."}</div>
        : items.map(item => (
          <div key={item.id} style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", padding: "10px 0", borderTop: "1px solid var(--line)", fontSize: 13, color: "var(--ink-2)" }}>
            <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {item.label || (fr ? "Sans nom" : "Unnamed")}{item.used_by ? ` — ${item.used_by}` : ""}
            </span>
            <span style={{ color: item.status === "active" ? "var(--accent)" : "var(--ink-3)" }}>{fr ? STATUS_FR[item.status] : item.status}</span>
            <span style={{ color: "var(--ink-3)" }}>{new Date(item.expires_at).toLocaleDateString(fr ? "fr-CA" : "en-CA")}</span>
            {item.status === "active" && <button onClick={() => revoke(item.id)} style={{ ...button, padding: "3px 8px" }}>{fr ? "Révoquer" : "Revoke"}</button>}
          </div>
        ))}
    </section>
  );
}
