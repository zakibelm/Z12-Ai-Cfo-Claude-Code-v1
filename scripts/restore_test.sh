#!/usr/bin/env bash
# Prouve qu'une sauvegarde est restaurable : la charge dans une base JETABLE (jamais la production).
# Usage : scripts/restore_test.sh [fichier.sql.gz]   (défaut : la plus récente)
set -euo pipefail
cd "$(dirname "$0")/.."
BACKUP_DIR="${BACKUP_DIR:-/var/backups/z12-cfo}"
[ -f .env ] && set -a && . ./.env && set +a
DB_USER="${POSTGRES_USER:-ai_cfo}"; DB_NAME="${POSTGRES_DB:-ai_cfo_db}"
FILE="${1:-$(ls -t "$BACKUP_DIR"/z12-*.sql.gz 2>/dev/null | head -1)}"
[ -n "$FILE" ] && [ -f "$FILE" ] || { echo "Aucune sauvegarde trouvée" >&2; exit 1; }
TMP_DB="restore_test_$$"
cleanup() { docker compose exec -T db dropdb -U "$DB_USER" --if-exists "$TMP_DB" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker compose exec -T db createdb -U "$DB_USER" "$TMP_DB"
docker compose exec -T db psql -U "$DB_USER" -d "$TMP_DB" -c 'CREATE EXTENSION IF NOT EXISTS vector;' >/dev/null 2>&1 || true
gunzip -c "$FILE" | docker compose exec -T db psql -U "$DB_USER" -d "$TMP_DB" -v ON_ERROR_STOP=1 -q >/dev/null
TABLES=$(docker compose exec -T db psql -U "$DB_USER" -d "$TMP_DB" -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
[ "${TABLES:-0}" -gt 0 ] || { echo "ERREUR: restauration sans table" >&2; exit 1; }
echo "OK restauration vérifiée : $FILE -> $TABLES tables (base jetable supprimée)"
