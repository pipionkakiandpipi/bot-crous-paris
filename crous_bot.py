#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CROUS Paris — surveillance des nouveaux logements + notification par email.

Basé sur AUTO-CROUS (https://github.com/Kdevos12/AUTO-CROUS, licence MIT),
adapté pour :
  - une notification par email (Gmail SMTP) au lieu d'un ping Discord ;
  - un filtrage géographique côté script (codes postaux 75xxx) : le paramètre
    "location" de l'API interne n'est plus accepté tel quel depuis la
    refonte du site ;
  - une exécution périodique via GitHub Actions (gratuit, tourne 24/7,
    même PC éteint).

Principe : l'API de recherche du site trouverunlogement.lescrous.fr ne
renvoie que des logements DISPONIBLES. Le script récupère tous les
logements, garde ceux de la zone surveillée (Paris), compare leurs
identifiants avec le passage précédent (seen.json) et envoie un email
dès qu'un nouveau logement apparaît. Le premier passage mémorise
l'existant sans envoyer d'email (anti faux positifs). Un logement qui
disparaît (réservé) puis réapparaît (réservation abandonnée) déclenche
à nouveau un email.

Variables d'environnement (ou fichier .env local) :
    GMAIL_USER             adresse Gmail utilisée pour envoyer (ex: prenom@gmail.com)
    GMAIL_APP_PASSWORD     mot de passe d'application Gmail (16 caractères)
    NOTIFY_EMAIL           destinataire(s), séparés par des virgules si plusieurs
    CROUS_TOOL_ID          optionnel — id de l'outil de recherche (défaut : 47)
    PARIS_POSTAL_PREFIXES  optionnel — préfixes de codes postaux (défaut : "75",
                           ex: "75,92,93,94" pour Paris + petite couronne)
    SEND_TEST_EMAIL        optionnel — "true" pour envoyer un email de test

Usage :
    python crous_bot.py --test-email   # email de test + passage de surveillance
    python crous_bot.py --dry-run      # surveillance simulée : écrit un aperçu
                                       # de l'email (email_preview.txt/.html)
                                       # sans rien envoyer ni modifier l'état
    python crous_bot.py                # passage normal (celui du cron)
"""
import html as html_mod
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "seen.json"
ENV_FILE = HERE / ".env"

API_URL = "https://trouverunlogement.lescrous.fr/api/fr/search/{tool_id}"
SITE_URL = "https://trouverunlogement.lescrous.fr/tools/{tool_id}"
OCCUPATION_LABELS = {"alone": "seul(e)", "couple": "en couple",
                     "house_sharing": "colocation"}
ATTEMPTS = 3          # tentatives d'appel API avant abandon
RETRY_DELAY = 30      # secondes entre deux tentatives (salle d'attente anti-surcharge)


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
    cfg = {
        "gmail_user": os.environ.get("GMAIL_USER", "").strip(),
        "gmail_app_password": os.environ.get("GMAIL_APP_PASSWORD", "").strip(),
        "notify_emails": [e.strip() for e in os.environ.get("NOTIFY_EMAIL", "").split(",")
                          if e.strip()],
        "tool_id": int(os.environ.get("CROUS_TOOL_ID", "47").strip() or "47"),
        "postal_prefixes": [p.strip().zfill(2) for p in
                            os.environ.get("PARIS_POSTAL_PREFIXES", "75").split(",")
                            if p.strip()],
        "send_test_email": (os.environ.get("SEND_TEST_EMAIL", "").strip().lower() == "true"
                            or "--test-email" in sys.argv),
        "dry_run": "--dry-run" in sys.argv,
    }
    return cfg


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
    fee = (item.get("bookingData") or {}).get("amount")
    return {
        "id": str(item.get("id")),
        "residence": res.get("label", "?"),
        "address": res.get("address", "?"),
        "type": item.get("label", "?"),
        "area_txt": fmt_area(item.get("area") or {}),
        "occupations": occupations,
        "booking_fee": f"{fmt_euros(fee)} €" if fee is not None else None,
        "high_demand": bool(item.get("highDemand")),
        "link": f"{SITE_URL.format(tool_id=tool_id)}/accommodations/{item.get('id')}",
    }


def load_state():
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        log("seen.json illisible — re-mémorisation complète (aucun email envoyé).")
        return None
    return data if isinstance(data, dict) and isinstance(data.get("items"), dict) else None


def save_state(current):
    STATE.write_text(
        json.dumps({"updated": datetime.now().isoformat(timespec="seconds"),
                    "items": current},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")


def write_gha_outputs(**kv):
    """Expose des résultats au workflow GitHub Actions (si applicable)."""
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as f:
        for key, value in kv.items():
            f.write(f"{key}={value}\n")


def build_notification(new, zone_count, tool_id):
    """Construit le sujet + corps texte + corps HTML de l'email d'alerte."""
    now = datetime.now().strftime("%d/%m/%Y à %H:%M")
    n = len(new)
    subject = f"CROUS Paris : {n} nouveau(x) logement(s) étudiant disponible(s) !"
    entries = sorted(new.values(), key=lambda s: (s["residence"], s["id"]))

    lines = []
    for s in entries:
        details = f"{s['type']} — {s['area_txt']} m²"
        if s["occupations"]:
            details += " — " + " / ".join(s["occupations"])
        lines.append(
            f"Résidence : {s['residence']}\n"
            f"Adresse : {s['address']}\n"
            f"{details}\n"
            + (f"Frais de dossier : {s['booking_fee']}\n" if s["booking_fee"] else "")
            + ("ATTENTION : logement très demandé, ne tardez pas !\n" if s["high_demand"] else "")
            + f"Voir et réserver : {s['link']}\n")
    text = (f"{n} nouveau(x) logement(s) CROUS disponible(s) dans la zone surveillée "
            f"(détecté le {now}).\n\n"
            + "\n".join(lines)
            + f"\nRechercher sur le site : {SITE_URL.format(tool_id=tool_id)}/search\n"
            + "\n— Bot CROUS Paris (surveillance automatique)\n"
            f"Pour réserver, connectez-vous avec votre compte étudiant sur mes-services.etudiant.gouv.fr.")

    cards = []
    for s in entries:
        occ = html_mod.escape(" / ".join(s["occupations"]) or "—")
        fee_line = (f'<p style="margin:4px 0;font-size:13px;color:#666;">'
                    f"Frais de dossier : {html_mod.escape(s['booking_fee'])}</p>"
                    if s["booking_fee"] else "")
        demand_line = (f'<p style="margin:4px 0;font-size:13px;color:#b30000;">'
                       f"<strong>Logement très demandé — ne tardez pas !</strong></p>"
                       if s["high_demand"] else "")
        cards.append(f"""
      <div style="border:1px solid #ddd;border-radius:8px;padding:16px;margin:0 0 16px;">
        <p style="margin:0 0 2px;font-size:17px;font-weight:bold;color:#161616;">{html_mod.escape(s['residence'])}</p>
        <p style="margin:0 0 10px;font-size:13px;color:#666;">{html_mod.escape(s['address'])}</p>
        <p style="margin:4px 0;font-size:14px;color:#333;">{html_mod.escape(s['type'])} &middot; {html_mod.escape(s['area_txt'])} m² &middot; {occ}</p>
        {fee_line}{demand_line}
        <a href="{s['link']}" style="display:inline-block;margin-top:10px;background:#000091;color:#ffffff;text-decoration:none;padding:10px 18px;border-radius:4px;font-size:14px;font-weight:bold;">Voir et réserver</a>
      </div>""")
    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:600px;margin:20px auto;background:#ffffff;border-radius:8px;overflow:hidden;">
    <div style="background:#000091;color:#ffffff;padding:20px 24px;">
      <h1 style="margin:0;font-size:19px;">{n} nouveau(x) logement(s) CROUS à Paris</h1>
      <p style="margin:6px 0 0;font-size:13px;opacity:.85;">Détecté le {now} — {zone_count} logement(s) au total dans la zone</p>
    </div>
    <div style="padding:24px;">{''.join(cards)}
      <p style="color:#666;font-size:12px;margin:0;">
        Email envoyé automatiquement par votre bot de surveillance CROUS.<br>
        Recherche complète : {SITE_URL.format(tool_id=tool_id)}/search
      </p>
    </div>
  </div>
</body></html>"""
    return subject, text, html


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


def check(cfg, zone_items):
    """Compare la zone surveillée avec l'état précédent, notifie les nouveautés."""
    current = {}
    for item in zone_items:
        summary = summarize(item, cfg["tool_id"])
        current[summary["id"]] = summary
    state = load_state()

    if state is None:
        save_state(current)
        log(f"Premier passage — {len(current)} logement(s) mémorisé(s) dans seen.json, "
            f"aucun email envoyé.")
        return

    seen = state.get("items") or {}
    new = {i: s for i, s in current.items() if i not in seen}

    if new:
        names = ", ".join(s["residence"] for s in new.values())
        log(f"NOUVEAUTÉ — {len(new)} logement(s) : {names}")
        subject, text, html = build_notification(new, len(current), cfg["tool_id"])
        if cfg["dry_run"]:
            (HERE / "email_preview.txt").write_text(text, encoding="utf-8")
            (HERE / "email_preview.html").write_text(html, encoding="utf-8")
            log(f"DRY-RUN — email simulé ({len(new)} nouveau(x)), aperçu dans "
                f"email_preview.txt / email_preview.html. État non modifié.")
            write_gha_outputs(new_count=0, paris_count=len(current))
            return
        send_email(cfg, subject, text, html)
        log(f"EMAIL ENVOYÉ à {', '.join(cfg['notify_emails'])}.")
        write_gha_outputs(new_count=len(new), paris_count=len(current))

    if not cfg["dry_run"] and current != seen:
        save_state(current)
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

    log(f"Démarrage — outil CROUS n°{cfg['tool_id']}, zone : codes postaux "
        f"{', '.join(cfg['postal_prefixes'])}xxx.")
    try:
        items = fetch_listings(cfg["tool_id"])
    except RuntimeError as e:
        die(str(e))

    zone_items = [it for it in items
                  if it.get("available", True) and in_zone(it, cfg["postal_prefixes"])]
    log(f"Recherche : {len(items)} logement(s) disponible(s) en France, "
        f"{len(zone_items)} dans la zone surveillée.")

    if cfg["send_test_email"]:
        if cfg["dry_run"]:
            log("DRY-RUN — email de test simulé : non envoyé.")
        else:
            try:
                send_test_email(cfg, len(zone_items), len(items))
            except Exception as e:
                die(f"échec de l'email de test (configuration Gmail ?) : {e}")

    check(cfg, zone_items)


if __name__ == "__main__":
    main()
