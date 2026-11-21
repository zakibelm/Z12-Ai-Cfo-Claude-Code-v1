#!/usr/bin/env bash
# Sauvegarde PostgreSQL compressée avec rotation. À planifier (voir scripts/install_backup_cron.sh).
# Variables : BACKUP_DIR (défaut /var/backups/z12-cfo), KEEP_DAYS (défaut 14).
set -euo pipefail
cd "$(dirname "$0")/.."
BACKUP_DIR="${BACKUP_DIR:-/var/backups/z12-cfo}"
KEEP_DAYS="${KEEP_DAYS:-14}"
[ -f .env ] && set -a && . ./.env && set +a
DB_USER="${POSTGRES_USER:-ai_cfo}"; DB_NAME="${POSTGRES_DB:-ai_cfo_db}"

umask 077
mkdir -p "$BACKUP_DIR"
OUT="$BACKUP_DIR/z12-$(date +%Y%m%d-%H%M%S).sql.gz"
docker compose exec -T db pg_dump -U "$DB_USER" --clean --if-exists "$DB_NAME" | gzip -9 > "$OUT.tmp"
# Une sauvegarde vide ou tronquée ne doit jamais remplacer une bonne.
if ! gzip -t "$OUT.tmp" || [ "$(stat -c %s "$OUT.tmp")" -lt 200 ]; then
  rm -f "$OUT.tmp"; echo "ERREUR: sauvegarde invalide" >&2; exit 1
fi
mv "$OUT.tmp" "$OUT"
find "$BACKUP_DIR" -name 'z12-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
echo "OK $OUT ($(du -h "$OUT" | cut -f1))"
