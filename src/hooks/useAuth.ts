// src/hooks/useAuth.ts -- Cookie HttpOnly Authentication hook -- Z12 AI CFO Suite
// C5: credentials: include partout -- pas de localStorage pour le JWT
import { useState, useEffect, useCallback } from "react";

const API = "/api";

export interface User {
  id: string;
  email: string;
  name?: string;
  /** Administrateur : seul à voir et modifier le RAG métier (vérifié côté serveur). */
  is_admin?: boolean;
}

export interface AuthHook {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, name?: string, inviteCode?: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Relit la session auprès du serveur (droits à jour, ex. statut administrateur). */
  refresh: () => Promise<void>;
  authFetch: (url: string, options?: RequestInit) => Promise<Response>;
}

export function useAuth(): AuthHook {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  // Verifier la session active au chargement (cookie HttpOnly gere par le navigateur)
  useEffect(() => {
    const checkSession = async () => {
      try {
        const res = await fetch(`${API}/auth/local/me`, {
          credentials: "include",
        });
        if (res.ok) {
          const data = await res.json();
          setUser(data);
        } else {
          setUser(null);
        }
      } catch {
        setUser(null);
      } finally {
        setLoading(false);
      }
    };
    checkSession();
  }, []);

  // Login : credentials include pour recevoir le cookie HttpOnly
  const login = useCallback(async (email: string, password: string) => {
    const res = await fetch(`${API}/auth/local/login`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: "Erreur de connexion" }));
      throw new Error(typeof err.detail === "string" ? err.detail : "Connexion echouee");
    }
    // Le cookie phoenix_session est pose par le serveur -- pas besoin de lire le token
    // Verifier la session immediatement
    const meRes = await fetch(`${API}/auth/local/me`, {
      credentials: "include",
    });
    if (meRes.ok) {
      const data = await meRes.json();
      setUser(data);
    } else {
      // Fallback : lire user depuis la reponse login
      const data = await res.json().catch(() => null);
      if (data?.user || data?.email) {
        setUser(data.user || { id: data.sub || "1", email: data.email || email });
      }
    }
  }, []);

  // Inscription : le backend cree le compte et pose le cookie de session
  const register = useCallback(async (email: string, password: string, name?: string, inviteCode?: string) => {
    const res = await fetch(`${API}/auth/local/register`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, ...(name ? { name } : {}), ...(inviteCode ? { invite_code: inviteCode } : {}) }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: "Erreur lors de l'inscription" }));
      throw new Error(typeof err.detail === "string" ? err.detail : "Inscription echouee");
    }
    const meRes = await fetch(`${API}/auth/local/me`, { credentials: "include" });
    if (meRes.ok) {
      setUser(await meRes.json());
    } else {
      throw new Error("Compte cree, mais la session n'a pas pu etre ouverte. Essayez de vous connecter.");
    }
  }, []);

  // Logout : appeler le backend pour invalider le cookie
  const logout = useCallback(async () => {
    try {
      await fetch(`${API}/auth/local/logout`, {
        method: "POST",
        credentials: "include",
      });
    } catch {
      // Ignorer les erreurs de logout
    }
    setUser(null);
  }, []);

  // authFetch : toutes les requetes authentifiees via cookie HttpOnly
  const authFetch = useCallback(async (url: string, options: RequestInit = {}): Promise<Response> => {
    return fetch(url, {
      ...options,
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
    });
  }, []);

  // Relit la session : une page restée ouverte pendant une mise à jour du serveur récupère
  // ainsi ses droits sans rechargement. Une panne passagère ne déconnecte pas l'utilisateur.
  const refresh = useCallback(async () => {
    try {
      const res = await fetch(`${API}/auth/local/me`, { credentials: "include" });
      if (res.ok) setUser(await res.json());
      else if (res.status === 401) setUser(null);
    } catch {
      // réseau indisponible : on garde la session affichée
    }
  }, []);

  return { user, loading, login, register, logout, refresh, authFetch };
}

export default useAuth;
