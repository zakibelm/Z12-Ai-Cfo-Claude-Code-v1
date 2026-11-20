"""Invitations : logique sans base de données."""
import invitations


def test_codes_are_unambiguous_and_normalized():
    codes = {invitations.generate_code() for _ in range(200)}
    assert len(codes) == 200
    for code in codes:
        assert len(code) == 14 and not set(code) & set("01OI")
    code = next(iter(codes))
    assert invitations.hash_code(code) == invitations.hash_code(f" {code.lower().replace('-', '')} ")
    assert invitations.hash_code(code) != invitations.hash_code(code[:-1] + ("A" if code[-1] != "A" else "B"))
    assert invitations.normalize_code(None) == ""


def test_default_mode_is_invite(monkeypatch):
    monkeypatch.delenv("REGISTRATION_MODE", raising=False)
    assert invitations.registration_mode() == "invite"
    for value, expected in ((" OPEN ", "open"), ("closed", "closed"), ("", "invite"), ("libre", "invite")):
        monkeypatch.setenv("REGISTRATION_MODE", value)
        assert invitations.registration_mode() == expected


def test_too_short_codes_never_reach_the_database():
    class Cursor:
        def execute(self, *a):
            raise AssertionError("la base ne doit pas être interrogée")

    for code in (None, "", "abc", "A-B-C"):
        assert invitations.consume(Cursor(), code, "x@exemple.ca") is False


def test_registration_fails_closed_without_database(monkeypatch):
    """Sans base, aucune inscription ne passe en mode invitation."""
    from fastapi.testclient import TestClient

    import auth_local
    import main
    import security_limits

    monkeypatch.delenv("REGISTRATION_MODE", raising=False)
    monkeypatch.setattr(security_limits.auth_limiter, "max_calls", 10_000)
    monkeypatch.setenv("SESSION_JWT_SECRET", "secret-de-test-" + "x" * 48)

    def no_db():
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(auth_local, "_conn", no_db)
    response = TestClient(main.app).post("/api/auth/local/register", json={
        "email": "x@exemple.ca", "password": "Un-mot-de-passe-solide-2026!", "invite_code": "ABCD-EFGH-JKLM"})
    assert response.status_code == 500
