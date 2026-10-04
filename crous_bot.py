#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CROUS Paris — surveillance des nouveaux logements + notification par email.

v3 — Basé sur AUTO-CROUS (https://github.com/Kdevos12/AUTO-CROUS, licence MIT),
adapté puis étendu :

  - MULTI-OUTILS : surveille en parallèle la phase complémentaire
    (outil 47) et l'attribution directe (outil 44, ouverte toute l'année) ;
  - LOKAVIZ : chambres / studios-T1 / T1bis <= 750 € (charges comprises)
    dans un rayon de 6 km autour du Lycée Rabelais (Paris 18e) — le site
    étant protégé par Anubis, la preuve de travail est résolue à chaque
    vérification (une requête par passage, usage individuel poli) ;
  - FILTRE CÔTÉ SCRIPT : codes postaux 75xxx (l'API n'accepte plus le
    paramètre "location") — un seul appel par outil récupère tout ;
  - ÉTAT + HISTORIQUE : seen.json (identifiants déjà vus, préfixés par
    outil "47:522") + history.jsonl (toute détection, flag réapparu) ;
  - ANTI-SPAM : un outil nouvellement ajouté est initialisé silencieusement
    (premier passage mémorise, sans email) ;
  - EMAIL V2 : sujet = résidence · type · loyer ; corps centré sur le lien
    direct, l'adresse et le prix ;
  - WATCHDOG : si aucune vérification planifiée réussie depuis 12 h,
    un email "BOT EN PANNE" est envoyé (exactement un par panne) ;
  - DIGEST : résumé hebdomadaire par email (détections, réapparitions,
    logements encore disponibles) ;
  - HÉBERGEMENT : GitHub Actions (gratuit, 24/7), zéro dépendance pip.

Variables d'environnement (ou fichier .env local) :
    GMAIL_USER             adresse Gmail utilisée pour envoyer
    GMAIL_APP_PASSWORD     mot de passe d'application Gmail (16 caractères)
    NOTIFY_EMAIL           destinataire(s), séparés par des virgules
    CROUS_TOOL_IDS         outils surveillés (défaut "47,44")
    PARIS_POSTAL_PREFIXES  préfixes de codes postaux (défaut "75")
    LOKAVIZ_ENABLED        "false" pour désactiver lokaviz (défaut actif)
    LOKAVIZ_TYPE_IDS       types lokaviz (défaut "4,1,146" = Chambre,
                          Studio ou T1, T1bis)
    LOKAVIZ_MAX_RENT       loyer max € charges comprises (défaut 750)
    SEND_TEST_EMAIL        "true" pour un email de test
    GITHUB_API_TOKEN       watchdog seulement — jeton GitHub (Actions)
    GITHUB_REPOSITORY      watchdog seulement — "compte/repo" (Actions)

Usage :
    python crous_bot.py                # passage normal (celui du cron)
    python crous_bot.py --test-email   # email de test + passage
    python crous_bot.py --dry-run      # simulate : aperçu email, état gelé
    python crous_bot.py --watchdog     # contrôle de santé (cron horaire)
    python crous_bot.py --digest       # résumé hebdo (cron dimanche)
"""
import hashlib
import html as html_mod
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "seen.json"
HISTORY = HERE / "history.jsonl"
PREVIEW_TXT = HERE / "email_preview.txt"
PREVIEW_HTML = HERE / "email_preview.html"
ENV_FILE = HERE / ".env"

API_URL = "https://trouverunlogement.lescrous.fr/api/fr/search/{tool_id}"
SITE_URL = "https://trouverunlogement.lescrous.fr/tools/{tool_id}"
OCCUPATION_LABELS = {"alone": "seul(e)", "couple": "en couple",
                     "house_sharing": "colocation"}
ATTEMPTS = 3          # tentatives d'appel API avant abandon (par outil)
RETRY_DELAY = 30      # secondes entre deux tentatives (salle d'attente)
WATCHDOG_THRESHOLD_MIN = 720  # plus ancienne réussite tolérée (min) — GitHub décale fortement les crons fréquents, on n'alerte qu'après 12 h sans vérification
WATCHDOG_WINDOW_MAX = 1440    # au-delà : panne déjà signalée, silence (24 h)
LEGACY_TOOL = 47      # outil de l'époque v1 (clés d'état non préfixées)
LOKAVIZ_TOOL = "lokaviz"   # source lokaviz.fr (clés d'état "lokaviz:<id>")
LOKAVIZ_BASE = "https://www.lokaviz.fr"
LOKAVIZ_DEFAULT_SEARCH = (
    "/rechercher-un-logement-etudiant/fiche-logement/affichage:liste"
    "?etab_id=118&pres_de=Lyc%C3%A9e+Rabelais+18e+%2875+-+Paris%29&nbkm=6"
    "&NE=48.98967992353915%2C2.4969863891601567"
    "&SW=48.80912453144312%2C2.193832397460938&ZOOM=12")
LOKAVIZ_TYPE_IDS = "4,1,146"   # Chambre, Studio ou T1, T1bis
LOKAVIZ_MAX_RENT = 750         # loyer max (€, charges comprises)
LOKAVIZ_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def log(line):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {line}", flush=True)


def die(msg):
    log(f"ERREUR — {msg}")
    sys.exit(1)


def load_env_file():
    """Charge un éventuel fichier .env (pour les tests locaux)."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config():
    tools_raw = (os.environ.get("CROUS_TOOL_IDS") or
                 os.environ.get("CROUS_TOOL_ID") or "47,44")
    return {
        "gmail_user": os.environ.get("GMAIL_USER", "").strip(),
        "gmail_app_password": os.environ.get("GMAIL_APP_PASSWORD", "").strip(),
        "notify_emails": [e.strip() for e in os.environ.get("NOTIFY_EMAIL", "").split(",")
                          if e.strip()],
        "tool_ids": [int(t) for t in tools_raw.replace(";", ",").split(",")
                     if t.strip().isdigit()],
        "postal_prefixes": [p.strip().zfill(2) for p in
                            os.environ.get("PARIS_POSTAL_PREFIXES", "75").split(",")
                            if p.strip()],
        "send_test_email": (os.environ.get("SEND_TEST_EMAIL", "").strip().lower() == "true"
                            or "--test-email" in sys.argv),
        "dry_run": "--dry-run" in sys.argv,
        "watchdog": "--watchdog" in sys.argv,
        "digest": "--digest" in sys.argv,
        "lokaviz_enabled": os.environ.get("LOKAVIZ_ENABLED", "true").strip().lower() != "false",
        "lokaviz_type_ids": [t.strip() for t in
                            os.environ.get("LOKAVIZ_TYPE_IDS", LOKAVIZ_TYPE_IDS).split(",")
                            if t.strip()],
        "lokaviz_max_rent": int(os.environ.get("LOKAVIZ_MAX_RENT", str(LOKAVIZ_MAX_RENT))),
        "lokaviz_max_pages": int(os.environ.get("LOKAVIZ_MAX_PAGES", "10")),
        "lokaviz_search_path": os.environ.get("LOKAVIZ_SEARCH_PATH", LOKAVIZ_DEFAULT_SEARCH),
    }


def post_json(url, data):
    """POST JSON, avec l'erreur d'origine en cas d'échec (copié d'AUTO-CROUS)."""
    req = urllib.request.Request(
        url, data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode()
    if not body.lstrip().startswith("{"):
        raise ValueError("réponse non-JSON (salle d'attente anti-surcharge ?)")
    return json.loads(body)


def post_with_retry(url, payload):
    """Appel API avec retries : le site renvoie parfois du non-JSON (salle
    d'attente) ou des erreurs transitoires en période d'attribution."""
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return post_json(url, payload)
        except (urllib.error.URLError, ValueError, TimeoutError, OSError) as e:
            last = e
            if attempt < ATTEMPTS:
                log(f"appel API échoué ({e}) — nouvelle tentative dans {RETRY_DELAY}s")
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"API CROUS injoignable après {ATTEMPTS} tentatives : {last}")


def fetch_listings(tool_id):
    """Récupère tous les logements en recherche (France entière), avec pagination."""
    url = API_URL.format(tool_id=tool_id)
    payload = {
        "idTool": tool_id, "need_aggregation": False, "page": 1, "pageSize": 100,
        "sector": None, "occupationModes": [], "residence": None, "precision": None,
        "equipment": [], "price": {"min": 0, "max": 10000000}, "location": None,
    }
    items, page = [], 1
    while True:
        payload["page"] = page
        data = post_with_retry(url, payload)
        results = data.get("results") or {}
        batch = results.get("items") or []
        items.extend(batch)
        total = (results.get("total") or {}).get("value") or len(items)
        if not batch or len(items) >= total:
            return items
        page += 1


# ---------------------------------------------------------------------------
# Lokaviz — chambres/studios/T1bis <= 750 € autour du Lycée Rabelais (Paris)
# ---------------------------------------------------------------------------

def solve_anubis(random_data, difficulty):
    """Preuve de travail Anubis : sha256(randomData + nonce) en hex doit
    commencer par `difficulty` zéros (difficulté 2 sur lokaviz : ~256 essais,
    instantané). Validé sur le vrai challenge du site (04/10/2026)."""
    prefix = "0" * difficulty
    nonce = 0
    while True:
        digest = hashlib.sha256(f"{random_data}{nonce}".encode()).hexdigest()
        if digest.startswith(prefix):
            return digest, nonce
        nonce += 1


class AnubisSession:
    """Session HTTP qui franchit le challenge Anubis de lokaviz.fr.

    Le site utilise Anubis (anti-scraping massif par preuve de travail) :
    un usage individuel poli — une requête de recherche par vérification,
    comme un étudiant qui rafraîchit la page — passe le filtre en résolvant
    la preuve de travail exactement comme un navigateur.
    """

    def __init__(self, base=LOKAVIZ_BASE, user_agent=LOKAVIZ_UA):
        self.base = base
        self.ua = user_agent
        self.cookies = {}

    def _request(self, path, redirect=True):
        url = path if path.startswith("http") else self.base + path
        req = urllib.request.Request(url, headers={
            "User-Agent": self.ua,
            "Accept": "text/html,application/xhtml+xml,application/json",
            "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()) or "",
        })
        opener = urllib.request.build_opener()
        if not redirect:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **kw):
                    return None
            opener = urllib.request.build_opener(NoRedirect)
        try:
            resp = opener.open(req, timeout=30)
        except urllib.error.HTTPError as e:
            resp = e
        for header in resp.headers.get_all("Set-Cookie") or []:
            m = re.match(r"\s*([^=;]+)=([^;]*)", header)
            if m and m.group(2):
                self.cookies[m.group(1).strip()] = m.group(2).strip()
        return resp.read().decode("utf-8", "replace")

    def get(self, path):
        """GET en résolvant le challenge si nécessaire (retourne le HTML)."""
        body = self._request(path)
        if "anubis_challenge" not in body:
            return body
        m = re.search(
            r'<script id="anubis_challenge" type="application/json">(.*?)</script>',
            body, re.S)
        if not m:
            raise RuntimeError("challenge Anubis introuvable dans la page")
        challenge = json.loads(m.group(1))["challenge"]
        response, nonce = solve_anubis(challenge["randomData"],
                                       challenge.get("difficulty", 2))
        params = urllib.parse.urlencode({
            "id": challenge["id"], "response": response, "nonce": nonce,
            "redir": path if path.startswith("/") else "/" + path,
            "elapsedTime": "120",
        })
        self._request(f"/.within.website/x/cmd/anubis/api/pass-challenge?{params}",
                      redirect=False)
        body = self._request(path)
        if "anubis_challenge" in body:
            raise RuntimeError("challenge Anubis non résolu (cookie refusé)")
        return body


def lokaviz_search_url(type_ids=None, max_rent=None):
    """URL de recherche lokaviz avec les filtres (types + loyer max)."""
    type_ids = type_ids or LOKAVIZ_TYPE_IDS.split(",")
    max_rent = max_rent or LOKAVIZ_MAX_RENT
    filters = urllib.parse.urlencode(
        [("refloglogement_id[]", t) for t in type_ids if t.strip()]
        + [("loyer", str(max_rent))])
    return f"{LOKAVIZ_DEFAULT_SEARCH}&{filters}"


def strip_tags(fragment):
    """HTML -> texte propre (retours à la ligne -> ", ")."""
    txt = re.sub(r"<br\s*/?>", ", ", fragment)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html_mod.unescape(txt)
    return re.sub(r"\s+", " ", txt).strip(" ,")


def parse_lokaviz_listings(html):
    """Extrait les annonces d'une page de résultats lokaviz (rendu serveur).

    Retourne des dictionnaires bruts : id, link, type_txt, address,
    rent_txt, charges, dispo, desc, ref.
    """
    items = []
    for li in re.findall(r"<li><table.*?</table>\s*</li>", html, re.S):
        id_m = re.search(r"logement_id:(\d+)", li)
        if not id_m:
            continue
        href_m = re.search(r'href="(/rechercher-un-logement/fiche-logement/[^"]+)"', li)
        type_m = re.search(r'<span class="type">(.*?)</span>', li, re.S)
        rent_m = re.search(r'<span class="loyer"><strong>(.*?)</strong>', li, re.S)
        charges_m = re.search(r'class="charges">(.*?)</span>', li, re.S)
        dispo_m = re.search(r'<p class="date"><span>Disponible (?:le|du)</span>(.*?)</p>',
                            li, re.S)
        desc_m = re.search(r'</a></p><p class="">([^<]*)</p>', li)
        ref_m = re.search(r"Ref\.(?:&nbsp;|\s)([^<]+)", li)
        addresses = re.findall(r'<p class="adresse">(.*?)</p>', li, re.S)
        addr_html = next((a for a in addresses if strip_tags(a)), "")
        items.append({
            "id": id_m.group(1),
            "link": LOKAVIZ_BASE + href_m.group(1) if href_m else
                    f"{LOKAVIZ_BASE}/rechercher-un-logement/fiche-logement/logement_id:{id_m.group(1)}",
            "type_txt": strip_tags(type_m.group(1)) if type_m else "?",
            "address": strip_tags(addr_html),
            "rent_txt": strip_tags(rent_m.group(1)) if rent_m else "",
            "charges": strip_tags(charges_m.group(1)) if charges_m else "",
            "dispo": strip_tags(dispo_m.group(1)) if dispo_m else "",
            "desc": strip_tags(desc_m.group(1)) if desc_m else "",
            "ref": strip_tags(ref_m.group(1)) if ref_m else "",
        })
    return items


def summarize_lokaviz(item):
    """Condense une annonce lokaviz en fiche état/email (format commun CROUS)."""
    rent_nums = [int(n) for n in re.findall(r"\d+", item.get("rent_txt", ""))]
    per_room = "par chambre" in item.get("rent_txt", "")
    if not rent_nums:
        price, rent_min = "loyer non communiqué", None
    else:
        lo = min(rent_nums)
        price = (f"{lo} €/mois" if len(set(rent_nums)) == 1
                 else f"de {lo} à {max(rent_nums)} €/mois")
        if per_room:
            price += " par chambre"
        rent_min = lo * 100                     # centimes, cohérent avec CROUS
    residence = re.sub(r"\s*m2\b", " m²", item["type_txt"])
    area_m = re.search(r"([\d.,]+)\s*m²", residence)
    occupations = [f"Loyer : {item['rent_txt']}"] if item.get("rent_txt") else []
    if item.get("charges"):
        occupations.append(f"dont charges : {item['charges']}")
    if item.get("dispo"):
        occupations.append(f"disponible {item['dispo']}")
    if item.get("desc"):
        occupations.append(item["desc"])
    return {
        "id": str(item["id"]), "tool": LOKAVIZ_TOOL, "source": "lokaviz",
        "residence": residence,
        "address": item["address"] or "adresse non renseignée",
        "type": item.get("desc") or residence,
        "area_txt": area_m.group(1) if area_m else "?",
        "occupations": occupations,
        "price": price, "rent_min": rent_min,
        "booking_fee": None, "high_demand": False,
        "link": item["link"],
    }


def fetch_lokaviz(cfg, session=None):
    """Recherche filtrée lokaviz avec pagination (au plus max_pages pages).

    Les filtres (types de logement, loyer max, zone autour du Lycée Rabelais)
    sont appliqués par le SERVEUR : la page ne contient que les annonces
    correspondant aux critères.
    """
    session = session or AnubisSession()
    path, _, query = (cfg.get("lokaviz_search_path") or LOKAVIZ_DEFAULT_SEARCH
                     ).partition("?")
    filters = urllib.parse.urlencode(
        [("refloglogement_id[]", t) for t in cfg["lokaviz_type_ids"] if t.strip()]
        + [("loyer", str(cfg["lokaviz_max_rent"]))])
    full_query = "&".join(x for x in (query, filters) if x)
    items, page = [], 1
    while page <= cfg.get("lokaviz_max_pages", 10):
        page_path = path if page == 1 else f"{path}/page:{page}"
        try:
            html = session.get(f"{page_path}?{full_query}")
        except (urllib.error.URLError, OSError, RuntimeError) as e:
            if page == 1:
                raise RuntimeError(f"lokaviz injoignable : {e}")
            log(f"lokaviz : page {page} illisible ({e}) — arrêt de la pagination.")
            break
        batch = parse_lokaviz_listings(html)
        items.extend(batch)
        nxt = re.search(r'href="([^"]*page:(\d+)[^"]*)"[^>]*id="pag_next"', html)
        if not nxt:
            break
        page = int(nxt.group(2))
    # dédoublonne par ID (sécurité si une page change entre deux requêtes)
    unique, seen_ids = [], set()
    for it in items:
        if it["id"] not in seen_ids:
            seen_ids.add(it["id"])
            unique.append(it)
    return unique


def in_zone(item, prefixes):
    r"""Vrai si le code postal de la résidence commence par l'un des préfixes.

    Un code postal français fait 5 chiffres ; les bornes (?<!\d)/(?!\d)
    évitent les faux positifs (ex: 67500 ne matche pas le préfixe 75).
    """
    address = (item.get("residence") or {}).get("address") or ""
    return any(re.search(rf"(?<!\d){p}\d{{3}}(?!\d)", address) for p in prefixes)


def fmt_euros(centimes):
    return f"{centimes / 100:.2f}".replace(".", ",")


def fmt_area(area):
    lo, hi = area.get("min"), area.get("max")
    if lo is None:
        return "?"
    if hi and lo != hi:
        return f"{fmt_num(lo)}-{fmt_num(hi)}"
    return fmt_num(lo)


def fmt_num(v):
    return str(int(v)) if float(v) == int(v) else str(v)


def summarize(item, tool_id):
    """Condense un logement en entrée d'état / fiche email."""
    res = item.get("residence") or {}
    occupations = []
    for mode in item.get("occupationModes") or []:
        rent = mode.get("rent") or {}
        lo, hi = rent.get("min"), rent.get("max")
        if lo is not None and hi is not None and lo != hi:
            price = f"{fmt_euros(lo)} à {fmt_euros(hi)} €"
        else:
            value = lo if lo is not None else hi
            price = f"{fmt_euros(value)} €" if value is not None else "loyer non communiqué"
        label = OCCUPATION_LABELS.get(mode.get("type"), mode.get("type") or "?")
        occupations.append(f"{label} : {price}")
    # prix affiché en évidence : loyer du mode d'occupation principal
    first_mode = (item.get("occupationModes") or [{}])[0]
    first_rent = (first_mode.get("rent") or {}).get("min")
    fee = (item.get("bookingData") or {}).get("amount")
    return {
        "id": str(item.get("id")),
        "tool": tool_id,
        "residence": res.get("label", "?"),
        "address": res.get("address", "?"),
        "type": item.get("label", "?"),
        "area_txt": fmt_area(item.get("area") or {}),
        "occupations": occupations,
        "price": f"{fmt_euros(first_rent)} €/mois" if first_rent is not None else "prix NC",
        "rent_min": first_rent,
        "booking_fee": f"{fmt_euros(fee)} €" if fee is not None else None,
        "high_demand": bool(item.get("highDemand")),
        "link": f"{SITE_URL.format(tool_id=tool_id)}/accommodations/{item.get('id')}",
    }


def load_state():
    """Charge seen.json ; None si absent/illisible (re-mémorisation silencieuse).

    Migration v1 -> v2 : les anciennes clés nues ("522") deviennent "47:522" ;
    l'absence d'initialized_tools signifie que seul l'outil 47 tournait.
    """
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        log("seen.json illisible — re-mémorisation complète (aucun email envoyé).")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        return None
    items = {f"{LEGACY_TOOL}:{k}" if re.fullmatch(r"\d+", k) else k: v
             for k, v in data["items"].items()}
    initialized = data.get("initialized_tools") or [LEGACY_TOOL]
    # outils CROUS (entiers) + sources nommées ("lokaviz") peuvent cohabiter
    tools = [int(t) if str(t).isdigit() else t for t in initialized]
    return {"items": items, "initialized_tools": tools}


def save_state(current, initialized_tools):
    # tri stable : entiers (outils CROUS) d'abord, puis sources nommées
    tools = sorted(set(initialized_tools), key=lambda t: (isinstance(t, str), t))
    STATE.write_text(
        json.dumps({"updated": datetime.now().isoformat(timespec="seconds"),
                    "initialized_tools": tools,
                    "items": current},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")


def load_history_entries():
    if not HISTORY.exists():
        return []
    entries = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def history_keys(entries=None):
    entries = load_history_entries() if entries is None else entries
    return {f"{e['tool']}:{e['id']}" for e in entries}


def append_history(new_items):
    """Ajoute une ligne par logement détecté (nouveau ou réapparu)."""
    with HISTORY.open("a", encoding="utf-8") as f:
        for key, s in sorted(new_items.items()):
            f.write(json.dumps(
                {"ts": datetime.now().isoformat(timespec="seconds"),
                 "tool": s["tool"], "id": s["id"], "residence": s["residence"],
                 "address": s["address"], "type": s["type"],
                 "rent_min": s["rent_min"], "reappeared": s.get("reappeared", False),
                 "link": s["link"]},
                ensure_ascii=False) + "\n")


def write_gha_outputs(**kv):
    """Expose des résultats au workflow GitHub Actions (si applicable)."""
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as f:
        for key, value in kv.items():
            f.write(f"{key}={value}\n")


# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------

def send_email(cfg, subject, text, html):
    """Envoie l'email via Gmail SMTP SSL (mot de passe d'application)."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = cfg["gmail_user"]
    msg["To"] = ", ".join(cfg["notify_emails"])
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as smtp:
        smtp.login(cfg["gmail_user"], cfg["gmail_app_password"])
        smtp.sendmail(cfg["gmail_user"], cfg["notify_emails"], msg.as_string())


def build_notification(new, zone_count):
    """Email d'alerte : le lien direct, le prix et l'adresse AVANT TOUT."""
    now = datetime.now().strftime("%d/%m/%Y à %H:%M")
    entries = sorted(new.values(), key=lambda s: (s["residence"], s["id"]))
    n = len(entries)
    sources = {s.get("source", "crous") for s in entries}
    brand = "Lokaviz Paris" if sources == {"lokaviz"} else (
        "Logements Paris" if len(sources) > 1 else "CROUS Paris")
    if n == 1:
        subject = f"{brand} — {entries[0]['residence']} · {entries[0]['price']}"
    else:
        subject = f"{brand} — {n} logements disponibles ({entries[0]['residence']}…)"

    blocks = []
    for s in entries:
        extra = []
        if s["occupations"]:
            extra.append(" / ".join(s["occupations"]))
        if s["booking_fee"]:
            extra.append(f"frais de dossier : {s['booking_fee']}")
        if s["high_demand"]:
            extra.append("logement très demandé, ne tardez pas !")
        blocks.append(
            f"Résidence : {s['residence']}\n"
            f"Adresse : {s['address']}\n"
            f"Prix : {s['price']}\n"
            f"Type : {s['type']} — {s['area_txt']} m²\n"
            + ("   " + " ; ".join(extra) + "\n" if extra else "")
            + f"RÉSERVER : {s['link']}\n")
    search_links = f"Recherche complète : {SITE_URL.format(tool_id=47)}/search\n"
    if "lokaviz" in sources:
        search_links += ("Recherche Lokaviz (filtres appliqués) : "
                         + lokaviz_search_url() + "\n")
    text = (f"{n} logement(s) disponible(s) — détecté le {now}.\n\n"
            + "\n".join(blocks)
            + "\n" + search_links
            + "\n— Bot logements étudiants Paris (surveillance automatique)\n")

    cards = []
    for s in entries:
        occ = html_mod.escape(" / ".join(s["occupations"]) or "—")
        fee_line = (f'<p style="margin:2px 0;font-size:13px;color:#666;">'
                    f"Frais de dossier : {html_mod.escape(s['booking_fee'])}</p>"
                    if s["booking_fee"] else "")
        demand_line = (f'<p style="margin:2px 0;font-size:13px;color:#b30000;">'
                       f"<strong>Logement très demandé — ne tardez pas !</strong></p>"
                       if s["high_demand"] else "")
        cards.append(f"""
      <div style="border:1px solid #ddd;border-radius:8px;padding:18px;margin:0 0 18px;">
        <p style="margin:0 0 4px;font-size:18px;font-weight:bold;color:#161616;">{html_mod.escape(s['residence'])}</p>
        <p style="margin:0 0 10px;font-size:22px;font-weight:bold;color:#000091;">{html_mod.escape(s['price'])}</p>
        <p style="margin:2px 0;font-size:14px;color:#333;">{html_mod.escape(s['address'])}</p>
        <p style="margin:2px 0;font-size:14px;color:#333;">{html_mod.escape(s['type'])} &middot; {html_mod.escape(s['area_txt'])} m² &middot; {occ}</p>
        {fee_line}{demand_line}
        <a href="{s['link']}" style="display:inline-block;margin-top:12px;background:#000091;color:#ffffff;text-decoration:none;padding:12px 26px;border-radius:5px;font-size:16px;font-weight:bold;">Voir et réserver</a>
      </div>""")
    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:600px;margin:20px auto;background:#ffffff;border-radius:8px;overflow:hidden;">
    <div style="background:#000091;color:#ffffff;padding:18px 24px;">
      <h1 style="margin:0;font-size:18px;">{n} logement(s) {brand} — {now}</h1>
      <p style="margin:6px 0 0;font-size:13px;opacity:.85;">{zone_count} logement(s) au total dans la zone surveillée</p>
    </div>
    <div style="padding:24px;">{''.join(cards)}
      <p style="color:#666;font-size:12px;margin:0;">
        Email automatique de votre bot de surveillance CROUS.
      </p>
    </div>
  </div>
</body></html>"""
    return subject, text, html


def send_test_email(cfg, zone_count, france_count):
    subject = "CROUS Paris — email de test du bot"
    text = (f"Ceci est un email de test de votre bot de surveillance CROUS.\n\n"
            f"Si vous le recevez, la configuration Gmail fonctionne.\n\n"
            f"Logements actuellement détectés : {zone_count} dans la zone surveillée, "
            f"{france_count} au total en France.\n"
            f"Vous recevrez un email dès qu'un nouveau logement apparaîtra.\n\n"
            f"— Bot CROUS Paris")
    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:600px;margin:20px auto;background:#ffffff;border-radius:8px;overflow:hidden;">
    <div style="background:#000091;color:#ffffff;padding:20px 24px;">
      <h1 style="margin:0;font-size:19px;">Email de test — bot CROUS Paris</h1>
    </div>
    <div style="padding:24px;">
      <p style="font-size:14px;color:#333;">Si vous recevez cet email, la configuration Gmail du bot fonctionne.</p>
      <p style="font-size:14px;color:#333;">Logements actuellement détectés :
      <strong>{zone_count}</strong> dans la zone surveillée ({france_count} au total en France).</p>
      <p style="font-size:14px;color:#333;">Vous recevrez un email dès qu'un nouveau logement apparaîtra.</p>
    </div>
  </div>
</body></html>"""
    send_email(cfg, subject, text, html)
    log(f"Email de test envoyé à {', '.join(cfg['notify_emails'])}.")


# ---------------------------------------------------------------------------
# Watchdog (dead man's switch)
# ---------------------------------------------------------------------------

def fetch_workflow_runs(token, repo, workflow="crous.yml", per_page=30):
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}"
           f"/runs?per_page={per_page}")
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}",
                      "Accept": "application/vnd.github+json",
                      "User-Agent": "crous-watchdog"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("workflow_runs", [])


def watchdog_decision(runs, now=None):
    """Analyse les runs du workflow de surveillance.

    Retourne None (tout va bien / panne déjà signalée) ou un dict
    décrivant la panne. Seuls les runs planifiés (cron) comptent : un run
    manuel ne prouve pas que la surveillance automatique fonctionne.
    """
    now = now or datetime.now(timezone.utc)
    scheduled = [r for r in runs if r.get("event") == "schedule"]
    if not scheduled:
        return {"age_min": None, "last_conclusion": None,
                "reason": "aucun run planifié récent (cron désactivé ?)"}
    last_ok = next((r for r in scheduled if r.get("conclusion") == "success"), None)
    if last_ok is None:
        return {"age_min": None, "last_conclusion": scheduled[0].get("conclusion"),
                "reason": "des runs planifiés existent mais aucun ne réussit"}
    updated = datetime.strptime(last_ok["updated_at"], "%Y-%m-%dT%H:%M:%SZ")
    updated = updated.replace(tzinfo=timezone.utc)
    age_min = int((now - updated).total_seconds() / 60)
    if WATCHDOG_THRESHOLD_MIN <= age_min <= WATCHDOG_WINDOW_MAX:
        return {"age_min": age_min, "last_conclusion": last_ok.get("conclusion"),
                "reason": f"dernière vérification réussie il y a {age_min} min"}
    return None


def build_watchdog_alert(decision):
    age = decision["age_min"]
    age_txt = f"il y a {age} min" if age is not None else "jamais"
    subject = f"PANNE bot CROUS — dernière vérification réussie {age_txt}"
    text = (f"LE BOT CROUS EST EN PANNE.\n\n"
            f"Dernière vérification réussie : {age_txt}.\n"
            f"Diagnostic : {decision['reason']} (dernier résultat de run : "
            f"{decision['last_conclusion'] or 'inconnu'}).\n\n"
            f"Vérifie les logs et déclenche un run manuel :\n"
            f"https://github.com/pipionkakiandpipi/bot-crous-paris/actions\n\n"
            f"— Bot CROUS Paris (watchdog)\n")
    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:600px;margin:20px auto;background:#ffffff;border-radius:8px;overflow:hidden;">
    <div style="background:#b30000;color:#ffffff;padding:20px 24px;">
      <h1 style="margin:0;font-size:19px;">Le bot CROUS est en panne</h1>
    </div>
    <div style="padding:24px;">
      <p style="font-size:15px;color:#333;">Dernière vérification réussie : <strong>{age_txt}</strong>.</p>
      <p style="font-size:14px;color:#333;">{html_mod.escape(decision['reason'])}
      (dernier résultat : {decision['last_conclusion'] or 'inconnu'}).</p>
      <a href="https://github.com/pipionkakiandpipi/bot-crous-paris/actions"
         style="display:inline-block;margin-top:12px;background:#b30000;color:#ffffff;text-decoration:none;padding:12px 22px;border-radius:5px;font-size:15px;font-weight:bold;">Voir les logs GitHub</a>
    </div>
  </div>
</body></html>"""
    return subject, text, html


def run_watchdog(cfg):
    token = os.environ.get("GITHUB_API_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not token or not repo:
        die("watchdog : variables GITHUB_API_TOKEN et GITHUB_REPOSITORY requises.")
    try:
        runs = fetch_workflow_runs(token, repo)
    except (urllib.error.URLError, OSError, ValueError) as e:
        die(f"watchdog : impossible de lire l'historique GitHub ({e}).")
    decision = watchdog_decision(runs)
    if decision is None:
        log("Watchdog : pas d'alerte (vérification réussie récente, ou panne déjà signalée).")
        return
    subject, text, html = build_watchdog_alert(decision)
    send_email(cfg, subject, text, html)
    log(f"Watchdog : EMAIL D'ALERTE envoyé ({decision['reason']}).")


# ---------------------------------------------------------------------------
# Digest hebdomadaire
# ---------------------------------------------------------------------------

def digest_stats(entries, now=None):
    now = now or datetime.now()
    if not entries:
        return {"detections": 0, "reappeared": 0, "first_day": None, "last_day": None}
    days = sorted(datetime.fromisoformat(e["ts"]).date() for e in entries)
    return {"detections": len(entries),
            "reappeared": sum(1 for e in entries if e.get("reappeared")),
            "first_day": days[0].strftime("%d/%m"),
            "last_day": days[-1].strftime("%d/%m")}


def build_digest_email(stats, available):
    period = (f"du {stats['first_day']} au {stats['last_day']}"
              if stats["first_day"] else "— historique vide pour l'instant")
    subject = f"Bot CROUS Paris — veille {period} : {stats['detections']} logement(s) détecté(s)"
    text = (f"Résumé de veille du bot CROUS Paris ({period}) :\n\n"
            f"- Logements détectés au total : {stats['detections']}\n"
            f"- Dont réapparus (réservations abandonnées) : {stats['reappeared']}\n"
            f"- Actuellement disponibles à Paris : {available}\n\n"
            f"Le bot fonctionne — il vérifie toutes les 10 minutes.\n\n"
            f"— Bot CROUS Paris (digest hebdomadaire)\n")
    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:600px;margin:20px auto;background:#ffffff;border-radius:8px;overflow:hidden;">
    <div style="background:#000091;color:#ffffff;padding:20px 24px;">
      <h1 style="margin:0;font-size:19px;">Bot CROUS Paris — résumé de veille</h1>
      <p style="margin:6px 0 0;font-size:13px;opacity:.85;">{period}</p>
    </div>
    <div style="padding:24px;">
      <p style="font-size:16px;color:#333;">Logements détectés au total : <strong>{stats['detections']}</strong></p>
      <p style="font-size:15px;color:#333;">Dont réapparus : {stats['reappeared']}</p>
      <p style="font-size:15px;color:#333;">Actuellement disponibles à Paris : <strong>{available}</strong></p>
      <p style="font-size:13px;color:#666;">Le bot fonctionne — il vérifie toutes les 10 minutes.</p>
    </div>
  </div>
</body></html>"""
    return subject, text, html


def run_digest(cfg):
    entries = load_history_entries()
    state = load_state() or {"items": {}}
    stats = digest_stats(entries)
    subject, text, html = build_digest_email(stats, len(state["items"]))
    send_email(cfg, subject, text, html)
    log(f"Digest envoyé — {stats['detections']} détection(s) au total, "
        f"{len(state['items'])} logement(s) disponible(s) dans la zone.")


# ---------------------------------------------------------------------------
# Vérification principale
# ---------------------------------------------------------------------------

def check(cfg, zone_by_tool):
    """Compare la zone surveillée (tous outils + lokaviz) avec l'état précédent."""
    current = {}
    for tool_id, items in zone_by_tool.items():
        for item in items:
            s = (summarize_lokaviz(item) if isinstance(tool_id, str)
                 else summarize(item, tool_id))
            current[f"{tool_id}:{s['id']}"] = s
        log(f"outil {tool_id} : {len(items)} logement(s) dans la zone surveillée")

    state = load_state()
    if state is None:
        save_state(current, zone_by_tool.keys())
        log(f"Premier passage — {len(current)} logement(s) mémorisé(s) dans "
            f"seen.json, aucun email envoyé.")
        return

    seen = state["items"]
    initialized = list(state["initialized_tools"])
    newly_initialized = False

    # Outils ajoutés depuis le dernier passage : initialisation silencieuse.
    # L'outil est marqué initialisé dès qu'il est surveillé avec succès,
    # même vide : ses FUTURS logements seront de vraies nouveautés.
    silent = set()
    for tool_id in zone_by_tool:
        if tool_id not in initialized:
            initialized.append(tool_id)
            newly_initialized = True
            keys = {k for k in current if k.startswith(f"{tool_id}:")}
            silent |= keys
            log(f"outil {tool_id} initialisé — {len(keys)} logement(s) "
                f"mémorisé(s) sans notification.")

    new = {k: s for k, s in current.items()
           if k not in seen and k not in silent}
    if new:
        known = history_keys()
        for s in new.values():
            s["reappeared"] = f"{s['tool']}:{s['id']}" in known
        names = ", ".join(s["residence"] for s in new.values())
        log(f"NOUVEAUTÉ — {len(new)} logement(s) : {names}")
        subject, text, html = build_notification(new, len(current))
        if cfg["dry_run"]:
            PREVIEW_TXT.write_text(text, encoding="utf-8")
            PREVIEW_HTML.write_text(html, encoding="utf-8")
            log(f"DRY-RUN — email simulé, aperçu écrit ({len(new)} nouveau(x)). "
                f"État et historique non modifiés.")
            write_gha_outputs(new_count=0, zone_count=len(current))
            return
        send_email(cfg, subject, text, html)
        append_history(new)
        log(f"EMAIL ENVOYÉ à {', '.join(cfg['notify_emails'])}.")
        write_gha_outputs(new_count=len(new), zone_count=len(current))

    if not cfg["dry_run"] and (newly_initialized or current != seen):
        save_state(current, initialized)
    log(f"OK — {len(current)} logement(s) en ligne dans la zone, {len(new)} nouveau(x).")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    load_env_file()
    cfg = load_config()
    if not (cfg["gmail_user"] and cfg["gmail_app_password"] and cfg["notify_emails"]):
        die("Configuration incomplète — définissez GMAIL_USER, GMAIL_APP_PASSWORD "
            "et NOTIFY_EMAIL (variables d'environnement ou fichier .env).")

    if cfg["watchdog"]:
        run_watchdog(cfg)
        return
    if cfg["digest"]:
        run_digest(cfg)
        return

    log(f"Démarrage — outils CROUS : {', '.join(str(t) for t in cfg['tool_ids'])}, "
        f"zone : codes postaux {', '.join(cfg['postal_prefixes'])}xxx"
        + (f" ; lokaviz : types {LOKAVIZ_TYPE_IDS} <= {LOKAVIZ_MAX_RENT} €"
           if cfg["lokaviz_enabled"] else ""))

    zone_by_tool, france_counts, failures = {}, {}, []
    for tool_id in cfg["tool_ids"]:
        try:
            items = fetch_listings(tool_id)
        except RuntimeError as e:
            failures.append(tool_id)
            log(f"outil {tool_id} : ÉCHEC ({e})")
            continue
        zone = [it for it in items
                if it.get("available", True) and in_zone(it, cfg["postal_prefixes"])]
        log(f"outil {tool_id} : {len(items)} logement(s) en ligne (France), "
            f"{len(zone)} dans la zone.")
        zone_by_tool[tool_id] = zone
        france_counts[tool_id] = len(items)

    if cfg["lokaviz_enabled"]:
        try:
            lokaviz_items = fetch_lokaviz(cfg)
            zone_by_tool[LOKAVIZ_TOOL] = lokaviz_items
            log(f"lokaviz : {len(lokaviz_items)} annonce(s) correspondant aux "
                f"filtres (types + {cfg['lokaviz_max_rent']} € max, autour du "
                f"Lycée Rabelais).")
        except RuntimeError as e:
            failures.append(LOKAVIZ_TOOL)
            log(f"lokaviz : ÉCHEC ({e})")

    if not zone_by_tool:
        die("API CROUS injoignable pour tous les outils — rien à vérifier.")

    if cfg["send_test_email"]:
        if cfg["dry_run"]:
            log("DRY-RUN — email de test simulé : non envoyé.")
        else:
            try:
                send_test_email(cfg,
                                sum(len(v) for v in zone_by_tool.values()),
                                sum(france_counts.values()))
            except Exception as e:
                die(f"échec de l'email de test (configuration Gmail ?) : {e}")

    check(cfg, zone_by_tool)


if __name__ == "__main__":
    main()
