// src/components/SettingsView.tsx
// Paramètres, en onglets : une seule chose à l'écran à la fois, dans une colonne lisible.
// Un utilisateur ne voit que sa propre consommation. Tout le reste (invitations, budgets de
// tous les comptes, modèles des agents, clé et préférences) est réservé à l'administrateur.
import React from "react";
import { OPENROUTER_MODELS, useLocalStorage } from '../utils/uiHelpers';
import Avatar from './Avatar';
import { AGENTS_STUDIO, AGENTS_DEF } from '../utils/agentsConfig';
import { InvitationsPanel } from './InvitationsPanel';
import { BudgetsPanel } from './BudgetsPanel';
import { money, BUDGET_REFRESH_EVENT } from './BudgetBadge';

export const DEFAULT_MODEL = "deepseek/deepseek-v4-pro";

// Une entrée par modèle réellement appelé : la liste affiche l'identifiant exact du modèle.
const MODEL_CHOICES: { id: string; provider: string }[] = (() => {
  const seen = new Set<string>();
  return (OPENROUTER_MODELS as any[]).filter(m => !seen.has(m.id) && seen.add(m.id))
    .map(m => ({ id: m.id, provider: m.id.split("/")[0] }));
})();
const PROVIDERS = Array.from(new Set(MODEL_CHOICES.map(m => m.provider)));

type Tab = "usage" | "access" | "budgets" | "models" | "general";

type Usage = { unlimited: boolean; budget_cad: number | null; spent_cad: number; remaining_cad: number | null;
  percent: number; warning: boolean; blocked: boolean; reason: string | null };

// Consommation du compte connecté : ce qu'il a dépensé, ce qui lui reste. Rien sur les autres comptes.
export function MyUsagePanel({ lang }: { lang: string }) {
  const fr = lang === "fr";
  const [usage, setUsage] = React.useState<Usage | null>(null);
  const [failed, setFailed] = React.useState(false);

  const load = React.useCallback(async () => {
    try {
      const res = await fetch("/api/budget");
      if (!res.ok) { setFailed(true); return; }
      setUsage(await res.json()); setFailed(false);
    } catch { setFailed(true); }
  }, []);
  React.useEffect(() => {
    load();
    window.addEventListener(BUDGET_REFRESH_EVENT, load);
    return () => window.removeEventListener(BUDGET_REFRESH_EVENT, load);
  }, [load]);

  const danger = "#e5484d", warn = "#d99a00";
  const tone = usage?.blocked ? danger : usage?.warning ? warn : undefined;
  const notice = !usage ? "" : usage.reason === "platform"
    ? (fr ? "Le service est momentanément suspendu. Il reprendra sous peu." : "The service is paused for the moment. It will resume shortly.")
    : usage.blocked
      ? (fr ? "Votre budget d'essai est épuisé. Contactez-nous pour poursuivre." : "Your trial budget is used up. Contact us to continue.")
      : usage.warning
        ? (fr ? `Il vous reste ${money(usage.remaining_cad, lang)}. Contactez-nous pour poursuivre après l'essai.`
              : `${money(usage.remaining_cad, lang)} left. Contact us to continue after the trial.`)
        : "";

  return (
    <section data-testid="my-usage-panel" className="set-card">
      <div className="set-h">{fr ? "Ma consommation" : "My usage"}</div>
      <div className="set-sub">
        {fr ? "Coût réel de vos questions et analyses, tous modèles confondus, en dollars canadiens."
            : "Actual cost of your questions and analyses, across all models, in Canadian dollars."}
      </div>
      {failed && !usage && <div role="alert" style={{ fontSize: 13, color: "var(--ink-3)" }}>{fr ? "Consommation indisponible pour l'instant." : "Usage unavailable for now."}</div>}
      {usage && (
        <>
          <div className="set-stats">
            <div className="set-stat">
              <div className="set-stat-label">{fr ? "Dépensé" : "Spent"}</div>
              <div className="set-stat-value" data-testid="usage-spent" style={tone ? { color: tone } : undefined}>{money(usage.spent_cad, lang)}</div>
              {!usage.unlimited && <div className="set-meter"><span style={{ width: `${usage.percent}%`, background: tone }} /></div>}
            </div>
            {!usage.unlimited && (
              <>
                <div className="set-stat">
                  <div className="set-stat-label">{fr ? "Budget d'essai" : "Trial budget"}</div>
                  <div className="set-stat-value" data-testid="usage-budget">{money(usage.budget_cad, lang)}</div>
                </div>
                <div className="set-stat">
                  <div className="set-stat-label">{fr ? "Restant" : "Remaining"}</div>
                  <div className="set-stat-value" data-testid="usage-remaining">{money(usage.remaining_cad, lang)}</div>
                </div>
              </>
            )}
          </div>
          {notice && <div role="status" style={{ fontSize: 13, color: tone || "var(--ink-2)" }}>{notice}</div>}
        </>
      )}
    </section>
  );
}

export function SettingsView({ lang, authFetch, isAdmin = false }: any) {
  const fr = lang === "fr";
  const [key, setKey] = useLocalStorage("z12-openrouter-key", "");
  const [settings, setSettings] = useLocalStorage("z12-agent-settings", {});
  const [testResult, setTestResult] = React.useState<string>("");
  const [testing, setTesting] = React.useState(false);

  const tabs: { id: Tab; label: string }[] = isAdmin ? [
    { id: "access", label: fr ? "Invitations" : "Invitations" },
    { id: "budgets", label: fr ? "Budgets" : "Budgets" },
    { id: "models", label: fr ? "Modèles des agents" : "Agent models" },
    { id: "general", label: fr ? "Clé et préférences" : "Key and preferences" },
  ] : [
    { id: "usage", label: fr ? "Ma consommation" : "My usage" },
  ];
  const [tab, setTab] = React.useState<Tab | null>(null);
  // Le statut administrateur arrive après le premier rendu : l'onglet par défaut le suit.
  const current: Tab = tab && tabs.some(t => t.id === tab) ? tab : tabs[0].id;

  const testConnection = async () => {
    setTesting(true); setTestResult("");
    try {
      const r = await fetch("https://openrouter.ai/api/v1/models", { headers: { Authorization: `Bearer ${key}` } });
      if (r.ok) {
        setTestResult(fr ? "Connexion réussie" : "Connection successful");
        if (authFetch) {
          try { await authFetch("/api/settings/save-key", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ api_key: key }) }); }
          catch (e) { console.warn("save-key DB error:", e); }
        }
      } else setTestResult(fr ? "Clé invalide" : "Invalid key");
    } catch { setTestResult(fr ? "Erreur réseau" : "Network error"); }
    setTesting(false);
  };

  const resetAgent = (agentId: string) => {
    const next = { ...settings };
    delete next[agentId];
    setSettings(next);
  };

  return (
    <main className="page" data-screen-label="Settings">
      <header className="page-head">
        <div>
          <div className="page-title">{fr ? "Paramètres" : "Settings"}</div>
          <div className="page-sub">
            {!isAdmin ? (fr ? "Votre consommation" : "Your usage")
              : fr ? `${MODEL_CHOICES.length} modèles · ${PROVIDERS.length} fournisseurs, via OpenRouter`
                   : `${MODEL_CHOICES.length} models · ${PROVIDERS.length} providers, via OpenRouter`}
          </div>
        </div>
      </header>

      <div className="set-tabs" role="tablist" aria-label={fr ? "Sections des paramètres" : "Settings sections"}>
        {tabs.map(t => (
          <button key={t.id} role="tab" aria-selected={current === t.id} data-testid={`settings-tab-${t.id}`}
            className={"set-tab" + (current === t.id ? " on" : "")} onClick={() => setTab(t.id)}>{t.label}</button>
        ))}
      </div>

      <div className="page-body">
        <div className="set-body" role="tabpanel">
          {current === "usage" && <MyUsagePanel lang={lang} />}
          {current === "access" && isAdmin && <InvitationsPanel lang={lang} />}
          {current === "budgets" && isAdmin && <BudgetsPanel lang={lang} />}

          {current === "models" && isAdmin && (
            <div className="set-card">
              <div className="set-h">{fr ? "Modèle assigné par agent" : "Model assigned per agent"}</div>
              <div className="set-sub">
                {fr ? `Par défaut : ${DEFAULT_MODEL}. Le nom affiché est celui du modèle réellement appelé.`
                    : `Default: ${DEFAULT_MODEL}. The name shown is the model actually called.`}
              </div>
              {AGENTS_DEF.map((a: any) => {
                const sa = AGENTS_STUDIO.find((x: any) => x.id === a.id) || AGENTS_STUDIO[0];
                const cur = settings[a.id]?.model || DEFAULT_MODEL;
                const custom = !!settings[a.id]?.model && settings[a.id].model !== DEFAULT_MODEL;
                return (
                  <div className="set-row" key={a.id}>
                    <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
                      <Avatar agent={sa} size={26} />
                      <div className="agent-name">{sa.name}</div>
                    </div>
                    <select className="set-select" value={cur} aria-label={(fr ? "Modèle de " : "Model for ") + sa.name}
                      onChange={(e: any) => setSettings({ ...settings, [a.id]: { ...settings[a.id], model: e.target.value } })}>
                      {!MODEL_CHOICES.some(m => m.id === cur) && <option value={cur}>{cur}</option>}
                      {PROVIDERS.map(provider => (
                        <optgroup key={provider} label={provider}>
                          {MODEL_CHOICES.filter(m => m.provider === provider).map(m => <option key={m.id} value={m.id}>{m.id.split("/").pop()}</option>)}
                        </optgroup>
                      ))}
                    </select>
                    <button className="btn" onClick={() => resetAgent(a.id)} disabled={!custom}
                      style={custom ? undefined : { opacity: 0.4, cursor: "default" }}
                      title={fr ? "Revenir au modèle par défaut" : "Back to the default model"}>
                      {fr ? "Par défaut" : "Default"}
                    </button>
                  </div>
                );
              })}
            </div>
          )}

          {current === "general" && isAdmin && (
            <>
              <div className="set-card">
                <div className="set-h">{fr ? "Clé API OpenRouter de secours" : "Fallback OpenRouter API key"}</div>
                <div className="set-sub">
                  {fr ? "Facultatif. La plateforme utilise la clé configurée sur le serveur. Cette clé ne sert que si le serveur n'en a pas."
                      : "Optional. The platform uses the key configured on the server. This key is only used if the server has none."}
                </div>
                <input className="set-input" value={key} onChange={(e: any) => setKey(e.target.value)} type="password"
                  placeholder="sk-or-v1-…" aria-label={fr ? "Clé API OpenRouter" : "OpenRouter API key"} />
                <div style={{ display: "flex", gap: 8, marginTop: 12, alignItems: "center", flexWrap: "wrap" }}>
                  <button className="btn btn-primary" onClick={testConnection} disabled={testing || !key}>
                    {testing ? (fr ? "Test en cours…" : "Testing…") : (fr ? "Tester la connexion" : "Test connection")}
                  </button>
                  <button className="btn" onClick={() => { setKey(""); setTestResult(""); }} disabled={!key}>{fr ? "Effacer" : "Clear"}</button>
                  {testResult && <span role="status" style={{ fontSize: 12.5, color: "var(--ink-2)" }}>{testResult}</span>}
                </div>
              </div>

              <div className="set-card">
                <div className="set-h">{fr ? "Préférences" : "Preferences"}</div>
                <div className="set-sub">{fr ? "Réglages de la plateforme, en lecture seule." : "Platform settings, read-only."}</div>
                <div className="set-row"><div>{fr ? "Région des données" : "Data region"}</div><div className="set-select" style={{ cursor: "default" }}>S3 ca-central-1</div><div></div></div>
                <div className="set-row"><div>{fr ? "Modèle de l'orchestrateur" : "Orchestrator model"}</div><div className="set-select" style={{ cursor: "default" }}>{DEFAULT_MODEL}</div><div></div></div>
                <div className="set-row"><div>{fr ? "RAG — seuil cosinus" : "RAG — cosine threshold"}</div><div className="set-select" style={{ cursor: "default" }}>0.6</div><div></div></div>
              </div>
            </>
          )}
        </div>
      </div>
    </main>
  );
}
