// Budget d'essai du compte : montant dépensé sur le montant accordé, en dollars canadiens.
// Rien n'est affiché pour un compte sans limite (administrateur).
import React, { useCallback, useEffect, useState } from "react";

export const BUDGET_REFRESH_EVENT = "z12-budget-refresh";
export function refreshBudget(): void {
  try { window.dispatchEvent(new Event(BUDGET_REFRESH_EVENT)); } catch { /* hors navigateur */ }
}

export function money(value: number | null | undefined, lang = "fr"): string {
  const amount = (value ?? 0).toFixed(2);
  return lang === "fr" ? `${amount.replace(".", ",")} $` : `$${amount}`;
}

type Budget = { unlimited: boolean; budget_cad: number | null; spent_cad: number; remaining_cad: number | null;
  percent: number; warning: boolean; blocked: boolean; reason: string | null };

export function BudgetBadge({ lang }: { lang: string }) {
  const fr = lang === "fr";
  const [budget, setBudget] = useState<Budget | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/budget");
      if (res.ok) setBudget(await res.json());
    } catch { /* on garde le dernier état connu */ }
  }, []);

  useEffect(() => {
    load();
    window.addEventListener(BUDGET_REFRESH_EVENT, load);
    const timer = window.setInterval(load, 60_000);
    return () => { window.removeEventListener(BUDGET_REFRESH_EVENT, load); window.clearInterval(timer); };
  }, [load]);

  if (!budget || (budget.unlimited && !budget.blocked)) return null;

  const platform = budget.reason === "platform";
  const color = budget.blocked ? "#e5484d" : budget.warning ? "#d99a00" : "var(--ink-3)";
  const text = platform
    ? (fr ? "Service suspendu : budget de la plateforme atteint" : "Service paused: platform budget reached")
    : budget.blocked
      ? (fr ? `Budget d'essai épuisé (${money(budget.budget_cad, lang)}) — contactez-nous pour poursuivre`
            : `Trial budget used up (${money(budget.budget_cad, lang)}) — contact us to continue`)
      : `${fr ? "Essai" : "Trial"} : ${money(budget.spent_cad, lang)} / ${money(budget.budget_cad, lang)}`;
  const title = budget.warning
    ? (fr ? `Il vous reste ${money(budget.remaining_cad, lang)}. Contactez-nous pour poursuivre après l'essai.`
          : `${money(budget.remaining_cad, lang)} left. Contact us to continue after the trial.`)
    : (fr ? "Consommation de votre essai, tous modèles confondus" : "Trial usage across all models");

  return (
    <span data-testid="budget-badge" role={budget.blocked || budget.warning ? "status" : undefined} title={title}
      style={{ fontSize: 11, color, border: `1px solid ${budget.blocked || budget.warning ? color : "var(--line)"}`,
               borderRadius: 999, padding: "3px 10px", whiteSpace: "nowrap" }}>
      {text}
    </span>
  );
}
