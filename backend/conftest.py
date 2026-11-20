"""Configuration pytest commune.

Les tests historiques (test_main.py) appellent l'API sans session : ils tournent en
AUTH_MODE=dev. Les tests de sécurité (tests/security) repassent explicitement en
strict, cas par cas, pour prouver le comportement de production.
"""
import os
import tempfile

os.environ.setdefault("AUTH_MODE", "dev")
os.environ.setdefault("UPLOAD_DIR", tempfile.mkdtemp(prefix="z12-test-uploads-"))
# Aucune base en test : le stockage retombe sur le JSON local.
os.environ.setdefault("DATABASE_URL", "")
