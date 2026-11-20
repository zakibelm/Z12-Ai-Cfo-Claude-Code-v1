// Navigation hors de l'application React (page de presentation statique).
// Isole dans un module pour pouvoir etre simule dans les tests.
export const LANDING_URL = "/landing.html";

export function goToLanding(): void {
  window.location.replace(LANDING_URL);
}

export type AuthRoute = "login" | "register" | null;

/** Route d'authentification demandee par l'URL, ou null pour toute autre page. */
export function authRouteFromPath(pathname: string): AuthRoute {
  const path = pathname.replace(/\/+$/, "");
  if (path === "/login") return "login";
  if (path === "/register") return "register";
  return null;
}
