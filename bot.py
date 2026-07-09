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

# ============================================================
# MARCHÉS ALTERNATIFS (Total Sets O/U 2.5 + Total Games O/U) — via OddsPapi/Pinnacle
# ============================================================
# INTERRUPTEUR PRINCIPAL — 3 modes :
#   "off"         : désactivé totalement, le bot fonctionne comme avant (DÉFAUT SÛR)
#   "observation" : récupère et LOGUE les cotes OddsPapi mais NE PARIE PAS
#                   (pour valider le format réel de la réponse via les logs)
#   "actif"       : génère de vrais tickets sur les marchés alternatifs
MARCHES_ALT_MODE = "off"   # ← passe à "observation" pour tester, puis "actif"

ODDSPAPI_HOST = "odds-api1.p.rapidapi.com"   # host OddsPapi sur RapidAPI (confirmé 09/07/2026)
ODDSPAPI_BOOKMAKER = "pinnacle"               # bookmaker de référence (le plus sharp)
MARKET_TOTAL_SETS_LIGNE = 2.5                 # Over 2.5 sets = "va au 3e set"
# Déclencheur : on ne regarde les marchés alt que sur les matchs "serrés"
# (cote du favori dans cette fourchette = match équilibré, incertain sur le vainqueur)
MARCHES_ALT_COTE_MIN = 1.50
MARCHES_ALT_COTE_MAX = 2.10
# Catégories de faux tennis à EXCLURE absolument (matchs simulés par ordinateur)
CATEGORIES_INTERDITES = ("simulated reality", "srl")

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
        "elevee":         {"v": 0, "d": 0},
        "moderee":        {"v": 0, "d": 0},
        "basse":          {"v": 0, "d": 0},
        "qualif_elevee":  {"v": 0, "d": 0},
        "qualif_moderee": {"v": 0, "d": 0},
        "qualif_basse":   {"v": 0, "d": 0},
    },
    "par_modele": {
        "opus":   {"v": 0, "d": 0},
        "sonnet": {"v": 0, "d": 0},
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
    """Détecte le type de marché depuis le texte du ticket (noms Winamax)."""
    t = ticket_texte.lower()
    if "combiné" in t or "combine" in t:
        return "combine"
    if "tiebreak" in t or "tie-break" in t:
        return "tiebreak"
    # Écart de jeux / écart de set = handicap (vérifier AVANT "jeux" pour over_under)
    if "écart de jeux" in t or "ecart de jeux" in t or "écart de set" in t or \
       "ecart de set" in t or "handicap" in t:
        return "handicap"
    # Nombre de jeux / nombre de sets = over/under
    if "nombre de jeux" in t or "nombre de set" in t or "over" in t or "under" in t:
        return "over_under"
    if "score exact" in t or "2-0" in t or "2-1" in t:
        return "score_exact"
    if "moneyline" in t or "vainqueur" in t or "gagne" in t or "victoire" in t:
        return "moneyline"
    return "autre"


def _detecter_niveau(ticket_texte):
    """Détecte le niveau de confiance depuis le texte du ticket."""
    t = ticket_texte.lower()
    # Qualif détecté en premier (mention "qualif" + niveau)
    est_qualif = "qualif" in t or "wimbledon q" in t or "roland-garros q" in t or \
                 "us open q" in t or "australian open q" in t
    if est_qualif:
        if "élevée" in t or "elevee" in t:
            return "qualif_elevee"
        if "modérée" in t or "moderee" in t:
            return "qualif_moderee"
        if "basse" in t:
            return "qualif_basse"
        return "qualif_basse"  # qualif par défaut = prudent
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
        f"\n\n📊 <b>BILAN ACEANALYTICS 🎾 TENNIS</b>\n"
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
        f"{emoji} <b>ACEANALYTICS 🎾 TENNIS — {label}</b>\n\n"
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

def _signature_pari(pari_info):
    """Signature stable d'un pari pour la déduplication : match + date + prono."""
    txt = re.sub(r"<[^>]+>", "", pari_info.get("pari", "")).lower()
    # Extraire match et prono des lignes du ticket
    match_m = re.search(r"match\s*:?\s*(.+)", txt)
    prono_m = re.search(r"prono\s*:?\s*(.+)", txt)
    match_s = match_m.group(1).strip()[:60] if match_m else ""
    prono_s = prono_m.group(1).strip()[:40] if prono_m else ""
    return f"{pari_info.get('date','')}|{match_s}|{prono_s}"


def sauvegarder_pari_pour_suivi(pari_info):
    if "pari" not in pari_info or "date" not in pari_info:
        logging.error(f"Structure invalide : {pari_info}")
        return
    if DRY_RUN:
        return
    paris, sha = _gh_get("pari_en_cours.json")
    if not isinstance(paris, list):
        paris = []
    # DÉDUPLICATION : ne pas ajouter un pari déjà présent (même match+date+prono).
    # Évite le double comptage si le même match passe à deux runs rapprochés.
    sig_nouveau = _signature_pari(pari_info)
    for p in paris:
        if _signature_pari(p) == sig_nouveau:
            logging.warning(f"Pari déjà en file (doublon évité) : {sig_nouveau}")
            return
    paris.append(pari_info)
    _gh_put("pari_en_cours.json", paris, "📌 Ajout pari", sha=sha)


# Liste de secours si le fichier GitHub est absent (catégories Winamax larges)
TOURNOIS_WINAMAX_DEFAUT = [
    "Wimbledon", "US Open", "Australian Open", "Roland Garros",
]

def charger_tournois_winamax():
    """
    Charge la liste des tournois Winamax depuis GitHub (tournois_winamax.json).
    Éditable à la main sur GitHub sans toucher au code. Sert de guide à Gemini
    pour chercher le calendrier quand RapidAPI est indisponible.
    """
    data, _ = _gh_get("tournois_winamax.json")
    if isinstance(data, dict) and data.get("tournois_actifs"):
        return data["tournois_actifs"]
    return TOURNOIS_WINAMAX_DEFAUT

# =====================================================================
# 6A-BIS. COMPTEUR DE QUOTA RAPIDAPI
# =====================================================================
# Toutes les sources tennis complémentaires (RapidAPI Tennis, SportAPI7,
# OddsPapi) partagent la MÊME clé RAPIDAPI_KEY = le même quota mensuel.
# Ce compteur trace la conso réelle par run et cumule sur le mois pour
# éviter les 429 surprise. N'affecte PAS l'analyse ni la stratégie.

_QUOTA_RUN = {"rapidapi": 0}  # compteur en mémoire pour ce run

def _quota_inc(n=1):
    """Incrémente le compteur de requêtes RapidAPI de ce run."""
    _QUOTA_RUN["rapidapi"] += n

def _quota_persister():
    """Cumule la conso du run dans quota_rapidapi.json (reset auto chaque mois)."""
    if DRY_RUN or _QUOTA_RUN["rapidapi"] == 0:
        return
    mois_actuel = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m")
    data, sha = _gh_get("quota_rapidapi.json")
    if not isinstance(data, dict) or data.get("mois") != mois_actuel:
        # Nouveau mois (ou première fois) → reset
        data = {"mois": mois_actuel, "rapidapi_utilise": 0}
        sha = sha  # garder le sha pour écraser l'ancien mois
    data["rapidapi_utilise"] = data.get("rapidapi_utilise", 0) + _QUOTA_RUN["rapidapi"]
    try:
        _gh_put("quota_rapidapi.json", data, "📊 Maj quota RapidAPI", sha=sha)
        logging.info(
            f"Quota RapidAPI — {_QUOTA_RUN['rapidapi']} req ce run / "
            f"{data['rapidapi_utilise']} cumulées en {mois_actuel}."
        )
    except Exception as e:
        logging.warning(f"Quota persist échoué : {e}")

# =====================================================================
# 6B. MODULE SPORTAPI7 — DONNÉES COMPLÉMENTAIRES TENNIS
# =====================================================================

SPORTAPI7_HOST = "sportapi7.p.rapidapi.com"
SPORTAPI7_BASE = "https://sportapi7.p.rapidapi.com/api/v1"

def _sportapi7_get(endpoint, params=None, retries=2):
    """Requête SportAPI7 avec retry rapide sur 429 (délais courts)."""
    if not RAPIDAPI_KEY:
        return None
    for tentative in range(1, retries + 1):
        try:
            _quota_inc()
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
                    time.sleep(3)  # Délai court — pas de blocage 5 min
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


def enrichir_sportapi7_tennis(rapid_matchs: list) -> list:
    """
    Enrichit les matchs tennis avec SportAPI7 en complément de RapidAPI Tennis.
    Endpoints : /sport/tennis/scheduled-events/{date} + /event/{id}/odds/1/all
    Utilise la même clé RAPIDAPI_KEY — pas de quota supplémentaire.
    """
    if not RAPIDAPI_KEY or not rapid_matchs:
        return rapid_matchs

    date_today = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d")

    try:
        # Récupérer tous les matchs tennis du jour — retry si 429
        data = None
        # Un seul appel — _sportapi7_get gère déjà les retries 429 en interne
        data = _sportapi7_get(f"sport/tennis/scheduled-events/{date_today}")

        if not data:
            logging.info("SportAPI7 Tennis — indisponible (429/quota), on continue sans.")
            return rapid_matchs

        events   = data.get("events", [])
        enrichis = 0
        logging.info(f"SportAPI7 Tennis — {len(events)} événement(s) disponible(s).")

        # Index par noms de joueurs
        sportapi7_index = {}
        for ev in events:
            home = ev.get("homeTeam", {}).get("name", "").lower()
            away = ev.get("awayTeam", {}).get("name", "").lower()
            if home and away:
                sportapi7_index[f"{home}|{away}"] = ev

        for m in rapid_matchs:
            j1  = m.get("joueur1", "").lower()
            j2  = m.get("joueur2", "").lower()
            ev  = sportapi7_index.get(f"{j1}|{j2}") or sportapi7_index.get(f"{j2}|{j1}")

            # Recherche partielle par nom de famille
            if not ev:
                for k, v in sportapi7_index.items():
                    k1, k2 = k.split("|")
                    nom1 = j1.split()[-1] if j1 else ""
                    nom2 = j2.split()[-1] if j2 else ""
                    if nom1 and nom2 and nom1 in k1 and nom2 in k2:
                        ev = v
                        break

            if ev:
                event_id = ev.get("id")

                # Tournoi confirmé
                tournament = ev.get("tournament", {})
                if tournament:
                    m["tournoi_sportapi7"] = tournament.get("name", "")

                # Cotes via SportAPI7 — avec gestion 404/429
                if event_id:
                    try:
                        odds_data = _sportapi7_get(f"event/{event_id}/odds/1/all")
                        if odds_data:
                            markets = odds_data.get("markets", [])
                            for market in markets:
                                if market.get("marketName", "").lower() in ["winner", "match winner"]:
                                    choices = market.get("choices", [])
                                    for c in choices:
                                        if c.get("name", "").lower() in ["1", "home"]:
                                            m["sportapi7_cote_j1"] = c.get("fractionalValue") or c.get("initialFractionalValue")
                                        elif c.get("name", "").lower() in ["2", "away"]:
                                            m["sportapi7_cote_j2"] = c.get("fractionalValue") or c.get("initialFractionalValue")
                        time.sleep(0.5)  # Éviter le 429
                    except Exception:
                        pass  # 404/429 → ignorer silencieusement

                m["sportapi7_id"] = event_id
                enrichis += 1

        logging.info(f"SportAPI7 Tennis — {enrichis} match(s) enrichi(s).")

    except Exception as e:
        logging.warning(f"SportAPI7 Tennis erreur : {e}")

    return rapid_matchs

# =====================================================================
# 6C. MODULE ODDSPAPI — COTES COMPLÉMENTAIRES TENNIS
# =====================================================================

ODDSPAPI_HOST = "odds-api1.p.rapidapi.com"
ODDSPAPI_BASE = "https://odds-api1.p.rapidapi.com"

def _oddspapi_get(endpoint, params=None, retries=2):
    """Requête OddsPapi avec la clé RapidAPI existante."""
    if not RAPIDAPI_KEY:
        return None
    for tentative in range(1, retries + 1):
        try:
            _quota_inc()
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
            if r.status_code == 400:
                # Logger le corps de l'erreur pour comprendre ce que l'API attend
                logging.warning(f"OddsPapi {endpoint} 400 — réponse: {r.text[:200]}")
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logging.warning(f"OddsPapi {endpoint} : {e}")
            return None
    return None


def enrichir_oddspapi_tennis(rapid_matchs, odds_matchs):
    """
    Complète les cotes Winamax via OddsPapi.
    IMPORTANT (confirmé via données réelles) :
      - sportId tennis = 12 (10=Soccer, 11=Basket, 12=Tennis, 13=Baseball)
      - slug bookmaker = "winamax.fr" (recherche tolérante "winamax")
      - /fixtures/today retourne bookmakers:{} VIDE → appeler /fixtures/odds
      - endpoint cotes = /fixtures/odds (doc officielle /odds), fallback /fixtures/odds/main
      - fixtureId au format complet "id1000..." (SINGULIER)
      - structure : bookmakerOdds.{slug}.markets.{marketId}.outcomes.{outId}.players.0.price
      - startTime en epoch SECONDES
      - INDÉPENDANT de RapidAPI Tennis : fonctionne même si rapid_matchs vide
    Ne remplace JAMAIS une cote existante (Odds API prioritaire).
    """
    if not RAPIDAPI_KEY:
        return odds_matchs

    WINA_SLUG = "winamax.fr"

    try:
        # 1) Lister les matchs tennis du jour (sportId 12 = Tennis ; 13 = Baseball !)
        data = _oddspapi_get("fixtures/today", {"sportId": 12})
        if not data:
            logging.info("OddsPapi Tennis — pas de fixtures.")
            return odds_matchs

        fixtures = data if isinstance(data, list) else data.get("fixtures", [])

        # Ne garder que les matchs à venir (Pre-Game) non déjà couverts.
        # PRIORISER hasOdds=true (vrais tournois cotés, pas les M15/qualifs obscurs).
        a_traiter = []
        for fx in fixtures:
            statut = fx.get("status", {}).get("statusName", "")
            if statut not in ("Pre-Game", "", None):
                continue
            participants = fx.get("participants", {})
            p1 = participants.get("participant1Name", "")
            p2 = participants.get("participant2Name", "")
            fixture_id = fx.get("fixtureId")
            if not (p1 and p2 and fixture_id):
                continue
            if f"{p1}|{p2}" in odds_matchs:
                continue
            has_odds = fx.get("hasOdds", False)
            a_traiter.append((has_odds, fixture_id, p1, p2, fx.get("startTime", 0)))

        # Trier : fixtures avec cotes en premier, puis limiter
        a_traiter.sort(key=lambda x: not x[0])  # hasOdds=True d'abord
        nb_avec_odds = sum(1 for x in a_traiter if x[0])
        logging.info(f"OddsPapi — {nb_avec_odds} fixture(s) avec cotes sur {len(a_traiter)} à venir.")
        a_traiter = [x[1:] for x in a_traiter[:25]]  # retirer le flag has_odds, plafond 25
        ajouts = 0

        def _prix_outcomes(outcomes):
            prices = []
            for out_id in sorted(outcomes.keys(), key=lambda x: int(x) if str(x).isdigit() else 999999):
                players = outcomes[out_id].get("players", {})
                pr = players.get("0", {}).get("price")
                if pr:
                    prices.append(pr)
            return prices

        diag_fait = False
        for fixture_id, p1, p2, start in a_traiter:
            # Essayer /fixtures/odds (= /odds doc officielle) puis /fixtures/odds/main en fallback
            # NE PAS filtrer par bookmaker dans la requête : on récupère tout puis on cherche Winamax
            odds_data = _oddspapi_get("fixtures/odds", {"fixtureId": fixture_id})
            if not odds_data:
                odds_data = _oddspapi_get("fixtures/odds/main", {"fixtureId": fixture_id})
            time.sleep(0.15)
            if not odds_data:
                continue

            od = odds_data[0] if isinstance(odds_data, list) and odds_data else odds_data
            if not isinstance(od, dict):
                continue

            # DIAGNOSTIC (1 seule fois) : voir la structure réelle et les bookmakers dispo
            if not diag_fait:
                diag_fait = True
                odds_section = od.get("odds", od.get("bookmakerOdds", {}))
                book_keys = list(odds_section.keys()) if isinstance(odds_section, dict) else "non-dict"
                logging.info(f"OddsPapi DIAG — slugs avec cotes: {book_keys[:30]}")

            # Cotes sous 'odds' (RapidAPI) OU 'bookmakerOdds' (doc officielle)
            book_odds = od.get("odds", {})
            if not isinstance(book_odds, dict) or not book_odds:
                book_odds = od.get("bookmakerOdds", {})
            if not isinstance(book_odds, dict) or not book_odds:
                continue
            wina = {}
            for slug_key, slug_val in book_odds.items():
                if "winamax" in slug_key.lower():
                    wina = slug_val
                    break
            # markets peut être directement sous wina, ou wina['markets']
            if isinstance(wina, dict):
                markets = wina.get("markets", wina)
            else:
                markets = {}
            if not isinstance(markets, dict) or not markets:
                continue

            c1 = c2 = None
            market_ids = sorted(markets.keys(), key=lambda x: int(x) if str(x).isdigit() else 999999)
            for mkt_id in market_ids:
                mkt = markets[mkt_id]
                if not isinstance(mkt, dict):
                    continue
                outcomes = mkt.get("outcomes", {})
                if len(outcomes) == 2 and not mkt.get("handicap"):
                    prices = _prix_outcomes(outcomes)
                    if len(prices) >= 2:
                        c1, c2 = prices[0], prices[1]
                        break

            if c1 and c2:
                heure_str = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if start else ""
                odds_matchs[f"{p1}|{p2}"] = {
                    "joueur1": p1, "joueur2": p2,
                    "heure_utc": heure_str,
                    "cote_j1": c1, "cote_j2": c2,
                    "source_cote": "Winamax (OddsPapi)",
                }
                ajouts += 1

        logging.info(f"OddsPapi Tennis — {ajouts} cote(s) Winamax ajoutée(s) ({len(a_traiter)} matchs testés).")

    except Exception as e:
        logging.warning(f"OddsPapi Tennis erreur : {e}")

    return odds_matchs

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
            # Conversion UTC → heure française (déterministe, comme RapidAPI)
            heure_fr_odds = commence[:16].replace("T", " ")
            try:
                iso = commence.replace("Z", "+00:00")
                dt_utc = datetime.fromisoformat(iso)
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                heure_fr_odds = dt_utc.astimezone(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass
            matchs[cle] = {
                "joueur1": j1, "joueur2": j2,
                "heure_utc": heure_fr_odds,
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
    quota_epuise = False
    for tour in ["atp", "wta"]:
        if quota_epuise:
            logging.info(f"RapidAPI {tour.upper()} — skip (quota déjà épuisé ce run).")
            break
        page = 1
        while True:
            try:
                url = f"https://tennis-api-atp-wta-itf.p.rapidapi.com/tennis/v2/{tour}/fixtures/{date_api}"
                _quota_inc()
                r   = requests.get(url, headers=headers, timeout=10, params={
                    "include":  "tournament,round",
                    "filter":   "PlayerGroup:singles",
                    "pageSize": 100,
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

                    # Tournoi et surface (lu avant le filtre qualif pour décider)
                    trn     = m.get("tournament") or {}
                    nom_trn = trn.get("name", "Tournoi inconnu")
                    court   = trn.get("court") or {}
                    surface = court.get("name", "non disponible")

                    # Qualifs : exclues SAUF pour les Grand Chelems (Winamax les cote).
                    est_qualif = str(m.get("seed1") or "") == "Q" or str(m.get("seed2") or "") == "Q"
                    nom_lower = nom_trn.lower()
                    est_grand_chelem = any(gc in nom_lower for gc in
                        ["wimbledon", "roland", "us open", "australian open", "french open"])
                    if est_qualif and not est_grand_chelem:
                        continue  # qualif de petit tournoi → skip

                    # Heure — conversion UTC → heure française DÉTERMINISTE (à la source).
                    # RapidAPI donne l'heure en UTC ISO (ex: "2026-06-29T14:05:00Z").
                    # On la convertit en Europe/Paris ici, dans le code, pour ne JAMAIS
                    # dépendre de Gemini (qui invente/décale les heures).
                    h_brut = m.get("date") or ""
                    heure_fr = "heure inconnue"
                    if "T" in str(h_brut):
                        try:
                            # Parser l'ISO UTC et convertir en Europe/Paris
                            iso = str(h_brut).replace("Z", "+00:00")
                            dt_utc = datetime.fromisoformat(iso)
                            if dt_utc.tzinfo is None:
                                dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                            dt_fr = dt_utc.astimezone(ZoneInfo("Europe/Paris"))
                            heure_fr = dt_fr.strftime("%Y-%m-%d %H:%M")  # heure française, sans 'UTC'
                        except Exception:
                            heure_fr = str(h_brut)[:16].replace("T", " ")
                    h = heure_fr

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
                # Plafond de pagination : 3 pages × 100 = 300 matchs max par tour.
                # Largement suffisant (un jour de Grand Chelem = ~150 matchs/tour max).
                # Évite de brûler le quota quotidien (50/jour) sur la pagination.
                if page >= 3:
                    logging.info(f"RapidAPI {tour.upper()} — plafond 3 pages atteint, on s'arrête (économie quota).")
                    break
                page += 1

            except requests.exceptions.HTTPError as e:
                logging.warning(f"RapidAPI {tour.upper()} p{page} : {e}")
                if "429" in str(e):
                    quota_epuise = True  # quota épuisé → inutile de tenter l'autre tour
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

    matchs_winamax        = []
    matchs_sans_cote      = []
    affiches              = set()
    noms_famille_affiches = set()  # Pour détecter doublons avec noms légèrement différents

    for m in rapid_matchs:
        j1, j2 = m["joueur1"], m["joueur2"]
        cle = f"{j1}|{j2}"
        if cle in affiches:
            continue

        # Déduplication par nom de famille (ex: "Andreescu" == "Bianca Andreescu")
        nom1 = j1.split()[-1].lower() if j1 else ""
        nom2 = j2.split()[-1].lower() if j2 else ""
        cle_famille = f"{nom1}|{nom2}"
        if cle_famille in noms_famille_affiches:
            logging.info(f"Doublon ignoré : {j1} vs {j2}")
            continue

        affiches.add(cle)
        noms_famille_affiches.add(cle_famille)

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
            ligne = (
                f"• {m['heure']} | {j1} vs {j2} | {m['tournoi']} | {m['surface']}"
                f" | Cotes {cote_trouvee['source_cote']}: {j1} {cote_trouvee['cote_j1']:.2f}"
                f" / {j2} {cote_trouvee['cote_j2']:.2f}"
            )
            # Marchés alternatifs avec VRAIES cotes Winamax (OddsPapi)
            marches_alt = cote_trouvee.get("marches_alternatifs", {})
            if marches_alt:
                m["marches_alternatifs"] = marches_alt
                alt_parts = []
                # Handicaps jeux
                hcaps = {}
                for k, v in marches_alt.items():
                    if k.startswith("hcap_j1_"):
                        h = k.replace("hcap_j1_", "")
                        hcaps.setdefault(h, {})["j1"] = v
                    elif k.startswith("hcap_j2_"):
                        h = k.replace("hcap_j2_", "")
                        hcaps.setdefault(h, {})["j2"] = v
                for h, vals in sorted(hcaps.items()):
                    alt_parts.append(f"Écart de jeux {h}: {j1} {vals.get('j1','?')}/{j2} {vals.get('j2','?')}")
                # Totaux jeux
                for k, v in sorted(marches_alt.items()):
                    if k.startswith("over_"):
                        ligne_ou = k.replace("over_", "")
                        alt_parts.append(f"Total {ligne_ou} jeux: Over {v}/Under {marches_alt.get(f'under_{ligne_ou}','?')}")
                if alt_parts:
                    ligne += " | Marchés Winamax réels → " + " | ".join(alt_parts)
            lignes.append(ligne)
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
            _quota_inc()
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




def _reparer_json_gemini(texte):
    """
    Tente de réparer un JSON Gemini légèrement malformé avant d'abandonner.
    Erreurs classiques quand la réponse est longue (beaucoup de matchs) :
    virgule finale avant ] ou }, JSON tronqué en plein milieu.
    Retourne un dict si réparable, sinon None.
    """
    # 1) Essai direct
    try:
        return json.loads(texte)
    except Exception:
        pass
    # 2) Retirer les virgules finales (trailing commas) : ,] ou ,}
    try:
        repare = re.sub(r",\s*([\]}])", r"\1", texte)
        return json.loads(repare)
    except Exception:
        pass
    # 3) JSON tronqué : tenter de fermer le tableau "matchs" au dernier match complet.
    #    On coupe après la dernière accolade fermante d'un objet match, puis on ferme.
    try:
        # Trouver la dernière occurrence d'un objet match complet "}"
        dernier = texte.rstrip().rfind("}")
        if dernier != -1:
            tronque = texte[:dernier + 1]
            # Fermer le tableau matchs et l'objet racine
            tronque += "]}"
            repare = re.sub(r",\s*([\]}])", r"\1", tronque)
            data = json.loads(repare)
            if data.get("matchs"):
                logging.warning(
                    f"JSON Gemini tronqué réparé — {len(data['matchs'])} match(s) récupéré(s)."
                )
                return data
    except Exception:
        pass
    return None


def collecter_donnees_tennis(date, heure, calendrier_injecte, rapid_matchs=None, heure_fin="23:59", tournois_winamax=None):
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

    # Liste des tournois Winamax pour guider la recherche de secours
    liste_tournois = ", ".join(tournois_winamax) if tournois_winamax else "Grand Chelem, ATP/WTA 250 à 1000"

    if calendrier_injecte:
        # Cas normal : calendrier déjà fourni par les API → Gemini enrichit seulement
        bloc = (
            f"{calendrier_injecte}\n\n"
            f"→ Calendrier COMPLET avec H2H et forme pré-collectés. Ne pas les re-vérifier.\n"
            f"→ IMPORTANT : Transmettre TOUS les matchs avec cotes disponibles — ne pas filtrer par heure.\n"
            f"→ ⏰ HEURES AUTORITAIRES — RÈGLE ABSOLUE : les heures du calendrier ci-dessus sont\n"
            f"   DÉJÀ en heure française correcte (converties depuis la source officielle).\n"
            f"   Tu DOIS recopier EXACTEMENT l'heure indiquée pour chaque match dans heure_match.\n"
            f"   ⛔ INTERDIT de modifier, recalculer, décaler ou 'corriger' une heure.\n"
            f"   ⛔ INTERDIT d'inventer une heure. Si un match du calendrier indique 16:05,\n"
            f"      tu écris heure_match '16:05'. Pas 18:00, pas 14:40. EXACTEMENT 16:05.\n"
            f"   Pour un match SANS heure dans le calendrier, cherche-la sur Winamax/Sportytrader\n"
            f"   (heure française) — ne l'invente JAMAIS.\n"
            f"→ Claude se chargera du filtrage par fenêtre horaire ({heure} → {heure_fin}).\n"
            f"→ Tes requêtes Google : UNIQUEMENT blessures, contexte psychologique, Hold%."
        )
    else:
        # SECOURS : aucune source API n'a fourni de calendrier (RapidAPI mort + Odds API vide).
        # Gemini cherche lui-même le calendrier des tournois Winamax sur le web.
        bloc = (
            f"⚠️ AUCUN calendrier pré-collecté (sources API indisponibles).\n"
            f"MISSION SPÉCIALE : cherche TOI-MÊME les matchs du {date} entre {heure} et {heure_fin}.\n\n"
            f"🗓️ RÈGLE DE DATE ABSOLUE — LA PLUS IMPORTANTE :\n"
            f"Tu ne retiens QUE les matchs qui se jouent RÉELLEMENT le {date} (date du jour).\n"
            f"⛔ INTERDIT de remonter un match programmé un AUTRE jour (demain, lundi, cette semaine).\n"
            f"   Exemple d'ERREUR GRAVE : un tournoi commence dans 3 jours → tu remontes ses matchs\n"
            f"   du 1er tour en les datant d'aujourd'hui. C'est FAUX. Ces matchs ne se jouent PAS le {date}.\n"
            f"Pour CHAQUE match, VÉRIFIE la date exacte sur la source : si ce n'est pas le {date},\n"
            f"tu l'IGNORES totalement. Mieux vaut 0 match qu'un match à la mauvaise date.\n"
            f"Si un tournoi de la liste n'a AUCUN match le {date} (pas encore commencé, jour de repos),\n"
            f"tu écris simplement qu'il n'a pas de match aujourd'hui et tu passes au suivant.\n"
            f"⛔ NE JAMAIS inventer un match pour 'remplir' un tournoi sans match aujourd'hui.\n\n"
            f"TOURNOIS À COUVRIR (uniquement ceux couverts par Winamax) :\n{liste_tournois}\n\n"
            f"⚠️ MÉTHODE OBLIGATOIRE — EXHAUSTIVITÉ TOURNOI PAR TOURNOI :\n"
            f"Tu DOIS traiter CHAQUE tournoi de la liste ci-dessus SÉPARÉMENT, un par un.\n"
            f"Pour CHAQUE tournoi, fais une recherche DÉDIÉE (ex: 'Eastbourne ATP ordre du jour {date}',\n"
            f"puis 'Bad Homburg WTA programme {date}', puis 'Wimbledon qualifications WTA {date}'...).\n"
            f"NE T'ARRÊTE PAS après quelques matchs : un tournoi peut avoir 4 à 8 matchs par jour.\n"
            f"Liste TOUS les matchs simples de CHAQUE tournoi DU JOUR {date}, sans en oublier aucun.\n"
            f"Objectif : ne manquer AUCUN match jouable. Mieux vaut 20 matchs listés que 8.\n\n"
            f"Pour CHAQUE match trouvé :\n"
            f"1. Match simple uniquement (PAS doubles). Qualifs : SEULEMENT si Grand Chelem.\n"
            f"2. ⏰ HEURE EN HEURE FRANÇAISE OBLIGATOIRE (fuseau Europe/Paris) :\n"
            f"   → L'heure du match DOIT être convertie en heure française (Paris).\n"
            f"   → ATTENTION : beaucoup de sites donnent l'heure en UTC/GMT. La France est\n"
            f"     à UTC+2 en été. Un match affiché 13:00 UTC = 15:00 heure française.\n"
            f"   → Vérifie sur Winamax ou Sportytrader (sites FR, déjà en heure française).\n"
            f"   → Indique l'heure française dans heure_match au format HH:MM.\n"
            f"3. Trouve la COTE WINAMAX RÉELLE :\n"
            f"   → coteur.com/cotes-tennis en priorité (spécialisé bookmakers français, affiche Winamax)\n"
            f"   → puis flashscore.fr (onglet cotes) ou sportytrader.com/fr/cotes/tennis/\n"
            f"   → Si cote Winamax trouvée → source_cote = 'Winamax (Coteur/Flashscore/Sportytrader)'\n"
            f"   → Si seulement une autre cote bookmaker EU → source_cote = nom du bookmaker\n"
            f"   → Si AUCUNE cote réelle → source_cote = 'non trouvée' (le match sera écarté)\n"
            f"⛔ INTERDIT d'inventer ou d'estimer une cote. Cote RÉELLE lue sur le site UNIQUEMENT.\n"
            f"4. PRIORITÉ À L'EXHAUSTIVITÉ : liste d'abord TOUS les matchs avec leur cote.\n"
            f"   Ajoute Hold%, forme, H2H ensuite SI tu as des recherches restantes — mais ne\n"
            f"   sacrifie JAMAIS un match entier pour enrichir les stats d'un autre.\n"
        )

    prompt = f"""
Tu es un agent de collecte tennis. Date : {date}. Heure : {heure} France.

MISSION : Enrichir les données avec stats, H2H et contexte.
{'Tu NE cherches PAS le calendrier — il est fourni ci-dessous.' if calendrier_injecte else 'Tu DOIS chercher le calendrier toi-même (voir mission spéciale ci-dessous).'}

{bloc}

RECHERCHES — MÉTHODE SYSTÉMATIQUE MATCH PAR MATCH :

⚠️ MÉTHODE OBLIGATOIRE — EXTRACTION STAT PAR STAT, MATCH PAR MATCH :
Procède EXACTEMENT comme pour le calendrier : avec méthode et exhaustivité.
Pour CHAQUE match retenu dans la fenêtre, traite-le INDIVIDUELLEMENT et remplis
sa fiche stat par stat. Ne passe PAS au match suivant tant que tu n'as pas tenté
de remplir les 4 stats prioritaires du match courant. Ne te contente JAMAIS de ce
que tu trouves vite : fais une recherche DÉDIÉE par donnée manquante.
Un match avec stats complètes permet à l'analyste de décider ; un match sans stats
est inexploitable. La qualité des stats = la qualité de l'analyse finale.

PRIORITÉ 1 — Pour CHAQUE match, faire une recherche dédiée par donnée, dans l'ordre :
1. Hold% des DEUX joueurs (donnée la plus importante) :
   → tennisabstract.com OU ultimatetennisstatistics.com OU tennisratio.com OU matchstat.com
   → Chercher : "Prénom Nom hold% tennisabstract" ou "Nom serve stats UTS"
   → Recherche SÉPARÉE pour J1 puis pour J2 si besoin — ne pas se contenter d'un seul
   → Si introuvable après recherche réelle → "non trouvé" (ne PAS inventer)

2. Forme récente des DEUX joueurs (5 derniers matchs, surface incluse) :
   → flashscore.fr OU sofascore.com OU matchstat.com
   → Si déjà fourni dans le calendrier → NE PAS re-chercher
   → Chercher la forme sur GAZON en priorité (surface actuelle)

3. H2H direct entre les deux joueurs (confrontations passées + surface) :
   → flashscore.fr OU tennisabstract.com OU atptour.com/wtatennis.com
   → Recherche dédiée "Joueur1 vs Joueur2 head to head"
   → Si première confrontation → l'indiquer explicitement (pas "non trouvé")

4. Stats avancées par surface si disponibles (Break%, Return%, Win% gazon) :
   → ultimatetennisstatistics.com OU tennisabstract.com

PRIORITÉ 2 — Contexte (1 recherche globale chacun, pas par match) :
5. Blessures/forfaits du jour :
   → eurosport.fr OU tennis.com OU atptour.com
6. Points ATP/WTA à défendre + classement actuel :
   → atptour.com OU wtatennis.com
7. Charge physique — heures jouées 72h, matchs enchaînés :
   → flashscore.fr OU sofascore.com

PRIORITÉ 3 — Vérification cotes matchs secondaires :
7. Pour tout match SANS cote dans le calendrier, chercher la cote Winamax réelle sur :
   → coteur.com/cotes-tennis (spécialisé bookmakers français ANJ, affiche Winamax) — SOURCE PRIORITAIRE
   → flashscore.fr (onglet cotes, compare Winamax aux concurrents)
   → sportytrader.com/fr/cotes/tennis/
   → Cote Winamax trouvée → source_cote = "Coteur"/"Flashscore"/"Sportytrader" (selon la source réelle)
   → Introuvable → source_cote = "non trouvée" (NE JAMAIS inventer une cote)

SOURCES PAR TYPE (utilise ces sites réels — ne jamais inventer une valeur non trouvée) :
• Hold% / Break% / Return%  → tennisabstract.com · ultimatetennisstatistics.com · tennisratio.com · matchstat.com
• Forme récente + scores    → flashscore.fr · sofascore.com · matchstat.com
• Stats par surface (Elo)   → tennisabstract.com (Elo par surface) · ultimatetennisstatistics.com
• Momentum / forme actuelle → tennisabstract.com (Elo surface récent) · predixsport.com (Form Index : mesure objective de qui surperforme en ce moment) · tennisratio.com (pressure points = solidité mentale dans les moments clés)
• Challengers / Futures     → tennisexplorer.com (couvre les circuits secondaires mieux que les autres)
• Classements + points      → atptour.com · wtatennis.com
• Blessures + actualités    → eurosport.fr · tennis.com · atptour.com
• Cotes Winamax             → coteur.com (priorité, marché français) · flashscore.fr · sportytrader.com

⚠️ RÈGLE DE FIABILITÉ DES SOURCES :
Pour CHAQUE donnée (cote ou stat), tu dois l'avoir RÉELLEMENT lue sur un des sites ci-dessus.
Si une donnée n'est pas trouvée sur ces sources → tu écris "non trouvé" pour ce champ.
⛔ Il est FORMELLEMENT INTERDIT d'estimer, deviner ou inventer une cote ou une statistique.
Une donnée inventée est pire qu'une donnée absente : "non trouvé" permet à Claude de s'adapter,
une valeur fausse le trompe. Dans le doute, écris toujours "non trouvé".

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
    "date_match": "JJ/MM/AAAA (date réelle du match — DOIT être {date}, sinon ne pas inclure)",
    "heure_match": "HH:MM (HEURE FRANÇAISE Europe/Paris — jamais UTC)",
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

    logging.info(f"Gemini enrichit les données — {date} {heure}…")

    # Boucle de tentatives : couvre à la fois les erreurs réseau/503 ET les
    # JSON invalides. Un JSON cassé (fréquent quand la réponse est longue =
    # beaucoup de matchs Wimbledon) déclenche un nouvel essai AVANT de tomber
    # sur le relais limité — ça évite de perdre les 3/4 des matchs.
    derniere_erreur = None
    for tentative in range(1, 4):
        try:
            rep = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.1,
                ),
            )
        except Exception as e:
            derniere_erreur = e
            msg = str(e)
            # Erreurs transitoires (serveur indisponible, déconnexion) → retry
            if any(x in msg for x in ["503", "UNAVAILABLE", "Server disconnected",
                                       "disconnected", "timeout", "Timeout"]):
                logging.warning(f"Gemini erreur transitoire tentative {tentative}/3 ({msg[:60]}) — retry dans {tentative * 15}s…")
                time.sleep(tentative * 15)
                continue
            else:
                logging.error(f"Erreur Gemini non-transitoire : {e}")
                return '{"matchs": [], "avertissements": "Erreur Gemini."}'

        # Réponse reçue → parser (avec réparation si légèrement malformé)
        texte = rep.text.strip()
        texte = re.sub(r"^```json\s*", "", texte)
        texte = re.sub(r"\s*```$", "", texte)
        data = _reparer_json_gemini(texte)
        if data is not None:
            logging.info(f"Gemini OK — {len(data.get('matchs', []))} match(s).")
            return json.dumps(data, ensure_ascii=False, indent=2)
        # JSON invalide et irréparable → réessayer un appel complet (pas le relais)
        logging.warning(
            f"Gemini JSON invalide tentative {tentative}/3 — nouvel essai "
            f"(évite de perdre le calendrier complet au profit du relais limité)."
        )
        time.sleep(3)

    # Les 3 tentatives ont échoué (réseau ou JSON) → on laisse le relais prendre le relais
    logging.error(f"Gemini : 3 tentatives échouées (dernière erreur : {derniere_erreur}).")
    return '{"matchs": [], "avertissements": "Erreur Gemini après 3 tentatives."}'

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
• Qualifications de PETITS tournois (hors Grand Chelem) → skip
• Qualifications de GRAND CHELEM (Wimbledon Q, Roland-Garros Q, US Open Q, Australian Open Q)
  → AUTORISÉES si cote Winamax réelle fournie. Voir section QUALIFS GRAND CHELEM plus bas.
• Hors tableau principal d'un petit tournoi → skip
• Cote "non trouvée" → skip automatique
• Match en doublon (même joueurs, heure différente) → garder uniquement le plus récent dans la fenêtre (pas de marché = impossible à jouer sur Winamax)

⛔ INTERDICTION ABSOLUE — COTES INVENTÉES :
• Tu n'as le DROIT d'utiliser QUE les cotes EXACTES fournies dans les données (cote_j1, cote_j2).
• Il est STRICTEMENT INTERDIT d'estimer, deviner ou inventer une cote.
• Si une cote n'est PAS fournie pour un marché → tu NE PEUX PAS jouer ce marché. Point final.
• Si tu écris "cote estimée" ou "non disponible précisément sur Winamax" → c'est une VIOLATION. Ne génère PAS ce ticket.
• Marchés handicap/over-under/score : utilise-les UNIQUEMENT si leur cote exacte est dans les données.
  Sinon → reste sur le Moneyline avec sa cote réelle, ou passe au match suivant.
• Le calcul de value DOIT se baser sur la cote réelle Winamax, jamais sur une estimation.

✅ MARCHÉS ALTERNATIFS AUTORISÉS (si cote réelle fournie) :
• Quand un match affiche "Marchés Winamax réels → Écart de jeux X / Total X jeux", ces cotes sont EXACTES et JOUABLES.
• Tu PEUX jouer un écart de jeux ou un total de jeux SI sa cote est explicitement listée.

📋 MARCHÉS WINAMAX TENNIS — NOMS EXACTS À UTILISER :
• "Vainqueur" → Moneyline (qui gagne le match)
• "Écart de jeux" → Outsider +X.5 jeux / Favori -X.5 jeux (PAS "handicap de jeux")
• "Écart de set" → handicap sets, ex: +1.5 / -1.5 sets
• "Score exact" → score en sets, ex: 2-0, 2-1
• "Nombre de jeux" → Over/Under total de jeux, ex: +21.5 jeux
• "Nombre de sets" → Over/Under sets, ex: +2.5 sets

⚠️ RÈGLES DE NOMMAGE STRICTES :
• Pour un handicap de jeux → écris "Écart de jeux" (jamais "handicap de jeux" qui n'existe pas).
• Pour un handicap de sets → écris "Écart de set".
• Respecte EXACTEMENT la ligne : si c'est +3.5, écris +3.5 (jamais +4.5).
• Exemple correct : "Alexandrova +3.5 jeux (écart de jeux)" à cote réelle 1.84.
• Exemple correct : "Over 21.5 jeux (nombre de jeux)" à cote réelle 1.90.
• Ces marchés offrent souvent plus de value que le Moneyline — exploite-les quand la cote réelle est fournie.
• ⚠️ N'utilise QUE les cotes réelles fournies — jamais d'estimation.

CALIBRATION PROBABILITÉS :
• Cote < 1.50  → MAX 75%
• 1.50-1.80    → MAX 68%
• 1.80-2.20    → MAX 58%
• > 2.20       → MAX 52%

🚫 RÈGLE ANTI-SURESTIMATION — DONNÉES MANQUANTES (CRITIQUE) :
Constat vérifié sur résultats réels : quand il manque des données importantes
(stats sur la surface du match, Hold% sur la surface, forme récente, H2H), le
modèle a tendance à combler le vide par une analyse narrative (forme générale,
"momentum", contexte psychologique) et à SURESTIMER un joueur — ce qui crée de
faux deltas et fait perdre.

RÈGLE ABSOLUE :
• Si tu n'as PAS les données de surface (ex: Hold%/forme/win% sur gazon pour un
  match sur gazon) d'un joueur, tu n'as PAS le droit d'estimer sa probabilité
  AU-DESSUS de sa probabilité de marché (= 1/cote réelle).
• Sans données suffisantes pour JUSTIFIER un écart, tu t'ALIGNES sur le marché.
• Tu ne survalorises JAMAIS un joueur sur la seule base d'arguments narratifs
  (momentum, forme générale toutes surfaces, contexte psychologique, "vulnérable
  sur cette surface" sans stats chiffrées à l'appui).
• Concrètement : si "données manquantes" inclut les stats de la surface du match
  pour un joueur, alors proba_estimée de CE joueur ≤ proba_marché (1/cote).
  → Résultat : pas d'écart au marché → pas de faux delta → souvent pas de pari.
• Tu PEUX t'écarter du marché et survaloriser un joueur UNIQUEMENT si tu as les
  données chiffrées de surface qui le justifient (comme pour un favori bien documenté).
Cette règle vaut pour TOUTES les surfaces (gazon, terre, dur) et tous les tournois.

📈 MOMENTUM OBJECTIF — NE PAS SE FIER QU'À L'HISTORIQUE (important) :
Un joueur peut être BIEN plus fort que ses stats de saison ne le suggèrent s'il est
dans un grand moment de carrière (titre récent, finale récente, série de victoires
contre du haut niveau). Inversement, un favori "sur le papier" peut être en déclin.
Pour capter cela SANS tomber dans la narration subjective, utilise des mesures OBJECTIVES :
• L'Elo par surface RÉCENT (tennisabstract) : reflète la vraie force actuelle, pas la réputation.
• Le Form Index (predixsport) : mesure chiffrée de qui surperforme en ce moment.
RÈGLE : si l'adversaire d'un favori a un Elo surface récent proche ou un Form Index élevé
(il "monte"), alors le favori est MOINS sûr que sa cote ne le suggère → n'augmente PAS
sa probabilité, reste au niveau du marché ou en-dessous. Un "favori sur réputation" contre
un joueur en pleine forme est un piège classique (ex : un ancien favori surface battu par
un joueur qui vient de gagner un grand titre).
⚠️ Ceci n'est PAS une autorisation à survaloriser l'outsider : c'est une raison d'être
PLUS PRUDENT sur le favori. Dans le doute entre deux joueurs de bon niveau (typique en
phase finale de gros tournoi), s'aligner sur le marché plutôt que de surestimer le favori.

🎓 QUALIFS GRAND CHELEM — RÈGLES SPÉCIALES :
Les qualifs de Grand Chelem (Wimbledon Q, etc.) sont jouables MAIS plus risquées :
joueurs moins connus, données plus pauvres, résultats plus volatils.
• CALIBRATION PRUDENTE (plafonds abaissés) :
  - Cote < 1.50  → MAX 60% (au lieu de 75%)
  - 1.50-1.80    → MAX 55%
  - 1.80-2.20    → MAX 50%
  - > 2.20       → MAX 45%
• EXIGENCE DE DONNÉES RENFORCÉE : si Hold%, forme OU H2H manquent pour les DEUX
  joueurs → données insuffisantes → NE PAS jouer ce match. Mieux vaut s'abstenir
  que parier à l'aveugle sur un joueur obscur.
• MISES QUALIF (réduites — phase de test de ce segment) :
  - Qualif confiance ÉLEVÉE  → 0,75%
  - Qualif confiance MODÉRÉE → 0,50%
  - Qualif confiance BASSE   → 0,25%
• Dans le ticket d'une qualif, indique CONFIANCE "QUALIF ÉLEVÉE/MODÉRÉE/BASSE"
  (le mot QUALIF est OBLIGATOIRE pour le suivi statistique séparé).
• Cote Winamax réelle TOUJOURS obligatoire (jamais d'estimation).

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

⚠️⚠️⚠️ RÈGLE ABSOLUE — FORMAT DE SORTIE — VIOLATION = ÉCHEC ⚠️⚠️⚠️
Ta réponse doit commencer DIRECTEMENT par 🔴 ou par AUCUN_MATCH.
ZÉRO tolérance pour tout texte avant le 🔴 ou AUCUN_MATCH.

EXEMPLES DE CE QUI EST INTERDIT (ne JAMAIS écrire) :
❌ "Let me analyze..." 
❌ "**FILTERING:**"
❌ "ÉTAPE 1..."
❌ "Voici mon analyse..."
❌ "Remaining matches..."
❌ Tout texte, titre, bullet point AVANT le 🔴

SEULES RÉPONSES VALIDES :
✅ 🎾 <b>ACEANALYTICS TENNIS...</b>  (si value trouvée)
✅ AUCUN_MATCH — [explication courte]  (si aucune value)

L'analyse est STRICTEMENT INTERNE — invisible — jamais dans la réponse.
Si tu commences par autre chose que 🔴 ou AUCUN_MATCH → tu as échoué.

ANALYSE EN 2 ÉTAPES (INTERNE — NE PAS AFFICHER) :

[1] FACTEURS BRUTS :
  Surface + forme 5 matchs + charge 72h + Hold% + H2H par surface
  Contexte psychologique : points à défendre, public local, GC dans 7j
  Fatigue : match long hier, titre récent, 3 matchs en 5j

[2] DÉCISION :
  Proba % → Cote Juste = 1/proba → Delta = Cote réelle - Cote Juste

  ⚖️ SEUIL DE DELTA ADAPTATIF SELON LA COTE (nouveau — en test) :
  Le delta minimum requis dépend de la cote, car un delta absolu de 0.10 est
  plus dur à atteindre sur un favori que sur un outsider (question de proportion).
  • Cote < 1.90 (favori)        → delta minimum requis : 0.07
  • Cote 1.90 à 2.50 (équilibré) → delta minimum requis : 0.10
  • Cote > 2.50 (outsider)       → delta minimum requis : 0.12
  Raison : on facilite les favoris modérés (zone historiquement rentable) et on
  durcit les outsiders (zone plus risquée). Applique le seuil correspondant à la
  cote réelle du pari.

  Delta < seuil requis → ❌ abandonné
  Delta ≥ seuil requis → VALUE ✅ → Kelly quart = ((p×c−1)/(c−1))×0.25
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
Qualif GC : ÉLEVÉE 0,75% · MODÉRÉE 0,50% · BASSE 0,25%

FORMAT (max {MAX_TICKETS} tickets, [SEPARATEUR] entre chaque) :
⚠️⚠️ RÈGLE ABSOLUE — INTERDICTION D'AFFICHER UN TICKET ABANDONNÉ ⚠️⚠️
Si Delta < seuil requis (selon la cote : 0.07 favori / 0.10 équilibré / 0.12 outsider)
→ le match est ABANDONNÉ → tu NE génères AUCUN ticket pour lui.
Tu ne dois JAMAIS écrire un bloc 🎾 PRONOSTIC avec un delta ❌ ou la mention "Abandon".
Un ticket affiché = un pari validé que l'abonné va jouer. Afficher un ticket abandonné
trompe l'abonné qui va miser sur un mauvais pari. C'est une FAUTE GRAVE.

PROCESSUS CORRECT :
• Delta ≥ seuil requis + analyse OK → générer le ticket 🎾 (envoyé tel quel à l'abonné)
• Delta < seuil requis → NE RIEN générer pour ce match, passer au suivant
• Si AUCUN match n'atteint son seuil → répondre UNIQUEMENT : AUCUN_MATCH + explication

N'envoyer QUE les tickets validés (Delta ≥ 0.10 + analyse confirmée).
Si 0 ticket validé → répondre : AUCUN_MATCH suivi d'une explication courte (max 80 mots) :
  · Citer 1-2 matchs analysés avec le joueur favori et pourquoi pas de value
  · Mentionner le delta et la raison principale (cote trop basse, données insuffisantes)
  · Ton naturel et direct, comme un analyste qui explique à ses abonnés
  Exemple : "AUCUN_MATCH — Fery favori sur gazon mais cote 1.40 trop basse (delta +0.07 insuffisant). Andreeva domine mais cote 1.42 sans value. Cotes trop serrées aujourd'hui."
La limite de {MAX_TICKETS} tickets est un PLAFOND, pas un objectif.
1 ticket excellent vaut mieux que 5 tickets moyens.
HTML uniquement <b>texte</b>. JAMAIS **texte**. POURQUOI max 60 mots.

🎾 <b>ACEANALYTICS TENNIS — PRONOSTIC [SIMPLE/COMBINÉ]</b> 🎾
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

def filtrer_matchs_par_fenetre(rapid_matchs, heure_debut, heure_fin):
    """
    Pré-filtre les matchs par fenêtre horaire AVANT l'envoi à Gemini.
    Concentre les requêtes Gemini sur les matchs pertinents de la session.
    Gère le passage minuit (session soir).
    """
    if not rapid_matchs:
        return rapid_matchs

    fenetre_nocturne = heure_fin < heure_debut  # ex: 19:30 → 05:00

    def _heure_match(m):
        h = m.get("heure", "")
        # Extraire HH:MM depuis "2026-06-23 16:30 UTC" ou "16:30"
        match = re.search(r"(\d{2}):(\d{2})", str(h))
        return match.group(0) if match else None

    matchs_filtres = []
    for m in rapid_matchs:
        hm = _heure_match(m)
        if not hm:
            # Heure inconnue → garder par sécurité (Claude filtrera)
            matchs_filtres.append(m)
            continue

        if fenetre_nocturne:
            # Match dans la fenêtre si après début OU avant fin (passe minuit)
            if hm >= heure_debut or hm <= heure_fin:
                matchs_filtres.append(m)
        else:
            if heure_debut <= hm <= heure_fin:
                matchs_filtres.append(m)

    logging.info(f"Filtrage horaire : {len(matchs_filtres)}/{len(rapid_matchs)} matchs dans la fenêtre {heure_debut}→{heure_fin}.")
    return matchs_filtres


# =====================================================================
# 12. ORCHESTRATION
# =====================================================================

def filtrer_json_par_fenetre(donnees_json, heure_debut, heure_fin, date_jour=None):
    """
    Filtre DUR et déterministe : retire du JSON Gemini les matchs dont
    heure_match est hors de la fenêtre [heure_debut, heure_fin], ET les matchs
    dont la date_match n'est pas celle du jour (anti-hallucination de date :
    empêche de remonter un match de lundi en le datant d'aujourd'hui).
    Gère le passage minuit (session SOIR, ex: 22:50 → 05:00).
    Indépendant de Claude — garantit qu'aucun match déjà joué ni à la mauvaise date n'est proposé.
    """
    try:
        data = json.loads(donnees_json)
    except Exception:
        return donnees_json

    matchs = data.get("matchs", [])
    if not matchs:
        return donnees_json

    # FILTRE DE DATE : retirer tout match dont la date n'est pas celle du jour.
    # Protège contre le bug où Gemini remonte des matchs d'un autre jour
    # (ex: 1er tour Wimbledon programmé lundi, daté à tort d'aujourd'hui).
    if date_jour:
        def _bonne_date(m):
            dm = str(m.get("date_match", "")).strip()
            if not dm:
                return True  # date absente → on ne filtre pas ici (prudence), l'heure filtrera
            # Normaliser : extraire JJ/MM/AAAA
            md = re.search(r"(\d{2})[/-](\d{2})[/-](\d{4})", dm)
            if not md:
                return True  # format inattendu → garder, ne pas casser
            date_normalisee = f"{md.group(1)}/{md.group(2)}/{md.group(3)}"
            return date_normalisee == date_jour
        avant = len(matchs)
        matchs = [m for m in matchs if _bonne_date(m)]
        retires_date = avant - len(matchs)
        if retires_date:
            logging.warning(
                f"Filtre DATE : {retires_date} match(s) à une date ≠ {date_jour} retiré(s) "
                f"(probable hallucination de date par Gemini)."
            )

    fenetre_nocturne = heure_fin < heure_debut  # ex: 22:50 → 05:00

    def _dans_fenetre(hm):
        m = re.search(r"(\d{2}):(\d{2})", str(hm))
        if not m:
            return True  # heure inconnue → garder (prudence)
        h = m.group(0)
        if fenetre_nocturne:
            return h >= heure_debut or h <= heure_fin
        return heure_debut <= h <= heure_fin

    gardes = [m for m in matchs if _dans_fenetre(m.get("heure_match", ""))]
    retires = len(matchs) - len(gardes)
    if retires:
        logging.info(f"Filtre fenêtre JSON : {retires} match(s) hors {heure_debut}→{heure_fin} retiré(s), {len(gardes)} gardé(s).")
    data["matchs"] = gardes
    return json.dumps(data, ensure_ascii=False, indent=2)


# ============================================================
# MARCHÉS ALTERNATIFS — récupération cotes OddsPapi/Pinnacle
# (Total Sets O/U 2.5 + Total Games O/U). Protégé par MARCHES_ALT_MODE.
# ============================================================

def _oddspapi_headers():
    return {"X-RapidAPI-Key": RAPIDAPI_KEY or "", "X-RapidAPI-Host": ODDSPAPI_HOST}


def _est_faux_tennis(fixture):
    """Exclut le SRL (Simulated Reality) = faux tennis simulé par ordinateur."""
    t = fixture.get("tournament", {}) or {}
    cat = str(t.get("categoryName", "")).lower()
    nom = str(t.get("tournamentName", "")).lower()
    if any(x in cat for x in CATEGORIES_INTERDITES):
        return True
    if "srl" in nom or "simulated" in nom:
        return True
    p1 = str(fixture.get("participants", {}).get("participant1Name", "")).lower()
    return "(srl)" in p1


def _sim_noms(a, b):
    from difflib import SequenceMatcher
    # Normaliser : retirer virgules, mettre en minuscules, trier les mots
    # (gère "Karolina Muchova" vs "Muchova, Karolina" — même mots, ordre différent)
    def norm(x):
        x = str(x).lower().replace(",", " ").replace("-", " ")
        mots = sorted(w for w in x.split() if w)
        return " ".join(mots)
    na, nb = norm(a), norm(b)
    # Score direct sur noms normalisés (mots triés)
    score = SequenceMatcher(None, na, nb).ratio()
    # Bonus : si le nom de famille (mot le plus long) est présent des deux côtés
    mots_a = set(na.split())
    mots_b = set(nb.split())
    communs = mots_a & mots_b
    if communs:
        # proportion de mots communs
        score = max(score, len(communs) / max(len(mots_a), len(mots_b)))
    return score


def oddspapi_fixtures_jour():
    """Récupère les vrais matchs tennis du jour (SRL exclu). [] si erreur."""
    try:
        r = requests.get(f"https://{ODDSPAPI_HOST}/fixtures/today",
                         headers=_oddspapi_headers(), params={"sportId": 12}, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logging.warning(f"OddsPapi fixtures indisponible : {e}")
        return []
    fixtures = data if isinstance(data, list) else data.get("fixtures", data.get("data", []))
    if not isinstance(fixtures, list):
        return []
    vrais = [f for f in fixtures if not _est_faux_tennis(f)]
    exclus = len(fixtures) - len(vrais)
    if exclus:
        logging.info(f"OddsPapi : {exclus} match(s) SRL exclu(s), {len(vrais)} vrai(s).")
    return vrais


def oddspapi_trouver_fixture(joueur1, joueur2, fixtures):
    """Trouve le fixtureId correspondant à un match (par noms, tolère les variantes)."""
    meilleur, score_max = None, 0.0
    for f in fixtures:
        p = f.get("participants", {})
        n1, n2 = str(p.get("participant1Name", "")), str(p.get("participant2Name", ""))
        direct  = (_sim_noms(joueur1, n1) + _sim_noms(joueur2, n2)) / 2
        inverse = (_sim_noms(joueur1, n2) + _sim_noms(joueur2, n1)) / 2
        score = max(direct, inverse)
        if score > score_max:
            score_max, meilleur = score, f.get("fixtureId")
    return meilleur if score_max >= 0.7 else None


def oddspapi_cotes(fixture_id):
    """
    Récupère les cotes Pinnacle des marchés alternatifs d'un match.
    Retourne : {"total_sets": {"over":x,"under":y,"ligne":2.5},
                "total_games": {"over":x,"under":y,"ligne":21.5}}
    """
    try:
        r = requests.get(f"https://{ODDSPAPI_HOST}/fixtures/odds",
                         headers=_oddspapi_headers(),
                         params={"fixtureId": fixture_id, "bookmakers": ODDSPAPI_BOOKMAKER},
                         timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logging.warning(f"OddsPapi cotes {fixture_id} : {e}")
        return {}

    # MODE OBSERVATION : loguer le format brut pour validation
    if MARCHES_ALT_MODE == "observation":
        apercu = json.dumps(data, ensure_ascii=False)[:1500]
        logging.info(f"[OBS] Format réponse OddsPapi odds : {apercu}")

    outcomes = _oddspapi_extraire_outcomes(data)
    res = {}
    for o in outcomes:
        bo = str(o.get("bookmakerOutcomeId", ""))
        price = o.get("price")
        if price is None:
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        m = re.match(r"(\d+(?:\.\d+)?)/(over|under)", bo)
        if not m:
            continue
        ligne, sens = float(m.group(1)), m.group(2)
        if ligne == 2.5:  # Total Sets
            res.setdefault("total_sets", {"ligne": 2.5})[sens] = price
        elif ligne >= 10:  # Total Games (lignes hautes)
            tg = res.setdefault("total_games", {})
            is_main = o.get("mainLine", False)
            if is_main or "ligne" not in tg:
                tg["ligne"], tg[sens] = ligne, price
            elif tg.get("ligne") == ligne:
                tg[sens] = price
    return res


def _oddspapi_extraire_outcomes(data):
    """Normalise la réponse OddsPapi en liste d'outcomes (price + bookmakerOutcomeId)."""
    out = []
    def _collecte(obj):
        if isinstance(obj, dict):
            if "price" in obj and "bookmakerOutcomeId" in obj:
                out.append(obj)
            else:
                for v in obj.values():
                    _collecte(v)
        elif isinstance(obj, list):
            for v in obj:
                _collecte(v)
    _collecte(data)
    return out


def analyser_marches_alternatifs(matchs_serres, date):
    """
    Pour chaque match serré, récupère les cotes sets/jeux Pinnacle et (si mode actif)
    demande à Claude d'estimer s'il y a de la value sur "Over 2.5 sets" ou Total Games.
    Retourne une liste de tickets marchés alternatifs (vide en mode observation/off).

    matchs_serres : liste de dicts {joueur1, joueur2, cote_j1, cote_j2, surface, ...}
    """
    if MARCHES_ALT_MODE == "off":
        return []
    if not RAPIDAPI_KEY:
        logging.info("Marchés alt : pas de clé RapidAPI, skip.")
        return []

    fixtures = oddspapi_fixtures_jour()
    if not fixtures:
        logging.info("Marchés alt : aucun fixture OddsPapi disponible.")
        return []

    tickets_alt = []
    for m in matchs_serres:
        j1, j2 = m.get("joueur1", ""), m.get("joueur2", "")
        fid = oddspapi_trouver_fixture(j1, j2, fixtures)
        if not fid:
            if MARCHES_ALT_MODE == "observation":
                # Diagnostic : montrer les noms OddsPapi les plus proches pour comprendre l'échec
                proches = []
                for f in fixtures[:400]:
                    p = f.get("participants", {})
                    n1 = str(p.get("participant1Name", ""))
                    n2 = str(p.get("participant2Name", ""))
                    s = max((_sim_noms(j1, n1) + _sim_noms(j2, n2)) / 2,
                            (_sim_noms(j1, n2) + _sim_noms(j2, n1)) / 2)
                    proches.append((s, f"{n1} vs {n2}"))
                proches.sort(reverse=True)
                top = proches[0] if proches else (0, "aucun")
                logging.info(f"[OBS] Pas de correspondance pour '{j1} vs {j2}' — meilleur candidat OddsPapi : '{top[1]}' (score {top[0]:.2f})")
            continue
        cotes = oddspapi_cotes(fid)
        if not cotes:
            if MARCHES_ALT_MODE == "observation":
                logging.info(f"[OBS] Match trouvé (fid={fid}) pour {j1} vs {j2} mais AUCUNE cote alt récupérée (match commencé ? format ?).")
            continue

        # MODE OBSERVATION : on logue ce qu'on a trouvé, sans parier
        if MARCHES_ALT_MODE == "observation":
            ts = cotes.get("total_sets", {})
            tg = cotes.get("total_games", {})
            logging.info(
                f"[OBS] {j1} vs {j2} — "
                f"Sets O/U 2.5: Over {ts.get('over','?')}/Under {ts.get('under','?')} | "
                f"Games O/U {tg.get('ligne','?')}: Over {tg.get('over','?')}/Under {tg.get('under','?')}"
            )
            continue

        # MODE ACTIF : demander à Claude d'estimer la value (à implémenter après validation)
        # Pour l'instant, en mode actif, on prépare la structure mais on reste prudent :
        # on ne génère un ticket que si la logique d'estimation est en place.
        # (Cette partie sera complétée une fois le format validé en observation.)
        logging.info(f"[ACTIF] Marchés alt prêts pour {j1} vs {j2} — estimation à implémenter.")

    return tickets_alt


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
    # Pré-filtrage horaire AVANT enrichissement — concentre les requêtes sur la fenêtre
    rapid_matchs       = filtrer_matchs_par_fenetre(rapid_matchs, heure, heure_fin)
    # OddsPapi SUPPRIMÉ : ne remontait jamais Winamax (prouvé par logs) et brûlait
    # ~25 req RapidAPI/run = 2400/mois >> quota 500/mois → tuait RapidAPI tout le mois.
    # Sans lui : ~180 req/mois, RapidAPI vit, calendrier complet revient.
    rapid_matchs       = enrichir_matchs_rapidapi(rapid_matchs, budget_requetes=4)
    # SportAPI7 SUPPRIMÉ : ses données (cotes, tournoi) n'étaient lues nulle part
    # dans le pipeline → requêtes RapidAPI gaspillées pour rien. Odds API couvre les cotes.
    calendrier_injecte = fusionner_calendrier(odds_matchs, rapid_matchs)
    tournois_winamax   = charger_tournois_winamax()  # Liste éditable sur GitHub
    if not calendrier_injecte:
        logging.info(f"Calendrier API vide → Gemini cherchera lui-même ({len(tournois_winamax)} tournois Winamax).")
    donnees_json       = collecter_donnees_tennis(date, heure, calendrier_injecte, rapid_matchs, heure_fin, tournois_winamax)

    # Filtre DUR déterministe : retirer les matchs hors fenêtre horaire AVANT Claude.
    # Évite que Claude propose un match déjà joué (ex: 09:00 en session SOIR 22:50→05:00).
    donnees_json = filtrer_json_par_fenetre(donnees_json, heure, heure_fin, date)

    # RELAIS GEMINI CALENDRIER : si après filtrage il ne reste AUCUN match jouable,
    # cela ne veut PAS dire qu'il n'y a rien à jouer — les API peuvent avoir remonté
    # uniquement des matchs hors fenêtre (ex: qualifs du matin) tout en MASQUANT les
    # vrais tournois Winamax de la fenêtre (Eastbourne, Bad Homburg...).
    # Dans ce cas, on force Gemini à chercher lui-même le calendrier des tournois Winamax.
    try:
        nb_jouables = len(json.loads(donnees_json).get("matchs", []))
    except Exception:
        nb_jouables = 0
    if nb_jouables == 0:
        logging.info(
            f"0 match jouable après filtre → relais Gemini calendrier forcé "
            f"({len(tournois_winamax)} tournois Winamax)."
        )
        # Appel SANS calendrier injecté → déclenche la mission spéciale (recherche web)
        donnees_json = collecter_donnees_tennis(date, heure, "", rapid_matchs, heure_fin, tournois_winamax)
        donnees_json = filtrer_json_par_fenetre(donnees_json, heure, heure_fin, date)

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

    # ----- MARCHÉS ALTERNATIFS (sets / jeux) — actif seulement si MARCHES_ALT_MODE != "off"
    if MARCHES_ALT_MODE != "off":
        try:
            matchs_all = json.loads(donnees_json).get("matchs", [])
            # Un match "serré" = cote du favori dans la fourchette (match équilibré)
            matchs_serres = []
            for m in matchs_all:
                try:
                    c1 = float(m.get("cote_j1") or 0)
                    c2 = float(m.get("cote_j2") or 0)
                except (TypeError, ValueError):
                    continue
                cotes_valides = [c for c in (c1, c2) if c > 0]
                if not cotes_valides:
                    continue
                cote_favori = min(cotes_valides)
                if MARCHES_ALT_COTE_MIN <= cote_favori <= MARCHES_ALT_COTE_MAX:
                    matchs_serres.append(m)
            if matchs_serres:
                logging.info(f"Marchés alt ({MARCHES_ALT_MODE}) : {len(matchs_serres)} match(s) serré(s) à examiner.")
                analyser_marches_alternatifs(matchs_serres, date)
            else:
                logging.info("Marchés alt : aucun match serré aujourd'hui.")
        except Exception as e:
            logging.warning(f"Marchés alt : erreur non bloquante : {e}")

    # Bascule dynamique Sonnet → Opus selon richesse des données
    nb_matchs_analyse = len(json.loads(donnees_json).get("matchs", []))
    modele_choisi = CLAUDE_OPUS if nb_matchs_analyse >= SEUIL_OPUS else CLAUDE_SONNET
    logging.info(f"Modèle Claude : {'Opus 🔥' if modele_choisi == CLAUDE_OPUS else 'Sonnet ⚡'} ({nb_matchs_analyse} matchs — seuil {SEUIL_OPUS})")

    try:
        logging.info(f"Claude analyse — {date} {heure}")
        rep = claude_client.messages.create(
            model=modele_choisi, max_tokens=4096, system=prompt,
            messages=[{"role": "user", "content":
                f"Analyse et propose les meilleurs paris (max {MAX_TICKETS}) — {date} {heure}.\n"
                f"RAPPEL CRITIQUE : Ta réponse commence OBLIGATOIREMENT par 🎾 ou AUCUN_MATCH. "
                f"Zéro texte avant. Pas de 'Let me analyze', pas de 'FILTERING', pas de markdown. "
                f"Premier caractère = 🎾 ou A (AUCUN_MATCH). Sinon c'est un échec."}],
        )
        texte = "\n".join(b.text for b in rep.content if hasattr(b, "text") and b.text).strip()
        logging.info(f"Claude OK ({len(texte)} chars) — {rep.usage.input_tokens} in / {rep.usage.output_tokens} out")

        # Filtre sécurité — si Claude commence par du texte parasite, on extrait la partie valide
        if not texte.startswith("🎾") and not texte.startswith("🔴") and not texte.startswith("AUCUN_MATCH"):
            for marqueur in ["🎾", "🔴", "AUCUN_MATCH"]:
                idx = texte.find(marqueur)
                if idx != -1:
                    logging.warning(f"Claude texte parasite ({idx} chars) — nettoyage automatique.")
                    texte = texte[idx:]
                    break
            else:
                logging.warning("Réponse Claude invalide — aucun marqueur trouvé.")

        # LOG DEBUG — après nettoyage
        logging.info(f"DEBUG Claude réponse (après nettoyage) : {texte[:300]}")

        # Vérifier AUCUN_MATCH uniquement si le texte commence par AUCUN_MATCH
        if texte.startswith("AUCUN_MATCH"):
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

        # Filtrer les tickets abandonnés en analysant le DELTA réel dans la ligne VALUE
        # Format attendu : "delta +0.07 ❌" ou "delta +0.17 ✅"
        def _heure_dans_fenetre(hm, debut, fin):
            """True si l'heure HH:MM est dans la fenêtre (gère passage minuit)."""
            if fin < debut:  # fenêtre nocturne (ex: 22:50 → 05:00)
                return hm >= debut or hm <= fin
            return debut <= hm <= fin

        def _ticket_valide(t):
            t_lower = t.lower()
            # Signaux d'abandon explicites dans le texte
            if any(sig in t_lower for sig in ["abandon de ce ticket", "ticket abandonné",
                                               "kelly 0%", "mise : 0%", "mise 0%"]):
                return False
            # Rejeter les cotes inventées/estimées
            if any(sig in t_lower for sig in ["cote estimée", "estimée", "non disponible précisément",
                                               "cote approximative", "estimation de cote",
                                               "cote non disponible", "cote handicap estimée"]):
                logging.info("Ticket rejeté — cote estimée/inventée détectée.")
                return False
            # FILTRE HORAIRE : extraire l'heure du ticket (ligne HEURE) et vérifier la fenêtre.
            # Empêche de parier sur un match déjà joué (ex: 09:00 en session soir 22:50→05:00).
            mh = re.search(r"heure\s*:?\s*</b>?\s*(\d{1,2})[h:](\d{2})", t_lower)
            if not mh:
                mh = re.search(r"(\d{1,2})[h:](\d{2})", t_lower)
            if mh:
                hm = f"{int(mh.group(1)):02d}:{mh.group(2)}"
                if not _heure_dans_fenetre(hm, heure, heure_fin):
                    logging.warning(f"Ticket rejeté — match à {hm} hors fenêtre {heure}→{heure_fin} (déjà joué ou trop tard).")
                    return False
            # Extraire le delta de la ligne VALUE : "delta +0.07" ou "delta -0.05"
            m = re.search(r"delta\s*([+-]?\d+[.,]\d+)", t_lower)
            if m:
                delta = float(m.group(1).replace(",", "."))
                # SEUIL ADAPTATIF selon la cote (cohérent avec le prompt Claude) :
                # favori (<1.90) → 0.07 ; équilibré (1.90-2.50) → 0.10 ; outsider (>2.50) → 0.12
                cote_ticket = None
                mcote = re.search(r"cote\s*:?\s*</b>?\s*(\d+[.,]\d+)", t_lower)
                if not mcote:
                    mcote = re.search(r"r[ée]elle\s*(\d+[.,]\d+)", t_lower)
                if mcote:
                    cote_ticket = float(mcote.group(1).replace(",", "."))
                if cote_ticket is not None and cote_ticket < 1.90:
                    seuil_delta = 0.07
                elif cote_ticket is not None and cote_ticket > 2.50:
                    seuil_delta = 0.12
                else:
                    seuil_delta = 0.10
                if delta < seuil_delta:
                    logging.info(f"Ticket rejeté — delta {delta:+.2f} < seuil {seuil_delta:.2f} (cote {cote_ticket}).")
                    return False  # Delta insuffisant pour cette cote → abandon
            # GARDE-FOU ÉCART MODÈLE-MARCHÉ (validé par les cas Wang et Ruse) :
            # Un faux delta géant apparaît quand Claude (surtout Opus) surestime trop
            # la proba d'un outsider vs le marché. Ex Wang : bot 45% vs marché 26% → +73%.
            # Ex Ruse : bot 52% vs marché 36% → +44%. Les deux ont perdu.
            # On extrait la proba estimée (X%) et la cote réelle (Z.ZZ) de la ligne VALUE :
            # "X% → juste Y.YY → réelle Z.ZZ → delta +D.DD"
            mp = re.search(r"value\s*:?\s*</b>?\s*(\d+(?:[.,]\d+)?)\s*%", t_lower)
            mc = re.search(r"r[ée]elle\s*(\d+[.,]\d+)", t_lower)
            if mp and mc:
                proba_bot = float(mp.group(1).replace(",", ".")) / 100
                cote_reelle = float(mc.group(1).replace(",", "."))
                proba_marche = 1 / cote_reelle if cote_reelle > 0 else 1
                if proba_marche > 0:
                    ecart_relatif = (proba_bot - proba_marche) / proba_marche
                    # Seuil ADAPTATIF selon la présence de données manquantes :
                    # Constat réel (Semenistaja, Gaston) : les paris perdants avaient
                    # des stats de surface manquantes ET une survalorisation. Quand des
                    # données importantes manquent, on durcit le seuil — le modèle n'a
                    # alors quasiment pas le droit de s'écarter du marché.
                    # Détecter si le ticket signale des données manquantes (≠ "Aucune")
                    dm_match = re.search(r"donn[ée]es manquantes\s*:?\s*(?:</b>)?\s*(.+)", t_lower)
                    donnees_manquantes = False
                    if dm_match:
                        contenu_dm = dm_match.group(1).strip()
                        donnees_manquantes = bool(contenu_dm) and not contenu_dm.startswith("aucune")
                    seuil = 0.10 if donnees_manquantes else 0.30
                    if ecart_relatif > seuil:
                        raison = "données manquantes + survalorisation" if donnees_manquantes else "faux delta probable (cf. Wang/Ruse)"
                        logging.warning(
                            f"Ticket rejeté — écart modèle-marché trop élevé : "
                            f"bot {proba_bot*100:.0f}% vs marché {proba_marche*100:.0f}% "
                            f"(+{ecart_relatif*100:.0f}%, seuil {seuil*100:.0f}%). {raison}."
                        )
                        return False
            return True

        tickets_valides = []
        for t in tickets:
            if not (t.startswith("🎾") or t.startswith("🔴")):
                continue  # Pas un ticket structuré valide
            if not _ticket_valide(t):
                logging.info("Ticket rejeté (delta < 0.10 ou abandon explicite) — non envoyé.")
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
                # Nettoyer le ticket — garder uniquement la partie 🎾 ou 🔴
                ticket_propre = ticket
                for marqueur in ["🎾", "🔴"]:
                    idx = ticket_propre.find(marqueur)
                    if idx != -1 and not ticket_propre.startswith(marqueur):
                        ticket_propre = ticket_propre[idx:]
                        break
                sauvegarder_pari_pour_suivi({
                    "pari":   ticket_propre,
                    "date":   date,
                    "marche": _detecter_marche(ticket_propre),
                    "niveau": _detecter_niveau(ticket_propre),
                    "modele": "opus" if modele_choisi == CLAUDE_OPUS else "sonnet",
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
        _quota_persister()
        logging.info(f"Terminé en {time.time() - debut:.1f}s.")

# =====================================================================
# 13. POINT D'ENTRÉE CLI
# =====================================================================

def envoyer_recap_hebdo():
    """
    Envoie un bilan de performance sur Telegram (à déclencher 1×/semaine via cron).
    Lecture seule de stats.json + quota_rapidapi.json — n'affecte PAS l'analyse.
    """
    s = charger_stats()
    total = s["victoires"] + s["defaites"]
    wr = calculer_winrate(s)

    lignes = [
        "📊 <b>ACEANALYTICS 🎾 TENNIS — BILAN HEBDO</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"✅ Victoires : {s['victoires']} | ❌ Défaites : {s['defaites']}",
        f"📈 <b>Win Rate global : {wr:.1f}%</b> ({total} paris)",
    ]

    # Détail par marché (si v2 disponible)
    par_marche = s.get("par_marche", {})
    if par_marche:
        détails = []
        for marche, vd in par_marche.items():
            v, d = vd.get("v", 0), vd.get("d", 0)
            if v + d > 0:
                wr_m = v / (v + d) * 100
                détails.append(f"  • {marche} : {v}V/{d}D ({wr_m:.0f}%)")
        if détails:
            lignes.append("\n🎯 <b>Par marché :</b>")
            lignes.extend(détails)

    # Détail par niveau de confiance
    par_niveau = s.get("par_niveau", {})
    if par_niveau:
        détails_n = []
        for niveau, vd in par_niveau.items():
            v, d = vd.get("v", 0), vd.get("d", 0)
            if v + d > 0:
                wr_n = v / (v + d) * 100
                détails_n.append(f"  • {niveau} : {v}V/{d}D ({wr_n:.0f}%)")
        if détails_n:
            lignes.append("\n🛡 <b>Par confiance :</b>")
            lignes.extend(détails_n)

    # Détail par modèle (Opus vs Sonnet) — pour comparer leur fiabilité
    par_modele = s.get("par_modele", {})
    if par_modele:
        détails_m = []
        for modele, vd in par_modele.items():
            v, d = vd.get("v", 0), vd.get("d", 0)
            if v + d > 0:
                wr_m = v / (v + d) * 100
                détails_m.append(f"  • {modele.capitalize()} : {v}V/{d}D ({wr_m:.0f}%)")
        if détails_m:
            lignes.append("\n🤖 <b>Par modèle :</b>")
            lignes.extend(détails_m)

    # Quota RapidAPI du mois
    quota, _ = _gh_get("quota_rapidapi.json")
    if isinstance(quota, dict):
        lignes.append(
            f"\n⚙️ Quota RapidAPI {quota.get('mois','?')} : "
            f"{quota.get('rapidapi_utilise', 0)} requêtes utilisées."
        )

    # Note de calibration : combien de paris avant validation
    if total < 50:
        lignes.append(f"\n💡 Phase bêta : {total}/50 paris pour valider la calibration.")
    else:
        lignes.append(f"\n🔬 {total} paris — calibration mesurable.")

    message = "\n".join(lignes)
    if DRY_RUN:
        logging.info("[DRY-RUN] Récap hebdo :")
        logging.info(message)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHANNEL_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        logging.info("✅ Récap hebdo envoyé.")
    except Exception as e:
        logging.warning(f"Échec récap hebdo : {e}")


if __name__ == "__main__":
    args         = [a for a in sys.argv[1:] if a != "--dry-run"]
    if not args:
        run_bot_autonome()
    elif args[0] == "recap":
        envoyer_recap_hebdo()
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
      
