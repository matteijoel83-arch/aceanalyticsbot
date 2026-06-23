"""
╔══════════════════════════════════════════════════════════════════════╗
║          BOT TENNIS ACEANALYTICS — bot.py v7.1                      ║
║  Architecture hybride : Gemini (recherche) + Claude (analyse)        ║
║  Pré-collecte : Odds API + RapidAPI Tennis → calendrier complet      ║
║                                                                      ║
║  Secrets GitHub requis :                                             ║
║    ANTHROPIC_API_KEY  · TELEGRAM_BOT_TOKEN · TELEGRAM_CHANNEL_ID    ║
║    GITHUB_TOKEN       · GITHUB_REPO · GEMINI_API_KEY                ║
║  Secrets optionnels :                                                ║
║    ODDS_API_KEY   (https://the-odds-api.com — 500 req/mois gratuit) ║
║    RAPIDAPI_KEY   (https://rapidapi.com — calendrier complet)        ║
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
        RotatingFileHandler("bot.log", maxBytes=5 * 1024 * 1024, backupCount=3),
        logging.StreamHandler(),
    ],
)

ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
GITHUB_TOKEN        = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO         = os.environ.get("GITHUB_REPO")
ODDS_API_KEY        = os.environ.get("ODDS_API_KEY")    # Optionnel
RAPIDAPI_KEY        = os.environ.get("RAPIDAPI_KEY")    # Optionnel

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

CLAUDE_SONNET  = "claude-sonnet-4-6"   # Sessions légères < 3 matchs
CLAUDE_OPUS    = "claude-opus-4-6"     # Sessions riches ≥ 3 matchs
CLAUDE_MODEL   = CLAUDE_SONNET         # Défaut — sera remplacé dynamiquement
GEMINI_MODEL   = "gemini-3.5-flash"   # Dernière version stable — meilleur que 2.5 Pro
SEUIL_OPUS     = 3                     # Nb matchs minimum pour basculer sur Opus
GITHUB_API    = "https://api.github.com"
GITHUB_HEADERS = {
    "Authorization":        f"Bearer {GITHUB_TOKEN}",
    "Accept":               "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

STATS_VERSION = 2
STATS_DEFAUT  = {
    "version":   STATS_VERSION,
    "victoires": 0,
    "defaites":  0,
    "par_marche": {
        "moneyline":   {"v": 0, "d": 0},
        "score_exact": {"v": 0, "d": 0},
        "over_under":  {"v": 0, "d": 0},
        "handicap":    {"v": 0, "d": 0},
        "tiebreak":    {"v": 0, "d": 0},
        "combine":     {"v": 0, "d": 0},
        "autre":       {"v": 0, "d": 0},
    },
    "par_niveau": {
        "elevee":  {"v": 0, "d": 0},
        "moderee": {"v": 0, "d": 0},
        "basse":   {"v": 0, "d": 0},
    }
}
TICKET_SEP    = "[SEPARATEUR]"
MAX_TICKETS   = 5

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
# 3. STATISTIQUES
# =====================================================================

def _migrer_stats(s):
    if s.get("version", 0) < 2:
        s["version"] = STATS_VERSION
        # Migration v1 → v2 : ajouter les nouveaux champs
        if "par_marche" not in s:
            s["par_marche"] = dict(STATS_DEFAUT["par_marche"])
        if "par_niveau" not in s:
            s["par_niveau"] = dict(STATS_DEFAUT["par_niveau"])
    return s


def charger_stats():
    s, _ = _gh_get("stats.json")
    if not isinstance(s, dict) or "victoires" not in s:
        return dict(STATS_DEFAUT)
    return _migrer_stats(s)


def calculer_winrate(s):
    total = s["victoires"] + s["defaites"]
    return (s["victoires"] / total * 100) if total > 0 else 0.0


def enregistrer_resultat(victoire, pari_termine=None):
    s, sha = _gh_get("stats.json")
    if not isinstance(s, dict) or "victoires" not in s:
        s = dict(STATS_DEFAUT)
    s["victoires" if victoire else "defaites"] += 1
    s = _migrer_stats(s)
    if DRY_RUN:
        logging.info(f"[DRY-RUN] Stats : {s}")
    else:
        _gh_put("stats.json", s, "🔄 Maj stats", sha=sha)
    if pari_termine and not DRY_RUN:
        paris, psha = _gh_get("pari_en_cours.json")
        if isinstance(paris, list):
            restants = [p for p in paris if p.get("pari") != pari_termine]
            if restants:
                _gh_put("pari_en_cours.json", restants, "🧹 Nettoyage", sha=psha)
            elif psha:
                _gh_delete("pari_en_cours.json", "🗑️ File vide", sha=psha)
    logging.info(f"{'✅ VICTOIRE' if victoire else '❌ DÉFAITE'} — {s['victoires']}V / {s['defaites']}D")

def _detecter_marche(ticket_texte):
    """Détecte le type de marché depuis le texte du ticket."""
    t = ticket_texte.lower()
    if "combiné" in t or "combine" in t:
        return "combine"
    if "tiebreak" in t:
        return "tiebreak"
    if "handicap" in t:
        return "handicap"
    if "over" in t or "under" in t or "jeux" in t:
        return "over_under"
    if "2-0" in t or "2-1" in t or "score" in t:
        return "score_exact"
    if "moneyline" in t or "vainqueur" in t or "gagne" in t or "victoire" in t:
        return "moneyline"
    return "autre"


def _detecter_niveau(ticket_texte):
    """Détecte le niveau de confiance depuis le texte du ticket."""
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
    h, sha = _gh_get("historique.json")
    return (h if isinstance(h, list) else []), sha


def sauvegarder_historique(hashes, sha):
    _gh_put("historique.json", hashes[-20:], "📚 Maj historique", sha=sha)

# =====================================================================
# 5. TELEGRAM
# =====================================================================

def _nettoyer_html_telegram(texte):
    """Nettoie le texte pour compatibilité HTML Telegram."""
    # Backslashes problématiques
    texte = texte.replace('\\"', '"')
    texte = re.sub(r'\\(?![nrt"\'\\])', '', texte)
    # Markdown résiduel → HTML
    texte = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', texte, flags=re.DOTALL)
    # Supprimer balises non supportées par Telegram
    texte = re.sub(r'<(?!/?(b|i|u|s|code|pre|a)(\s[^>]*)?>)[^>]+>', '', texte)
    # Supprimer balises fermantes orphelines </b> </i> sans ouvrante correspondante
    for tag in ['b', 'i', 'u', 's', 'code', 'pre']:
        ouvertes = texte.count(f'<{tag}>')
        fermees  = texte.count(f'</{tag}>')
        if fermees > ouvertes:
            # Supprimer les fermantes en excès (de droite à gauche)
            for _ in range(fermees - ouvertes):
                texte = texte[::-1].replace(f'>{tag}/<'[::-1], '', 1)[::-1]
        elif ouvertes > fermees:
            # Fermer les ouvrantes non fermées
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
        f"\n\n📊 <b>BILAN ACEANALYTICS</b>\n"
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
                logging.warning("Telegram 400 — fallback texte brut.")
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
    _alerter_telegram_erreur("❌ bot.py : échec envoi ticket.")
    return False


def _envoyer_notification_sans_ticket(raison, session=""):
    stats  = charger_stats()
    emoji  = "🌅" if session == "MATIN" else "🌆" if session == "APRÈS-MIDI" else "🌃"
    label  = f"Session {session}" if session else "Analyse"
    msg    = (
        f"{emoji} <b>ACEANALYTICS — {label}</b>\n\n"
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
            json={"chat_id": TELEGRAM_CHANNEL_ID, "text": f"⚠️ ERREUR BOT\n{msg}"},
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
    paris, sha = _gh_get("pari_en_cours.json")
    if not isinstance(paris, list):
        paris = []
    paris.append(pari_info)
    _gh_put("pari_en_cours.json", paris, "📌 Ajout pari", sha=sha)

# =====================================================================
# 7. MODULE A — PRÉ-COLLECTE ODDS API
# =====================================================================

def precollecte_odds_api(heure_utc_min):
    matchs = {}
    if not ODDS_API_KEY:
        logging.info("ODDS_API_KEY absente.")
        return matchs
    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/tennis/odds",
            params={"apiKey": ODDS_API_KEY, "regions": "eu",
                    "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"},
            timeout=10,
        )
        r.raise_for_status()
        data  = r.json()
        quota = r.headers.get("x-requests-remaining", "?")
        for m in data:
            commence = m.get("commence_time", "")
            if commence < heure_utc_min:
                continue
            j1  = m.get("home_team", "?")
            j2  = m.get("away_team", "?")
            cle = f"{j1}|{j2}"
            c1  = c2 = None
            src = "non trouvée"
            for bk in m.get("bookmakers", []):
                is_w = "winamax" in bk.get("key", "").lower()
                if is_w or not c1:
                    for mkt in bk.get("markets", []):
                        if mkt.get("key") == "h2h":
                            out = {o["name"]: o["price"] for o in mkt.get("outcomes", [])}
                            c1  = out.get(j1)
                            c2  = out.get(j2)
                            src = "Winamax" if is_w else bk.get("title", "EU")
                    if is_w:
                        break
            matchs[cle] = {
                "joueur1": j1, "joueur2": j2,
                "heure_utc": commence[:16].replace("T", " ") + " UTC",
                "cote_j1": c1, "cote_j2": c2, "source_cote": src,
            }
        logging.info(f"Odds API — {len(matchs)} match(s). Quota : {quota}")
    except Exception as e:
        logging.warning(f"Odds API : {e}")
    return matchs

# =====================================================================
# 8. MODULE B — PRÉ-COLLECTE RAPIDAPI TENNIS
# =====================================================================

def precollecte_rapidapi_tennis(date_fr):
    """
    Récupère le calendrier tennis via RapidAPI Tennis (matchstat.com).
    Endpoint correct : /tennis/v2/{type}/fixtures/{date}
    Inclut tournoi, surface, round, cotes odd1/odd2.
    Pagine automatiquement pour tout récupérer.
    """
    matchs = []
    if not RAPIDAPI_KEY:
        logging.info("RAPIDAPI_KEY absente — pré-collecte RapidAPI ignorée.")
        return matchs
    date_api = datetime.strptime(date_fr, "%d/%m/%Y").strftime("%Y-%m-%d")
    headers  = {
        "x-rapidapi-key":  RAPIDAPI_KEY,
        "x-rapidapi-host": "tennis-api-atp-wta-itf.p.rapidapi.com",
    }
    for tour in ["atp", "wta"]:
        page = 1
        while True:
            try:
                url = f"https://tennis-api-atp-wta-itf.p.rapidapi.com/tennis/v2/{tour}/fixtures/{date_api}"
                r   = requests.get(url, headers=headers, timeout=10, params={
                    "include":  "tournament,round",
                    "filter":   "PlayerGroup:singles",
                    "pageSize": 50,
                    "pageNo":   page,
                })
                r.raise_for_status()
                data = r.json()

                # Réponse = liste directe ou {data: [...], hasNextPage: bool}
                if isinstance(data, list):
                    items    = data
                    has_next = False
                else:
                    items    = data.get("data", [])
                    has_next = data.get("hasNextPage", False)

                for m in items:
                    j1 = m.get("player1") or {}
                    j2 = m.get("player2") or {}
                    n1 = j1.get("name", "")
                    n2 = j2.get("name", "")
                    if not n1 or not n2:
                        continue

                    # Exclure qualifiés (seed = "Q")
                    if str(m.get("seed1") or "") == "Q" or str(m.get("seed2") or "") == "Q":
                        continue

                    # Heure
                    h = m.get("date") or "heure inconnue"
                    if "T" in str(h):
                        h = h[:16].replace("T", " ") + " UTC"

                    # Tournoi et surface
                    trn     = m.get("tournament") or {}
                    nom_trn = trn.get("name", "Tournoi inconnu")
                    court   = trn.get("court") or {}
                    surface = court.get("name", "non disponible")

                    # Round
                    rnd     = m.get("round") or {}
                    round_n = rnd.get("name", "")

                    matchs.append({
                        "joueur1":    n1.strip(),
                        "joueur2":    n2.strip(),
                        "id1":        j1.get("id"),   # ID RapidAPI — utilisé pour H2H/stats
                        "id2":        j2.get("id"),
                        "tour":       tour,            # atp ou wta
                        "heure":      h,
                        "tournoi":    f"{nom_trn} — {round_n}".strip(" —"),
                        "surface":    surface,
                        "odd1":       m.get("odd1"),
                        "odd2":       m.get("odd2"),
                    })

                if not has_next:
                    break
                page += 1

            except requests.exceptions.HTTPError as e:
                logging.warning(f"RapidAPI {tour.upper()} p{page} : {e}")
                break
            except Exception as e:
                logging.warning(f"RapidAPI {tour.upper()} erreur : {e}")
                break

    logging.info(f"RapidAPI Tennis — {len(matchs)} match(s) singles.")
    return matchs

# =====================================================================
# 9. FUSION DES DEUX SOURCES
# =====================================================================

# =====================================================================
# 9. FUSION DES DEUX SOURCES
# =====================================================================

def fusionner_calendrier(odds_matchs, rapid_matchs):
    """
    Logique de fusion en 3 étapes :
    1. RapidAPI Tennis → calendrier complet (468+ matchs)
    2. Odds API → filtre "disponible sur Winamax" (cotes vérifiées)
    3. Intersection → uniquement les matchs RapidAPI avec cote Winamax confirmée
       + données complètes RapidAPI (H2H, forme, surface)
    """
    lignes = ["📋 MATCHS DISPONIBLES SUR WINAMAX (calendrier RapidAPI filtré) :\n"]

    # Index Odds API par nom de joueur pour la correspondance
    odds_index = {}
    for cle, m in odds_matchs.items():
        odds_index[m["joueur1"].lower()] = m
        odds_index[m["joueur2"].lower()] = m

    matchs_winamax   = []  # Matchs RapidAPI avec cote Winamax confirmée
    matchs_sans_cote = []  # Matchs RapidAPI sans cote Winamax
    affiches         = set()

    for m in rapid_matchs:
        j1, j2 = m["joueur1"], m["joueur2"]
        cle = f"{j1}|{j2}"
        if cle in affiches:
            continue
        affiches.add(cle)

        # Chercher cote Winamax correspondante dans Odds API
        cote_trouvee = None
        for nom, data in odds_index.items():
            if j1.lower() in nom or nom in j1.lower() or j2.lower() in nom or nom in j2.lower():
                if data.get("cote_j1") and data.get("cote_j2"):
                    cote_trouvee = data
                    break

        if cote_trouvee:
            # ✅ Match disponible sur Winamax — on enrichit avec les données RapidAPI
            m["cote_j1"]    = cote_trouvee["cote_j1"]
            m["cote_j2"]    = cote_trouvee["cote_j2"]
            m["source_cote"] = cote_trouvee["source_cote"]
            matchs_winamax.append(m)
            lignes.append(
                f"• {m['heure']} | {j1} vs {j2} | {m['tournoi']} | {m['surface']}"
                f" | Cotes {cote_trouvee['source_cote']}: {j1} {cote_trouvee['cote_j1']:.2f}"
                f" / {j2} {cote_trouvee['cote_j2']:.2f}"
            )
        else:
            # ❌ Pas de cote Winamax — Gemini vérifiera sur Sportytrader
            matchs_sans_cote.append(m)

    # Ajouter les matchs Odds API non trouvés dans RapidAPI (fallback)
    for cle, m in odds_matchs.items():
        j1, j2 = m["joueur1"], m["joueur2"]
        if f"{j1}|{j2}" not in affiches:
            affiches.add(f"{j1}|{j2}")
            if m.get("cote_j1") and m.get("cote_j2"):
                lignes.append(
                    f"• {m['heure_utc']} | {j1} vs {j2}"
                    f" | Cotes {m['source_cote']}: {j1} {m['cote_j1']:.2f}"
                    f" / {j2} {m['cote_j2']:.2f}"
                )
                matchs_winamax.append({
                    "joueur1": j1, "joueur2": j2,
                    "heure": m["heure_utc"], "tournoi": "ATP/WTA",
                    "surface": "non disponible",
                    "cote_j1": m["cote_j1"], "cote_j2": m["cote_j2"],
                    "source_cote": m["source_cote"],
                })

    # Ajouter les matchs sans cote pour que Gemini vérifie sur Sportytrader
    if matchs_sans_cote:
        lignes.append(f"\n📋 MATCHS À VÉRIFIER SUR SPORTYTRADER ({len(matchs_sans_cote)}) :")
        for m in matchs_sans_cote[:20]:  # Limiter pour ne pas surcharger Gemini
            lignes.append(f"• {m['heure']} | {m['joueur1']} vs {m['joueur2']} | {m['tournoi']}")

    total_winamax = len(matchs_winamax)
    logging.info(f"Calendrier fusionné : {total_winamax} match(s) Winamax confirmés + {len(matchs_sans_cote)} à vérifier.")

    if total_winamax == 0 and not matchs_sans_cote:
        return ""

    lignes.append(f"\nTotal Winamax confirmé : {total_winamax} match(s).")
    return "\n".join(lignes)

# =====================================================================
# 10. MODULE C — ENRICHISSEMENT RAPIDAPI (H2H + Forme + Stats)
# =====================================================================

def enrichir_matchs_rapidapi(rapid_matchs: list, budget_requetes: int = 6) -> list:
    """
    Enrichit les matchs avec H2H, forme récente et stats via RapidAPI.
    Budget strict : max budget_requetes appels pour garder < 500/mois.
    Priorité : matchs avec cotes disponibles en premier.
    Les IDs joueurs viennent directement des fixtures — pas de résolution nom→ID.
    """
    if not RAPIDAPI_KEY or not rapid_matchs:
        return rapid_matchs

    headers = {
        "x-rapidapi-key":  RAPIDAPI_KEY,
        "x-rapidapi-host": "tennis-api-atp-wta-itf.p.rapidapi.com",
    }

    def _get(url, params=None):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=8)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logging.warning(f"RapidAPI enrichissement : {e}")
            return None

    # Prioriser les matchs avec cotes (plus analysables)
    tries = sorted(rapid_matchs, key=lambda m: (m.get("odd1") is None))

    requetes_utilisees = 0
    base = "https://tennis-api-atp-wta-itf.p.rapidapi.com/tennis/v2"

    for m in tries:
        if requetes_utilisees >= budget_requetes:
            break

        id1  = m.get("id1")
        id2  = m.get("id2")
        tour = m.get("tour", "atp")

        if not id1 or not id2:
            continue

        # --- REQ 1 : H2H info (bilan all-time + récent) ---
        if requetes_utilisees < budget_requetes:
            data = _get(f"{base}/{tour}/h2h/info/{id1}/{id2}")
            requetes_utilisees += 1
            if data:
                p1w = data.get("player1AllWins", "?")
                p2w = data.get("player2AllWins", "?")
                p1r = data.get("player1Wins", "?")
                p2r = data.get("player2Wins", "?")
                m["h2h_api"] = (
                    f"{m['joueur1']} {p1w} victoires all-time / "
                    f"{m['joueur2']} {p2w} victoires all-time. "
                    f"Récent : {p1r} vs {p2r}"
                )

        # --- REQ 2 : H2H matches (forme des 2 joueurs + surface en 1 seule requête) ---
        if requetes_utilisees < budget_requetes:
            data = _get(
                f"{base}/{tour}/h2h/matches/{id1}/{id2}",
                params={"include": "tournament.court,round", "pageSize": 10}
            )
            requetes_utilisees += 1
            if data:
                matchs_h2h = data.get("data", [])
                h2h_detail = []
                for pm in matchs_h2h[:5]:
                    gagnant = pm.get("player1", {}).get("id")
                    surf    = ((pm.get("tournament") or {}).get("court") or {}).get("name", "?")
                    rnd     = (pm.get("round") or {}).get("name", "?")
                    score   = pm.get("result", "")
                    vainqueur = m["joueur1"] if gagnant == id1 else m["joueur2"]
                    h2h_detail.append(f"{vainqueur} ({surf} / {rnd}) {score}")
                m["h2h_detail_api"] = " | ".join(h2h_detail) if h2h_detail else "Première confrontation"

        # --- REQ 3 : Forme récente J1 (5 derniers matchs avec surface) ---
        if requetes_utilisees < budget_requetes:
            data = _get(
                f"{base}/{tour}/player/past-matches/{id1}",
                params={"include": "tournament.court", "pageSize": 5}
            )
            requetes_utilisees += 1
            if data:
                forme = []
                for pm in data.get("data", [])[:5]:
                    gagnant = pm.get("player1", {}).get("id")
                    surf    = ((pm.get("tournament") or {}).get("court") or {}).get("name", "?")
                    adv     = pm.get("player2", {}) if gagnant == id1 else pm.get("player1", {})
                    res     = "V" if gagnant == id1 else "D"
                    forme.append(f"{res} vs {adv.get('name','?')} ({surf}) {pm.get('result','')}")
                m["forme_j1_api"] = " | ".join(forme) if forme else "non disponible"

        # --- REQ 4 : Forme récente J2 ---
        if requetes_utilisees < budget_requetes:
            data = _get(
                f"{base}/{tour}/player/past-matches/{id2}",
                params={"include": "tournament.court", "pageSize": 5}
            )
            requetes_utilisees += 1
            if data:
                forme = []
                for pm in data.get("data", [])[:5]:
                    gagnant = pm.get("player1", {}).get("id")
                    surf    = ((pm.get("tournament") or {}).get("court") or {}).get("name", "?")
                    adv     = pm.get("player2", {}) if gagnant == id2 else pm.get("player1", {})
                    res     = "V" if gagnant == id2 else "D"
                    forme.append(f"{res} vs {adv.get('name','?')} ({surf}) {pm.get('result','')}")
                m["forme_j2_api"] = " | ".join(forme) if forme else "non disponible"

        # --- REQ 5 : Stats service/retour J1 (Hold%, aces, break points) ---
        if requetes_utilisees < budget_requetes:
            data = _get(f"{base}/{tour}/player/match-stats/{id1}")
            requetes_utilisees += 1
            if data:
                srv = (data.get("data") or {}).get("serviceStats") or {}
                bp  = (data.get("data") or {}).get("breakPointsServeStats") or {}
                fs_in  = srv.get("firstServeGm", 0) or 0
                fs_tot = srv.get("firstServeOfGm", 1) or 1
                ws1_in  = srv.get("winningOnFirstServeGm", 0) or 0
                ws1_tot = srv.get("winningOnFirstServeOfGm", 1) or 1
                ws2_in  = srv.get("winningOnSecondServeGm", 0) or 0
                ws2_tot = srv.get("winningOnSecondServeOfGm", 1) or 1
                bp_face = bp.get("breakPointFacedGm", 0) or 0
                bp_save = bp.get("breakPointSavedGm", 0) or 0
                hold_pct = round((bp_save / bp_face * 100) if bp_face > 0 else 0, 1)
                m["stats_j1_api"] = (
                    f"1ère balle: {round(fs_in/fs_tot*100,1) if fs_tot else '?'}% | "
                    f"Pts gagnés/1ère: {round(ws1_in/ws1_tot*100,1) if ws1_tot else '?'}% | "
                    f"Pts gagnés/2ème: {round(ws2_in/ws2_tot*100,1) if ws2_tot else '?'}% | "
                    f"Hold%: {hold_pct}% | "
                    f"Aces: {srv.get('acesGm','?')} | DF: {srv.get('doubleFaultsGm','?')}"
                )

        # --- REQ 6 : Stats service/retour J2 ---
        if requetes_utilisees < budget_requetes:
            data = _get(f"{base}/{tour}/player/match-stats/{id2}")
            requetes_utilisees += 1
            if data:
                srv = (data.get("data") or {}).get("serviceStats") or {}
                bp  = (data.get("data") or {}).get("breakPointsServeStats") or {}
                fs_in  = srv.get("firstServeGm", 0) or 0
                fs_tot = srv.get("firstServeOfGm", 1) or 1
                ws1_in  = srv.get("winningOnFirstServeGm", 0) or 0
                ws1_tot = srv.get("winningOnFirstServeOfGm", 1) or 1
                ws2_in  = srv.get("winningOnSecondServeGm", 0) or 0
                ws2_tot = srv.get("winningOnSecondServeOfGm", 1) or 1
                bp_face = bp.get("breakPointFacedGm", 0) or 0
                bp_save = bp.get("breakPointSavedGm", 0) or 0
                hold_pct = round((bp_save / bp_face * 100) if bp_face > 0 else 0, 1)
                m["stats_j2_api"] = (
                    f"1ère balle: {round(fs_in/fs_tot*100,1) if fs_tot else '?'}% | "
                    f"Pts gagnés/1ère: {round(ws1_in/ws1_tot*100,1) if ws1_tot else '?'}% | "
                    f"Pts gagnés/2ème: {round(ws2_in/ws2_tot*100,1) if ws2_tot else '?'}% | "
                    f"Hold%: {hold_pct}% | "
                    f"Aces: {srv.get('acesGm','?')} | DF: {srv.get('doubleFaultsGm','?')}"
                )

    logging.info(f"RapidAPI enrichissement — {requetes_utilisees} requête(s) utilisée(s).")
    return tries  # Retourne dans l'ordre priorisé




def collecter_donnees_tennis(date, heure, calendrier_injecte, rapid_matchs=None, heure_fin="23:59"):
    # Enrichir le texte du calendrier avec les données RapidAPI déjà collectées
    if rapid_matchs and calendrier_injecte:
        lignes_enrichies = []
        for ligne in calendrier_injecte.split("\n"):
            lignes_enrichies.append(ligne)
            for m in rapid_matchs:
                if m.get("joueur1", "") in ligne and m.get("joueur2", "") in ligne:
                    if m.get("h2h_api"):
                        lignes_enrichies.append(f"  ↳ H2H: {m['h2h_api']}")
                    if m.get("h2h_detail_api"):
                        lignes_enrichies.append(f"  ↳ H2H détail: {m['h2h_detail_api']}")
                    if m.get("forme_j1_api"):
                        lignes_enrichies.append(f"  ↳ Forme {m['joueur1']}: {m['forme_j1_api']}")
                    if m.get("stats_j1_api"):
                        lignes_enrichies.append(f"  ↳ Stats service {m['joueur1']}: {m['stats_j1_api']}")
                    if m.get("forme_j2_api"):
                        lignes_enrichies.append(f"  ↳ Forme {m['joueur2']}: {m['forme_j2_api']}")
                    if m.get("stats_j2_api"):
                        lignes_enrichies.append(f"  ↳ Stats service {m['joueur2']}: {m['stats_j2_api']}")
                    break
        calendrier_injecte = "\n".join(lignes_enrichies)

    bloc = (
        f"{calendrier_injecte}\n\n"
        f"→ Calendrier COMPLET avec H2H et forme pré-collectés. Ne pas les re-vérifier.\n"
        f"→ IMPORTANT : Transmettre TOUS les matchs avec cotes disponibles — ne pas filtrer par heure.\n"
        f"→ Indiquer l'heure exacte de chaque match dans le champ heure_match.\n"
        f"→ Claude se chargera du filtrage par fenêtre horaire ({heure} → {heure_fin}).\n"
        f"→ Tes requêtes Google : UNIQUEMENT blessures, contexte psychologique, Hold%."
        if calendrier_injecte else
        f"Cherche les matchs du {date} entre {heure} et {heure_fin} sur flashscore.fr et atptour.com."
    )

    prompt = f"""
Tu es un agent de collecte tennis. Date : {date}. Heure : {heure} France.

MISSION : Enrichir les données avec stats, H2H et contexte.
Tu NE cherches PAS le calendrier — il est fourni ci-dessous.
Tes 15 requêtes Google : UNIQUEMENT stats, H2H, blessures, contexte.

{bloc}

RECHERCHES (max 15 requêtes — H2H et forme déjà fournis si disponibles) :

PRIORITÉ 1 — Données souvent manquantes (chercher en premier) :
1. Hold% et stats service/retour (1 requête par match prioritaire) :
   → tennisabstract.com OU ultimatetennisstatistics.com OU tennisratio.com OU matchstat.com
   → Combiner J1+J2 dans la même requête si possible
   → Chercher : "Prénom Nom hold% tennisabstract" ou "Nom serve stats UTS"

2. Forme récente détaillée — UNIQUEMENT si forme_j1_api ou forme_j2_api = "non disponible" :
   → flashscore.fr OU sofascore.com OU matchstat.com
   → 5 derniers matchs avec surface + score + adversaire
   → Si déjà fourni dans le calendrier → NE PAS re-chercher

3. Stats avancées par surface (Break%, Return%, Win% gazon/terre/dur) :
   → ultimatetennisstatistics.com OU tennisabstract.com
   → Particulièrement utile pour matchs sur surface spécifique

PRIORITÉ 2 — Contexte :
4. Blessures/forfaits (1 requête globale) :
   → eurosport.fr OU tennis.com OU atptour.com
5. Points ATP/WTA à défendre + classement actuel :
   → atptour.com OU wtatennis.com
6. Charge physique — heures jouées 72h, matchs enchaînés :
   → flashscore.fr OU sofascore.com

PRIORITÉ 3 — Vérification cotes matchs secondaires :
7. Pour tout match SANS cote dans le calendrier :
   → sportytrader.com/fr/cotes/tennis/
   → Cote Winamax trouvée → source_cote = "Sportytrader"
   → Introuvable → source_cote = "non trouvée"

SOURCES PAR TYPE :
• Hold% / Break% / Return%  → tennisabstract.com · ultimatetennisstatistics.com · tennisratio.com · matchstat.com
• Forme récente + scores    → flashscore.fr · sofascore.com · matchstat.com
• Stats par surface         → ultimatetennisstatistics.com · tennisabstract.com
• Classements + points      → atptour.com · wtatennis.com
• Blessures + actualités    → eurosport.fr · tennis.com · atptour.com
• Cotes                     → sportytrader.com

⚠️ IMPORTANT : Inclure TOUS les matchs du calendrier dans le JSON — même ceux hors fenêtre {heure}→{heure_fin}.
   Claude filtrera par fenêtre horaire. Ton rôle est de collecter les stats, pas de filtrer.
⚠️ NE PAS re-chercher le H2H ni la forme si déjà fournis dans le calendrier.
⚠️ Un match sans cote vérifiée = source_cote "non trouvée".

RÈGLES DE PRIORITÉ :
- Retenir OBLIGATOIREMENT tous les matchs avec cotes disponibles (Odds API ou Sportytrader)
- Pour les matchs sans stats complètes → inclure avec "non trouvé" dans les champs manquants
- Ne jamais exclure un match à cause de données manquantes — Claude décidera
- Si tu manques de requêtes, inclure le match avec les données partielles disponibles
EXCLURE : qualifications, doubles. INCLURE : tableau principal uniquement.

FORMAT JSON STRICT :
{{
  "heure_collecte": "{heure}",
  "matchs": [{{
    "heure_match": "HH:MM",
    "joueur1": "Nom", "joueur2": "Nom",
    "tournoi": "Nom", "surface": "Terre/Dur/Gazon", "indoor": false,
    "cote_j1": 1.XX, "cote_j2": 1.XX, "source_cote": "Winamax/non trouvée",
    "forme_j1": ["V","D"], "forme_j2": ["V","D"],
    "details_forme_j1": "résumé", "details_forme_j2": "résumé",
    "hold_pct_j1": "XX% ou non trouvé", "hold_pct_j2": "XX% ou non trouvé",
    "h2h_recents": "résumé", "charge_physique_j1": "résumé", "charge_physique_j2": "résumé",
    "alertes_physiques": "résumé ou Aucune", "absence_recente": "résumé ou Aucune",
    "contexte_psychologique": "résumé", "contexte": "résumé"
  }}],
  "avertissements": "données incertaines"
}}

Champ introuvable → "non trouvé". JSON valide, sans backticks.
"""

    try:
        logging.info(f"Gemini enrichit les données — {date} {heure}…")

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
                    logging.info("Gemini — mode 15 requêtes activé ✅")
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
                    logging.warning(f"Gemini 503 tentative {tentative}/3 — retry dans {tentative * 15}s…")
                    time.sleep(tentative * 15)
                else:
                    raise
        if derniere_erreur and "503" in str(derniere_erreur):
            logging.error(f"Gemini 503 après 3 tentatives : {derniere_erreur}")
            return '{"matchs": [], "avertissements": "Gemini indisponible (503) — réessayer plus tard."}'

        texte = rep.text.strip()
        texte = re.sub(r"^```json\s*", "", texte)
        texte = re.sub(r"\s*```$", "", texte)
        data  = json.loads(texte)
        logging.info(f"Gemini OK — {len(data.get('matchs', []))} match(s).")
        return json.dumps(data, ensure_ascii=False, indent=2)
    except json.JSONDecodeError as e:
        logging.error(f"Gemini JSON invalide : {e}")
        return '{"matchs": [], "avertissements": "Erreur Gemini JSON."}'
    except Exception as e:
        logging.error(f"Erreur Gemini : {e}")
        return '{"matchs": [], "avertissements": "Erreur Gemini."}'

# =====================================================================
# 11. PROMPT CLAUDE
# =====================================================================

def construire_prompt_claude(date, heure, donnees_json, heure_fin="23:59"):
    session = "MATIN" if heure < "14:00" else "APRÈS-MIDI"
    try:
        avertissements = json.loads(donnees_json).get("avertissements", "Aucun")
    except Exception:
        avertissements = "Non disponibles"

    return f"""Tu es un expert en paris tennis. Date : {date} · {heure} France · Session {session}.

DONNÉES COLLECTÉES (source unique — ne pas chercher sur internet) :
{donnees_json}

⚠️ AVERTISSEMENTS : {avertissements}
→ Données manquantes importantes → abandonner le match.

Tu n'as PAS accès à internet. Analyse uniquement les données fournies.

FILTRES IMMÉDIATS :
• Match commencé avant {heure} → skip
• Match commençant après {heure_fin} → skip (hors fenêtre session)
• absence_recente > 2 mois → skip
• alertes_physiques → marchés de jeux interdits + mise 0.5%
• Retour 3-8 semaines → marchés alternatifs + mise 0.5%
• Qualifications ou hors tableau principal → skip
• Cote "non trouvée" → skip automatique (pas de marché = impossible à jouer sur Winamax)

CALIBRATION PROBABILITÉS :
• Cote < 1.50  → MAX 75%
• 1.50-1.80    → MAX 68%
• 1.80-2.20    → MAX 58%
• > 2.20       → MAX 52%

NIVEAUX DE CONFIANCE ET MISES :
⚠️ Le niveau est déterminé par la qualité des données ET la solidité de l'analyse :

ÉLEVÉE (3%) — toutes les conditions réunies :
  · Delta ≥ 0.10 ✅
  · Analyse solide (forme, H2H par surface, contexte) ✅
  · Données complètes ou quasi-complètes ✅
  · Pas d'alerte physique majeure ✅

MODÉRÉE (2%) — analyse correcte mais légères lacunes :
  · Delta ≥ 0.10 ✅
  · Analyse correcte mais quelques données manquantes
  · H2H limité ou forme partielle acceptable
  · Logique analytique convaincante malgré les lacunes

BASSE (1%) — données insuffisantes ou cote très élevée :
  · Delta ≥ 0.10 ✅ mais données importantes manquantes
  · OU cote > 2.50 (variance élevée)
  · OU première confrontation sans contexte suffisant
  · OU alertes physiques présentes
  · OU retour de blessure 3-8 semaines

Plafonds absolus (quel que soit le niveau) :
  · Alertes physiques → MAX 1%
  · Retour 3-8 semaines → MAX 1%
  · Combiné → MAX 2%

⚠️ RÈGLE ABSOLUE — FORMAT DE SORTIE :
Ta réponse doit commencer DIRECTEMENT par 🔴 ou par AUCUN_MATCH.
STRICTEMENT INTERDIT avant le 🔴 :
  · Étapes d'analyse (ÉTAPE 1, ÉTAPE 2, FILTRAGE, etc.)
  · Calculs Kelly intermédiaires
  · Liste des matchs skippés ou abandonnés
  · Tout texte introductif ou explicatif
  · Titres de section
L'analyse est INTERNE et ne doit JAMAIS apparaître dans ta réponse.
Premier caractère de ta réponse = 🔴 ou A (de AUCUN_MATCH). Rien d'autre.

ANALYSE EN 2 ÉTAPES (INTERNE — NE PAS AFFICHER) :
⚠️ Ta réponse commence DIRECTEMENT par 🔴 ou AUCUN_MATCH.

[1] FACTEURS BRUTS :
  Surface + forme 5 matchs + charge 72h + Hold% + H2H par surface
  Contexte psychologique : points à défendre, public local, GC dans 7j
  Fatigue : match long hier, titre récent, 3 matchs en 5j

[2] DÉCISION :
  Proba % → Cote Juste = 1/proba → Delta = Cote réelle - Cote Juste
  Delta < 0.10 → ❌ abandonné
  Delta ≥ 0.10 → VALUE ✅ → Kelly quart = ((p×c−1)/(c−1))×0.25
  Zéro value → AUCUN_MATCH

DOUBLE VALIDATION (TOUS les marchés) :
  1. Delta ≥ 0.10 ✅  2. Analyse [1] justifie le marché ✅
  Si l'une manque → abandonné.

CONFIANCE ÉLEVÉE :
  Moneyline (si supériorité claire) · 2-0 · 2-1 · Handicap Jeux
  Combiné max 2 (tournois différents OU surfaces différentes) mise 1%

CONFIANCE MODÉRÉE — Moneyline INTERDIT :
  Serveurs (Hold>83%) → Over jeux · Tiebreak
  Serré → +2.5 sets · Score 2-1 · Over jeux
  Dominant → 2-0 · Under jeux
  Prenable → Handicap +4.5 · Score 2-1
  Surface lente → Under · +2.5 sets · 2-1
  Combiné MODÉRÉE → INTERDIT

MISES (Kelly quart plafonné par niveau) :
Simple ÉLEVÉE 3% · Simple MODÉRÉE 2% · Simple BASSE 1% · Combiné MAX 2%

FORMAT (max {MAX_TICKETS} tickets, [SEPARATEUR] entre chaque) :
⚠️ RÈGLE FONDAMENTALE : N'envoyer QUE les tickets validés (Delta ≥ 0.10 + analyse confirmée).
Les tickets abandonnés (delta négatif, pas de value) → NE PAS les inclure dans la réponse.
Si 0 ticket validé → répondre : AUCUN_MATCH suivi d'une explication courte (max 80 mots) :
  · Citer 1-2 matchs analysés avec le joueur favori et pourquoi pas de value
  · Mentionner le delta et la raison principale (cote trop basse, données insuffisantes)
  · Ton naturel et direct, comme un analyste qui explique à ses abonnés
  Exemple : "AUCUN_MATCH — Sinner écrase Etcheverry sur gazon mais cote 1.19 trop basse (delta -0.14). Medvedev domine à Halle mais même problème (delta -0.08). Cotes trop compressées aujourd'hui, pas de value exploitable."
La limite de {MAX_TICKETS} tickets est un PLAFOND, pas un objectif.
1 ticket excellent vaut mieux que 5 tickets moyens.
HTML uniquement <b>texte</b>. JAMAIS **texte**. POURQUOI max 60 mots.

🔴 <b>PRONOSTIC [SIMPLE/COMBINÉ]</b> 🔴
━━━━━━━━━━━━━━━━━━━━
🏟 <b>MATCHS :</b> [A vs B]
🏆 <b>COMPÉTITION :</b> [Tournoi]
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

    # Fenêtre horaire selon la session
    # Matin      (09h30) → matchs 09h30 → 14h30
    # Après-midi (14h30) → matchs 14h30 → 19h30
    # Soir       (19h30) → matchs 19h30 → 00h00
    if heure < "14:00":
        session   = "MATIN"
        heure_fin = "14:30"
    elif heure < "19:00":
        session   = "APRÈS-MIDI"
        heure_fin = "19:30"
    else:
        session   = "SOIR"
        heure_fin = "05:00"
    logging.info(f"Session {session} — fenêtre {heure} → {heure_fin}")

    heure_utc_min      = (maintenant.astimezone(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
    odds_matchs        = precollecte_odds_api(heure_utc_min)
    rapid_matchs       = precollecte_rapidapi_tennis(date)
    rapid_matchs       = enrichir_matchs_rapidapi(rapid_matchs, budget_requetes=6)
    calendrier_injecte = fusionner_calendrier(odds_matchs, rapid_matchs)
    donnees_json       = collecter_donnees_tennis(date, heure, calendrier_injecte, rapid_matchs, heure_fin)

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

    prompt = construire_prompt_claude(date, heure, donnees_json, heure_fin)

    # LOG DEBUG — affiche les matchs que Gemini a retournés
    try:
        donnees_debug = json.loads(donnees_json)
        matchs_debug  = donnees_debug.get("matchs", [])
        logging.info(f"DEBUG — {len(matchs_debug)} match(s) transmis à Claude :")
        for i, m in enumerate(matchs_debug, 1):
            logging.info(
                f"  [{i}] {m.get('joueur1','?')} vs {m.get('joueur2','?')} "
                f"| {m.get('heure_match','?')} | {m.get('surface','?')} "
                f"| Cotes {m.get('cote_j1','?')} / {m.get('cote_j2','?')} "
                f"| Hold J1: {m.get('hold_pct_j1','?')} / J2: {m.get('hold_pct_j2','?')}"
            )
    except Exception as e:
        logging.warning(f"DEBUG log matchs : {e}")

    # Bascule dynamique Sonnet → Opus selon richesse des données
    nb_matchs_analyse = len(json.loads(donnees_json).get("matchs", []))
    modele_choisi = CLAUDE_OPUS if nb_matchs_analyse >= SEUIL_OPUS else CLAUDE_SONNET
    logging.info(f"Modèle Claude : {'Opus 🔥' if modele_choisi == CLAUDE_OPUS else 'Sonnet ⚡'} ({nb_matchs_analyse} matchs — seuil {SEUIL_OPUS})")

    try:
        logging.info(f"Claude analyse — {date} {heure}")
        rep = claude_client.messages.create(
            model=modele_choisi, max_tokens=4096, system=prompt,
            messages=[{"role": "user", "content":
                f"Analyse et propose les meilleurs paris (max {MAX_TICKETS}) — {date} {heure}."}],
        )
        texte = "\n".join(b.text for b in rep.content if hasattr(b, "text") and b.text).strip()
        logging.info(f"Claude OK ({len(texte)} chars) — {rep.usage.input_tokens} in / {rep.usage.output_tokens} out")

        # LOG DEBUG — affiche la réponse brute de Claude (500 premiers chars)
        logging.info(f"DEBUG Claude réponse : {texte[:500]}")

        if "AUCUN_MATCH" in texte:
            nb = len(json.loads(donnees_json).get("matchs", []))
            # Extraire l'explication de Claude (500 premiers chars après AUCUN_MATCH)
            explication = ""
            idx = texte.find("AUCUN_MATCH")
            if idx != -1:
                suite = texte[idx + len("AUCUN_MATCH"):].strip()
                # Nettoyer et limiter à 200 chars
                explication = re.sub(r'<[^>]+>', '', suite)[:200].strip()
                if explication:
                    explication = f"\n\n💬 <i>{explication}</i>"
            _envoyer_notification_sans_ticket(
                f"{nb} match(s) analysé(s) — aucune value suffisante détectée.{explication}\n\n"
                f"On passe notre chemin. 💼",
                session=session
            )
            return
        if len(texte) <= 20:
            return

        tickets = [t.strip() for t in texte.split(TICKET_SEP) if len(t.strip()) > 20][:MAX_TICKETS]

        # Filtrer les tickets abandonnés — Claude les affiche pour transparence
        # mais ils ne doivent pas être sauvegardés ni comptabilisés
        mots_abandon = ["abandonné", "delta négatif", "pas de value", "ticket abandonné",
                        "kelly 0%", "aucune value", "interdit"]
        tickets_valides = []
        for t in tickets:
            t_lower = t.lower()
            if any(mot in t_lower for mot in mots_abandon):
                logging.info(f"Ticket abandonné détecté — non sauvegardé.")
                continue
            tickets_valides.append(t)
        tickets = tickets_valides
        if not tickets:
            _envoyer_notification_sans_ticket(
                f"Matchs analysés — aucune value retenue après validation stricte.\n"
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
                sauvegarder_pari_pour_suivi({
                    "pari":   ticket,
                    "date":   date,
                    "marche": _detecter_marche(ticket),
                    "niveau": _detecter_niveau(ticket),
                })
                hashes_connus.add(h)
                nouveaux_hashes.append(h)
                paris_envoyes += 1
                if i < len(tickets):
                    time.sleep(1)

        if nouveaux_hashes and not DRY_RUN:
            sauvegarder_historique(list(hashes_connus), hist_sha)
        logging.info(f"✅ {paris_envoyes} ticket(s) envoyé(s).")

    except Exception as e:
        logging.error(f"Erreur critique : {e}", exc_info=True)
        _alerter_telegram_erreur(f"bot.py a planté : {e}")
    finally:
        logging.info(f"Terminé en {time.time() - debut:.1f}s.")

# =====================================================================
# 13. POINT D'ENTRÉE CLI
# =====================================================================

if __name__ == "__main__":
    args         = [a for a in sys.argv[1:] if a != "--dry-run"]
    if not args:
        run_bot_autonome()
    elif args[0] == "resultat" and len(args) == 2:
        flag = args[1].lower()
        if flag in ("v", "victoire", "win", "1"):
            enregistrer_resultat(True)
        elif flag in ("d", "defaite", "lose", "0"):
            enregistrer_resultat(False)
        else:
            print(f"❌ Utilise 'v' ou 'd'.")
            sys.exit(1)
    elif args[0] in ("--help", "-h"):
        print(__doc__)
    else:
        print(f"❌ Commande inconnue.")
        sys.exit(1)
