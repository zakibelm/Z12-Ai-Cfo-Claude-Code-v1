#!/usr/bin/env bash
# Z12 AI CFO — mise à jour avec sauvegarde préalable et retour arrière automatique.
set -euo pipefail
cd "$(dirname "$0")"
HEALTH_URL="${HEALTH_URL:-http://localhost:8000/health}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/z12-cfo}"
info() { echo "-> $1"; }
START=$(date +%s)

info "1/4 Sauvegarde préventive"
scripts/backup.sh
LATEST=$(ls -t "$BACKUP_DIR"/z12-*.sql.gz | head -1)

info "2/4 Téléchargement des nouvelles images"
docker compose pull

info "3/4 Redémarrage (le schéma se met à jour au démarrage du backend)"
docker compose up -d --remove-orphans

info "4/4 Vérification de santé"
for _ in $(seq 1 12); do
  sleep 5
  if [ "$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)" = "200" ]; then
    echo "OK mise à jour réussie en $(( $(date +%s) - START ))s"; exit 0
  fi
done

echo "ÉCHEC : le backend ne répond pas — retour arrière" >&2
docker compose logs --tail=50 backend >&2 || true
docker compose down
docker compose up -d db
sleep 10
# --clean est inclus dans la sauvegarde : elle remplace les objets existants.
gunzip -c "$LATEST" | docker compose exec -T db psql -U "${POSTGRES_USER:-ai_cfo}" -d "${POSTGRES_DB:-ai_cfo_db}" -q >/dev/null
docker compose up -d
echo "Base restaurée depuis $LATEST. Rétablir l'image précédente si le problème persiste (docker compose logs backend)." >&2
exit 1
