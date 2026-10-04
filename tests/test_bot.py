# -*- coding: utf-8 -*-
"""Tests du bot CROUS Paris (v2) — unittest, aucune dépendance.

Les fixtures viennent d'une réponse RÉELLE de l'API CROUS du 03/10/2026
(outil 47) ; seules deux adresses ont été modifiées en 75xxx Paris pour
couvrir le filtre géographique (aucun logement Paris réel disponible ce
jour-là). Voir tests/fixtures/.
"""
import copy
import hashlib
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import crous_bot

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["results"]["items"]


def zone_items(items, prefixes=("75",)):
    return [it for it in items
            if it.get("available", True) and crous_bot.in_zone(it, list(prefixes))]


def make_cfg(tools=(47, 44), dry_run=False):
    return {"gmail_user": "t@gmail.com", "gmail_app_password": "x",
            "notify_emails": ["t@gmail.com"], "tool_ids": list(tools),
            "postal_prefixes": ["75"], "send_test_email": False,
            "dry_run": dry_run, "lokaviz_type_ids": ["4", "1", "146"],
            "lokaviz_max_rent": 750, "lokaviz_max_pages": 10}


class BotTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        crous_bot.STATE = Path(self.tmp.name) / "seen.json"
        crous_bot.HISTORY = Path(self.tmp.name) / "history.jsonl"
        crous_bot.PREVIEW_TXT = Path(self.tmp.name) / "email_preview.txt"
        crous_bot.PREVIEW_HTML = Path(self.tmp.name) / "email_preview.html"

    def tearDown(self):
        self.tmp.cleanup()

    def write_state(self, items, initialized=(47,)):
        crous_bot.STATE.write_text(json.dumps(
            {"updated": "2026-10-03T12:00:00",
             "initialized_tools": list(initialized), "items": items},
            ensure_ascii=False), encoding="utf-8")

    def run_check(self, cfg, items_by_tool):
        zone_by_tool = {t: zone_items(items) for t, items in items_by_tool.items()}
        with mock.patch.object(crous_bot, "send_email") as fake_send:
            crous_bot.check(cfg, zone_by_tool)
            return fake_send


class TestZone(BotTestCase):
    def test_filtre_paris(self):
        items = load_fixture("api_47.json")
        paris = zone_items(items)
        labels = sorted((it.get("residence") or {}).get("label") for it in paris)
        self.assertEqual(labels, ["RESIDENCE TEST PARIS 1", "RESIDENCE TEST PARIS 2"])

    def test_anti_faux_positifs(self):
        cases = [
            ("1 rue X 67500 STRASBOURG", False),
            ("1 rue X 40000 MONT-DE-MARSAN", False),
            ("1 rue X 75013 PARIS", True),
            ("1 rue X 75020 PARIS", True),
            ("1 rue X 97110 POINTE-A-PITRE", False),
            ("1 rue X 750130 BOGUS", False),
        ]
        for address, expected in cases:
            item = {"residence": {"address": address}}
            self.assertEqual(crous_bot.in_zone(item, ["75"]), expected, address)

    def test_adresse_absente(self):
        self.assertFalse(crous_bot.in_zone({"residence": {}}, ["75"]))
        self.assertFalse(crous_bot.in_zone({}, ["75"]))


class TestSummarize(BotTestCase):
    def test_resume_prix_lien(self):
        item = load_fixture("api_47.json")[4]  # RESIDENCE TEST PARIS 1
        item["occupationModes"] = [
            {"type": "alone", "rent": {"min": 45000, "max": 47000}},
            {"type": "house_sharing", "rent": {"min": 30000, "max": 30000}}]
        s = crous_bot.summarize(item, 44)
        self.assertEqual(s["price"], "450,00 €/mois")   # loyer min des modes
        self.assertEqual(s["link"], f"https://trouverunlogement.lescrous.fr"
                                    f"/tools/44/accommodations/{item['id']}")
        self.assertIn("75010 PARIS", s["address"])
        self.assertTrue(s["occupations"])

    def test_resume_sans_loyer(self):
        item = load_fixture("api_47.json")[4]
        item["occupationModes"] = []
        self.assertEqual(crous_bot.summarize(item, 47)["price"], "prix NC")


class TestMultiOutils(BotTestCase):
    def test_premier_passage_initialise_silencieusement(self):
        cfg = make_cfg()
        fake = self.run_check(cfg, {47: load_fixture("api_47.json"),
                                    44: load_fixture("api_44.json")})
        fake.assert_not_called()                      # aucun email au 1er passage
        state = json.loads(crous_bot.STATE.read_text(encoding="utf-8"))
        self.assertEqual(sorted(state["initialized_tools"]), [44, 47])
        self.assertIn("47:1079", state["items"])      # clé namespaced
        self.assertIn("44:90001", state["items"])

    def test_initialisation_outil_neuf_sans_email(self):
        # état v1 : outil 47 seul déjà initialisé
        items47 = load_fixture("api_47.json")
        current47 = {f"47:{crous_bot.summarize(it, 47)['id']}": crous_bot.summarize(it, 47)
                     for it in zone_items(items47)}
        self.write_state(current47, initialized=(47,))
        cfg = make_cfg(tools=(47, 44))                # le 44 arrive pour la 1re fois
        fake = self.run_check(cfg, {47: items47, 44: load_fixture("api_44.json")})
        fake.assert_not_called()                      # init 44 = silencieux
        state = json.loads(crous_bot.STATE.read_text(encoding="utf-8"))
        self.assertIn("44:90001", state["items"])
        self.assertEqual(sorted(state["initialized_tools"]), [44, 47])

    def test_outil_vide_marque_initialise(self):
        """Un outil vide à son ajout doit être marqué initialisé dans l'état :
        ses futurs logements seront de VRAIES nouveautés -> email."""
        items47 = load_fixture("api_47.json")
        current47 = {f"47:{crous_bot.summarize(it, 47)['id']}": crous_bot.summarize(it, 47)
                     for it in zone_items(items47)}
        self.write_state(current47, initialized=(47,))
        cfg = make_cfg(tools=(47, 44))
        # 1) l'outil 44 est surveillé mais vide (0 logement dans la zone)
        fake = self.run_check(cfg, {47: items47, 44: []})
        fake.assert_not_called()
        state = json.loads(crous_bot.STATE.read_text(encoding="utf-8"))
        self.assertIn(44, state["initialized_tools"])
        # 2) des logements apparaissent ensuite dans le 44 -> email
        fake = self.run_check(cfg, {47: items47, 44: load_fixture("api_44.json")})
        fake.assert_called_once()      # vraie nouveauté, pas une init

    def test_nouveau_logement_outil_44(self):
        items47, items44 = load_fixture("api_47.json"), load_fixture("api_44.json")
        current = {f"47:{crous_bot.summarize(it, 47)['id']}": crous_bot.summarize(it, 47)
                   for it in zone_items(items47)}
        # état : les items de base de l'outil 44 sont déjà vus (outil initialisé)
        current.update({f"44:{crous_bot.summarize(it, 44)['id']}": crous_bot.summarize(it, 44)
                        for it in zone_items(items44)})
        self.write_state(current, initialized=(44, 47))
        # un NOUVEAU logement apparaît dans l'outil 44
        items44 = copy.deepcopy(items44)
        extra = copy.deepcopy(items44[0])
        extra["id"] = 95000
        extra["residence"]["label"] = "RESIDENCE NOUVELLE 44"
        items44.append(extra)
        fake = self.run_check(make_cfg(), {47: items47, 44: items44})
        fake.assert_called_once()                     # email envoyé
        subject, text, html = fake.call_args[0][1], fake.call_args[0][2], fake.call_args[0][3]
        self.assertIn("RESIDENCE NOUVELLE 44", subject + text + html)
        self.assertIn("/tools/44/accommodations/95000", text + html)
        # historique : 1 ligne, outil 44
        lines = [json.loads(l) for l in
                 crous_bot.HISTORY.read_text(encoding="utf-8").splitlines() if l]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["tool"], 44)
        self.assertEqual(lines[0]["id"], "95000")
        self.assertFalse(lines[0]["reappeared"])

    def test_migration_cles_nues_vers_47(self):
        # état v1 (clés non préfixées) -> doit être lisible et migré
        crous_bot.STATE.write_text(json.dumps(
            {"updated": "2026-10-01T00:00:00", "items": {"1079": {"residence": "X"}}},
            ensure_ascii=False), encoding="utf-8")
        state = crous_bot.load_state()
        self.assertIn("47:1079", state["items"])
        self.assertNotIn("1079", state["items"])
        self.assertEqual(state["initialized_tools"], [47])   # défaut legacy

    def test_etat_illisible_reinitialise(self):
        crous_bot.STATE.write_text("pas du json", encoding="utf-8")
        self.assertIsNone(crous_bot.load_state())


class TestEmailV2(BotTestCase):
    def test_email_contient_lien_prix_adresse(self):
        item = load_fixture("api_47.json")[4]
        item["occupationModes"] = [{"type": "alone", "rent": {"min": 45000, "max": 45000}}]
        s = crous_bot.summarize(item, 47)
        subject, text, html = crous_bot.build_notification(
            {f"47:{s['id']}": s}, zone_count=1)
        # exigence utilisateur : TOUJOURS lien + prix + adresse
        self.assertIn("RESIDENCE TEST PARIS 1", subject)
        self.assertIn("450,00", subject)             # prix visible sans ouvrir
        for content in (text, html):
            self.assertIn(s["link"], content)         # lien direct
            self.assertIn("75010 PARIS", content)    # adresse
            self.assertIn("450,00", content)         # prix
        self.assertIn("RÉSERVER", html.upper())      # bouton d'action

    def test_sujet_multiple(self):
        a = crous_bot.summarize(load_fixture("api_47.json")[4], 47)
        b = crous_bot.summarize(load_fixture("api_47.json")[5], 47)
        subject, _, _ = crous_bot.build_notification(
            {"47:1079": a, "47:6": b}, zone_count=2)
        self.assertIn("2 logements", subject)


class TestHistorique(BotTestCase):
    def test_reapparition_flag(self):
        items47 = load_fixture("api_47.json")
        current = {f"47:{crous_bot.summarize(it, 47)['id']}": crous_bot.summarize(it, 47)
                   for it in zone_items(items47)}
        # 1er passage : détection -> historique
        self.write_state({}, initialized=(44, 47))
        self.run_check(make_cfg(tools=(47,)), {47: items47})
        # le logement disparaît (réservé) puis revient
        empty = {k: v for k, v in current.items() if k != "47:1079"}
        self.write_state(empty, initialized=(44, 47))
        fake = self.run_check(make_cfg(tools=(47,)), {47: items47})
        fake.assert_called_once()
        lines = [json.loads(l) for l in
                 crous_bot.HISTORY.read_text(encoding="utf-8").splitlines() if l]
        reap = [l for l in lines if l["id"] == "1079"]
        self.assertEqual(len(reap), 2)
        self.assertFalse(reap[0]["reappeared"])
        self.assertTrue(reap[1]["reappeared"])       # 2e apparition = réapparu


class TestWatchdog(BotTestCase):
    @staticmethod
    def run_at(minutes_ago, conclusion, event="schedule"):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
        return {"event": event, "conclusion": conclusion,
                "updated_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ")}

    def test_fenetres_alerte(self):
        now = datetime.now(timezone.utc)
        t = crous_bot.WATCHDOG_THRESHOLD_MIN
        w = crous_bot.WATCHDOG_WINDOW_MAX
        # dernière réussite récente -> silence
        self.assertIsNone(crous_bot.watchdog_decision(
            [self.run_at(5, "success"), self.run_at(15, "failure")], now))
        # dernière réussite au-delà du seuil -> ALERTE (panne en cours, 1 seul mail)
        self.assertIsNotNone(crous_bot.watchdog_decision(
            [self.run_at(t + 20, "failure"), self.run_at(t + 40, "success")], now))
        # panne ancienne (> fenêtre max) -> silence (déjà alerté)
        self.assertIsNone(crous_bot.watchdog_decision(
            [self.run_at(w + 200, "failure"), self.run_at(w + 220, "success")], now))
        # aucun run de vérification -> ALERTE (crons GitHub et externe morts ?)
        self.assertIsNotNone(crous_bot.watchdog_decision([], now))
        # runs mais aucun succès -> ALERTE
        self.assertIsNotNone(crous_bot.watchdog_decision(
            [self.run_at(10, "failure")], now))
        # un run déclenché à distance (cron externe) EST une preuve de vie
        self.assertIsNone(crous_bot.watchdog_decision(
            [self.run_at(2, "success", event="workflow_dispatch")], now))
        # mélange : succès distant récent + échecs cron anciens -> pas d'alerte
        self.assertIsNone(crous_bot.watchdog_decision(
            [self.run_at(30, "failure"),
             self.run_at(5, "success", event="workflow_dispatch")], now))

    def test_contenu_alerte(self):
        now = datetime.now(timezone.utc)
        age = crous_bot.WATCHDOG_THRESHOLD_MIN + 60
        decision = crous_bot.watchdog_decision(
            [self.run_at(age + 20, "failure"), self.run_at(age, "success")], now)
        subject, text, html = crous_bot.build_watchdog_alert(decision)
        self.assertIn("PANNE", subject)
        self.assertIn(str(age), subject)   # âge depuis la dernière RÉUSSITE (minutes)
        self.assertIn("actions", text)        # lien vers les logs GitHub


class TestLocalWatchdog(BotTestCase):
    """Watchdog VM (migration Oracle) : panne si l'état ne rafraîchit plus."""

    @staticmethod
    def state_at(minutes_ago):
        from datetime import timedelta
        ts = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(
            timespec="seconds")
        return {"updated": ts, "items": {}}

    def test_etat_frais_silence(self):
        self.assertIsNone(crous_bot.local_watchdog_decision(
            self.state_at(5)))

    def test_etat_panne_alerte(self):
        age = crous_bot.WATCHDOG_THRESHOLD_MIN + 30
        decision = crous_bot.local_watchdog_decision(self.state_at(age))
        self.assertIsNotNone(decision)
        self.assertEqual(decision["age_min"], age)
        subject, text, html = crous_bot.build_watchdog_alert(decision)
        self.assertIn("PANNE", subject)

    def test_etat_jamais_ecrit_alerte(self):
        decision = crous_bot.local_watchdog_decision(None)
        self.assertIsNotNone(decision)
        self.assertIsNone(decision["age_min"])

    def test_panne_ancienne_silence_deja_signalee(self):
        age = crous_bot.WATCHDOG_WINDOW_MAX + 200
        self.assertIsNone(crous_bot.local_watchdog_decision(self.state_at(age)))


class TestDigest(BotTestCase):
    def test_stats_et_email(self):
        base = datetime(2026, 10, 3, 12, 0)
        entries = [
            {"ts": "2026-10-01T12:00:00", "tool": 47, "id": "1079",
             "residence": "RESIDENCE TEST PARIS 1", "reappeared": False},
            {"ts": "2026-10-02T12:00:00", "tool": 47, "id": "6",
             "residence": "RESIDENCE TEST PARIS 2", "reappeared": False},
            {"ts": "2026-10-03T12:00:00", "tool": 44, "id": "90001",
             "residence": "RESIDENCE TEST PARIS 3", "reappeared": True},
        ]
        stats = crous_bot.digest_stats(entries, now=base)
        self.assertEqual(stats["detections"], 3)
        self.assertEqual(stats["reappeared"], 1)
        self.assertEqual(stats["first_day"], "01/10")
        self.assertEqual(stats["last_day"], "03/10")
        subject, text, html = crous_bot.build_digest_email(stats, available=2)
        self.assertIn("3", subject)
        self.assertIn("2", text)            # disponibles actuels
        self.assertIn("01/10", text)

    def test_digest_vide(self):
        stats = crous_bot.digest_stats([], now=datetime(2026, 10, 3, 12, 0))
        subject, text, html = crous_bot.build_digest_email(stats, available=0)
        self.assertIn("0", subject)


# ---------------------------------------------------------------------------
# Lokaviz (v3) — chambres/studios/T1bis <= 750 € autour du Lycée Rabelais
# ---------------------------------------------------------------------------

def load_html(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestLokaviz(BotTestCase):
    def test_puissance_de_travail_anubis(self):
        for difficulty in (1, 2):
            digest, nonce = crous_bot.solve_anubis("lokaviz-test", difficulty)
            self.assertEqual(
                digest, hashlib.sha256(f"lokaviz-test{nonce}".encode()).hexdigest())
            self.assertTrue(digest.startswith("0" * difficulty))

    def test_page_vide(self):
        self.assertEqual(crous_bot.parse_lokaviz_listings(
            load_html("lokaviz_empty.html")), [])

    def test_parse_annonce_filtree(self):
        items = crous_bot.parse_lokaviz_listings(load_html("lokaviz_match.html"))
        self.assertEqual(len(items), 1)
        s = crous_bot.summarize_lokaviz(items[0])
        self.assertEqual(s["id"], "161553")
        self.assertEqual(s["tool"], "lokaviz")
        self.assertEqual(s["rent_min"], 69000)      # centimes (cohérent CROUS)
        self.assertIn("Chambre", s["residence"])
        self.assertIn("690", s["price"])
        self.assertIn("75018", s["address"])       # Paris 18e
        self.assertTrue(s["link"].startswith("https://www.lokaviz.fr/"))
        self.assertIn("logement_id:161553", s["link"])

    def test_url_filtres(self):
        url = crous_bot.lokaviz_search_url()
        self.assertIn("loyer=750", url)
        self.assertIn("refloglogement_id", url)
        self.assertIn("etab_id=118", url)          # autour du Lycée Rabelais
        self.assertIn("nbkm=6", url)

    def test_fetch_pagination(self):
        pages = {"1": load_html("lokaviz_match.html"), "2": load_html("lokaviz_page2.html")}

        class FakeSession:
            calls = []

            def get(self, path):
                FakeSession.calls.append(path)
                m = re.search(r"/page:(\d+)", path)
                return pages[m.group(1) if m else "1"]

        items = crous_bot.fetch_lokaviz(make_cfg(), session=FakeSession())
        self.assertEqual(sorted(i["id"] for i in items), ["161553", "161554"])
        self.assertEqual(len(FakeSession.calls), 2)   # s'arrête : pas de page 3
        self.assertIn("page:2", FakeSession.calls[1])
        self.assertIn("loyer=750", FakeSession.calls[0])

    def test_etat_outils_mixtes(self):
        self.write_state({}, initialized=(44, 47, "lokaviz"))
        st = crous_bot.load_state()
        self.assertIn("lokaviz", st["initialized_tools"])
        self.assertIn(47, st["initialized_tools"])
        # ré-save sans crash (tri d'une liste int + str mélangés)
        crous_bot.save_state(st["items"], st["initialized_tools"])
        self.assertIn("lokaviz", crous_bot.load_state()["initialized_tools"])

    def test_init_silencieuse_puis_nouveaute(self):
        cfg = make_cfg()
        # items BRUTS parsés (check() les résume lui-même)
        lok = crous_bot.parse_lokaviz_listings(load_html("lokaviz_match.html"))
        self.write_state({}, initialized=(44, 47))

        def run(zone_by_tool):
            with mock.patch.object(crous_bot, "send_email") as fake:
                crous_bot.check(cfg, zone_by_tool)
                return fake

        # 1er passage lokaviz : initialisation silencieuse, aucun email
        fake = run({47: [], "lokaviz": lok})
        fake.assert_not_called()
        state = json.loads(crous_bot.STATE.read_text(encoding="utf-8"))
        self.assertIn("lokaviz", state["initialized_tools"])
        self.assertIn("lokaviz:161553", state["items"])

        # 2e passage : l'annonce disparaît -> retirée de l'état
        run({47: [], "lokaviz": []})
        state = json.loads(crous_bot.STATE.read_text(encoding="utf-8"))
        self.assertNotIn("lokaviz:161553", state["items"])

        # 3e passage : elle revient -> email, sujet Lokaviz, historique
        fake = run({47: [], "lokaviz": lok})
        fake.assert_called_once()
        subject, text, html = fake.call_args[0][1], fake.call_args[0][2], fake.call_args[0][3]
        self.assertIn("Lokaviz", subject)
        self.assertIn("690", text + html)
        self.assertIn("logement_id:161553", text + html)
        lines = [json.loads(l) for l in
                 crous_bot.HISTORY.read_text(encoding="utf-8").splitlines() if l]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["tool"], "lokaviz")


if __name__ == "__main__":
    unittest.main()
