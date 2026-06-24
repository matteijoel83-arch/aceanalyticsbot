"""
╔══════════════════════════════════════════════════════════════════════╗
║          BOT FOOTBALL ACEANALYTICS — bot_football.py v1.0           ║
║  Architecture hybride : Gemini (recherche) + Claude (analyse)        ║
║  Pré-collecte : Odds API + API-Football → calendrier complet         ║
║                                                                      ║
║  Secrets GitHub requis :                                             ║
║    ANTHROPIC_API_KEY  · TELEGRAM_BOT_TOKEN · TELEGRAM_CHANNEL_ID    ║
║    GITHUB_TOKEN       · GITHUB_REPO · GEMINI_API_KEY                ║
║  Secrets optionnels :                                                ║
║    ODDS_API_KEY   (https://the-odds-api.com — 500 req/mois gratuit) ║
║    RAPIDAPI_KEY   (https://rapidapi.com — API-Football)              ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os, sys, json, hashlib, logging, re, time, base64, requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from logging.handlers import RotatingFileHandler
import anthropic
from google import genai
from google.genai import types

# =====================================================================
# 1. CONFIGURATION & LOGGING
# =====================================================================

DRY_RUN = "--dry-run" in sys.argv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler("bot_football.log", maxBytes=5 * 1024 * 1024, backupCount=3),
        logging.StreamHandler(),
    ],
)

ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
GITHUB_TOKEN        = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO         = os.environ.get("GITHUB_REPO")
ODDS_API_KEY        = os.environ.get("ODDS_API_KEY")
RAPIDAPI_KEY        = os.environ.get("RAPIDAPI_KEY")
FOOTBALL_API_KEY    = os.environ.get("FOOTBALL_API_KEY")  # football-data.org — gratuit

MISSING = [k for k, v in {
    "ANTHROPIC_API_KEY":   ANTHROPIC_API_KEY,
    "GEMINI_API_KEY":      GEMINI_API_KEY,
    "TELEGRAM_BOT_TOKEN":  TELEGRAM_BOT_TOKEN,
    "TELEGRAM_CHANNEL_ID": TELEGRAM_CHANNEL_ID,
    "GITHUB_TOKEN":        GITHUB_TOKEN,
    "GITHUB_REPO":         GITHUB_REPO,
}.items() if not v]

if MISSING:
    logging.critical(f"Secrets manquants : {', '.join(MISSING)}")
    sys.exit(1)

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

CLAUDE_SONNET  = "claude-sonnet-4-6"
CLAUDE_OPUS    = "claude-opus-4-6"
CLAUDE_MODEL   = CLAUDE_SONNET
GEMINI_MODEL   = "gemini-3.5-flash"
SEUIL_OPUS     = 3
GITHUB_API     = "https://api.github.com"
GITHUB_HEADERS = {
    "Authorization":        f"Bearer {GITHUB_TOKEN}",
    "Accept":               "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

STATS_VERSION = 1
STATS_DEFAUT  = {
    "version":   STATS_VERSION,
    "victoires": 0,
    "defaites":  0,
    "par_marche": {
        "1n2":         {"v": 0, "d": 0},
        "score_exact": {"v": 0, "d": 0},
        "over_under":  {"v": 0, "d": 0},
        "handicap":    {"v": 0, "d": 0},
        "btts":        {"v": 0, "d": 0},
        "combine":     {"v": 0, "d": 0},
        "autre":       {"v": 0, "d": 0},
    },
    "par_niveau": {
        "elevee":  {"v": 0, "d": 0},
        "moderee": {"v": 0, "d": 0},
        "basse":   {"v": 0, "d": 0},
    }
}

TICKET_SEP  = "[SEPARATEUR]"
MAX_TICKETS = 5

# =====================================================================
# 2. COUCHE GITHUB
# =====================================================================

def _gh_get(path):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    try:
        r = requests.get(url, headers=GITHUB_HEADERS, timeout=10)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        d = r.json()
        return json.loads(base64.b64decode(d["content"].replace("\n", "")).decode()), d["sha"]
    except Exception as e:
        logging.error(f"GitHub GET '{path}' : {e}")
        return None, None


def _gh_put(path, contenu, message, sha=None, retries=2):
    url     = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(
            json.dumps(contenu, ensure_ascii=False, indent=2).encode()
        ).decode(),
    }
    if sha:
        payload["sha"] = sha
    for t in range(1, retries + 1):
        try:
            r = requests.put(url, headers=GITHUB_HEADERS, json=payload, timeout=15)
            if r.status_code == 409 and t < retries:
                _, sha_frais = _gh_get(path)
                if sha_frais:
                    payload["sha"] = sha_frais
                time.sleep(1)
                continue
            r.raise_for_status()
            logging.info(f"GitHub '{path}' OK — {message}")
            return True
        except Exception as e:
            logging.error(f"GitHub PUT '{path}' tentative {t} : {e}")
            if t < retries:
                time.sleep(2)
    return False


def _gh_delete(path, message, sha):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    try:
        r = requests.delete(url, headers=GITHUB_HEADERS,
                            json={"message": message, "sha": sha}, timeout=10)
        r.raise_for_status()
        logging.info(f"GitHub '{path}' supprimé.")
        return True
    except Exception as e:
        logging.error(f"GitHub DELETE '{path}' : {e}")
        return False

# =====================================================================
# 3. STATISTIQUES FOOTBALL (fichiers séparés du tennis)
# =====================================================================

def _migrer_stats(s):
    if s.get("version", 0) < 1:
        s["version"] = STATS_VERSION
        if "par_marche" not in s:
            s["par_marche"] = dict(STATS_DEFAUT["par_marche"])
        if "par_niveau" not in s:
            s["par_niveau"] = dict(STATS_DEFAUT["par_niveau"])
    return s


def charger_stats():
    s, _ = _gh_get("stats_football.json")
    if not isinstance(s, dict) or "victoires" not in s:
        return dict(STATS_DEFAUT)
    return _migrer_stats(s)


def calculer_winrate(s):
    total = s["victoires"] + s["defaites"]
    return (s["victoires"] / total * 100) if total > 0 else 0.0


def _detecter_marche(ticket_texte):
    t = ticket_texte.lower()
    if "combiné" in t or "combine" in t:
        return "combine"
    if "btts" in t or "les deux" in t:
        return "btts"
    if "handicap" in t:
        return "handicap"
    if "over" in t or "under" in t or "buts" in t:
        return "over_under"
    if "score" in t or "exact" in t:
        return "score_exact"
    if "1n2" in t or "victoire" in t or "nul" in t or "gagne" in t:
        return "1n2"
    return "autre"


def _detecter_niveau(ticket_texte):
    t = ticket_texte.lower()
    if "élevée" in t or "elevee" in t:
        return "elevee"
    if "modérée" in t or "moderee" in t:
        return "moderee"
    if "basse" in t:
        return "basse"
    return "autre"

# =====================================================================
# 4. DÉDUPLICATION PAR HASH SHA-256
# =====================================================================

def _hash_ticket(ticket):
    return hashlib.sha256(
        re.sub(r"\s+", " ", ticket.strip().lower())[:300].encode()
    ).hexdigest()


def charger_historique():
    h, sha = _gh_get("historique_football.json")
    return (h if isinstance(h, list) else []), sha


def sauvegarder_historique(hashes, sha):
    _gh_put("historique_football.json", hashes[-20:], "📚 Maj historique football", sha=sha)

# =====================================================================
# 5. TELEGRAM
# =====================================================================

def _nettoyer_html_telegram(texte):
    texte = texte.replace('\\"', '"')
    texte = re.sub(r'\\(?![nrt"\'\\])', '', texte)
    texte = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', texte, flags=re.DOTALL)
    texte = re.sub(r'<(?!/?(b|i|u|s|code|pre|a)(\s[^>]*)?>)[^>]+>', '', texte)
    for tag in ['b', 'i', 'u', 's', 'code', 'pre']:
        ouvertes = texte.count(f'<{tag}>')
        fermees  = texte.count(f'</{tag}>')
        if fermees > ouvertes:
            for _ in range(fermees - ouvertes):
                texte = texte[::-1].replace(f'>{tag}/<'[::-1], '', 1)[::-1]
        elif ouvertes > fermees:
            texte += f'</{tag}>' * (ouvertes - fermees)
    return texte


def _tronquer(texte, limite=3500):
    if len(texte) <= limite:
        return texte
    coupe = texte.rfind("\n", 0, limite)
    return texte[:coupe if coupe != -1 else limite] + "\n\n… [Analyse tronquée]"


def envoyer_sur_telegram(message, stats=None, retries=3):
    if stats is None:
        stats = charger_stats()
    message = _nettoyer_html_telegram(message)
    sig = (
        f"\n\n📊 <b>BILAN FOOTBALL ACEANALYTICS</b>\n"
        f"✅ V: {stats['victoires']} | ❌ D: {stats['defaites']}\n"
        f"📈 <b>Win Rate : {calculer_winrate(stats):.1f}%</b>"
    )
    html = message + sig
    if len(html) > 4000:
        html = _tronquer(re.sub(r"<[^>]+>", "", message), 3500) + sig
        parse_mode = None
    else:
        parse_mode = "HTML"
    if DRY_RUN:
        logging.info(f"[DRY-RUN] Telegram ({len(html)} chars)")
        return True
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHANNEL_ID, "text": html}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    for t in range(1, retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
                continue
            r.raise_for_status()
            logging.info("✅ Telegram envoyé.")
            return True
        except requests.exceptions.Timeout:
            logging.warning(f"Telegram timeout {t}.")
        except requests.exceptions.HTTPError as e:
            logging.error(f"Telegram HTTP {e} — {r.text}")
            if r.status_code == 400 and parse_mode == "HTML":
                try:
                    r2 = requests.post(url, json={
                        "chat_id": TELEGRAM_CHANNEL_ID,
                        "text": re.sub(r"<[^>]+>", "", html)[:4000]
                    }, timeout=10)
                    r2.raise_for_status()
                    logging.info("✅ Telegram envoyé (texte brut).")
                    return True
                except Exception as e2:
                    logging.error(f"Telegram fallback échoué : {e2}")
            break
        except Exception as e:
            logging.error(f"Telegram erreur : {e}")
        if t < retries:
            time.sleep(2 ** t)
    logging.error("❌ Telegram : échec définitif.")
    return False


def _envoyer_notification_sans_ticket(raison, session=""):
    stats  = charger_stats()
    emoji  = "⚽🌆" if session == "APRÈS-MIDI" else "⚽🌃"
    label  = f"Session {session}" if session else "Analyse"
    msg    = (
        f"{emoji} <b>⚽ ACEANALYTICS FOOTBALL — {label}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔍 <b>Résultat :</b> Aucune value détectée\n\n"
        f"{raison}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Bilan : ✅ {stats['victoires']}V | ❌ {stats['defaites']}D | "
        f"📈 Win Rate : {calculer_winrate(stats):.1f}%"
    )
    if DRY_RUN:
        logging.info(f"[DRY-RUN] Notification sans ticket — {label}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHANNEL_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        logging.info(f"Notification sans ticket envoyée — {label}.")
    except Exception as e:
        logging.warning(f"Échec notification : {e}")


def _alerter_telegram_erreur(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHANNEL_ID, "text": f"⚠️ ERREUR BOT FOOTBALL\n{msg}"},
            timeout=5,
        )
    except Exception:
        pass

# =====================================================================
# 6. PARIS EN COURS
# =====================================================================

def sauvegarder_pari_pour_suivi(pari_info):
    if "pari" not in pari_info or "date" not in pari_info:
        logging.error(f"Structure invalide : {pari_info}")
        return
    if DRY_RUN:
        return
    paris, sha = _gh_get("pari_en_cours_football.json")
    if not isinstance(paris, list):
        paris = []
    paris.append(pari_info)
    _gh_put("pari_en_cours_football.json", paris, "📌 Ajout pari football", sha=sha)

# =====================================================================
# 6B. MODULE SPORTAPI7 — DONNÉES COMPLÉMENTAIRES FOOTBALL
# =====================================================================

SPORTAPI7_HOST = "sportapi7.p.rapidapi.com"
SPORTAPI7_BASE = "https://sportapi7.p.rapidapi.com/api/v1"

def _sportapi7_get(endpoint, params=None, retries=2):
    """Requête SportAPI7 avec retry rapide sur 429 (délais courts)."""
    if not RAPIDAPI_KEY:
        return None
    for tentative in range(1, retries + 1):
        try:
            r = requests.get(
                f"{SPORTAPI7_BASE}/{endpoint}",
                headers={
                    "x-rapidapi-key":  RAPIDAPI_KEY,
                    "x-rapidapi-host": SPORTAPI7_HOST,
                },
                params=params,
                timeout=8,
            )
            if r.status_code == 429:
                if tentative < retries:
                    time.sleep(3)
                    continue
                return None
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            if "429" in str(e):
                if tentative < retries:
                    time.sleep(3)
                    continue
                return None
            return None
        except Exception:
            return None
    return None


def enrichir_sportapi7_football(api_matchs: list) -> list:
    """
    Enrichit les matchs football avec SportAPI7.
    Endpoints utilisés :
    - /sport/football/scheduled-events/{date} → matchs du jour
    - /event/{id}/odds/1/all                  → cotes
    - /event/{id}/lineups                     → compositions
    - /event/{id}/statistics                  → possession, tirs, passes
    - /unique-tournament/{id}/season/{id}/standings/total → classement
    Utilise la même clé RAPIDAPI_KEY.
    """
    if not RAPIDAPI_KEY or not api_matchs:
        return api_matchs

    date_today = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d")

    try:
        # Récupérer tous les matchs football du jour
        data = _sportapi7_get(f"sport/football/scheduled-events/{date_today}")
        if not data:
            logging.info("SportAPI7 Football — pas de données disponibles.")
            return api_matchs

        events   = data.get("events", [])
        enrichis = 0
        logging.info(f"SportAPI7 Football — {len(events)} événement(s) disponible(s).")

        # Index par noms d'équipes
        sportapi7_index = {}
        for ev in events:
            home = ev.get("homeTeam", {}).get("name", "").lower()
            away = ev.get("awayTeam", {}).get("name", "").lower()
            if home and away:
                sportapi7_index[f"{home}|{away}"] = ev

        for m in api_matchs:
            eq1 = m.get("equipe1", "").lower()
            eq2 = m.get("equipe2", "").lower()
            ev  = sportapi7_index.get(f"{eq1}|{eq2}") or sportapi7_index.get(f"{eq2}|{eq1}")

            # Recherche partielle (4 premiers caractères)
            if not ev:
                for k, v in sportapi7_index.items():
                    k1, k2 = k.split("|")
                    if (eq1[:4] in k1 or k1[:4] in eq1) and \
                       (eq2[:4] in k2 or k2[:4] in eq2):
                        ev = v
                        break

            if ev:
                event_id      = ev.get("id")
                tournament_id = ev.get("tournament", {}).get("uniqueTournament", {}).get("id")
                season_id     = ev.get("season", {}).get("id")

                # --- Cotes ---
                if event_id:
                    try:
                        odds_data = _sportapi7_get(f"event/{event_id}/odds/1/all")
                        if odds_data:
                            for market in odds_data.get("markets", []):
                                if "winner" in market.get("marketName", "").lower():
                                    for c in market.get("choices", []):
                                        nom = c.get("name", "").lower()
                                        val = c.get("fractionalValue") or c.get("initialFractionalValue")
                                        if nom in ["1", "home"]:
                                            m["sportapi7_cote_1"] = val
                                        elif nom in ["x", "draw"]:
                                            m["sportapi7_cote_nul"] = val
                                        elif nom in ["2", "away"]:
                                            m["sportapi7_cote_2"] = val
                        time.sleep(0.5)
                    except Exception:
                        pass  # 404/429 → ignorer silencieusement

                # --- Lineups (compositions) ---
                if event_id:
                    try:
                        lineup_data = _sportapi7_get(f"event/{event_id}/lineups")
                        if lineup_data:
                            confirmed = lineup_data.get("confirmed", False)
                            m["lineups_confirmes"] = confirmed
                            if confirmed:
                                home_lineup = lineup_data.get("home", {})
                                away_lineup = lineup_data.get("away", {})
                                m["formation_eq1"] = home_lineup.get("formation", "non disponible")
                                m["formation_eq2"] = away_lineup.get("formation", "non disponible")
                        time.sleep(0.5)
                    except Exception:
                        pass

                # --- Classement pour contexte enjeux ---
                if tournament_id and season_id:
                    try:
                        standings_data = _sportapi7_get(
                            f"unique-tournament/{tournament_id}/season/{season_id}/standings/total"
                        )
                        if standings_data:
                            standings = standings_data.get("standings", [])
                            if standings:
                                rows = standings[0].get("rows", [])
                                for row in rows:
                                    team_name = row.get("team", {}).get("name", "").lower()
                                    pos       = row.get("position", "?")
                                    pts       = row.get("points", "?")
                                    if eq1 in team_name or team_name in eq1:
                                        m["classement_eq1"] = f"#{pos} ({pts} pts)"
                                    elif eq2 in team_name or team_name in eq2:
                                        m["classement_eq2"] = f"#{pos} ({pts} pts)"
                        time.sleep(0.5)
                    except Exception:
                        pass

                m["sportapi7_id"] = event_id
                enrichis += 1

        logging.info(f"SportAPI7 Football — {enrichis} match(s) enrichi(s).")

    except Exception as e:
        logging.warning(f"SportAPI7 Football erreur : {e}")

    return api_matchs

# =====================================================================
# 6C. MODULE ODDSPAPI — COTES COMPLÉMENTAIRES FOOTBALL
# =====================================================================

ODDSPAPI_HOST = "odds-api1.p.rapidapi.com"
ODDSPAPI_BASE = "https://odds-api1.p.rapidapi.com"

def _oddspapi_get(endpoint, params=None, retries=2):
    """Requête OddsPapi avec la clé RapidAPI existante."""
    if not RAPIDAPI_KEY:
        return None
    for tentative in range(1, retries + 1):
        try:
            r = requests.get(
                f"{ODDSPAPI_BASE}/{endpoint}",
                headers={
                    "x-rapidapi-key":  RAPIDAPI_KEY,
                    "x-rapidapi-host": ODDSPAPI_HOST,
                },
                params=params,
                timeout=8,
            )
            if r.status_code == 429:
                time.sleep(tentative * 5)
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logging.warning(f"OddsPapi {endpoint} : {e}")
            return None
    return None


def enrichir_oddspapi_football(api_matchs, odds_matchs):
    """
    Complète les cotes Winamax manquantes via OddsPapi (300+ bookmakers).
    Endpoint /fixtures/today (sportId soccer = 1) puis cotes 1/N/2.
    Ne remplace jamais une cote existante.
    """
    if not RAPIDAPI_KEY or not api_matchs:
        return odds_matchs

    try:
        # sportId football/soccer = 10 sur OddsPapi
        data = _oddspapi_get("fixtures/today", {
            "sportId":    10,
            "bookmakers": "winamax",
        })
        if not data:
            logging.info("OddsPapi Football — pas de données disponibles.")
            return odds_matchs

        fixtures = data if isinstance(data, list) else data.get("fixtures", [])
        ajouts   = 0

        for fx in fixtures:
            participants = fx.get("participants", {})
            eq1 = participants.get("participant1Name", "")
            eq2 = participants.get("participant2Name", "")
            if not eq1 or not eq2:
                continue

            cle = f"{eq1}|{eq2}"
            if cle in odds_matchs:
                continue

            bm_meta = fx.get("bookmakers", {}).get("winamax", {})
            if not bm_meta.get("hasOdds", False):
                continue

            fixture_id = fx.get("fixtureId")
            if not fixture_id:
                continue

            odds_data = _oddspapi_get("fixtures/odds/main", {
                "fixtureIds": fixture_id,
                "bookmakers": "winamax",
            })
            time.sleep(0.3)
            if not odds_data:
                continue

            odds_list = odds_data if isinstance(odds_data, list) else [odds_data]
            for od in odds_list:
                wina = od.get("odds", {}).get("winamax", {})
                c1 = cnul = c2 = None
                marches_alt = {}  # Marchés alternatifs avec vraies cotes

                for mkt_id, mkt in wina.items():
                    if not isinstance(mkt, dict):
                        continue
                    handicap = mkt.get("handicap", 0)
                    outcomes = mkt.get("outcomes", {})

                    # Marché 101 = Full Time Result (1X2)
                    if str(mkt_id) == "101":
                        for out_id, out in outcomes.items():
                            players = out.get("players", {}) if isinstance(out, dict) else {}
                            price = None
                            for p_id, p_data in players.items():
                                price = p_data.get("price")
                                break
                            if str(out_id) == "101" and price:
                                c1 = price
                            elif str(out_id) == "102" and price:
                                cnul = price
                            elif str(out_id) == "103" and price:
                                c2 = price

                    # Marché 104 = BTTS (Both Teams To Score)
                    elif str(mkt_id) == "104":
                        for out_id, out in outcomes.items():
                            players = out.get("players", {}) if isinstance(out, dict) else {}
                            price = None
                            for p_id, p_data in players.items():
                                price = p_data.get("price")
                                break
                            if str(out_id) == "104" and price:
                                marches_alt["btts_oui"] = price
                            elif str(out_id) == "105" and price:
                                marches_alt["btts_non"] = price

                    # Marché 106 = Over/Under (handicap = ligne, ex 2.5)
                    elif str(mkt_id) == "106":
                        for out_id, out in outcomes.items():
                            players = out.get("players", {}) if isinstance(out, dict) else {}
                            price = None
                            for p_id, p_data in players.items():
                                price = p_data.get("price")
                                break
                            if str(out_id) == "106" and price:
                                marches_alt[f"over_{handicap}"] = price
                            elif str(out_id) == "107" and price:
                                marches_alt[f"under_{handicap}"] = price

                if c1 and c2:
                    start = fx.get("startTime", 0)
                    heure_str = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if start else ""
                    comp = fx.get("tournament", {}).get("tournamentName", "Football")
                    entry = {
                        "equipe1": eq1, "equipe2": eq2,
                        "heure_utc": heure_str,
                        "competition": comp,
                        "cote_1": c1, "cote_nul": cnul, "cote_2": c2,
                        "source_cote": "Winamax (OddsPapi)",
                    }
                    if marches_alt:
                        entry["marches_alternatifs"] = marches_alt
                    odds_matchs[cle] = entry
                    ajouts += 1
                    break

        logging.info(f"OddsPapi Football — {ajouts} cote(s) Winamax ajoutée(s).")

    except Exception as e:
        logging.warning(f"OddsPapi Football erreur : {e}")

    return odds_matchs

# =====================================================================
# 7. MODULE A — PRÉ-COLLECTE ODDS API FOOTBALL
# =====================================================================

def precollecte_odds_api(heure_utc_min):
    matchs = {}
    if not ODDS_API_KEY:
        logging.info("ODDS_API_KEY absente.")
        return matchs

    # Limite : matchs du jour uniquement (max 24h après maintenant)
    maintenant_utc = datetime.now(timezone.utc)
    heure_utc_max  = (maintenant_utc + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
    # Ligues couvertes par Odds API
    sports = [
        "soccer_france_ligue_one",
        "soccer_england_league1",
        "soccer_spain_la_liga",
        "soccer_germany_bundesliga",
        "soccer_italy_serie_a",
        "soccer_uefa_champs_league",
        "soccer_uefa_europa_league",
        "soccer_fifa_world_cup",          # Coupe du Monde 2026 ⭐
        "soccer_conmebol_copa_america",   # Copa América
        "soccer_uefa_european_championship",  # Euro
    ]
    for sport in sports:
        try:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport}/odds",
                params={
                    "apiKey":     ODDS_API_KEY,
                    "regions":    "eu",
                    "markets":    "h2h",
                    "oddsFormat": "decimal",
                    "dateFormat": "iso",
                },
                timeout=10,
            )
            if r.status_code == 404:
                continue
            r.raise_for_status()
            data  = r.json()
            quota = r.headers.get("x-requests-remaining", "?")
            for m in data:
                commence = m.get("commence_time", "")
                if commence < heure_utc_min:
                    continue
                # Exclure matchs au-delà de 24h (matchs futurs semaines/mois)
                if commence > heure_utc_max:
                    continue
                eq1 = m.get("home_team", "?")
                eq2 = m.get("away_team", "?")
                cle = f"{eq1}|{eq2}"
                c1 = c2 = cnul = None
                src = "non trouvée"
                for bk in m.get("bookmakers", []):
                    is_w = "winamax" in bk.get("key", "").lower()
                    if is_w or not c1:
                        for mkt in bk.get("markets", []):
                            if mkt.get("key") == "h2h":
                                out  = {o["name"]: o["price"] for o in mkt.get("outcomes", [])}
                                c1   = out.get(eq1)
                                c2   = out.get(eq2)
                                cnul = out.get("Draw")
                                src  = "Winamax" if is_w else bk.get("title", "EU")
                        if is_w:
                            break
                matchs[cle] = {
                    "equipe1":    eq1,
                    "equipe2":    eq2,
                    "heure_utc":  commence[:16].replace("T", " ") + " UTC",
                    "competition": sport.replace("soccer_", "").replace("_", " ").title(),
                    "cote_1":     c1,
                    "cote_nul":   cnul,
                    "cote_2":     c2,
                    "source_cote": src,
                }
            logging.info(f"Odds API {sport} — {len([m for m in matchs.values()])} match(s). Quota : {quota}")
        except Exception as e:
            logging.warning(f"Odds API {sport} : {e}")
    logging.info(f"Odds API Football — {len(matchs)} match(s) total.")
    return matchs

# =====================================================================
# 8. MODULE B — PRÉ-COLLECTE FOOTBALL-DATA.ORG v4
# =====================================================================

def precollecte_api_football(date_fr):
    """
    Récupère le calendrier football via football-data.org v4.
    Endpoint : GET /v4/matches?date={date}
    Plan gratuit : 10 req/min · 12 compétitions incluses
    Header : X-Auth-Token
    """
    matchs = []
    if not FOOTBALL_API_KEY:
        logging.info("FOOTBALL_API_KEY absente — pré-collecte football-data.org ignorée.")
        return matchs

    date_api = datetime.strptime(date_fr, "%d/%m/%Y").strftime("%Y-%m-%d")
    headers  = {"X-Auth-Token": FOOTBALL_API_KEY}

    # Compétitions gratuites disponibles (EL retiré — accès 403 plan gratuit)
    # WC = FIFA World Cup 2026 (en cours !)
    competitions = ["PL", "PD", "BL1", "SA", "FL1", "CL", "WC", "EC"]

    for comp in competitions:
        try:
            r = requests.get(
                f"https://api.football-data.org/v4/competitions/{comp}/matches",
                headers=headers,
                params={"dateFrom": date_api, "dateTo": date_api},
                timeout=10,
            )
            if r.status_code == 404:
                continue
            r.raise_for_status()
            data = r.json()

            comp_name = data.get("competition", {}).get("name", comp)

            for m in data.get("matches", []):
                statut = m.get("status", "")
                # Exclure matchs terminés ou en cours
                if statut in ["FINISHED", "IN_PLAY", "PAUSED", "CANCELLED", "POSTPONED"]:
                    continue

                home = m.get("homeTeam", {}).get("name", "")
                away = m.get("awayTeam", {}).get("name", "")
                if not home or not away:
                    continue

                # Heure UTC
                utc_date = m.get("utcDate", "")
                if "T" in utc_date:
                    utc_date = utc_date[:16].replace("T", " ") + " UTC"

                matchs.append({
                    "equipe1":     home.strip(),
                    "equipe2":     away.strip(),
                    "match_id":    m.get("id"),
                    "heure":       utc_date,
                    "competition": comp_name,
                    "journee":     m.get("matchday", ""),
                    "stade":       m.get("venue", ""),
                })

            # Pause pour respecter la limite 10 req/min
            time.sleep(6)

        except requests.exceptions.HTTPError as e:
            logging.warning(f"football-data.org {comp} : {e}")
        except Exception as e:
            logging.warning(f"football-data.org {comp} erreur : {e}")

    logging.info(f"football-data.org — {len(matchs)} match(s) récupéré(s).")
    return matchs

# =====================================================================
# 9. FUSION DES DEUX SOURCES
# =====================================================================

def _normaliser_equipe(nom):
    """Normalise un nom d'équipe pour détecter les doublons.
    'Bosnia & Herzegovina' == 'Bosnia-Herzegovina' == 'Bosnia and Herzegovina'."""
    n = nom.lower().strip()
    # Uniformiser les séparateurs
    n = n.replace(" & ", " ").replace("&", " ")
    n = n.replace(" and ", " ").replace("-", " ").replace("_", " ")
    # Retirer les espaces multiples
    n = " ".join(n.split())
    # Retirer les mots courants non distinctifs
    for mot in ["republic", "rep", "fc", "national", "team"]:
        n = n.replace(f" {mot}", "").replace(f"{mot} ", "")
    return n.strip()


def fusionner_calendrier(odds_matchs, api_matchs):
    lignes = ["📋 MATCHS FOOTBALL DISPONIBLES SUR WINAMAX :\n"]

    odds_index = {}
    for cle, m in odds_matchs.items():
        odds_index[m["equipe1"].lower()] = m
        odds_index[m["equipe2"].lower()] = m

    matchs_winamax        = []
    matchs_sans_cote      = []
    affiches              = set()
    noms_normalises       = set()  # Détection doublons par noms normalisés

    for m in api_matchs:
        eq1, eq2 = m["equipe1"], m["equipe2"]
        cle = f"{eq1}|{eq2}"
        if cle in affiches:
            continue

        # Déduplication par noms normalisés (Bosnia & Herzegovina == Bosnia-Herzegovina)
        cle_norm = f"{_normaliser_equipe(eq1)}|{_normaliser_equipe(eq2)}"
        if cle_norm in noms_normalises:
            logging.info(f"Doublon football ignoré : {eq1} vs {eq2}")
            continue

        affiches.add(cle)
        noms_normalises.add(cle_norm)

        cote_trouvee = None
        for nom, data in odds_index.items():
            if eq1.lower() in nom or nom in eq1.lower() or eq2.lower() in nom or nom in eq2.lower():
                if data.get("cote_1") and data.get("cote_2"):
                    cote_trouvee = data
                    break

        if cote_trouvee:
            m["cote_1"]      = cote_trouvee["cote_1"]
            m["cote_nul"]    = cote_trouvee.get("cote_nul")
            m["cote_2"]      = cote_trouvee["cote_2"]
            m["source_cote"] = cote_trouvee["source_cote"]
            matchs_winamax.append(m)
            cotes_str = (
                f"{eq1} {cote_trouvee['cote_1']:.2f} / "
                f"Nul {cote_trouvee.get('cote_nul', '?')} / "
                f"{eq2} {cote_trouvee['cote_2']:.2f}"
            )
            ligne = (
                f"• {m['heure']} | {eq1} vs {eq2} | {m['competition']}"
                f" | Cotes {cote_trouvee['source_cote']}: {cotes_str}"
            )
            # Enrichissements SportAPI7
            extras = []
            if m.get("classement_eq1"):
                extras.append(f"{eq1} {m['classement_eq1']}")
            if m.get("classement_eq2"):
                extras.append(f"{eq2} {m['classement_eq2']}")
            if m.get("formation_eq1") and m.get("lineups_confirmes"):
                extras.append(f"Formation {eq1}: {m['formation_eq1']} | {eq2}: {m.get('formation_eq2','?')}")
            if m.get("sportapi7_cote_1"):
                extras.append(f"Cotes SportAPI7: {m['sportapi7_cote_1']}/{m.get('sportapi7_cote_nul','?')}/{m.get('sportapi7_cote_2','?')}")
            # Marchés alternatifs avec VRAIES cotes Winamax (OddsPapi)
            marches_alt = cote_trouvee.get("marches_alternatifs", {})
            if marches_alt:
                m["marches_alternatifs"] = marches_alt
                alt_parts = []
                if "btts_oui" in marches_alt:
                    alt_parts.append(f"BTTS Oui {marches_alt['btts_oui']}/Non {marches_alt.get('btts_non','?')}")
                for k, v in sorted(marches_alt.items()):
                    if k.startswith("over_"):
                        ligne_ou = k.replace("over_", "")
                        under_key = f"under_{ligne_ou}"
                        alt_parts.append(f"O/U {ligne_ou}: Over {v}/Under {marches_alt.get(under_key,'?')}")
                if alt_parts:
                    extras.append("Marchés Winamax réels → " + " | ".join(alt_parts))
            if extras:
                ligne += " | " + " | ".join(extras)
            lignes.append(ligne)
        else:
            matchs_sans_cote.append(m)

    # Matchs Odds API non trouvés dans API-Football
    for cle, m in odds_matchs.items():
        eq1, eq2 = m["equipe1"], m["equipe2"]
        if f"{eq1}|{eq2}" not in affiches:
            affiches.add(f"{eq1}|{eq2}")
            if m.get("cote_1") and m.get("cote_2"):
                cotes_str = (
                    f"{eq1} {m['cote_1']:.2f} / "
                    f"Nul {m.get('cote_nul', '?')} / "
                    f"{eq2} {m['cote_2']:.2f}"
                )
                lignes.append(
                    f"• {m['heure_utc']} | {eq1} vs {eq2} | {m['competition']}"
                    f" | Cotes {m['source_cote']}: {cotes_str}"
                )
                matchs_winamax.append({
                    "equipe1": eq1, "equipe2": eq2,
                    "heure": m["heure_utc"],
                    "competition": m["competition"],
                    "cote_1": m["cote_1"], "cote_nul": m.get("cote_nul"),
                    "cote_2": m["cote_2"], "source_cote": m["source_cote"],
                })

    if matchs_sans_cote:
        lignes.append(f"\n📋 MATCHS À VÉRIFIER SUR SPORTYTRADER ({len(matchs_sans_cote)}) :")
        for m in matchs_sans_cote[:15]:
            lignes.append(f"• {m['heure']} | {m['equipe1']} vs {m['equipe2']} | {m['competition']}")

    total = len(matchs_winamax)
    logging.info(f"Calendrier fusionné : {total} match(s) Winamax + {len(matchs_sans_cote)} à vérifier.")

    if total == 0 and not matchs_sans_cote:
        return ""

    lignes.append(f"\nTotal Winamax confirmé : {total} match(s).")
    return "\n".join(lignes)

# =====================================================================
# 10. COLLECTE GEMINI (stats + contexte football)
# =====================================================================

def collecter_donnees_football(date, heure, calendrier_injecte, heure_fin="23:59"):
    bloc = (
        f"{calendrier_injecte}\n\n"
        f"→ Calendrier COMPLET. Ne pas le re-vérifier.\n"
        f"→ IMPORTANT : Transmettre TOUS les matchs avec cotes — ne pas filtrer par heure.\n"
        f"→ Claude filtrera par fenêtre horaire ({heure} → {heure_fin}).\n"
        f"→ Tes requêtes : UNIQUEMENT stats, contexte, blessures."
        if calendrier_injecte else
        f"Cherche les matchs de football du {date} entre {heure} et {heure_fin} sur flashscore.fr."
    )

    prompt = f"""
Tu es un agent de collecte données football. Date : {date}. Heure : {heure} France.

MISSION : Enrichir les données des matchs avec stats, forme et contexte.
Tu NE cherches PAS le calendrier — il est fourni ci-dessous.
Tes 15 requêtes Google : UNIQUEMENT stats, forme, blessures, contexte.

{bloc}

RECHERCHES (max 15 requêtes) :

PRIORITÉ 1 — Stats et forme (les plus importantes) :
1. Forme récente équipe domicile (5 derniers matchs) :
   → flashscore.fr OU sofascore.com OU fbref.com
   → Résultats, buts marqués/encaissés, domicile/extérieur
2. Forme récente équipe extérieure (5 derniers matchs) :
   → Même sources
3. xG et xGA (expected goals) :
   → understat.com OU fbref.com
   → xG moyen sur 5 matchs, xGA moyen
4. Stats défensives/offensives :
   → whoscored.com OU fbref.com
   → Buts/match, clean sheets, BTTS%

PRIORITÉ 2 — H2H et contexte :
5. H2H (5 dernières confrontations) :
   → flashscore.fr OU sofascore.com
   → Résultats, buts, domicile/extérieur
6. Absences et blessures :
   → transfermarkt.fr OU premierinjuries.com OU flashscore.fr
   → Joueurs absents, suspensions, retours
7. Contexte calendrier :
   → Match Europa/Ligue des Champions 3 jours avant/après ?
   → Classement actuel et enjeux (relégation/titre/Europa)
   → flashscore.fr OU atptour.com

PRIORITÉ 3 — Cotes matchs secondaires :
8. Vérification cotes sur sportytrader.com/fr/cotes/football/
   → Pour matchs SANS cote dans le calendrier
   → Cote Winamax trouvée → source_cote = "Sportytrader"
   → Introuvable → source_cote = "non trouvée"

SOURCES PAR TYPE :
• Forme récente + scores   → flashscore.fr · sofascore.com · matchstat.com
• xG / xGA                 → understat.com · fbref.com
• Stats avancées           → whoscored.com · fbref.com
• Blessures/absences       → transfermarkt.fr · premierinjuries.com
• Classements/contexte     → flashscore.fr · fifa.com (pour CdM)
• Cotes                    → sportytrader.com

⚠️ COUPE DU MONDE 2026 EN COURS — contexte spécial :
   → Pression maximale sur chaque match (élimination directe en phases finales)
   → Fatigue accumulée si équipe a joué 3+ matchs en 10 jours
   → Absences sur cartons jaunes/rouges particulièrement importantes
   → Public neutre sauf matchs sur sol américain (avantage USA/Canada/Mexique)

⚠️ Inclure TOUS les matchs du calendrier dans le JSON — Claude filtrera par heure.
⚠️ Un match sans cote vérifiée = source_cote "non trouvée".

FORMAT JSON STRICT :
{{
  "heure_collecte": "{heure}",
  "matchs": [{{
    "heure_match": "HH:MM",
    "equipe1": "Nom équipe domicile",
    "equipe2": "Nom équipe extérieure",
    "competition": "Ligue 1 / Premier League / etc.",
    "stade": "Nom du stade ou non disponible",
    "cote_1": 1.XX,
    "cote_nul": 3.XX,
    "cote_2": 2.XX,
    "source_cote": "Winamax/Sportytrader/non trouvée",
    "forme_eq1": ["V", "D", "N", "V", "V"],
    "forme_eq2": ["D", "V", "N", "D", "V"],
    "details_forme_eq1": "résumé 5 matchs avec buts",
    "details_forme_eq2": "résumé 5 matchs avec buts",
    "xg_eq1": "X.X ou non trouvé",
    "xga_eq1": "X.X ou non trouvé",
    "xg_eq2": "X.X ou non trouvé",
    "xga_eq2": "X.X ou non trouvé",
    "buts_marques_eq1": "X.X/match ou non trouvé",
    "buts_encaisses_eq1": "X.X/match ou non trouvé",
    "buts_marques_eq2": "X.X/match ou non trouvé",
    "buts_encaisses_eq2": "X.X/match ou non trouvé",
    "clean_sheets_eq1": "X sur 5 ou non trouvé",
    "clean_sheets_eq2": "X sur 5 ou non trouvé",
    "btts_pct": "XX% ou non trouvé",
    "h2h_recents": "résumé 5 dernières confrontations",
    "absences_eq1": "joueurs absents ou Aucune",
    "absences_eq2": "joueurs absents ou Aucune",
    "contexte_eq1": "Europa J-3, relégable, etc.",
    "contexte_eq2": "Europa J-3, relégable, etc.",
    "enjeux": "titre / relégation / Europa / sans enjeu"
  }}],
  "avertissements": "données incertaines"
}}

Champ introuvable → "non trouvé". JSON valide, sans backticks.
"""

    try:
        logging.info(f"Gemini Football enrichit — {date} {heure}…")
        derniere_erreur = None
        for tentative in range(1, 4):
            try:
                # Essayer avec max_remote_calls=15 — fallback sans si non supporté
                try:
                    rep = gemini_client.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            tools=[types.Tool(google_search=types.GoogleSearch())],
                            temperature=0.1,
                            max_remote_calls=15,
                        ),
                    )
                    logging.info("Gemini Football — mode 15 requêtes activé ✅")
                except Exception as e_max:
                    if "extra inputs" in str(e_max) or "max_remote_calls" in str(e_max):
                        logging.warning("max_remote_calls non supporté — fallback 10 requêtes.")
                        rep = gemini_client.models.generate_content(
                            model=GEMINI_MODEL,
                            contents=prompt,
                            config=types.GenerateContentConfig(
                                tools=[types.Tool(google_search=types.GoogleSearch())],
                                temperature=0.1,
                            ),
                        )
                    else:
                        raise
                break
            except Exception as e:
                derniere_erreur = e
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    logging.warning(f"Gemini 503 tentative {tentative}/3 — retry {tentative*15}s…")
                    time.sleep(tentative * 15)
                else:
                    raise
        if derniere_erreur and "503" in str(derniere_erreur):
            logging.error(f"Gemini 503 après 3 tentatives.")
            return '{"matchs": [], "avertissements": "Gemini indisponible."}'

        texte = rep.text.strip()
        texte = re.sub(r"^```json\s*", "", texte)
        texte = re.sub(r"\s*```$", "", texte)
        data  = json.loads(texte)
        logging.info(f"Gemini OK — {len(data.get('matchs', []))} match(s) football.")
        return json.dumps(data, ensure_ascii=False, indent=2)
    except json.JSONDecodeError as e:
        logging.error(f"Gemini JSON invalide : {e}")
        return '{"matchs": [], "avertissements": "Erreur Gemini JSON."}'
    except Exception as e:
        logging.error(f"Erreur Gemini : {e}")
        return '{"matchs": [], "avertissements": "Erreur Gemini."}'

# =====================================================================
# 11. PROMPT CLAUDE FOOTBALL
# =====================================================================

def construire_prompt_claude(date, heure, donnees_json, heure_fin="23:59", fenetre_nocturne=False):
    session = "MATIN" if heure < "14:00" else "APRÈS-MIDI" if heure < "19:00" else "SOIR"
    try:
        avertissements = json.loads(donnees_json).get("avertissements", "Aucun")
    except Exception:
        avertissements = "Non disponibles"

    return f"""Tu es un expert en paris football. Date : {date} · {heure} France · Session {session}.

DONNÉES COLLECTÉES (source unique — ne pas chercher sur internet) :
{donnees_json}

⚠️ AVERTISSEMENTS : {avertissements}
→ Données manquantes importantes → abandonner le match.
Tu n'as PAS accès à internet. Analyse uniquement les données fournies.

FILTRES IMMÉDIATS :
• Match commencé avant {heure} → skip
• Match commençant après {heure_fin} → skip (hors fenêtre session)
• Cote "non trouvée" → skip automatique
• Équipe en Europa/CL dans 3 jours → rotation probable → BASSE systématique
• Derby local → variance élevée → BASSE systématique
• Équipe déjà qualifiée/reléguée → motivation douteuse → skip si enjeux nuls
• Absences clés (buteur principal, gardien titulaire) → BASSE

⛔ INTERDICTION ABSOLUE — COTES INVENTÉES :
• Tu n'as le DROIT d'utiliser QUE les cotes EXACTES fournies (cote_1, cote_nul, cote_2).
• INTERDIT d'estimer, deviner ou inventer une cote.
• Si une cote n'est PAS fournie pour un marché → tu NE PEUX PAS jouer ce marché.
• Si tu écris "cote estimée" ou "non disponible précisément" → VIOLATION. Ne génère PAS ce ticket.
• Marchés Over/Under, BTTS, Handicap : UNIQUEMENT si leur cote exacte est dans les données.
  Sinon → reste sur le 1N2 avec sa cote réelle, ou passe au match suivant.

✅ MARCHÉS ALTERNATIFS AUTORISÉS (si cote réelle fournie) :
• Quand un match affiche "Marchés Winamax réels → BTTS / O/U", ces cotes sont EXACTES et JOUABLES.
• Tu PEUX jouer BTTS Oui/Non ou Over/Under SI leur cote est explicitement listée.
• Exemple : "O/U 2.5: Over 1.85/Under 1.95" → tu peux jouer Over 2.5 à 1.85 (cote réelle).
• Ces marchés offrent souvent plus de value que le 1N2 — exploite-les quand la cote réelle est là.

⚠️ FENÊTRE HORAIRE SPÉCIALE — ÉVÉNEMENTS INTERNATIONAUX :
• Les matchs de Coupe du Monde / Copa América aux USA peuvent se jouer
  tard le soir ou après minuit heure française (jusqu'à {heure_fin}).
• Un match à 01:00, 02:00, 03:00 du matin fait partie de la SESSION SOIR en cours
  s'il se joue dans la nuit suivant le {date}.
• NE PAS skip un match nocturne international sous prétexte qu'il est "le lendemain" —
  il appartient à la session du soir actuelle si avant {heure_fin}.
• Pour les matchs en journée normale : skip si prévu un autre jour que {date}.

FILTRES COUPE DU MONDE (si applicable) :
• Phase de groupes → 3ème match + équipe déjà qualifiée → skip (rotation massive)
• Suspensions sur cartons jaunes → impact majeur → vérifier obligatoirement
• Équipe jouant son 3ème match en 8 jours → fatigue → BASSE systématique
• Favoris mondiaux (France/Brésil/Angleterre) sur petite équipe → cotes trop basses → delta souvent négatif

CALIBRATION PROBABILITÉS (football plus aléatoire que tennis) :
• Favori clair  (cote < 1.50)  → MAX 70%
• Favori modéré (1.50-1.80)    → MAX 62%
• Match serré   (1.80-2.20)    → MAX 55%
• Outsider      (> 2.20)       → MAX 48%
• Nul           (cote 3.00-4.00) → MAX 35%

⚠️ RÈGLE ABSOLUE — FORMAT DE SORTIE :
Ta réponse commence DIRECTEMENT par 🔴 ou AUCUN_MATCH. Rien avant.
L'analyse est INTERNE — ne jamais l'afficher.

ANALYSE EN 2 ÉTAPES (INTERNE) :

[1] FACTEURS BRUTS :
  • Forme récente 5 matchs (V/N/D + buts)
  • xG et xGA moyens — indicateur de performance réelle
  • Buts marqués/encaissés par match
  • Clean sheets récents
  • BTTS% historique
  • H2H 5 dernières confrontations (domicile/extérieur)
  • Absences clés (attaquant principal, gardien)
  • Contexte : Europa/CL dans 3j, relégation, titre, sans enjeu
  • Avantage domicile (important en football)

[2] DÉCISION :
  Probabilité % (respecter plafonds)
  Cote Juste = 1 / probabilité
  Delta = Cote réelle - Cote Juste
  Delta < 0.10 → ❌ abandonné
  Delta ≥ 0.10 → VALUE ✅
  Kelly quart = ((p×c−1)/(c−1)) × 0.25
  Zéro value → AUCUN_MATCH

DOUBLE VALIDATION :
  1. Delta ≥ 0.10 ✅
  2. Analyse [1] confirme le marché ✅
  Si l'une manque → abandonné.

NIVEAUX DE CONFIANCE ET MISES :

ÉLEVÉE (3%) :
  · Delta ≥ 0.10 + données complètes + analyse solide
  · Pas d'absences majeures + pas de contexte défavorable

MODÉRÉE (2%) :
  · Delta ≥ 0.10 + quelques lacunes acceptables
  · Logique analytique convaincante

BASSE (1%) :
  · Données insuffisantes OU cote > 2.50
  · OU derby OU rotation probable OU absences clés

Plafonds absolus :
  · Derby / rotation probable → MAX 1%
  · Absences clés → MAX 1%
  · Combiné → MAX 2%

MARCHÉS DISPONIBLES :

CONFIANCE ÉLEVÉE :
  • 1N2 (si supériorité claire + données solides)
  • Over 2.5 buts (si xG élevés des deux équipes)
  • Under 1.5 buts (si défenses solides + xGA bas)
  • BTTS Oui (si BTTS% > 65% et formes offensives)
  • Handicap asiatique (-1 / -1.5)
  • Combiné max 2 (compétitions différentes) — mise 1%

CONFIANCE MODÉRÉE — 1N2 INTERDIT si cote < 1.40 :
  • Over/Under buts (1.5 / 2.5 / 3.5)
  • BTTS Oui/Non
  • Victoire + Over 1.5 buts
  • Mi-temps / Match (1N2 mi-temps)
  • Combiné MODÉRÉE → INTERDIT

MISES :
Simple ÉLEVÉE 3% · Modérée 2% · Basse 1% · Combiné MAX 2%

FORMAT (max {MAX_TICKETS} tickets, [SEPARATEUR] entre chaque) :
N'envoyer QUE les tickets validés (Delta ≥ 0.10 + analyse confirmée).
Tickets abandonnés → NE PAS inclure.
Si 0 ticket → AUCUN_MATCH + explication courte (max 80 mots, citer matchs analysés).
HTML uniquement <b>texte</b>. POURQUOI max 60 mots.

⚽ <b>PRONOSTIC FOOTBALL [SIMPLE/COMBINÉ]</b> ⚽
━━━━━━━━━━━━━━━━━━━━
🏟 <b>MATCH :</b> [Eq1 vs Eq2]
🏆 <b>COMPÉTITION :</b> [Ligue]
⏰ <b>HEURE :</b> [Heure]
━━━━━━━━━━━━━━━━━━━━
✅ <b>PRONO :</b> [Pronostic]
📈 <b>COTE :</b> [Cote]
💰 <b>MISE :</b> [% Kelly]
🛡 <b>CONFIANCE :</b> [ÉLEVÉE/MODÉRÉE/BASSE]
━━━━━━━━━━━━━━━━━━━━
🧮 <b>VALUE :</b> [X% → juste Y.YY → réelle Z.ZZ → delta +D.DD ✅ → Kelly W%]
📌 <b>POURQUOI ?</b> [Max 60 mots]
⚠️ <b>DONNÉES MANQUANTES :</b> [Stats absentes ou Aucune]
"""

def filtrer_matchs_par_fenetre(api_matchs, heure_debut, heure_fin):
    """
    Pré-filtre les matchs football par fenêtre horaire AVANT Gemini.
    Gère le passage minuit (session soir CdM jusqu'à 06:00).
    """
    if not api_matchs:
        return api_matchs

    fenetre_nocturne = heure_fin < heure_debut

    def _heure_match(m):
        h = m.get("heure", "")
        match = re.search(r"(\d{2}):(\d{2})", str(h))
        return match.group(0) if match else None

    matchs_filtres = []
    for m in api_matchs:
        hm = _heure_match(m)
        if not hm:
            matchs_filtres.append(m)
            continue

        if fenetre_nocturne:
            if hm >= heure_debut or hm <= heure_fin:
                matchs_filtres.append(m)
        else:
            if heure_debut <= hm <= heure_fin:
                matchs_filtres.append(m)

    logging.info(f"Filtrage horaire : {len(matchs_filtres)}/{len(api_matchs)} matchs dans la fenêtre {heure_debut}→{heure_fin}.")
    return matchs_filtres


# =====================================================================
# 12. ORCHESTRATION
# =====================================================================

def run_bot_autonome():
    debut      = time.time()
    maintenant = datetime.now(ZoneInfo("Europe/Paris"))
    date       = maintenant.strftime("%d/%m/%Y")
    heure      = maintenant.strftime("%H:%M")

    if DRY_RUN:
        logging.info("MODE DRY-RUN")

    if heure < "19:00":
        session   = "APRÈS-MIDI"
        heure_fin = "19:30"
        fenetre_nocturne = False
    else:
        session   = "SOIR"
        heure_fin = "06:00"  # CdM USA → matchs jusqu'à 6h00 heure France
        fenetre_nocturne = True  # La fenêtre passe minuit
    logging.info(f"⚽ Session FOOTBALL {session} — fenêtre {heure} → {heure_fin}"
                 f"{' (passe minuit — matchs nocturnes CdM inclus)' if fenetre_nocturne else ''}")

    heure_utc_min = (maintenant.astimezone(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")

    odds_matchs        = precollecte_odds_api(heure_utc_min)
    api_matchs         = precollecte_api_football(date)
    # Pré-filtrage horaire AVANT enrichissement SportAPI7 — économise les requêtes
    api_matchs         = filtrer_matchs_par_fenetre(api_matchs, heure, heure_fin)
    odds_matchs        = enrichir_oddspapi_football(api_matchs, odds_matchs)  # Cotes complémentaires
    api_matchs         = enrichir_sportapi7_football(api_matchs)  # Source complémentaire
    calendrier_injecte = fusionner_calendrier(odds_matchs, api_matchs)
    donnees_json       = collecter_donnees_football(date, heure, calendrier_injecte, heure_fin)

    try:
        donnees = json.loads(donnees_json)
        if not donnees.get("matchs"):
            _envoyer_notification_sans_ticket(
                f"Aucun match disponible sur Winamax pour cette session.\n"
                f"Le bot reprendra à la prochaine session.",
                session=session
            )
            return
    except Exception:
        pass

    prompt = construire_prompt_claude(date, heure, donnees_json, heure_fin, fenetre_nocturne)

    # Debug matchs
    try:
        matchs_debug = json.loads(donnees_json).get("matchs", [])
        logging.info(f"DEBUG — {len(matchs_debug)} match(s) transmis à Claude :")
        for i, m in enumerate(matchs_debug, 1):
            logging.info(
                f"  [{i}] {m.get('equipe1','?')} vs {m.get('equipe2','?')} "
                f"| {m.get('heure_match','?')} | {m.get('competition','?')} "
                f"| Cotes {m.get('cote_1','?')} / {m.get('cote_nul','?')} / {m.get('cote_2','?')}"
            )
    except Exception as e:
        logging.warning(f"DEBUG : {e}")

    # Bascule Sonnet/Opus
    nb_matchs     = len(json.loads(donnees_json).get("matchs", []))
    modele_choisi = CLAUDE_OPUS if nb_matchs >= SEUIL_OPUS else CLAUDE_SONNET
    logging.info(f"Modèle Claude : {'Opus 🔥' if modele_choisi == CLAUDE_OPUS else 'Sonnet ⚡'} ({nb_matchs} matchs)")

    try:
        logging.info(f"Claude Football analyse — {date} {heure}")
        rep = claude_client.messages.create(
            model=modele_choisi, max_tokens=4096, system=prompt,
            messages=[{"role": "user", "content":
                f"Analyse et propose les meilleurs paris football (max {MAX_TICKETS}) — {date} {heure}.\n"
                f"RAPPEL CRITIQUE : Ta réponse commence OBLIGATOIREMENT par ⚽ ou AUCUN_MATCH. "
                f"Zéro texte avant. Premier caractère = ⚽ ou A. Sinon c'est un échec."}],
        )
        texte = "\n".join(b.text for b in rep.content if hasattr(b, "text") and b.text).strip()
        logging.info(f"Claude OK ({len(texte)} chars) — {rep.usage.input_tokens} in / {rep.usage.output_tokens} out")
        logging.info(f"DEBUG Claude : {texte[:300]}")

        # Filtre sécurité — extrait la partie valide si Claude ajoute du texte parasite
        if not texte.startswith("⚽") and not texte.startswith("🔴") and not texte.startswith("AUCUN_MATCH"):
            for marqueur in ["⚽", "🔴", "AUCUN_MATCH"]:
                idx = texte.find(marqueur)
                if idx != -1:
                    logging.warning(f"Claude Football — texte parasite nettoyé ({idx} chars).")
                    texte = texte[idx:]
                    break

        # Vérifier AUCUN_MATCH uniquement si le texte commence par AUCUN_MATCH
        if texte.startswith("AUCUN_MATCH"):
            explication = ""
            idx = texte.find("AUCUN_MATCH")
            if idx != -1:
                suite = texte[idx + len("AUCUN_MATCH"):].strip()
                explication = re.sub(r'<[^>]+>', '', suite)[:200].strip()
                if explication:
                    explication = f"\n\n💬 <i>{explication}</i>"
            _envoyer_notification_sans_ticket(
                f"{nb_matchs} match(s) analysé(s) — aucune value détectée.{explication}\n\n"
                f"On passe notre chemin. 💼",
                session=session
            )
            return

        if len(texte) <= 20:
            return

        tickets = [t.strip() for t in texte.split(TICKET_SEP) if len(t.strip()) > 20][:MAX_TICKETS]

        # Filtrer les tickets abandonnés en analysant le DELTA réel dans la ligne VALUE
        def _ticket_valide(t):
            t_lower = t.lower()
            if any(sig in t_lower for sig in ["abandon de ce ticket", "ticket abandonné",
                                               "kelly 0%", "mise : 0%", "mise 0%"]):
                return False
            # Rejeter les cotes inventées/estimées
            if any(sig in t_lower for sig in ["cote estimée", "estimée", "non disponible précisément",
                                               "cote approximative", "estimation de cote",
                                               "cote non disponible"]):
                logging.info("Ticket rejeté — cote estimée/inventée détectée.")
                return False
            m = re.search(r"delta\s*([+-]?\d+[.,]\d+)", t_lower)
            if m:
                delta = float(m.group(1).replace(",", "."))
                if delta < 0.10:
                    return False
            return True

        tickets_valides = []
        for t in tickets:
            if not (t.startswith("⚽") or t.startswith("🔴")):
                continue
            if not _ticket_valide(t):
                logging.info("Ticket rejeté (delta < 0.10 ou abandon) — non envoyé.")
                continue
            tickets_valides.append(t)
        tickets = tickets_valides

        if not tickets:
            _envoyer_notification_sans_ticket(
                f"Matchs analysés — aucune value retenue après validation.\n"
                f"On passe notre chemin. 💼",
                session=session
            )
            return

        historique, hist_sha = charger_historique()
        hashes_connus   = set(historique)
        nouveaux_hashes = []
        paris_envoyes   = 0
        stats_cached    = charger_stats()

        for i, ticket in enumerate(tickets, 1):
            h = _hash_ticket(ticket)
            if h in hashes_connus:
                logging.warning(f"Ticket {i} : doublon.")
                continue
            if envoyer_sur_telegram(ticket, stats=stats_cached):
                # Nettoyer le ticket — garder uniquement la partie ⚽ ou 🔴
                ticket_propre = ticket
                for marqueur in ["⚽", "🔴"]:
                    idx = ticket_propre.find(marqueur)
                    if idx != -1 and not ticket_propre.startswith(marqueur):
                        ticket_propre = ticket_propre[idx:]
                        break
                sauvegarder_pari_pour_suivi({
                    "pari":   ticket_propre,
                    "date":   date,
                    "marche": _detecter_marche(ticket_propre),
                    "niveau": _detecter_niveau(ticket_propre),
                })
                hashes_connus.add(h)
                nouveaux_hashes.append(h)
                paris_envoyes += 1
                if i < len(tickets):
                    time.sleep(1)

        if nouveaux_hashes and not DRY_RUN:
            sauvegarder_historique(list(hashes_connus), hist_sha)
        logging.info(f"✅ {paris_envoyes} ticket(s) football envoyé(s).")

    except Exception as e:
        logging.error(f"Erreur critique : {e}", exc_info=True)
        _alerter_telegram_erreur(f"bot_football.py a planté : {e}")
    finally:
        logging.info(f"Terminé en {time.time() - debut:.1f}s.")

# =====================================================================
# 13. POINT D'ENTRÉE CLI
# =====================================================================

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if not args:
        run_bot_autonome()
    elif args[0] in ("--help", "-h"):
        print(__doc__)
    else:
        print("❌ Commande inconnue.")
        sys.exit(1)
