# Bot CROUS Paris — alerte email dès qu'un logement apparaît

Surveille en continu [trouverunlogement.lescrous.fr](https://trouverunlogement.lescrous.fr)
et **envoie un email dès qu'un nouveau logement étudiant devient disponible à
Paris**, avec le lien direct, le prix et l'adresse pour réserver vite.

- **100 % gratuit** : hébergé sur GitHub Actions (tourne 24h/24, même PC éteint)
- **Deux outils surveillés** : phase complémentaire (47) **et** attribution
  directe (44, ouverte toute l'année) — le double de chances
- **Notification email** via Gmail (SMTP + mot de passe d'application)
- **Zéro faux positif** : seul un logement *nouveau* déclenche un email
  (le premier passage mémorise l'existant sans rien envoyer)
- **Watchdog intégré** : si le bot tombe en panne, un email
  « BOT EN PANNE » vous prévient (un seul par panne)
- **Résumé hebdo** : chaque dimanche, un email confirme que tout veille
  (détections, réapparitions, dispo actuel)
- **Historique complet** : chaque détection est journalisée (`history.jsonl`)
- **Tests automatiques** : 17 tests unitaires joués à chaque push
- **Zéro dépendance** : Python standard uniquement, exécution en ~30 secondes
- Basé sur [AUTO-CROUS](https://github.com/Kdevos12/AUTO-CROUS) (MIT), adapté
  email + Paris + multi-outils + GitHub Actions

> Les logements de la phase complémentaire partent en quelques minutes.
> Ce bot fait une vérification toutes les 10 minutes — moins de trafic
> qu'un étudiant qui rafraîchit la page à la main.

## Installation (10 minutes)

### 1. Créer le dépôt GitHub

1. Créez un compte sur [github.com](https://github.com) si besoin (gratuit).
2. Créez un **nouveau dépôt public et vide** : *New repository* → nom (ex: `bot-crous-paris`)
   → **Public** (important : les minutes GitHub Actions sont illimitées sur un
   dépôt public ; un dépôt privé serait limité à 2000 min/mois, insuffisant pour
   un check toutes les 10 min) → **sans** README auto → *Create repository*.
3. Poussez ce code depuis ce dossier :

```bash
git init -b main
git add crous_bot.py README.md .gitignore .env.example .github/
git commit -m "Bot CROUS Paris : surveillance + alerte email"
git remote add origin https://github.com/VOTRE_COMPTE/bot-crous-paris.git
git push -u origin main
```

### 2. Créer le mot de passe d'application Gmail

L'email est envoyé depuis votre compte Gmail via SMTP. Gmail n'accepte pas votre
mot de passe habituel : il faut un **mot de passe d'application**.

1. Activez la **validation en deux étapes** sur votre compte Google
   (https://myaccount.google.com/security) si ce n'est pas déjà fait.
2. Créez un mot de passe d'application : https://myaccount.google.com/apppasswords
   (nom : `bot crous`). Gmail affiche un code à **16 caractères** — copiez-le.

### 3. Configurer les secrets du dépôt

Dans votre dépôt GitHub : *Settings* → *Secrets and variables* → *Actions*
→ *New repository secret*. Ajoutez :

| Nom du secret     | Valeur                                              |
|-------------------|-----------------------------------------------------|
| `GMAIL_USER`      | votre adresse Gmail (ex: `prenom@gmail.com`)        |
| `GMAIL_APP_PASSWORD` | le code à 16 caractères de l'étape 2             |
| `NOTIFY_EMAIL`    | l'adresse qui reçoit les alertes (peut être la même, ou une autre, ou plusieurs séparées par des virgules) |

### 4. Tester

Dans votre dépôt : onglet **Actions** → **Vérification CROUS Paris** →
*Run workflow* → cochez **"Envoyer aussi un email de test"** → *Run workflow*.

- Vous devez recevoir un email de test → la configuration Gmail fonctionne.
- Le log affiche : nombre de logements en ligne (France + zone Paris).
- Le premier passage **mémorise** les logements existants sans envoyer d'alerte.

C'est tout. Le bot tourne maintenant tout seul, toutes les 10 minutes.

## Comment ça marche

1. Toutes les 10 minutes, GitHub Actions lance `crous_bot.py`.
2. Le script interroge l'API JSON interne du site CROUS (celle que la page
   appelle elle-même) pour **chaque outil surveillé** (47 phase complémentaire
   + 44 attribution directe) et récupère tous les logements en recherche.
3. Il ne garde que ceux de la zone surveillée (codes postaux `75xxx`), les
   identifie par outil (`47:522`) et compare avec le passage précédent
   (`seen.json`).
4. Un logement **nouveau** → email avec **résidence, prix et type dans le
   sujet**, et dans le corps : adresse, gros bouton « Voir et réserver »
   (lien direct). L'état et l'historique sont commités dans le dépôt.
5. Un logement qui disparaît (réservé par quelqu'un) puis **réapparaît**
   (réservation abandonnée) déclenche à nouveau un email — c'est voulu,
   c'est une nouvelle chance de l'attraper (marqué `reappeared` dans
   l'historique).
6. **Watchdog** (chaque heure) : si aucune vérification réussie depuis 35 à
   95 minutes → email « PANNE bot CROUS ». Exactement un email par panne ;
   silence = tout va bien.
7. **Digest** (chaque dimanche 19-20h) : email de résumé — nombre de
   détections depuis le début, réapparitions, logements actuellement
   disponibles à Paris.

## Personnalisation

Dans le workflow `.github/workflows/crous.yml` (section *env* de l'étape
"Vérification des logements CROUS") :

- `PARIS_POSTAL_PREFIXES` : `75` (Paris) ; `75,92,93,94` (Paris + petite
  couronne) ; `75,77,78,91,92,93,94,95` (toute l'Île-de-France).
- `CROUS_TOOL_IDS` : `47,44` aujourd'hui. Si le CROUS ouvre l'outil 2027,
   vérifiez `https://trouverunlogement.lescrous.fr/api/global/context`
   (champ `tools.currentSchoolYear.id`) et adaptez. Un outil ajouté est
   **initialisé silencieusement** (pas d'email pour les logements déjà en
   ligne, seulement pour les nouveaux).

Le destinataire des emails (`NOTIFY_EMAIL`) : jusqu'à ~500/jour côté Gmail,
largement suffisant (vous recevrez quelques emails par semaine au plus).

## Utilisation locale (optionnel)

Pour tester sur votre machine sans GitHub Actions :

```bash
cp .env.example .env        # puis remplissez les 3 valeurs
py crous_bot.py --test-email   # email de test + passage de surveillance
py crous_bot.py --dry-run      # simule un email, l'écrit dans email_preview.txt/.html
python -m unittest discover -s tests   # joue les tests
```

Le fichier `.env` est ignoré par git (jamais envoyé sur GitHub).

## Limites à connaître

- **Retards possibles du cron** : GitHub Actions peut avoir quelques minutes de
  retard en période de forte charge. Inévitable en hébergement gratuit.
- **Salle d'attente anti-surcharge** : le site CROUS se met en protection en
  période d'attribution. Le script retente 3 fois (30 s d'écart). Si un seul
  outil répond, l'autre est retenté au passage suivant sans rien casser.
- **Phase 2026** : la phase complémentaire (outil 47) se termine le 02/11/2026.
  L'attribution directe (outil 44) reste ouverte toute l'année. L'an prochain,
  vérifiez le nouvel outil (voir Personnalisation).
- **Arrêter le bot** : dépôt → onglet *Actions* → sélectionner le workflow →
  *Disable workflow* (ou supprimer le dépôt).

## Confidentialité

Le dépôt est public mais **ne contient aucun secret** : l'adresse Gmail, le mot
de passe d'application et le destinataire sont chiffrés dans les *secrets* GitHub.
Le fichier `seen.json` ne contient que des identifiants de logements publics.

---

Projet dérivé de [AUTO-CROUS](https://github.com/Kdevos12/AUTO-CROUS) (MIT,
Kdevos12) — lui-même pensé pour contourner la bureaucratie, pas le site :
une requête légère toutes les 10 minutes, moins qu'un étudiant qui rafraîchit
sa page. Licence MIT.
