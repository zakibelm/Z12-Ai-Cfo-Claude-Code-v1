// src/components/LoginView.tsx
// Ecran de connexion et d'inscription — Z12 AI CFO Suite
//
// Autonome : toutes les couleurs et la police sont fixees ici. Cet ecran s'affiche
// avant le Studio, dont la feuille de style n'est pas encore chargee.
import React, { useEffect, useState, FormEvent } from "react";
import { LANDING_URL } from "../utils/nav";

const GOOGLE_LOGIN_ENABLED = (import.meta as any).env?.VITE_GOOGLE_LOGIN === "1";
const MIN_PASSWORD = 10;

const C = {
  bg: "#0E0D0B", surface: "#16140F", surface2: "#1C1A14", line: "#2A2720",
  ink: "#F5F2E8", ink2: "#B8B2A0", ink3: "#8A8474", accent: "#10B981",
  warn: "#F0865A", warnBg: "rgba(220,60,0,.12)",
};
const FONT = "'Geist', 'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";

type Mode = "login" | "register";

interface LoginViewProps {
  onLogin: (email: string, password: string) => Promise<boolean>;
  onRegister?: (email: string, password: string, name?: string, inviteCode?: string) => Promise<boolean>;
  initialMode?: Mode;
  loading?: boolean;
  error?: string | null;
}

const inputStyle: React.CSSProperties = {
  width: "100%", boxSizing: "border-box", padding: "10px 14px",
  background: C.surface2, border: `1px solid ${C.line}`, borderRadius: 8,
  fontSize: 14, color: C.ink, fontFamily: FONT, outline: "none",
};
const labelStyle: React.CSSProperties = { fontSize: 12, color: C.ink2, display: "block", marginBottom: 6 };

export function LoginView({ onLogin, onRegister, initialMode = "login", loading = false, error = null }: LoginViewProps) {
  const [mode, setMode] = useState<Mode>(onRegister ? initialMode : "login");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  // Inscription sur invitation : le serveur dit si un code est exigé. Un lien d'invitation
  // (/register?code=…) remplit le champ. Le serveur reste seul juge de la validité du code.
  const [registrationMode, setRegistrationMode] = useState<"invite" | "open" | "closed">("invite");
  const [inviteCode, setInviteCode] = useState(() => {
    try { return new URLSearchParams(window.location.search).get("code") || ""; } catch { return ""; }
  });
  useEffect(() => {
    let cancelled = false;
    fetch("/api/auth/local/registration-mode")
      .then(res => (res.ok ? res.json() : null))
      .then(data => { if (!cancelled && data && ["invite", "open", "closed"].includes(data.mode)) setRegistrationMode(data.mode); })
      .catch(() => { /* sans réponse, on garde le mode le plus strict : le code est demandé */ });
    return () => { cancelled = true; };
  }, []);

  // La page n'a pas encore de feuille de style : on neutralise la marge du navigateur.
  useEffect(() => {
    const previous = { margin: document.body.style.margin, background: document.body.style.background };
    document.body.style.margin = "0";
    document.body.style.background = C.bg;
    return () => {
      document.body.style.margin = previous.margin;
      document.body.style.background = previous.background;
    };
  }, []);

  const switchMode = (next: Mode) => {
    setMode(next);
    setLocalError(null);
    window.history.replaceState(null, "", (next === "register" ? "/register" : "/login") + (next === "register" ? window.location.search : ""));
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setLocalError(null);
    if (!email.trim() || !password) {
      setLocalError("Adresse courriel et mot de passe requis");
      return;
    }
    if (mode === "register" && password.length < MIN_PASSWORD) {
      setLocalError(`Le mot de passe doit contenir au moins ${MIN_PASSWORD} caractères`);
      return;
    }
    if (mode === "register" && registrationMode === "closed") {
      setLocalError("Les inscriptions sont fermées pour le moment");
      return;
    }
    if (mode === "register" && registrationMode === "invite" && !inviteCode.trim()) {
      setLocalError("Un code d'invitation est requis pour créer un compte");
      return;
    }
    setSubmitting(true);
    const ok = mode === "register" && onRegister
      ? await onRegister(email.trim(), password, name.trim() || undefined,
          registrationMode === "invite" ? inviteCode.trim() : undefined)
      : await onLogin(email.trim(), password);
    if (!ok) setSubmitting(false);
  };

  const isRegister = mode === "register";
  const busy = submitting || loading;
  const displayError = localError || error;

  return (
    <div style={{
      minHeight: "100vh", boxSizing: "border-box", background: C.bg, color: C.ink, fontFamily: FONT,
      display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: 24,
    }}>
      <div style={{
        background: C.surface, border: `1px solid ${C.line}`, borderRadius: 16, padding: 40,
        width: "100%", maxWidth: 400, boxSizing: "border-box", boxShadow: "0 24px 80px rgba(0,0,0,.5)",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 32 }}>
          <div style={{
            width: 40, height: 40, borderRadius: 10, background: `linear-gradient(135deg, ${C.accent}, #0E9F8E)`,
            display: "grid", placeItems: "center", color: "#0a0a0a", fontWeight: 700, fontSize: 16,
          }}>Z</div>
          <div>
            <div style={{ fontWeight: 600, fontSize: 15, letterSpacing: "-0.02em", color: C.ink }}>Z12 AI CFO Suite</div>
            <div style={{ fontSize: 11, color: C.ink3, marginTop: 2 }}>Direction financière augmentée</div>
          </div>
        </div>

        <h1 style={{ fontSize: 20, fontWeight: 600, margin: "0 0 6px", letterSpacing: "-0.02em", color: C.ink, fontFamily: FONT }}>
          {isRegister ? "Créer un compte" : "Connexion"}
        </h1>
        <p style={{ fontSize: 13, color: C.ink3, margin: "0 0 28px" }}>
          {isRegister ? "Ouvrez votre accès à l'équipe CPA virtuelle" : "Accédez à votre équipe CPA virtuelle"}
        </p>

        <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {isRegister && (
            <div>
              <label htmlFor="z12-name" style={labelStyle}>Nom (facultatif)</label>
              <input id="z12-name" type="text" autoComplete="name" value={name} onChange={e => setName(e.target.value)}
                disabled={busy} style={inputStyle} />
            </div>
          )}
          <div>
            <label htmlFor="z12-email" style={labelStyle}>Adresse courriel</label>
            <input id="z12-email" type="email" autoComplete="email" value={email} onChange={e => setEmail(e.target.value)}
              placeholder="vous@exemple.com" disabled={busy} style={inputStyle} />
          </div>
          <div>
            <label htmlFor="z12-password" style={labelStyle}>Mot de passe</label>
            <input id="z12-password" type="password" autoComplete={isRegister ? "new-password" : "current-password"}
              value={password} onChange={e => setPassword(e.target.value)} placeholder="••••••••••"
              disabled={busy} style={inputStyle} />
            {isRegister && (
              <div style={{ fontSize: 11, color: C.ink3, marginTop: 6 }}>{MIN_PASSWORD} caractères minimum</div>
            )}
          </div>

          {isRegister && registrationMode === "invite" && (
            <div>
              <label htmlFor="z12-invite" style={labelStyle}>Code d'invitation</label>
              <input id="z12-invite" type="text" autoComplete="off" autoCapitalize="characters" spellCheck={false}
                value={inviteCode} onChange={e => setInviteCode(e.target.value)} placeholder="ABCD-EFGH-JKLM"
                disabled={busy} style={{ ...inputStyle, letterSpacing: "0.08em" }} />
              <div style={{ fontSize: 11, color: C.ink3, marginTop: 6 }}>L'accès se fait sur invitation.</div>
            </div>
          )}
          {isRegister && registrationMode === "closed" && (
            <div style={{ fontSize: 13, color: C.ink2 }}>Les inscriptions sont fermées pour le moment.</div>
          )}

          {displayError && (
            <div role="alert" style={{
              padding: "10px 14px", background: C.warnBg, border: `1px solid ${C.warn}`,
              borderRadius: 8, fontSize: 13, color: C.warn,
            }}>
              {displayError}
            </div>
          )}

          <button type="submit" disabled={busy} style={{
            padding: "11px 14px", borderRadius: 8, border: "none", background: C.accent, color: "#06281d",
            fontSize: 14, fontWeight: 600, fontFamily: FONT, cursor: busy ? "default" : "pointer", opacity: busy ? 0.6 : 1,
          }}>
            {busy ? "Un instant…" : isRegister ? "Créer mon compte" : "Se connecter"}
          </button>
        </form>

        {onRegister && (
          <p style={{ fontSize: 13, color: C.ink3, margin: "20px 0 0", textAlign: "center" }}>
            {isRegister ? "Déjà un compte ? " : "Pas encore de compte ? "}
            <button type="button" onClick={() => switchMode(isRegister ? "login" : "register")} style={{
              background: "none", border: "none", padding: 0, color: C.accent, fontSize: 13, fontFamily: FONT, cursor: "pointer",
            }}>
              {isRegister ? "Se connecter" : "S'inscrire"}
            </button>
          </p>
        )}

        {GOOGLE_LOGIN_ENABLED && (
          <div style={{ marginTop: 20, textAlign: "center" }}>
            <a href="/api/auth/google" style={{
              display: "block", padding: 10, borderRadius: 8, border: `1px solid ${C.line}`,
              fontSize: 13, color: C.ink2, textDecoration: "none",
            }}>
              Continuer avec Google
            </a>
          </div>
        )}
      </div>

      <a href={LANDING_URL} style={{ marginTop: 20, fontSize: 12, color: C.ink3, textDecoration: "none" }}>
        ← Retour à la présentation
      </a>
    </div>
  );
}

export default LoginView;
