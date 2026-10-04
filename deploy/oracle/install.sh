#!/usr/bin/env bash
# Installe le bot logements étudiants Paris sur une VM Linux
# (prévu pour Oracle Cloud "Always Free" — Ubuntu 22.04/24.04).
#
# Usage sur la VM :
#   curl -fsSL -o /tmp/install.sh \
#     https://raw.githubusercontent.com/pipionkakiandpipi/bot-crous-paris/main/deploy/oracle/install.sh
#   sudo bash /tmp/install.sh
#
# Pose 4 questions (Gmail, destinataire, jeton GitHub — masqués) puis :
#   - clone le dépôt dans /opt/bot-crous
#   - installe la cron de vérification toutes les 10 minutes
#   - installe le watchdog VM (alerte si l'état ne rafraîchit plus depuis 12 h)
#   - installe le résumé hebdo (dimanche, heure serveur)
#   - lance un test immédiat (email de confirmation + premier passage)
set -euo pipefail

REPO_URL="https://github.com/pipionkakiandpipi/bot-crous-paris.git"
APP_DIR="/opt/bot-crous"
LOG_FILE="/var/log/bot-crous.log"

[ "$(id -u)" = 0 ] || { echo "ERREUR : lance avec  sudo bash install.sh"; exit 1; }

echo "==> Paquets (git, python3, cron)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 cron >/dev/null
systemctl enable --now cron 2>/dev/null || service cron start 2>/dev/null || true

echo "==> Code du bot…"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --rebase --autostash --quiet
else
  git clone --quiet "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
touch "$LOG_FILE"

echo "==> Configuration Gmail (mot de passe masqué)…"
if ! grep -q "^GMAIL_USER=." .env 2>/dev/null; then
  read -rp "  Adresse Gmail expéditrice : " GMAIL_USER
  read -rsp "  Mot de passe d'application Gmail (16 caractères, masqué) : " GMAIL_APP_PASSWORD; echo
  read -rp "  Destinataire des alertes (email) : " NOTIFY_EMAIL
  cat > .env <<EOF
GMAIL_USER=$GMAIL_USER
GMAIL_APP_PASSWORD=$GMAIL_APP_PASSWORD
NOTIFY_EMAIL=$NOTIFY_EMAIL
EOF
  chmod 600 .env
  echo "  .env écrit (droits 600, jamais commité)."
fi

echo "==> Jeton GitHub pour pousser l'état (masqué)…"
if [ ! -f /root/.git-credentials ]; then
  read -rsp "  Fine-grained PAT (Contents: Read and write sur bot-crous-paris) : " GH_PAT; echo
  git config --global credential.helper store
  printf 'protocol=https\nhost=github.com\nusername=bot-crous\npassword=%s\n' "$GH_PAT" \
    | git credential approve
  chmod 600 /root/.git-credentials
fi
git config --global user.name  "crous-bot[bot]"
git config --global user.email "actions@github.com"
git push --quiet 2>/dev/null && echo "  push de test OK." \
  || { echo "  ERREUR : le jeton ne permet pas d'écrire sur le dépôt."; exit 1; }

echo "==> Cron : vérification toutes les 10 minutes, watchdog, digest…"
cat > /etc/cron.d/bot-crous <<EOF
# Bot logements étudiants Paris — installé par install.sh
*/10 * * * * root $APP_DIR/deploy/oracle/run-check.sh >> $LOG_FILE 2>&1
7 * * * * root cd $APP_DIR && /usr/bin/python3 crous_bot.py --local-watchdog >> $LOG_FILE 2>&1
0 18 * * 0 root cd $APP_DIR && /usr/bin/python3 crous_bot.py --digest >> $LOG_FILE 2>&1
EOF
chmod 644 /etc/cron.d/bot-crous

cat > /etc/logrotate.d/bot-crous <<'EOF'
/var/log/bot-crous.log {
  weekly
  rotate 8
  compress
  missingok
  notifempty
}
EOF

echo "==> Test immédiat (email de confirmation + premier passage)…"
python3 crous_bot.py --test-email
"$APP_DIR/deploy/oracle/run-check.sh"

echo
echo "INSTALLATION TERMINÉE"
echo "  - Logs     : tail -f $LOG_FILE"
echo "  - État     : git -C $APP_DIR log --oneline -3"
echo "  - Prochain passage automatique : dans <= 10 minutes"
echo "Pense à désactiver la cron GitHub (onglet Actions > Vérification CROUS"
echo "Paris > menu ... > Disable workflow) pour éviter les doublons d'état."
