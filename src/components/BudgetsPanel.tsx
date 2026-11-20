// Budgets (administrateur) : consommation réelle par compte et plafond réglable compte par compte.
import React, { useCallback, useEffect, useState } from "react";
import { money } from "./BudgetBadge";

type Account = { user_id: string; email: string | null; is_admin: boolean; calls: number; unlimited: boolean;
  budget_cad: number | null; spent_cad: number; percent: number; warning: boolean; blocked: boolean };
type Overview = { accounts: Account[]; default_budget_cad: number;
  platform: { month_spent_cad: number; cap_cad: number | null; blocked: boolean } };

export function BudgetsPanel({ lang }: { lang: string }) {
  const fr = lang === "fr";
  const [data, setData] = useState<Overview | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/admin/budgets");
      if (res.ok) setData(await res.json());
    } catch { /* on garde le dernier état connu */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = async (userId: string, value: number | null) => {
    setError("");
    const res = await fetch(`/api/admin/budgets/${encodeURIComponent(userId)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ budget_cad: value }) });
    if (!res.ok) { const d = await res.json().catch(() => ({})); setError(d?.detail || `Erreur ${res.status}`); return; }
    setEdits(current => { const next = { ...current }; delete next[userId]; return next; });
    load();
  };

  const apply = (account: Account) => {
    const raw = (edits[account.user_id] ?? "").replace(",", ".").trim();
    const value = Number(raw);
    if (!raw || !Number.isFinite(value) || value < 0) { setError(fr ? "Montant invalide" : "Invalid amount"); return; }
    save(account.user_id, value);
  };

  const field: React.CSSProperties = { width: 84, background: "var(--bg)", border: "1px solid var(--line-2)", borderRadius: 7, padding: "6px 8px", color: "var(--ink)", fontSize: 13 };
  if (!data) return null;

  const cap = data.platform.cap_cad;
  const platformPercent = cap ? Math.min(100, Math.round(100 * data.platform.month_spent_cad / cap)) : 0;
  const danger = "#e5484d", warn = "#d99a00";

  return (
    <section data-testid="budgets-panel" className="set-card">
      <div className="set-h">{fr ? "Budgets de consommation" : "Usage budgets"}</div>
      <div className="set-sub">
        {fr ? "Dépense réelle de chaque compte, tous modèles confondus, en dollars canadiens."
            : "Actual spending per account, across all models, in Canadian dollars."}
      </div>

      <div className="set-stats">
        <div className="set-stat" data-testid="platform-budget">
          <div className="set-stat-label">{fr ? "Plateforme ce mois-ci" : "Platform this month"}</div>
          <div className="set-stat-value" style={data.platform.blocked ? { color: danger } : undefined}>
            {money(data.platform.month_spent_cad, lang)}{cap != null && ` / ${money(cap, lang)}`}
          </div>
          {cap != null && <div className="set-meter"><span style={{ width: `${platformPercent}%`, background: data.platform.blocked ? danger : undefined }} /></div>}
        </div>
        <div className="set-stat">
          <div className="set-stat-label">{fr ? "Budget d'essai par défaut" : "Default trial budget"}</div>
          <div className="set-stat-value">{money(data.default_budget_cad, lang)}</div>
          <div style={{ fontSize: 11.5, color: "var(--ink-3)", marginTop: 6 }}>{fr ? "Par compte invité, au total" : "Per invited account, in total"}</div>
        </div>
      </div>

      {error && <div role="alert" style={{ color: danger, fontSize: 13, marginBottom: 8 }}>{error}</div>}
      {data.accounts.length === 0
        ? <div style={{ fontSize: 13, color: "var(--ink-3)" }}>{fr ? "Aucune consommation pour l'instant." : "No usage yet."}</div>
        : (
          <div style={{ overflowX: "auto" }}>
            <table className="set-table">
              <thead>
                <tr>
                  <th>{fr ? "Compte" : "Account"}</th>
                  <th className="num">{fr ? "Dépensé / budget" : "Spent / budget"}</th>
                  <th className="num">{fr ? "Appels" : "Calls"}</th>
                  <th>{fr ? "Modifier le budget" : "Change budget"}</th>
                </tr>
              </thead>
              <tbody>
                {data.accounts.map(account => (
                  <tr key={account.user_id}>
                    <td style={{ color: "var(--ink)", wordBreak: "break-word" }}>
                      {account.email || `${fr ? "Compte" : "Account"} ${account.user_id}`}{account.is_admin ? (fr ? " (administrateur)" : " (admin)") : ""}
                    </td>
                    <td className="num" style={{ color: account.blocked ? danger : account.warning ? warn : undefined }}>
                      {money(account.spent_cad, lang)}{account.unlimited ? (fr ? " · sans limite" : " · no limit") : ` / ${money(account.budget_cad, lang)} (${account.percent} %)`}
                    </td>
                    <td className="num">{account.calls}</td>
                    <td>
                      {account.unlimited
                        ? <span style={{ color: "var(--ink-3)" }}>—</span>
                        : (
                          <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                            <input aria-label={(fr ? "Budget de " : "Budget for ") + (account.email || account.user_id)} inputMode="decimal"
                              value={edits[account.user_id] ?? ""} placeholder={String(account.budget_cad ?? "")}
                              onChange={e => setEdits(current => ({ ...current, [account.user_id]: e.target.value }))} style={field} />
                            <button className="btn btn-primary" onClick={() => apply(account)}>{fr ? "Fixer" : "Set"}</button>
                            <button className="btn" onClick={() => save(account.user_id, null)}
                              title={fr ? "Revenir au budget d'essai par défaut" : "Back to the default trial budget"}>{fr ? "Défaut" : "Default"}</button>
                          </div>
                        )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
    </section>
  );
}
