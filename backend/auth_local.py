from __future__ import annotations
import os, secrets, hashlib, datetime as dt
from typing import Optional
import jwt, psycopg, bcrypt
from psycopg.rows import dict_row
from fastapi import APIRouter, HTTPException, Request, Response, Cookie
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth import auth_mode, require_session_secret
from security_limits import auth_limiter

DATABASE_URL       = os.getenv("DATABASE_URL", "").strip()
# Le secret de session est lu via auth.require_session_secret() : aucun défaut.
SESSION_TTL_HOURS  = int(os.getenv("SESSION_TTL_HOURS", "12"))
FRONTEND_URL       = os.getenv("FRONTEND_URL", "https://phoenix.optigenius.pro").strip().rstrip("/")
COOKIE_NAME        = "phoenix_session"
IS_HTTPS           = FRONTEND_URL.startswith("https://")

router = APIRouter(prefix="/api/auth/local", tags=["auth-local"])

def _conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def _now():
    return dt.datetime.now(dt.timezone.utc)

def _sign_jwt(user_id, jti, expires_at):
    return jwt.encode(
        {"sub": str(user_id), "jti": jti,
         "iat": int(_now().timestamp()), "exp": int(expires_at.timestamp()),
         "aud": "phoenix", "iss": "phoenix-auth"},
        require_session_secret(), algorithm="HS256")

def _token_fingerprint(token: str) -> str:
    """Empreinte SHA-256 du jeton : la base ne garde jamais le JWT lui-même."""
    return hashlib.sha256(token.encode()).hexdigest()

# Hash factice : égalise le temps de réponse quand le compte n'existe pas (anti-énumération).
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password-for-timing", bcrypt.gensalt()).decode()

def _set_cookie(resp, token):
    resp.set_cookie(key=COOKIE_NAME, value=token, max_age=SESSION_TTL_HOURS*3600,
        httponly=True, secure=IS_HTTPS, samesite="lax", path="/")

def _clear_cookie(resp):
    resp.delete_cookie(key=COOKIE_NAME, path="/")

def _get_user(token):
    if not token:
        return None
    try:
        p = jwt.decode(token, require_session_secret(), algorithms=["HS256"], audience="phoenix")
        uid = p.get("sub")
        jti = p.get("jti")
        if not uid or not jti:
            return None
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """SELECT u.id, u.email, u.name, u.picture_url
                   FROM local_users u
                   JOIN user_sessions s ON s.user_id = u.id
                   WHERE u.id = %s::integer AND s.jti = %s
                     AND s.is_active = TRUE AND s.expires_at > NOW()
                     AND u.is_active = TRUE LIMIT 1""",
                (uid, jti))
            return cur.fetchone()
    except Exception:
        return None

class RegisterBody(BaseModel):
    email: str
    password: str
    name: Optional[str] = None
    invite_code: Optional[str] = None

class LoginBody(BaseModel):
    email: str
    password: str

class ForgotBody(BaseModel):
    email: str

class ResetBody(BaseModel):
    token: str
    password: str

def _client_key(request: Request, email: str = "") -> str:
    return f"{request.client.host if request.client else 'unknown'}:{email}"


def _check_password(password: str) -> None:
    if len(password) < 10:
        raise HTTPException(400, "Le mot de passe doit contenir au moins 10 caracteres")
    if len(password.encode()) > 72:
        # bcrypt tronque au-dela de 72 octets : on refuse plutot que d'ignorer la fin.
        raise HTTPException(400, "Le mot de passe est trop long (72 octets maximum)")


def _send_reset_email(email: str, reset_url: str) -> bool:
    """Envoie le lien par courriel si SMTP est configure. Retourne True si envoye."""
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    if not (host and user and password):
        print("[WARN] SMTP non configure : lien de reinitialisation non envoye")
        return False
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = "Z12 AI CFO - Reinitialisation de votre mot de passe"
    msg["From"] = os.getenv("SMTP_FROM", user)
    msg["To"] = email
    msg.set_content(
        "Bonjour,\n\nPour choisir un nouveau mot de passe, ouvrez ce lien (valide 1 heure) :\n"
        f"{reset_url}\n\nSi vous n'etes pas a l'origine de cette demande, ignorez ce message.\n"
    )
    try:
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=15) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] Envoi du courriel de reinitialisation: {e}")
        return False


@router.post("/register")
async def register(body: RegisterBody, request: Request):
    auth_limiter.check(_client_key(request))
    email = body.email.lower().strip()
    if "@" not in email or len(email) > 254:
        raise HTTPException(400, "Adresse courriel invalide")
    _check_password(body.password)
    # Inscription sur invitation par défaut (voir invitations.py).
    import invitations

    mode = invitations.registration_mode()
    if mode == "closed":
        raise HTTPException(403, "Les inscriptions sont fermées")
    require_session_secret()  # avant de créer le compte : un code n'est pas consommé pour rien
    hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    try:
        with _conn() as c, c.cursor() as cur:
            # Le code est consommé dans la même transaction que la création du compte :
            # si celle-ci échoue (courriel déjà pris), le code reste utilisable.
            if mode == "invite" and not invitations.consume(cur, body.invite_code, email):
                raise HTTPException(403, invitations.INVALID_CODE)
            cur.execute(
                "INSERT INTO local_users (email, password_hash, name) VALUES (%s, %s, %s) RETURNING id",
                (email, hashed, body.name or email.split("@")[0]))
            c.commit()
            user_id = cur.fetchone()["id"]
    except HTTPException:
        raise
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(409, "Cet email est deja utilise")
        print(f"[ERROR] register: {e}")
        raise HTTPException(500, "Erreur lors de la creation du compte")
    jti = secrets.token_urlsafe(24)
    exp = _now() + dt.timedelta(hours=SESSION_TTL_HOURS)
    token = _sign_jwt(user_id, jti, exp)
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO user_sessions (user_id, jti, expires_at, token) VALUES (%s, %s, %s, %s)",
            (str(user_id), jti, exp, _token_fingerprint(token)))
        c.commit()
    resp = JSONResponse({"ok": True, "email": email})
    _set_cookie(resp, token)
    return resp

@router.post("/login")
async def login(body: LoginBody, request: Request):
    email = body.email.lower().strip()
    # Frein au bourrage d'identifiants : par IP + courriel.
    auth_limiter.check(_client_key(request, email))
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, email, name, picture_url, password_hash, is_active FROM local_users WHERE email = %s LIMIT 1",
            (email,))
        user = cur.fetchone()
    if not user or not user["is_active"]:
        bcrypt.checkpw(body.password.encode()[:72], _DUMMY_HASH.encode())
        raise HTTPException(401, "Email ou mot de passe incorrect")
    if not bcrypt.checkpw(body.password.encode(), user["password_hash"].encode()):
        raise HTTPException(401, "Email ou mot de passe incorrect")
    jti = secrets.token_urlsafe(24)
    exp = _now() + dt.timedelta(hours=SESSION_TTL_HOURS)
    token = _sign_jwt(user["id"], jti, exp)
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO user_sessions (user_id, jti, expires_at, token) VALUES (%s, %s, %s, %s)",
            (str(user["id"]), jti, exp, _token_fingerprint(token)))
        c.commit()
    resp = JSONResponse({"ok": True, "user": {
        "id": str(user["id"]), "email": user["email"],
        "name": user["name"], "picture_url": user["picture_url"]}})
    _set_cookie(resp, token)
    return resp

@router.get("/me")
async def me(phoenix_session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME)):
    user = _get_user(phoenix_session)
    if not user:
        raise HTTPException(401, "Non authentifie")
    from security_limits import is_admin

    return {"id": str(user["id"]), "email": user["email"],
            "name": user["name"], "picture_url": user["picture_url"], "locale": None,
            # Sert seulement à l'affichage : chaque route vérifie le droit côté serveur.
            "is_admin": is_admin(str(user["id"]))}

@router.post("/logout")
async def logout(phoenix_session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME)):
    if phoenix_session:
        try:
            p = jwt.decode(phoenix_session, require_session_secret(), algorithms=["HS256"], audience="phoenix")
            jti = p.get("jti")
            if jti:
                with _conn() as c, c.cursor() as cur:
                    cur.execute("UPDATE user_sessions SET is_active = FALSE WHERE jti = %s", (jti,))
                    c.commit()
        except Exception:
            pass
    resp = JSONResponse({"ok": True})
    _clear_cookie(resp)
    return resp

@router.post("/forgot-password")
async def forgot_password(body: ForgotBody, request: Request):
    auth_limiter.check(_client_key(request))
    email = body.email.lower().strip()
    user = None
    token = secrets.token_urlsafe(32)
    exp = _now() + dt.timedelta(hours=1)
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT id FROM local_users WHERE email = %s LIMIT 1", (email,))
            user = cur.fetchone()
            if user:
                cur.execute(
                    """INSERT INTO password_reset_tokens (user_id, token, expires_at)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (user_id) DO UPDATE
                       SET token = EXCLUDED.token, expires_at = EXCLUDED.expires_at""",
                    (str(user["id"]), token, exp))
                c.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] forgot-password: {e}")
        user = None
    reset_url = f"{FRONTEND_URL}/login?reset_token={token}"
    if user:
        _send_reset_email(email, reset_url)
    # Reponse identique que le compte existe ou non, et le lien ne sort que par courriel.
    response = {"ok": True, "message": "Si ce compte existe, un courriel de reinitialisation a ete envoye."}
    if auth_mode() == "dev" and user:
        response["reset_url"] = reset_url  # confort de developpement uniquement
    return response

@router.post("/reset-password")
async def reset_password(body: ResetBody, request: Request):
    auth_limiter.check(_client_key(request))
    _check_password(body.password)
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT user_id, expires_at FROM password_reset_tokens WHERE token = %s LIMIT 1",
            (body.token,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(400, "Token invalide ou expire")
        if row["expires_at"] < _now():
            raise HTTPException(400, "Token expire")
        hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
        # local_users.id est un entier (SERIAL) : comparaison en texte, pas de cast uuid.
        cur.execute("UPDATE local_users SET password_hash = %s WHERE id::text = %s",
                    (hashed, str(row["user_id"])))
        # Un changement de mot de passe ferme toutes les sessions ouvertes.
        cur.execute("UPDATE user_sessions SET is_active = FALSE WHERE user_id::text = %s",
                    (str(row["user_id"]),))
        cur.execute("DELETE FROM password_reset_tokens WHERE token = %s", (body.token,))
        c.commit()
    return {"ok": True, "message": "Mot de passe reinitialise"}
