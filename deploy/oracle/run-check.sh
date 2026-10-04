#!/usr/bin/env bash
# Vérification CROUS + Lokaviz — appelé par la cron VM toutes les 10 minutes.
# Récupère l'état depuis GitHub, vérifie, renvoie l'état mis à jour.
set -u
REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_DIR"
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Une seule exécution à la fois (l'API CROUS peut être lente en cas de retry)
exec 9>/tmp/bot-crous-check.lock
flock -n 9 || { echo "[$(date '+%F %T')] passage précédent en cours — ignoré."; exit 0; }

# État à jour depuis GitHub (au cas où une autre machine a écrit)
git pull --rebase --autostash --quiet 2>/dev/null \
  || echo "[$(date '+%F %T')] pull GitHub impossible — état local utilisé."

python3 crous_bot.py
STATUS=$?

# Sauvegarde de l'état vers GitHub (même format que l'ancien workflow Actions)
if [ -n "$(git status --porcelain -- seen.json history.jsonl 2>/dev/null)" ]; then
  git add seen.json
  [ -f history.jsonl ] && git add history.jsonl
  git -c user.name="crous-bot[bot]" -c user.email="actions@github.com" \
      commit -m "Surveillance : état mis à jour" --quiet
  if git push --quiet 2>/dev/null; then
    echo "[$(date '+%F %T')] état commité vers GitHub."
  else
    echo "[$(date '+%F %T')] push impossible — sera retenté au passage suivant."
  fi
fi
exit $STATUS
