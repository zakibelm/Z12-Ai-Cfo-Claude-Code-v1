#!/usr/bin/env bash
# Installe la sauvegarde quotidienne (02:15) et le test de restauration hebdomadaire (dimanche 03:00).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG=/var/log/z12-backup.log
( crontab -l 2>/dev/null | grep -v 'z12-backup-managed' ;
  echo "15 2 * * * cd $ROOT && scripts/backup.sh >> $LOG 2>&1 # z12-backup-managed"
  echo "0 3 * * 0 cd $ROOT && scripts/restore_test.sh >> $LOG 2>&1 # z12-backup-managed" ) | crontab -
echo "Cron installé. Journal : $LOG"
