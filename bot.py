"""
╔══════════════════════════════════════════════════════════════════════╗
║          BOT TENNIS ACEANALYTICS — bot.py v8.0                        ║
║  Architecture hybride : Gemini (recherche) + Claude (analyse)         ║
║  Pré-collecte : Odds API + RapidAPI Tennis → calendrier complet       ║
║                                                                      ║
║  PRINCIPALES CAPACITÉS (état 21/07/2026) :                            ║
║   • Modèle probabiliste Barnett-Clarke (proba calculée par surface)  ║
║   • Repli OddsPapi DIRECT (api.oddspapi.io) sur 429 RapidAPI —        ║
║     fixtures + cotes Pinnacle, pool de requêtes séparé                ║
║   • Repli cote Pinnacle : complète un match jouable sans cote Winamax ║
║   • Marchés alternatifs (Sets O/U, Games O/U) via cotes Pinnacle      ║
║   • Pré-filtre TOURNOIS AVANT Gemini (v7.9) : ne documenter que les   ║
║     matchs jouables → couverture concentrée sur les bons matchs       ║
║   • 8 garde-fous _ticket_valide + clause de corroboration BC/Claude   ║
║   • Harnais hors-ligne : `python bot.py test` (18 contrôles)          ║
║                                                                      ║
║  Secrets GitHub requis :                                             ║
║    ANTHROPIC_API_KEY  · TELEGRAM_BOT_TOKEN · TELEGRAM_CHANNEL_ID     ║
║    GITHUB_TOKEN       · GITHUB_REPO · GEMINI_API_KEY                 ║
║  Secrets optionnels :                                                ║
║    ODDS_API_KEY   (https://the-odds-api.com — 500 req/mois gratuit)  ║
║    RAPIDAPI_KEY   (https://rapidapi.com — calendrier complet)        ║
║    ODDSPAPI_KEY   (https://oddspapi.io — pool direct, repli 429)      ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os, sys, json, hashlib, logging, re, time, base64, requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from logging.handlers import RotatingFileHandler
# v7.8 : imports lourds tolérants — `python bot.py test` doit tourner hors-ligne
# sans anthropic/google-genai installés ni clés configurées (harnais 100% local).
try:
    import anthropic
except ImportError:
    anthropic = None
try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

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

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if anthropic else None
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if genai else None

CLAUDE_SONNET  = "claude-sonnet-4-6"   # Sessions légères < 3 matchs
CLAUDE_OPUS    = "claude-opus-4-8"     # Sessions riches ≥ 3 matchs (CORRIGÉ v7.2)
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
VERSION = "8.8"   # affichée au démarrage de chaque run — fin des doutes de version

MARCHES_ALT_MODE = "actif"   # ← "actif" depuis le 10/07/2026 (observation validée : format Pinnacle confirmé, Patch B OK)

ODDSPAPI_HOST = "odds-api1.p.rapidapi.com"   # host OddsPapi sur RapidAPI (confirmé 09/07/2026)
ODDSPAPI_BOOKMAKER = "pinnacle"               # bookmaker de référence (le plus sharp)

# v7.7 : API OddsPapi DIRECTE (compte propre, pool de requêtes SÉPARÉ de RapidAPI).
# Utilisée UNIQUEMENT en repli quand RapidAPI répond 429/erreur — préserve le
# quota RapidAPI et n'entame le pool direct que si nécessaire. Auth = apiKey en
# query (≠ headers RapidAPI), hôte api.oddspapi.io, endpoints /v4/*.
ODDSPAPI_DIRECT_KEY  = os.environ.get("ODDSPAPI_KEY")
ODDSPAPI_DIRECT_HOST = "https://api.oddspapi.io"


def _oddspapi_direct_get(chemin, params, timeout=15):
    """GET sur l'API OddsPapi directe (repli). apiKey en query. None si absent/429."""
    if not ODDSPAPI_DIRECT_KEY:
        return None
    params = dict(params or {})
    params["apiKey"] = ODDSPAPI_DIRECT_KEY
    for tentative in (1, 2):
        try:
            _quota_inc("oddspapi_direct")  # compteur séparé du pool RapidAPI
            r = requests.get(f"{ODDSPAPI_DIRECT_HOST}{chemin}", params=params, timeout=timeout)
            if r.status_code == 429 and tentative == 1:
                logging.info("OddsPapi DIRECT : 429 — pause 2s puis nouvel essai.")
                time.sleep(2)
                continue
            r.raise_for_status()
            logging.info(f"OddsPapi DIRECT : {chemin} OK (repli RapidAPI).")
            return r.json()
        except Exception as e:
            if tentative == 2:
                logging.warning(f"OddsPapi DIRECT indisponible : {e}")
            else:
                time.sleep(1)
    return None
MARKET_TOTAL_SETS_LIGNE = 2.5                 # Over 2.5 sets = "va au 3e set"
# PATCH B — Total Games : ne garder que les lignes de MATCH complet.
# Les totaux d'UN SEUL set (9.5-12.5) polluaient via la mainLine (10.5).
MARCHES_ALT_GAMES_MIN   = 18.5
MARCHES_ALT_GAMES_MAX   = 26.5
MARCHES_ALT_GAMES_CIBLE = 22.5   # ligne principale de match la plus courante
# Value contre Pinnacle (sharp) : BANDE d'edge acceptable + plafonds prudents.
#   edge = proba estimée par Claude − proba implicite Pinnacle
#   - sous MARGE_MIN : pas assez de value pour parier contre un book sharp
#   - au-dessus d'ECART_MAX : Claude diverge trop → c'est probablement LUI qui a tort
MARCHES_ALT_MARGE_MIN = 0.06
MARCHES_ALT_ECART_MAX = 0.15
MARCHES_ALT_PROBA_MAX = 0.65   # plafond proba estimée (anti-surconfiance marché dérivé)
MARCHES_ALT_MISE      = 0.5    # mise fixe % (phase test — cote Winamax à vérifier à la main)
# Déclencheur : on ne regarde les marchés alt que sur les matchs "serrés"
# (cote du favori dans cette fourchette = match équilibré, incertain sur le vainqueur)
MARCHES_ALT_COTE_MIN = 1.50
MARCHES_ALT_COTE_MAX = 2.10
# Plafond d'appels /fixtures/odds par run (économie quota — v7.3.2)
MARCHES_ALT_MAX_APPELS = 4
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
    },
    "par_surface": {
        "terre":  {"v": 0, "d": 0},
        "dur":    {"v": 0, "d": 0},
        "gazon":  {"v": 0, "d": 0},
        "autre":  {"v": 0, "d": 0},
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
    if "par_modele" not in s:
        s["par_modele"] = dict(STATS_DEFAUT["par_modele"])
    if "par_surface" not in s:
        s["par_surface"] = dict(STATS_DEFAUT["par_surface"])
    return s


def charger_stats():
    s, _ = _gh_get("stats.json")
    if not isinstance(s, dict) or "victoires" not in s:
        return dict(STATS_DEFAUT)
    return _migrer_stats(s)


def calculer_winrate(s):
    total = s["victoires"] + s["defaites"]
    return (s["victoires"] / total * 100) if total > 0 else 0.0


def enregistrer_resultat(victoire, index_pari=None):
    """
    Enregistre une victoire/défaite.
    index_pari : position (1-based) du pari dans pari_en_cours.json (cf. commande 'file').
    Si fourni → met à jour les stats segmentées (marché/niveau/modèle)
    et retire le pari de la file. Sinon → stats globales uniquement (comme avant).
    """
    s, sha = _gh_get("stats.json")
    if not isinstance(s, dict) or "victoires" not in s:
        s = dict(STATS_DEFAUT)
    s = _migrer_stats(s)
    s["victoires" if victoire else "defaites"] += 1

    if index_pari is not None:
        paris, psha = _gh_get("pari_en_cours.json")
        if isinstance(paris, list) and 1 <= index_pari <= len(paris):
            p = paris.pop(index_pari - 1)
            cle = "v" if victoire else "d"
            # v7.4 : YIELD — profit en unités de mise (% de bankroll)
            cote  = p.get("cote")
            mise  = p.get("mise_pct")
            profit = None
            if cote and mise:
                profit = round((cote - 1) * mise, 4) if victoire else round(-mise, 4)
                roi = s.setdefault("roi", {"mise_totale": 0.0, "profit": 0.0})
                roi["mise_totale"] = round(roi.get("mise_totale", 0.0) + mise, 4)
                roi["profit"]      = round(roi.get("profit", 0.0) + profit, 4)
            for champ, section in [("marche", "par_marche"),
                                   ("niveau", "par_niveau"),
                                   ("modele", "par_modele"),
                                   ("surface", "par_surface")]:
                val = p.get(champ)
                if val and val in s.get(section, {}):
                    s[section][val][cle] += 1
                    if profit is not None:
                        seg = s[section][val]
                        seg["mise"]   = round(seg.get("mise", 0.0) + mise, 4)
                        seg["profit"] = round(seg.get("profit", 0.0) + profit, 4)
                elif val:
                    logging.warning(f"Segment inconnu '{val}' dans {section} — ignoré.")
            resume = re.sub(r"<[^>]+>", "", p.get("pari", ""))[:80]
            logging.info(f"Pari réglé [{index_pari}] : {resume}")
            if not DRY_RUN:
                if paris:
                    _gh_put("pari_en_cours.json", paris, "🧹 Pari réglé", sha=psha)
                elif psha:
                    _gh_delete("pari_en_cours.json", "🗑️ File vide", sha=psha)
        else:
            logging.warning(f"Index pari {index_pari} introuvable dans la file — stats globales seulement.")

    if DRY_RUN:
        logging.info(f"[DRY-RUN] Stats : {s}")
    else:
        _gh_put("stats.json", s, "🔄 Maj stats", sha=sha)
    logging.info(f"{'✅ VICTOIRE' if victoire else '❌ DÉFAITE'} — {s['victoires']}V / {s['defaites']}D")


def lister_file_paris():
    """Affiche la file des paris en cours avec leur numéro (pour 'resultat v/d N')."""
    paris, _ = _gh_get("pari_en_cours.json")
    if not paris:
        print("File vide — aucun pari en cours.")
        return
    for i, p in enumerate(paris, 1):
        txt = re.sub(r"<[^>]+>", "", p.get("pari", "")).replace("\n", " ")[:90]
        print(f"[{i}] {p.get('date','?')} | {p.get('marche','?')} | "
              f"{p.get('niveau','?')} | {p.get('modele','?')} | {txt}")


def _detecter_marche(ticket_texte):
    """
    Détecte le type de marché depuis le texte du ticket (noms Winamax).
    v7.5.1 : classification basée PRIORITAIREMENT sur la ligne PRONO — le texte
    complet (POURQUOI, H2H...) peut contenir des motifs trompeurs. Constat réel
    (15/07) : un ticket 'Vainqueur' classé à tort 'score_exact' parce que le
    POURQUOI mentionnait 'H2H 2-1', capté par l'ancien test '2-0'/'2-1' sur
    tout le texte.
    """
    # Isoler la ligne PRONO (celle qui décrit RÉELLEMENT le pari joué)
    m = re.search(r"prono\s*:\s*(?:</b>)?\s*(.+)", ticket_texte, re.IGNORECASE)
    ligne_prono = m.group(1).strip().lower() if m else ""

    def _classer(t):
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
        return None

    # 1) Classification sur la ligne PRONO uniquement (fiable, pas de bruit)
    resultat = _classer(ligne_prono) if ligne_prono else None
    if resultat:
        return resultat
    # 2) Repli sur le texte complet SANS le test '2-0'/'2-1' (trop permissif
    #    hors du contexte PRONO — peut matcher un H2H, une date, etc.)
    t = ticket_texte.lower()
    if "combiné" in t or "combine" in t:
        return "combine"
    if "tiebreak" in t or "tie-break" in t:
        return "tiebreak"
    if "écart de jeux" in t or "ecart de jeux" in t or "écart de set" in t or \
       "ecart de set" in t or "handicap" in t:
        return "handicap"
    if "nombre de jeux" in t or "nombre de set" in t or "over" in t or "under" in t:
        return "over_under"
    if "score exact" in t:
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


def _normaliser_surface(surface_brute):
    """
    Normalise un champ 'surface' (venant de RapidAPI ou Gemini, en FR ou EN,
    ex: 'Clay', 'Terre battue', 'Hard', 'Gazon') vers terre/dur/gazon/autre.
    v7.5 : suivi du yield par surface (Terre/Dur/Gazon) — aucune décision
    d'analyse n'en dépend, c'est un segment de MESURE uniquement.
    """
    s = str(surface_brute or "").lower()
    if "clay" in s or "terre" in s:
        return "terre"
    if "grass" in s or "gazon" in s:
        return "gazon"
    if "hard" in s or "dur" in s:
        return "dur"
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
    # v7.9.6 : le texte libre de Claude peut contenir <, > ou & ("cote <1.90")
    # qui cassent le mode HTML de Telegram → on échappe, en préservant les
    # balises autorisées si Claude en a mis (il n'en met pas dans AUCUN_MATCH).
    raison = (str(raison).replace("&", "&amp;")
              .replace("<", "&lt;").replace(">", "&gt;"))
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
    # v7.9.6 : passer par la fonction ROBUSTE (vérif du statut + repli texte brut).
    # Bug du 24/07 : l'ancien requests.post ne vérifiait PAS la réponse — Telegram
    # renvoyait 400 (le "<1.90" du texte de Claude cassait le HTML) et le log
    # affichait quand même "envoyée". Échec silencieux, notification jamais reçue.
    ok = envoyer_sur_telegram(msg)
    if ok:
        logging.info(f"Notification sans ticket envoyée — {label}.")
    else:
        logging.error(f"❌ Notification sans ticket NON délivrée — {label}.")


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
    prono_m = re.search(r"prono\s*:\s*(.+)", txt)
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


def sauvegarder_ticket_rejete(ticket_texte, raison, date):
    """
    v7.5.2 — MESURE PURE : archive un ticket rejeté par les garde-fous
    (delta insuffisant, écart modèle-marché, cote estimée, hors fenêtre...)
    dans tickets_rejetes.json. Ne touche JAMAIS stats.json, n'envoie AUCUNE
    notification Telegram, et n'influence AUCUNE décision d'analyse.
    But : dans quelques semaines, un règlement séparé (comme 'regler' pour
    les vrais paris) dira si les garde-fous rejettent net plus de gagnants
    que de perdants — donnée qu'on n'avait qu'au jugé jusqu'ici (cf. le rejet
    Etcheverry/Merida Aguilar du 15/07, qui aurait gagné).
    """
    if DRY_RUN:
        return
    marche = _detecter_marche(ticket_texte)
    niveau = _detecter_niveau(ticket_texte)
    entree = {
        "pari":         ticket_texte,
        "date":         date,
        "raison_rejet": raison or "inconnue",
        "marche":       marche,
        "niveau":       niveau,
    }
    rejets, sha = _gh_get("tickets_rejetes.json")
    if not isinstance(rejets, list):
        rejets = []
    rejets.append(entree)
    # Plafond large (300) — fichier de mesure, pas de file d'action à vider vite
    _gh_put("tickets_rejetes.json", rejets[-300:], "🔍 Ticket rejeté archivé", sha=sha)


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
# Toutes les sources tennis complémentaires (RapidAPI Tennis, OddsPapi)
# partagent la MÊME clé RAPIDAPI_KEY = le même quota mensuel.
# Ce compteur trace la conso réelle par run et cumule sur le mois pour
# éviter les 429 surprise. N'affecte PAS l'analyse ni la stratégie.

_QUOTA_RUN = {"tennisapi": 0, "oddspapi": 0}  # compteurs en mémoire pour ce run

def _quota_inc(api="tennisapi", n=1):
    """Incrémente le compteur de requêtes de l'API donnée pour ce run."""
    _QUOTA_RUN[api] = _QUOTA_RUN.get(api, 0) + n

def _quota_persister():
    """
    Cumule la conso du run dans quota_rapidapi.json.
    v7.4 : compteurs SÉPARÉS par API — leurs budgets sont distincts :
      - tennisapi : 50 req/JOUR  (Tennis API ATP-WTA-ITF)
      - oddspapi  : 1000 req/MOIS (OddsPapi)
    Le compteur jour de tennisapi se reset chaque jour, les mois chaque mois.
    """
    if DRY_RUN or (_QUOTA_RUN["tennisapi"] == 0 and _QUOTA_RUN["oddspapi"] == 0
                   and _QUOTA_RUN.get("oddspapi_direct", 0) == 0):
        return
    now = datetime.now(ZoneInfo("Europe/Paris"))
    mois_actuel = now.strftime("%Y-%m")
    jour_actuel = now.strftime("%Y-%m-%d")
    data, sha = _gh_get("quota_rapidapi.json")
    if not isinstance(data, dict) or data.get("mois") != mois_actuel:
        data = {"mois": mois_actuel, "tennisapi_mois": 0, "oddspapi_mois": 0,
                "jour": jour_actuel, "tennisapi_jour": 0}
    if data.get("jour") != jour_actuel:
        data["jour"] = jour_actuel
        data["tennisapi_jour"] = 0
    data["tennisapi_mois"] = data.get("tennisapi_mois", 0) + _QUOTA_RUN["tennisapi"]
    data["tennisapi_jour"] = data.get("tennisapi_jour", 0) + _QUOTA_RUN["tennisapi"]
    data["oddspapi_mois"]  = data.get("oddspapi_mois", 0)  + _QUOTA_RUN["oddspapi"]
    # v7.7 : pool OddsPapi DIRECT (compte propre, budget séparé du RapidAPI)
    data["oddspapi_direct_mois"] = (data.get("oddspapi_direct_mois", 0)
                                    + _QUOTA_RUN.get("oddspapi_direct", 0))
    try:
        _gh_put("quota_rapidapi.json", data, "📊 Maj quotas API", sha=sha)
        logging.info(
            f"Quotas — TennisAPI : {_QUOTA_RUN['tennisapi']} ce run, "
            f"{data['tennisapi_jour']}/50 aujourd'hui, {data['tennisapi_mois']} ce mois · "
            f"OddsPapi RapidAPI : {_QUOTA_RUN['oddspapi']} ce run, {data['oddspapi_mois']}/1000 ce mois · "
            f"OddsPapi DIRECT : {_QUOTA_RUN.get('oddspapi_direct', 0)} ce run, "
            f"{data['oddspapi_direct_mois']} ce mois."
        )
    except Exception as e:
        logging.warning(f"Quota persist échoué : {e}")

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
                params_fx = {
                    "include":  "tournament,round",
                    "filter":   "PlayerGroup:singles",
                    "pageSize": 100,
                    "pageNo":   page,
                }
                # v7.4.2 : pause anti-rafale sur la pagination aussi (429 WTA p2
                # constaté le 14/07 — les pages partaient dans la même seconde,
                # tronquant le calendrier WTA).
                time.sleep(1.0)
                _quota_inc("tennisapi")
                r = requests.get(url, headers=headers, timeout=10, params=params_fx)
                if r.status_code == 429:
                    # Débit, pas forcément quota : 1 nouvel essai après pause
                    logging.warning(f"RapidAPI {tour.upper()} p{page} : 429 — pause 3s puis nouvel essai.")
                    time.sleep(3)
                    _quota_inc("tennisapi")
                    r = requests.get(url, headers=headers, timeout=10, params=params_fx)
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

def fusionner_calendrier(odds_matchs, rapid_matchs):
    """
    Logique de fusion en 3 étapes :
    1. RapidAPI Tennis → calendrier complet (468+ matchs)
    2. Odds API → filtre "disponible sur Winamax" (cotes vérifiées)
    3. Intersection → uniquement les matchs RapidAPI avec cote Winamax confirmée
       + données complètes RapidAPI (H2H, forme, surface)
    CORRIGÉ v7.2 : la correspondance exige LES DEUX joueurs (plus un seul),
    et inverse les cotes si l'ordre des joueurs diffère entre les sources.
    """
    lignes = ["📋 MATCHS DISPONIBLES SUR WINAMAX (calendrier RapidAPI filtré) :\n"]

    def _noms_matchent(a, b):
        a, b = a.lower().strip(), b.lower().strip()
        if not a or not b:
            return False
        return a in b or b in a

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

        # Chercher cote Winamax correspondante dans Odds API.
        # Exiger que LES DEUX joueurs correspondent (évite d'attribuer les cotes
        # d'un autre match sur un simple nom de famille commun type "Zverev").
        cote_trouvee = None
        inverse      = False
        for data in odds_matchs.values():
            if not (data.get("cote_j1") and data.get("cote_j2")):
                continue
            o1, o2 = data["joueur1"], data["joueur2"]
            if _noms_matchent(j1, o1) and _noms_matchent(j2, o2):
                cote_trouvee, inverse = data, False
                break
            if _noms_matchent(j1, o2) and _noms_matchent(j2, o1):
                cote_trouvee, inverse = data, True
                break

        if cote_trouvee:
            # ✅ Match disponible sur Winamax — enrichir avec les cotes,
            # en respectant l'ordre des joueurs (swap si sources inversées)
            c1 = cote_trouvee["cote_j2"] if inverse else cote_trouvee["cote_j1"]
            c2 = cote_trouvee["cote_j1"] if inverse else cote_trouvee["cote_j2"]
            m["cote_j1"]     = c1
            m["cote_j2"]     = c2
            m["source_cote"] = cote_trouvee["source_cote"]
            matchs_winamax.append(m)
            lignes.append(
                f"• {m['heure']} | {j1} vs {j2} | {m['tournoi']} | {m['surface']}"
                f" | Cotes {cote_trouvee['source_cote']}: {j1} {c1:.2f}"
                f" / {j2} {c2:.2f}"
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
            _quota_inc("tennisapi")
            # v7.4.1 : pause anti-rafale — les 429 du 13/07 sont arrivés sur des
            # requêtes tirées dans la même seconde (limite de débit, pas de quota :
            # la pagination venait de passer sans erreur une seconde avant).
            time.sleep(1.0)
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
            f"   → ⛔ Une cote d'un AUTRE bookmaker (bet365, Betclic, Unibet...) ne compte PAS :\n"
            f"     si le tournoi n'est pas proposé par Winamax (beaucoup de Challengers ne le\n"
            f"     sont pas), le match est INJOUABLE pour nos abonnés → source_cote = nom du\n"
            f"     bookmaker trouvé, et le match sera écarté automatiquement par le code.\n"
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

⚠️⚠️ RÈGLE FONDAMENTALE — TOUTE STAT DOIT ÊTRE FILTRÉE PAR SURFACE ⚠️⚠️
Une stat "toutes surfaces confondues" est TROMPEUSE et pire qu'une absence de stat.
Raison mesurée : sur gazon même un serveur moyen tient son service au-dessus de 85%,
alors que sur terre les taux de hold chutent pour tout le monde. Un Hold% de 76% sur
TERRE peut donc valoir MIEUX qu'un Hold% de 85% sur GAZON. Comparer deux joueurs avec
des moyennes toutes surfaces revient à comparer des pommes et des poires.
→ Pour CHAQUE match, tu cherches les stats UNIQUEMENT sur la surface de CE match
  (indiquée dans le calendrier ci-dessus : Terre / Dur / Gazon), sur 52 semaines.
→ Sur tennisabstract.com et ultimatetennisstatistics.com, ce filtre par surface
  existe explicitement — utilise-le. Requête type : "Nom Prénom tennisabstract clay"
  ou "Nom ultimate tennis statistics clay serve return".
→ Si tu ne trouves QUE la moyenne toutes surfaces → écris "non trouvé", PAS la moyenne.
  Une donnée hors-surface est une donnée FAUSSE pour cette analyse.
→ Indique TOUJOURS la taille d'échantillon (nb de matchs sur cette surface / 52 sem).
  Moins de 5 matchs sur la surface = échantillon non significatif, signale-le.

PRIORITÉ 1 — Pour CHAQUE match, recherche dédiée par donnée, SUR LA SURFACE DU MATCH :
1. % de points gagnés au SERVICE et au RETOUR des DEUX joueurs (LE PLUS IMPORTANT) :
   → tennisabstract.com OU ultimatetennisstatistics.com (filtre surface obligatoire)
   → Ce sont les deux entrées du modèle probabiliste — priorité absolue sur tout le reste
   → Chercher : "Nom tennisabstract [clay/grass/hard] serve return points won"

2. Hold% et Break% des DEUX joueurs SUR CETTE SURFACE :
   → mêmes sources, filtre surface obligatoire
   → Hold% + Break% = Indice Combiné (>105% = niveau élite, <95% = vulnérable)

2-BIS. STATS PRIORITAIRES SELON LA SURFACE (v8.0 — décisives pour l'analyse) :
   Le revêtement change ce qui décide un match. Cherche EN PRIORITÉ, pour les
   deux joueurs, les stats de la ligne correspondant à la surface du match :

   • TERRE BATTUE (lente, rebond haut, échanges longs) :
     → % points gagnés sur 2E SERVICE (élite >54%, exposé <46%)
     → % jeux de retour gagnés / Break% (excellent >28%, faible <18%)
     → % conversion des balles de break
     Le service est peu neutralisant : c'est la 2e balle et le retour qui décident.

   • GAZON (ultra-rapide, rebond bas, points courts) :
     → % points gagnés sur 1ER SERVICE (inviolable >78%, vulnérable <68%)
     → Hold% (standard des tops serveurs >88%, vulnérable <78%)
     → ratio aces/doubles fautes · % tie-breaks gagnés
     Les breaks sont rares : tout se joue sur la 1re balle et les tie-breaks.

   • DUR EXTÉRIEUR (neutre, rebond régulier) :
     → Dominance Ratio (DR) sur la surface (dominant >1.15, dominé <0.90)
     → % balles de break SAUVÉES (excellente résistance >65%)
     → équilibre Hold% / Break% · % points 1er ET 2e service
     Surface de polyvalence : chercher le déséquilibre global, pas un seul coup.

   • DUR INDOOR (très rapide, aucune météo) :
     → Hold% (norme du circuit en salle : 83-88%)
     → % 1er service RENTRÉ (>66% = étouffe l'adversaire)
     → % points gagnés sur le 2e service ADVERSE
     Un seul break décide souvent la manche.

   ⚠️ Si la stat prioritaire de la surface est introuvable, dis-le explicitement
   dans "avertissements" — c'est une information importante pour l'analyse.

3. Forme récente des DEUX joueurs (5 derniers matchs) :
   → flashscore.fr OU sofascore.com OU matchstat.com
   → Si déjà fourni dans le calendrier → NE PAS re-chercher
   → Privilégier les matchs joués SUR LA SURFACE DU MATCH ANALYSÉ

4. H2H direct entre les deux joueurs (confrontations passées + surface) :
   → flashscore.fr OU tennisabstract.com OU atptour.com/wtatennis.com
   → Recherche dédiée "Joueur1 vs Joueur2 head to head"
   → Préciser combien de ces duels ont eu lieu SUR LA SURFACE DU MATCH
   → Si première confrontation → l'indiquer explicitement (pas "non trouvé")

PRIORITÉ 1-BIS — CHARGE PHYSIQUE, EN CHIFFRES (pas en prose) :
Pour CHAQUE joueur, remplis les champs fatigue_j1 / fatigue_j2 avec des NOMBRES :
   → minutes_7j : total de minutes jouées sur les 7 derniers jours (flashscore
     affiche la durée de chaque match — additionne-les)
   → nb_matchs_7j : nombre de matchs disputés sur 7 jours
   → heures_depuis_dernier_match : heures écoulées depuis la FIN de son dernier match
   → nb_3sets_consecutifs : nombre de matchs en 3 sets consécutifs récents
⚠️ Si une valeur est introuvable, mets 0 (le code traitera 0 comme "inconnu").
⛔ Ne mets JAMAIS de phrase dans ces champs : ce sont des nombres, exploités
   directement par le code. Une phrase les rend inutilisables.

PRIORITÉ 2 — Contexte (1 recherche globale chacun, pas par match) :
5. Blessures/forfaits du jour :
   → eurosport.fr OU tennis.com OU atptour.com
6. Points ATP/WTA à défendre + classement actuel :
   → atptour.com OU wtatennis.com
7. (Charge physique : déjà couverte en PRIORITÉ 1-BIS, en chiffres.)

PRIORITÉ 3 — Vérification cotes matchs secondaires :
7. Pour tout match SANS cote dans le calendrier, chercher la cote Winamax réelle sur :
   → coteur.com/cotes-tennis (spécialisé bookmakers français ANJ, affiche Winamax) — SOURCE PRIORITAIRE
   → flashscore.fr (onglet cotes, compare Winamax aux concurrents)
   → sportytrader.com/fr/cotes/tennis/
   → Cote Winamax trouvée → source_cote = "Winamax (Coteur)" / "Winamax (Flashscore)" /
     "Winamax (Sportytrader)" selon le site où tu l'as vue. ⚠️ Le mot "Winamax" est
     OBLIGATOIRE dans source_cote : le code écarte automatiquement tout match dont la
     source ne le contient pas.
   → Si le site n'affiche QUE d'autres bookmakers pour ce match (tournoi absent de
     Winamax, fréquent sur les Challengers) → source_cote = nom du bookmaker vu
     (ex: "bet365") — le match sera écarté, c'est voulu : il est injouable.
   → Introuvable → source_cote = "non trouvée" (NE JAMAIS inventer une cote)

SOURCES PAR TYPE (utilise ces sites réels — ne jamais inventer une valeur non trouvée) :
• Service/Retour % par surface → tennisabstract.com · ultimatetennisstatistics.com (filtre surface)
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
EXCLURE : qualifications (sauf Grand Chelem), doubles, et TOUS les tournois
ITF/Futures (M15/M25/W15 à W100) — Winamax ne les propose pas et leurs stats
sont introuvables. INCLURE : ATP, WTA, Challenger, Grand Chelem uniquement.

FORMAT JSON STRICT :
{{
  "heure_collecte": "{heure}",
  "matchs": [{{
    "date_match": "JJ/MM/AAAA (date réelle du match — DOIT être {date}, sinon ne pas inclure)",
    "heure_match": "HH:MM (HEURE FRANÇAISE Europe/Paris — jamais UTC)",
    "joueur1": "Nom", "joueur2": "Nom",
    "tournoi": "Nom", "circuit": "ATP ou WTA", "surface": "Terre/Dur/Gazon", "indoor": false,
    "cote_j1": 1.XX, "cote_j2": 1.XX, "source_cote": "Winamax/non trouvée",
    "forme_j1": ["V","D"], "forme_j2": ["V","D"],
    "details_forme_j1": "résumé", "details_forme_j2": "résumé",
    "hold_pct_j1": "XX% SUR LA SURFACE DU MATCH ou non trouvé",
    "hold_pct_j2": "XX% SUR LA SURFACE DU MATCH ou non trouvé",
    "stats_surface_j1": {{
      "serve_pts_won": "58.2%", "return_pts_won": "41.0%",
      "break_pct": "24%", "echantillon": "N matchs sur cette surface/52sem"
    }},
    "stats_surface_j2": {{
      "serve_pts_won": "non trouvé", "return_pts_won": "non trouvé",
      "break_pct": "non trouvé", "echantillon": "0 matchs sur cette surface/52sem"
    }},
    ⚠️ Chaque champ stat contient SOIT le pourcentage (ex: "58.2%") SOIT exactement
    "non trouvé" — JAMAIS les deux à la fois ("60% ou non trouvé" est INTERDIT et
    casse le modèle). Les exemples j1/j2 ci-dessus montrent les deux cas.
    "h2h_recents": "résumé",
    "fatigue_j1": {{
      "minutes_7j": 0, "nb_matchs_7j": 0, "heures_depuis_dernier_match": 0,
      "nb_3sets_consecutifs": 0
    }},
    "fatigue_j2": {{
      "minutes_7j": 0, "nb_matchs_7j": 0, "heures_depuis_dernier_match": 0,
      "nb_3sets_consecutifs": 0
    }},
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
    # v8.6 — BUDGET TEMPS. Constat du 30/07 : Gemini a renvoyé trois réponses
    # VIDES d'affilée (77s, puis 252s, puis jamais) et le job GitHub Actions a
    # été tué au timeout de 30 min — aucune notification, aucun ticket, rien.
    # Une reprise qui consomme tout le budget est pire qu'un abandon propre :
    # on préfère rendre la main pour que le run se termine et notifie.
    _budget_gemini_s = 420          # 7 min maximum pour l'ensemble des tentatives
    _debut_gemini = time.time()

    for tentative in range(1, 4):
        if time.time() - _debut_gemini > _budget_gemini_s:
            logging.error(f"Gemini : budget de {_budget_gemini_s // 60} min épuisé après "
                          f"{tentative - 1} tentative(s) — abandon propre pour laisser le "
                          f"run se terminer (le relais calendrier prendra la suite).")
            return '{"matchs": [], "avertissements": "Gemini indisponible (budget temps epuise)."}'

        try:
            rep = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.1,
                    # v7.7.4 : plafond de recherches 10 → 14 (PAS plus).
                    # Historique : à 10, Gemini ne documentait que 2-3 matchs/run
                    # (couverture bornée). Tenté 24 le 20/07 → l'appel n'est
                    # JAMAIS revenu, timeout GitHub Actions à 30 min, run tué.
                    # La latence du grounding n'est pas linéaire au-delà de ~15
                    # appels. 14 = gain modeste de couverture (+1-2 matchs) sans
                    # risquer le timeout. NE PAS remonter sans tester la durée.
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        maximum_remote_calls=14
                    ),
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
        # GARDE v7.2 : rep.text peut être None (réponse bloquée/vide) → retry
        texte = (rep.text or "").strip()
        if not texte:
            derniere_erreur = "réponse Gemini vide (rep.text=None)"
            # v8.6 : une réponse vide se répète presque toujours (même prompt,
            # même modèle) et chaque tentative coûte de plus en plus cher.
            # On s'arrête à 2 essais au lieu de 3.
            if tentative >= 2:
                logging.error("Gemini : 2 réponses vides consécutives — abandon "
                              "(la 3e tentative a fait sauter le timeout le 30/07).")
                return '{"matchs": [], "avertissements": "Gemini a renvoyé des réponses vides."}'
            logging.warning(f"Gemini réponse vide tentative {tentative}/3 — retry…")
            time.sleep(5)
            continue
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

# Écart maximal toléré entre l'estimation de Claude et le modèle (points de %).
# ⚠️ SEUIL PROVISOIRE : aucune donnée réelle ne l'a encore calibré. Il est
# volontairement GÉNÉREUX — le modèle est i.i.d. (il ignore momentum, pression,
# contexte), donc Claude a de vraies raisons de s'en écarter. Il n'attrape que
# les cas flagrants type Altmaier. Chaque comparaison est loguée : c'est ce
# journal qui permettra de le calibrer sur des faits plutôt qu'au jugé.
BC_ECART_MAX_POINTS = 20.0


def construire_bloc_modele(donnees_json):
    """
    Calcule la proba de référence Barnett-Clarke + le contexte fatigue de chaque
    match, et les met en forme pour le prompt de Claude.
    Retourne (texte_pour_prompt, {(j1,j2): proba_ref}) — le dict sert au garde-fou.
    """
    try:
        matchs = json.loads(donnees_json).get("matchs", [])
    except Exception:
        return "", {}

    blocs, refs = [], {}
    for m in matchs:
        j1, j2 = m.get("joueur1", ""), m.get("joueur2", "")
        if not j1 or not j2:
            continue
        morceaux = []
        proba, detail = bc_reference_match(m)
        if proba is not None:
            refs[(j1.lower(), j2.lower())] = proba
            morceaux.append(
                f"📐 RÉFÉRENCE CHIFFRÉE : {detail}\n"
                f"   → cote juste modèle : {j1} {1/proba:.2f} / {j2} {1/(1-proba):.2f}"
            )
        else:
            morceaux.append(f"📐 RÉFÉRENCE CHIFFRÉE : indisponible ({detail}).")
        txt_fat, _ = bc_indice_fatigue(m)
        if txt_fat:
            morceaux.append("🔋 CHARGE PHYSIQUE :\n" + txt_fat)
        if morceaux:
            blocs.append(f"— {j1} vs {j2} —\n" + "\n".join(morceaux))

    if not blocs:
        return "", refs

    entete = f"""
=== MODÈLE PROBABILISTE DE RÉFÉRENCE (Barnett-Clarke) ===
Pour les matchs ci-dessous, une probabilité a été calculée EN PYTHON à partir des
seuls % de points gagnés au service et au retour SUR LA SURFACE, par dérivation
analytique point → jeu → set → match. Aucune narration, aucun H2H, aucun ressenti.

COMMENT T'EN SERVIR :
• C'est ton POINT DE DÉPART, pas ton point d'arrivée. Le modèle suppose les points
  indépendants : il ignore le momentum, la pression, le contexte, la fatigue.
• Tu PEUX t'en écarter — mais uniquement avec une justification CHIFFRÉE (une stat
  qu'il n'a pas vue), jamais avec un récit ("il mène le H2H", "il a de l'envie").
• Un écart de plus de {BC_ECART_MAX_POINTS:.0f} points sans justification chiffrée sera REJETÉ par le code.
• "RÉFÉRENCE CHIFFRÉE : indisponible" → tu analyses comme avant, mais la règle
  anti-surestimation s'applique en plein : sans chiffres de surface, tu t'alignes
  sur le marché.

⚠️ LEÇON DU 15/07 (Altmaier vs Darderi, perdu 6-4 6-4) : le bot avait les Hold%
(76,5% contre 78,4% en faveur de Darderi, mieux classé et tenant du titre) et a
quand même parié Altmaier sur la foi d'un H2H de 3 matchs et de "la pression des
points à défendre", en déclarant "aucune donnée manquante". C'est EXACTEMENT ce que
ce modèle existe pour empêcher. Les chiffres d'abord, le récit ensuite — et le récit
ne renverse les chiffres que s'il est lui-même chiffré.

"""
    return entete + "\n\n".join(blocs) + "\n", refs


def construire_prompt_claude(date, heure, donnees_json, heure_fin="23:59"):
    # v7.5.3 : SOIR existait dans run_bot_autonome mais pas ici — après 19h,
    # Claude recevait "Session APRÈS-MIDI" à tort.
    if heure < "14:00":
        session = "MATIN"
    elif heure < "19:00":
        session = "APRÈS-MIDI"
    else:
        session = "SOIR"
    bloc_modele, _refs = construire_bloc_modele(donnees_json)
    try:
        avertissements = json.loads(donnees_json).get("avertissements", "Aucun")
    except Exception:
        avertissements = "Non disponibles"

    return f"""Tu es un expert en paris tennis. Date : {date} · {heure} France · Session {session}.

DONNÉES COLLECTÉES (source unique — ne pas chercher sur internet) :
{donnees_json}
{bloc_modele}

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
• Si tu écris "cote estimée" ou "non disponible précisément sur Winamax" → cest une VIOLATION. Ne génère PAS ce ticket.
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

🎾 GRILLE D'ANALYSE PAR SURFACE (v8.0 — CŒUR DE LA STRATÉGIE) :
Le revêtement change ce qui décide un match. Une stat clé sur gazon devient
secondaire sur terre. AVANT toute estimation, identifie la surface et applique
la grille correspondante. Les seuils ci-dessous sont les normes du circuit.

┌─ TERRE BATTUE — épreuve de force et d'endurance (lente, rebond haut)
│  Normes : Hold 78-82% · Break 22-28% · 1er service 68-73% · 2e service 52-57%
│  DÉCIDENT : % 2e service (élite >54%, exposé <46%) · Break% (fort >28%, faible <18%)
│             conversion des balles de break · ratio fautes directes/winners
│  Dynamique : échanges longs (65%+ des points >5 coups), service peu neutralisant,
│              primauté de l'endurance, du lift et du contre.
│  ⛔ PIÈGE : surévaluer un gros serveur — il perd 15-20% d'efficacité d'ace sur
│     terre. Un serveur dominant sur dur n'est PAS le même joueur sur ocre.
│     Privilégier le lift et une endurance supérieure à 2h30.
└─
┌─ GAZON — prime à la première balle (ultra-rapide, rebond bas)
│  Normes : Hold 85-90%+ · Break 12-18% · 1er service 76-82% · 2e service 48-53%
│  DÉCIDENT : % 1er service (inviolable >78%, incapable <68%) · Hold% (>88% top,
│             <78% vulnérable) · aces/doubles fautes · % tie-breaks gagnés
│  Dynamique : 70%+ des points en 1 à 4 coups. L'écart entre valeur du service et
│              du retour est à son MAXIMUM.
│  ⛔ PIÈGE : les breaks sont rares — un avantage au retour pèse beaucoup moins
│     qu'ailleurs. 25-30% des sets vont au tie-break : le sang-froid décide.
└─
┌─ DUR EXTÉRIEUR — équilibre polyvalent (neutre/rapide, rebond régulier)
│  Normes : Hold 80-85% · Break 18-24% · 1er service 72-77% · 2e service 50-54%
│  DÉCIDENT : Dominance Ratio (dominant >1.15, dominé <0.90) · équilibre
│             Hold%/Break% · % balles de break sauvées (excellent >65%)
│  Dynamique : test de polyvalence globale, équilibre service/fond de court.
│  ⛔ PIÈGE : la vitesse réelle varie fortement d'un tournoi à l'autre. Vent et
│     chaleur extrême usent physiquement — croiser avec la charge des 7 jours.
└─
┌─ DUR INDOOR — vitesse pure (très rapide, aucune météo)
│  Normes : Hold 83-88% · Break 15-20% · 1er service 74-80% · 2e service 50-55%
│  DÉCIDENT : Hold% · % 1er service rentré (>66% étouffe l'adversaire) ·
│             % points sur 2e service adverse · efficacité en 1re frappe
│  Dynamique : conditions parfaites, les frappeurs à plat s'expriment sans déchet.
│  ⛔ PIÈGE : un SEUL break décide souvent la manche — la marge de l'outsider au
│     retour est minimale. Ne pas parier un retourneur contre un gros serveur ici.
└─

RÈGLES D'USAGE DE LA GRILLE :
• Le Dominance Ratio = % points gagnés en retour / % points perdus au service.
  >1.20 ultra-dominant · ~1.00 neutre (match indécis) · <0.85 en difficulté.
• Compare TOUJOURS les stats sur la surface du match, jamais toutes surfaces
  confondues — c'est la première cause de fausse estimation.
• Situe chaque joueur par rapport aux normes ci-dessus : un Hold de 80% est
  médiocre sur gazon mais correct sur terre. Un chiffre ne vaut que comparé
  à la norme de SA surface.
• Si un joueur est "élite" sur les stats prioritaires de la surface et son
  adversaire "vulnérable", c'est un signal fort — mentionne-le explicitement.
• À l'inverse, si l'avantage d'un joueur porte sur une stat SECONDAIRE pour
  cette surface, ne la survalorise pas.

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
  · Delta ≥ seuil requis (0.07/0.10/0.12 selon la cote — voir plus bas) ✅
  · Analyse solide (forme, H2H par surface, contexte) ✅
  · Données complètes ou quasi-complètes ✅
  · Pas d'alerte physique majeure ✅

MODÉRÉE (2%) — analyse correcte mais légères lacunes :
  · Delta ≥ seuil requis ✅
  · Analyse correcte mais quelques données manquantes
  · H2H limité ou forme partielle acceptable
  · Logique analytique convaincante malgré les lacunes

BASSE (1%) — données insuffisantes ou cote très élevée :
  · Delta ≥ seuil requis ✅ mais données importantes manquantes
  · OU cote > 2.50 (variance élevée)
  · OU première confrontation sans contexte suffisant
  · OU alertes physiques présentes
  · OU retour de blessure 3-8 semaines

Plafonds absolus (quel que soit le niveau) :
  · Alertes physiques → MAX 1%
  · Retour 3-8 semaines → MAX 1%
  · Combiné → MAX 2%

⚠️⚠️⚠️ RÈGLE ABSOLUE — FORMAT DE SORTIE — VIOLATION = ÉCHEC ⚠️⚠️⚠️
Ta réponse doit commencer DIRECTEMENT par 🎾 ou par AUCUN_MATCH.
ZÉRO tolérance pour tout texte avant le 🎾 ou AUCUN_MATCH.

EXEMPLES DE CE QUI EST INTERDIT (ne JAMAIS écrire) :
❌ "Let me analyze..." 
❌ "**FILTERING:**"
❌ "ÉTAPE 1..."
❌ "Voici mon analyse..."
❌ "Remaining matches..."
❌ Tout texte, titre, bullet point AVANT le 🎾

SEULES RÉPONSES VALIDES :
✅ 🎾 <b>ACEANALYTICS TENNIS...</b>  (si value trouvée)
✅ AUCUN_MATCH — [explication courte]  (si aucune value)

L'analyse est STRICTEMENT INTERNE — invisible — jamais dans la réponse.
Si tu commences par autre chose que 🎾 ou AUCUN_MATCH → tu as échoué.

ANALYSE EN 2 ÉTAPES (INTERNE — NE PAS AFFICHER) :

[1] FACTEURS BRUTS :
  Surface + forme 5 matchs + charge 72h + Hold% + H2H par surface
  Contexte psychologique : points à défendre, public local, GC dans 7j
  Fatigue : match long hier, titre récent, 3 matchs en 5j

[2] DÉCISION :
  Proba % → Cote Juste = 1/proba → Delta = Cote réelle - Cote Juste

  ⚖️ SEUIL DE DELTA ADAPTATIF SELON LA COTE (règle unique — remplace tout seuil fixe) :
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
  1. Delta ≥ seuil requis ✅  2. Analyse [1] justifie le marché ✅
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

N'envoyer QUE les tickets validés (Delta ≥ seuil requis + analyse confirmée).
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

⚠️ RÈGLE COTE PINNACLE (v7.8) : si le match a source_cote contenant "Pinnacle"
(cote complétée par le repli, pas vue sur Winamax), tu DOIS ajouter cette ligne
à la fin du ticket, telle quelle :
⚠️ <b>COTE PINNACLE</b> — vérifier la cote Winamax avant de miser (peut différer)
Et tu appliques le seuil de delta OUTSIDER (le plus strict) quel que soit le
profil : la cote Winamax réelle peut être moins bonne que la Pinnacle affichée.
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
# 11-BIS. MODÈLE PROBABILISTE POINT-PAR-POINT (Barnett-Clarke)
# =====================================================================
# v7.6 — Calcule une probabilité de victoire de RÉFÉRENCE à partir des seules
# statistiques service/retour par surface. Aucune calibration, aucun coefficient
# ajusté : c'est de la probabilité pure (Barnett & Clarke, 2005), dérivée
# analytiquement du point → jeu → tie-break → set → match.
#
# POURQUOI : constat du 15/07 (Altmaier). Claude annonçait "45%" sur la foi d'un
# H2H de 3 matchs, en déclarant lui-même "aucune donnée manquante" — ce qui
# désactivait le garde-fou (seuil 30% au lieu de 10%). Le contrôle était donc
# DÉCLARATIF : Claude choisissait la sévérité qu'on lui appliquait.
# Ici, Python calcule une proba de référence à partir des CHIFFRES. Claude doit
# justifier tout écart. Le contrôle devient CALCULÉ.
#
# LIMITE ASSUMÉE : le modèle suppose les points indépendants et identiquement
# distribués (i.i.d.). Il ignore donc le momentum, la pression et la fatigue
# intra-match. C'est une référence de base, PAS une vérité — d'où le fait qu'on
# autorise Claude à s'en écarter avec justification chiffrée.

# Moyennes du circuit : % de points gagnés au service, par surface.
# ⚠️ PARAMÈTRES APPROCHÉS — dérivés d'une étude PLOS One sur 4 669 points de
# Grand Chelem 2021 (efficacité 1ère balle : 69% terre / 75% gazon / 75% dur ;
# 2ème balle ~55% quelle que soit la surface) combinée à ~62% de 1ères balles
# en jeu, et recoupés avec la moyenne tour ~63% (Berkeley Sports Analytics).
# Ces constantes sont VOLONTAIREMENT isolées ici pour être corrigées dès qu'une
# source fiable par surface sera disponible.
# Note : une erreur sur ces moyennes décale p_A ET p_B dans le même sens, donc
# s'annule en grande partie sur la probabilité de match (c'est l'ÉCART qui pèse).
# v8.1 — MOYENNES DU CIRCUIT : % de points gagnés au SERVICE, par circuit ET
# surface. Source : relevé fourni par l'exploitant (moyennes de tour, 52 sem.).
# Validation croisée : les Hold% que ces valeurs impliquent via le modèle
# (ATP 78.5/82.1/84.3/86.4%) tombent exactement dans les fourchettes de la
# matrice d'analyse (78-82 / 80-85 / 83-88 / 85-90%) → cohérence confirmée.
# L'écart ATP/WTA est d'environ 8 points : c'est LUI qui écrasait les joueuses
# quand une moyenne ATP leur était appliquée (bug du 28/07 : hold calculé 30%).
BC_SERVE_MOYEN_CIRCUIT = {
    "atp": {"terre": 0.625, "dur": 0.645, "dur_indoor": 0.658, "gazon": 0.672},
    "wta": {"terre": 0.545, "dur": 0.565, "dur_indoor": 0.575, "gazon": 0.590},
}
BC_SERVE_MOYEN_CIRCUIT["atp"]["autre"] = BC_SERVE_MOYEN_CIRCUIT["atp"]["dur"]
BC_SERVE_MOYEN_CIRCUIT["wta"]["autre"] = BC_SERVE_MOYEN_CIRCUIT["wta"]["dur"]

# Rétrocompatibilité : l'ancien nom pointe sur l'ATP (défaut historique).
BC_SERVE_MOYEN_SURFACE = BC_SERVE_MOYEN_CIRCUIT["atp"]

# NORMES DU CIRCUIT par surface. Les seuils serve1/serve2/elite/vulnerable et
# les pièges sont issus de la matrice d'analyse (données ATP). Les hold/break
# sont déclinés PAR CIRCUIT : en WTA les breaks sont bien plus fréquents, un
# hold de 66% y est normal alors qu'il serait catastrophique en ATP.
# Servent : (1) aux contrôles de cohérence du modèle, (2) à qualifier le profil
# d'un joueur (élite / vulnérable) et (3) à guider la collecte et l'analyse.
# 'prioritaires' = les stats qui décident réellement du match sur cette surface.
BC_NORMES_SURFACE = {
    "terre": {
        "libelle": "Terre battue (lente, rebond haut)",
        "hold": (0.78, 0.82), "break": (0.22, 0.28),
        "serve1": (0.68, 0.73), "serve2": (0.52, 0.57),
        "prioritaires": ["% gagné 2e service", "% points gagnés en retour",
                         "% conversion balles de break", "ratio fautes/winners"],
        "elite":     {"serve2": 0.54, "break": 0.28},
        "vulnerable": {"serve2": 0.46, "break": 0.18},
        "piege": "Surévaluer un gros serveur : il perd 15-20% d'efficacité d'ace "
                 "sur terre. Privilégier lift et endurance (>2h30).",
    },
    "gazon": {
        "libelle": "Gazon (ultra-rapide, rebond bas)",
        "hold": (0.85, 0.90), "break": (0.12, 0.18),
        "serve1": (0.76, 0.82), "serve2": (0.48, 0.53),
        "prioritaires": ["% gagné 1er service", "ratio aces/doubles fautes",
                         "% points au filet", "% tie-breaks gagnés"],
        "elite":     {"serve1": 0.78, "hold": 0.88},
        "vulnerable": {"serve1": 0.68, "hold": 0.78},
        "piege": "Les breaks sont rares (Hold >88%) : un écart de retour pèse peu, "
                 "tout se joue sur le 1er service et les tie-breaks.",
    },
    "dur": {
        "libelle": "Dur extérieur (neutre/rapide, rebond régulier)",
        "hold": (0.80, 0.85), "break": (0.18, 0.24),
        "serve1": (0.72, 0.77), "serve2": (0.50, 0.54),
        "prioritaires": ["Dominance Ratio (DR)", "équilibre Hold%/Break%",
                         "% balles de break sauvées", "% points 1er et 2e service"],
        "elite":     {"dr": 1.15, "bp_sauvees": 0.65},
        "vulnerable": {"dr": 0.90},
        "piege": "Vitesse réelle variable d'un tournoi à l'autre (Indian Wells lent "
                 "vs Shanghai rapide). Météo : vent et chaleur usent physiquement.",
    },
    "dur_indoor": {
        "libelle": "Dur indoor (très rapide, conditions parfaites)",
        "hold": (0.83, 0.88), "break": (0.15, 0.20),
        "serve1": (0.74, 0.80), "serve2": (0.50, 0.55),
        "prioritaires": ["Hold %", "% 1er service rentré",
                         "% points sur 2e service adverse", "efficacité 1re frappe"],
        "elite":     {"serve1_rentre": 0.66, "hold": 0.88},
        "vulnerable": {"hold": 0.80},
        "piege": "Un seul break décide souvent la manche : la marge de l'outsider "
                 "au retour est minimale. Avantage aux frappeurs à plat.",
    },
}
BC_NORMES_SURFACE["autre"] = BC_NORMES_SURFACE["dur"]

# v8.1 — NORMES HOLD/BREAK PAR CIRCUIT. Dérivées des moyennes de service
# ci-dessus via le modèle lui-même (bc_proba_jeu) : aucune valeur inventée.
# Écart majeur : en WTA on tient son service ~16 points de moins qu'en ATP,
# et on breake environ deux fois plus. Juger une joueuse à l'aune des normes
# masculines produit des probabilités absurdes (constat du 28/07).
BC_NORMES_CIRCUIT = {
    "atp": {
        "terre":      {"hold": (0.755, 0.815), "break": (0.185, 0.245)},
        "dur":        {"hold": (0.790, 0.850), "break": (0.150, 0.210)},
        "dur_indoor": {"hold": (0.815, 0.875), "break": (0.125, 0.185)},
        "gazon":      {"hold": (0.835, 0.895), "break": (0.105, 0.165)},
    },
    "wta": {
        "terre":      {"hold": (0.580, 0.640), "break": (0.360, 0.420)},
        "dur":        {"hold": (0.630, 0.690), "break": (0.310, 0.370)},
        "dur_indoor": {"hold": (0.650, 0.710), "break": (0.290, 0.350)},
        "gazon":      {"hold": (0.685, 0.745), "break": (0.255, 0.315)},
    },
}
for _c in ("atp", "wta"):
    BC_NORMES_CIRCUIT[_c]["autre"] = BC_NORMES_CIRCUIT[_c]["dur"]


def bc_proba_point_service(serve_pts_won, return_pts_won_adversaire, surface="autre",
                           circuit="atp"):
    """
    Probabilité que le serveur gagne un point, ajustée par la qualité du retourneur.
    Formule de combinaison (Barnett & Clarke) :
        p = f_serveur - g_retourneur + g_moyen
    où g_moyen = 1 - f_moyen (par CIRCUIT et par surface).
    Vérification : deux joueurs moyens → p = f_moyen (le modèle ne dérive pas).
    v8.1 : le circuit compte autant que la surface — la moyenne de service WTA
    est ~8 points sous l'ATP. Utiliser la moyenne masculine pour une joueuse
    écrasait sa probabilité (hold calculé à 30%, constat du 28/07).
    """
    table = BC_SERVE_MOYEN_CIRCUIT.get(str(circuit).lower(), BC_SERVE_MOYEN_CIRCUIT["atp"])
    f_moyen = table.get(surface, table["autre"])
    g_moyen = 1.0 - f_moyen
    p = serve_pts_won - return_pts_won_adversaire + g_moyen
    return min(max(p, 0.01), 0.99)  # borne de sécurité numérique


def bc_proba_jeu(p):
    """
    Probabilité de remporter un jeu de service. Forme close exacte.
    Décomposition : 40-0 + 40-15 + 40-30 + (deuce × proba de conclure depuis deuce).
    Le terme deuce vaut p²/(p²+q²), et p²+q² = 1-2pq.
    Contrôles : bc_proba_jeu(0.5) = 0.5 exactement ; bc_proba_jeu(0.65) ≈ 0.830
    (cohérent avec la relation ATP connue : 65% de points au service ≈ 83% de hold).
    """
    q = 1.0 - p
    denom = 1.0 - 2.0 * p * q
    if denom <= 0:
        return 1.0 if p > 0.5 else 0.0
    return (p**4
            + 4.0 * p**4 * q
            + 10.0 * p**4 * q**2
            + 20.0 * p**5 * q**3 / denom)


def bc_proba_tiebreak(pa, pb):
    """
    Probabilité que A remporte un tie-break (premier à 7, écart de 2).
    Rotation de service réelle : A sert le point 1, puis les services vont par
    paires (B-B, A-A, B-B, ...). Récursion mémoïsée sur le score.
    pa = proba que A gagne un point sur SON service ; pb = idem pour B.
    """
    from functools import lru_cache

    @lru_cache(maxsize=None)
    def t(a, b):
        if a >= 7 and a - b >= 2:
            return 1.0
        if b >= 7 and b - a >= 2:
            return 0.0
        # Sécurité : au-delà de 6-6, l'écart de 2 finit toujours par arriver ;
        # on borne la récursion pour éviter une profondeur infinie théorique.
        if a > 30 or b > 30:
            return 0.5
        m = a + b                      # numéro du point à jouer (0-indexé)
        a_sert = ((m + 1) // 2) % 2 == 0
        p_a_gagne_le_point = pa if a_sert else (1.0 - pb)
        return (p_a_gagne_le_point * t(a + 1, b)
                + (1.0 - p_a_gagne_le_point) * t(a, b + 1))

    return t(0, 0)


def bc_proba_set(pa, pb, a_sert_premier=True):
    """
    Probabilité que A remporte un set (premier à 6 jeux, écart de 2, tie-break à 6-6).
    Récursion mémoïsée sur le score en jeux, avec alternance réelle du service.
    """
    from functools import lru_cache

    ga = bc_proba_jeu(pa)          # proba que A tienne son service
    gb = bc_proba_jeu(pb)          # proba que B tienne le sien
    tb = bc_proba_tiebreak(pa, pb)

    @lru_cache(maxsize=None)
    def s(a, b, a_sert):
        if a >= 6 and a - b >= 2:
            return 1.0
        if b >= 6 and b - a >= 2:
            return 0.0
        if a == 6 and b == 6:
            return tb
        if a_sert:
            return ga * s(a + 1, b, False) + (1.0 - ga) * s(a, b + 1, False)
        return gb * s(a, b + 1, True) + (1.0 - gb) * s(a + 1, b, True)

    return s(0, 0, a_sert_premier)


def bc_proba_match(pa, pb, best_of=3):
    """
    Probabilité que A remporte le match.
    Le tirage au sort du premier serveur est inconnu → on moyenne les deux cas.
    best_of=3 : premier à 2 sets. best_of=5 : premier à 3 sets.
    Hypothèse standard : sets indépendants et de même probabilité.
    """
    s = 0.5 * (bc_proba_set(pa, pb, True) + bc_proba_set(pa, pb, False))
    if best_of == 5:
        # 3-0 + 3-1 + 3-2
        return s**3 * (1.0 + 3.0 * (1.0 - s) + 6.0 * (1.0 - s)**2)
    # 2-0 + 2-1
    return s**2 * (3.0 - 2.0 * s)


def bc_dominance_ratio(serve_pts_won, return_pts_won):
    """
    Dominance Ratio (Tennis Abstract) — définition EXACTE :
        DR = % de points gagnés au RETOUR ÷ % de points perdus au SERVICE
           = g / (1 - f)
    ⚠️ Ce n'est PAS "crée plus de balles de break qu'il n'en concède" (glose
    répandue mais fausse) : le DR compare des POINTS, pas des balles de break.

    Lecture (le seuil 1.00 est la moyenne du circuit PAR CONSTRUCTION, puisque
    le retourneur moyen gagne exactement ce que le serveur moyen concède) :
      DR > 1.20 : rouleau compresseur
      DR > 1.00 : domine — produit plus au retour qu'il ne fuit au service
      DR < 1.00 : subit — s'en sort surtout grâce aux points clés (variance)
    Le DR n'apporte AUCUNE information au modèle Barnett-Clarke (qui exploite f
    et g directement, et plus finement). Son intérêt est d'être LISIBLE : une
    seule valeur résume l'ascendant, utilisable dans le tableau comparatif.
    """
    pts_perdus_service = 1.0 - serve_pts_won
    if pts_perdus_service <= 0:
        return None
    return return_pts_won / pts_perdus_service


def bc_indice_combine(hold_pct, break_pct):
    """
    Indice Combiné = Hold% + Break%. > 105% = niveau élite, < 95% = vulnérable.
    Retourne None si l'une des deux stats manque (on n'invente pas).
    """
    if hold_pct is None or break_pct is None:
        return None
    return hold_pct + break_pct


def bc_coherence_hold(serve_pts_won, hold_pct_annonce, tolerance=0.08):
    """
    Contrôle de cohérence : le Hold% annoncé par Gemini est-il compatible avec
    le % de points gagnés au service ? bc_proba_jeu(f) donne le hold attendu
    contre un retourneur moyen. Un écart important signale une stat incohérente
    — typiquement un Hold% pris sur une AUTRE surface que le service%.
    Retourne (True/False, hold_attendu) ou (None, None) si non calculable.
    """
    if serve_pts_won is None or hold_pct_annonce is None:
        return None, None
    attendu = bc_proba_jeu(serve_pts_won)
    return abs(attendu - hold_pct_annonce) <= tolerance, attendu


# --- FATIGUE (v7.6) ------------------------------------------------------
# ⚠️ CHOIX ASSUMÉ : la fatigue n'entre PAS dans le calcul de probabilité.
# Raison : la recherche établit la fatigue PHYSIOLOGIQUE (force max -14% après
# match, récupération à 72h ; qualités explosives dégradées après un tournoi de
# 3 jours) mais PAS son effet chiffré sur l'issue d'un match ATP. Une étude sur
# 3 matchs de 2h en 3 jours ne trouve aucune baisse notable de performance chez
# des joueurs bien récupérés — et les pros ont des staffs dédiés. Aucun
# coefficient crédible n'existe : en inventer un ("-0.5% de service par 100
# minutes") fabriquerait de la fausse précision et corromprait le modèle.
# Ce qu'on fait à la place : mesurer la fatigue de façon DÉTERMINISTE et la
# donner à Claude comme CONTEXTE, + s'en servir comme plafond de confiance.
# Les seuils de récupération ci-dessous sont, eux, documentés (48-72h).
FATIGUE_RECUP_INCOMPLETE_H = 24   # < 24h : récupération clairement incomplète
FATIGUE_RECUP_PARTIELLE_H  = 48   # 24-48h : partielle
FATIGUE_RECUP_COMPLETE_H   = 72   # > 72h : considérée complète


def _bc_parse_nombre(valeur):
    """Extrait un nombre ('240 min', '3', 240) → 240.0. None si illisible.
    v7.9.2 : chiffre PRIORITAIRE (même correctif que _bc_parse_pct du 20/07) —
    Gemini recopie parfois le gabarit ("240 min ou non trouvé") ; l'ancien test
    jetait la valeur avant de chercher le chiffre. "non trouvé" ne vaut que seul."""
    if valeur is None:
        return None
    if isinstance(valeur, (int, float)):
        return float(valeur)
    txt = str(valeur).strip().lower()
    if not txt:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", txt)
    return float(m.group(1).replace(",", ".")) if m else None


def bc_indice_fatigue(m):
    """
    Mesure DESCRIPTIVE de l'asymétrie de fatigue entre les deux joueurs.
    Ne renvoie AUCUNE probabilité — uniquement des faits chiffrés et des
    drapeaux, à charge pour Claude de les pondérer et pour le garde-fou de
    plafonner la confiance.
    Retourne (texte_pour_prompt, dict_drapeaux) ou (None, {}) si données absentes.
    """
    f1 = m.get("fatigue_j1") or {}
    f2 = m.get("fatigue_j2") or {}
    min1, min2 = _bc_parse_nombre(f1.get("minutes_7j")), _bc_parse_nombre(f2.get("minutes_7j"))
    h1, h2 = _bc_parse_nombre(f1.get("heures_depuis_dernier_match")), _bc_parse_nombre(f2.get("heures_depuis_dernier_match"))
    n1, n2 = _bc_parse_nombre(f1.get("nb_matchs_7j")), _bc_parse_nombre(f2.get("nb_matchs_7j"))

    if all(x is None for x in (min1, min2, h1, h2, n1, n2)):
        return None, {}

    j1, j2 = m.get("joueur1", "J1"), m.get("joueur2", "J2")
    drapeaux = {}
    lignes = []

    if min1 is not None and min2 is not None:
        ecart = min1 - min2
        lignes.append(f"Minutes jouées sur 7j : {j1} {min1:.0f} min · {j2} {min2:.0f} min "
                      f"(écart {ecart:+.0f} min)")
        # Asymétrie forte = simple signalement, PAS un ajustement de proba
        base = min(min1, min2)
        if base > 0 and max(min1, min2) / base >= 2.0:
            plus_charge = j1 if min1 > min2 else j2
            drapeaux["asymetrie_forte"] = plus_charge
            lignes.append(f"⚠️ {plus_charge} a joué au moins 2× plus de minutes que son adversaire.")

    for joueur, h in ((j1, h1), (j2, h2)):
        if h is None:
            continue
        if h < FATIGUE_RECUP_INCOMPLETE_H:
            drapeaux.setdefault("recup_incomplete", []).append(joueur)
            lignes.append(f"⚠️ {joueur} : {h:.0f}h depuis son dernier match — récupération "
                          f"incomplète (documenté : la force max n'est récupérée qu'à ~72h).")
        elif h < FATIGUE_RECUP_PARTIELLE_H:
            lignes.append(f"{joueur} : {h:.0f}h de repos — récupération partielle.")
        else:
            lignes.append(f"{joueur} : {h:.0f}h de repos — récupération considérée complète.")

    if n1 is not None and n2 is not None:
        lignes.append(f"Matchs sur 7j : {j1} {n1:.0f} · {j2} {n2:.0f}")

    lignes.append("→ Ces éléments sont du CONTEXTE, pas un ajustement de probabilité : "
                  "aucun coefficient fiable ne relie la fatigue à l'issue d'un match ATP. "
                  "Ils justifient de la PRUDENCE (confiance revue à la baisse), jamais de "
                  "survaloriser le joueur frais au-delà de ce que disent ses chiffres.")
    return "\n".join(lignes), drapeaux


def _bc_parse_pct(valeur):
    """Extrait un pourcentage ('64.2%', '64,2', 64.2) → 0.642. None si illisible.
    v7.7.5 : le NOMBRE prime. Constat du 20/07 : Gemini recopie le gabarit du
    schéma et produit "60.3% ou non trouvé" — l'ancien test `"non trouv" in txt`
    jetait la valeur AVANT de chercher le chiffre → modèle silencieux à tort sur
    un match aux stats complètes (Kopriva/Buse, éch. 43 et 38 matchs).
    Règle : s'il y a un nombre, on le prend ; "non trouvé" ne vaut que seul."""
    if valeur is None:
        return None
    if isinstance(valeur, (int, float)):
        v = float(valeur)
        return v / 100.0 if v > 1.5 else v
    txt = str(valeur).strip().lower()
    if not txt:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", txt)
    if not m:
        return None  # aucun chiffre ("non trouvé", "?", vide) → donnée absente
    v = float(m.group(1).replace(",", "."))
    return v / 100.0 if v > 1.5 else v


def _bc_cle_surface(m):
    """
    v8.0 — Clé de surface pour le MODÈLE (distingue le dur INDOOR du dur extérieur).
    Le tableau des normes montre un écart réel : hold 83-88% en salle contre 80-85%
    dehors — un serveur y est nettement moins breakable. Le champ 'indoor' du schéma
    Gemini existait mais n'était pas exploité.
    ⚠️ Ne remplace PAS _normaliser_surface(), qui reste en terre/dur/gazon/autre
    pour la segmentation des statistiques (stats.json).
    """
    base = _normaliser_surface(m.get("surface"))
    if base == "dur":
        ind = m.get("indoor")
        if ind is True or str(ind).strip().lower() in ("true", "oui", "yes", "1"):
            return "dur_indoor"
        # Certains tournois indoor ne sont signalés que dans le nom
        nom = f"{m.get('tournoi','')} {m.get('surface','')}".lower()
        if "indoor" in nom or "en salle" in nom or "hallenstadion" in nom:
            return "dur_indoor"
    return base


def _bc_circuit(m):
    """
    v8.1 — Détermine le circuit (atp/wta) d'un match. Décisif : les moyennes de
    service diffèrent de ~8 points entre les deux circuits.
    Ordre : champ explicite du JSON Gemini → nom du tournoi → défaut ATP.
    """
    c = str(m.get("circuit") or m.get("tour") or "").strip().lower()
    if c in ("wta", "féminin", "feminin", "women", "w"):
        return "wta"
    if c in ("atp", "masculin", "men", "m"):
        return "atp"
    nom = f"{m.get('tournoi', '')}".lower()
    if "wta" in nom or "ladies" in nom or "women" in nom or "féminin" in nom:
        return "wta"
    return "atp"   # défaut historique


def bc_reference_match(m):
    """
    Calcule la probabilité de référence de J1 à partir des stats de surface d'un
    match du JSON Gemini. Retourne (proba_j1, details_texte) ou (None, raison).
    N'invente RIEN : si une des 4 stats manque, on renvoie None et le modèle se tait.
    """
    s1 = m.get("stats_surface_j1") or {}
    s2 = m.get("stats_surface_j2") or {}
    f1 = _bc_parse_pct(s1.get("serve_pts_won"))
    g1 = _bc_parse_pct(s1.get("return_pts_won"))
    f2 = _bc_parse_pct(s2.get("serve_pts_won"))
    g2 = _bc_parse_pct(s2.get("return_pts_won"))
    if None in (f1, g1, f2, g2):
        return None, "stats service/retour par surface incomplètes"

    # PORTIER 1 — plausibilité brute. Une stat hors de ces bornes n'est pas une
    # stat de tennis : c'est une hallucination ou un champ mal rempli.
    if not (0.45 <= f1 <= 0.85 and 0.45 <= f2 <= 0.85):
        return None, f"service% implausible ({f1:.0%} / {f2:.0%}) — donnée rejetée"
    if not (0.15 <= g1 <= 0.55 and 0.15 <= g2 <= 0.55):
        return None, f"retour% implausible ({g1:.0%} / {g2:.0%}) — donnée rejetée"

    # PORTIER 2 — cohérence interne. Si le Hold% annoncé ne colle pas au hold
    # qu'implique le service%, les deux stats viennent probablement de surfaces
    # différentes (le bug qu'on vient de corriger dans la collecte). On refuse.
    for hold_brut, f_joueur, nom in ((m.get("hold_pct_j1"), f1, m.get("joueur1", "J1")),
                                     (m.get("hold_pct_j2"), f2, m.get("joueur2", "J2"))):
        hold = _bc_parse_pct(hold_brut)
        coherent, attendu = bc_coherence_hold(f_joueur, hold)
        if coherent is False:
            return None, (f"incohérence {nom} : service {f_joueur:.0%} implique un hold "
                          f"~{attendu:.0%}, mais {hold:.0%} annoncé — stats de surfaces "
                          f"différentes ? Modèle désactivé sur ce match.")

    # PORTIER 3 — échantillon. v8.0.1 : seuil relevé de 5 à 10 matchs.
    # Constat du 28/07 : Andres Martin (qualifié, 6 matchs sur dur) ressortait
    # avec de MEILLEURES stats qu'Ugo Humbert (top 30, 18 matchs) → le modèle
    # donnait Humbert perdant à 38,9% contre un marché à 82%. Six matchs contre
    # une opposition faible gonflent les pourcentages : c'est du bruit, pas un
    # signal. La matrice d'analyse par surface prend elle-même 10 matchs comme
    # cadre de référence minimal.
    for s, nom in ((s1, m.get("joueur1", "J1")), (s2, m.get("joueur2", "J2"))):
        n = _bc_parse_nombre(s.get("echantillon"))
        if n is not None and n < 10:
            return None, f"échantillon trop faible ({nom} : {n:.0f} matchs sur la surface)"

    surface = _bc_cle_surface(m)   # v8.0 : distingue dur indoor / extérieur
    circuit = _bc_circuit(m)       # v8.1 : ATP et WTA n'ont pas les mêmes normes
    pa = bc_proba_point_service(f1, g2, surface, circuit)   # J1 sert contre J2
    pb = bc_proba_point_service(f2, g1, surface, circuit)   # J2 sert contre J1

    # PORTIER 4 (v8.0.1) — PLAUSIBILITÉ DE LA SORTIE, pas seulement de l'entrée.
    # Constat du 28/07 : des stats valides à l'entrée (Bucsa 54% service, dans les
    # bornes du portier 1) produisaient un hold calculé de 30% et une proba de
    # match de 4,6% — physiquement impossible chez un professionnel, la norme du
    # circuit sur dur étant 80-85%. Cause : les moyennes de référence sont
    # calibrées sur des normes ATP ; appliquées à des stats WTA (service
    # structurellement plus bas), le modèle écrase la joueuse.
    # Règle : si le hold calculé tombe plus de 20 points sous la norme basse de
    # la surface, les données ne sont pas compatibles avec le modèle → il SE TAIT
    # (fidèle au principe : ne rien inventer plutôt que produire une absurdité).
    _nc = BC_NORMES_CIRCUIT.get(circuit, BC_NORMES_CIRCUIT["atp"])
    norme_hold = _nc.get(surface, _nc["dur"])["hold"]
    seuil_bas = norme_hold[0] - 0.20
    for p_srv, nom in ((pa, m.get("joueur1", "J1")), (pb, m.get("joueur2", "J2"))):
        hold_calc = bc_proba_jeu(p_srv)
        if hold_calc < seuil_bas:
            return None, (f"hold calculé implausible ({nom} : {hold_calc:.0%} contre une "
                          f"norme {circuit.upper()}/{surface} de "
                          f"{norme_hold[0]:.0%}-{norme_hold[1]:.0%}) — stats erronées ou "
                          f"incompatibles. Modèle désactivé sur ce match.")

    proba_j1 = bc_proba_match(pa, pb, best_of=3)

    dr1 = bc_dominance_ratio(f1, g1)
    dr2 = bc_dominance_ratio(f2, g2)
    # Indice Combiné (Hold% + Break%) : >105% élite, <95% vulnérable.
    # Purement lisible — n'entre pas dans le calcul, qui exploite f et g.
    ic1 = bc_indice_combine(_bc_parse_pct(m.get("hold_pct_j1")),
                            _bc_parse_pct(s1.get("break_pct")))
    ic2 = bc_indice_combine(_bc_parse_pct(m.get("hold_pct_j2")),
                            _bc_parse_pct(s2.get("break_pct")))
    j1, j2 = m.get("joueur1", "J1"), m.get("joueur2", "J2")
    details = (
        f"Modèle Barnett-Clarke ({circuit.upper()}/{surface}) — "
        f"{j1} : {f1:.1%} service / {g1:.1%} retour"
        f"{f' / DR {dr1:.2f}' if dr1 else ''}"
        f"{f' / indice {ic1:.0%}' if ic1 else ''} · "
        f"{j2} : {f2:.1%} service / {g2:.1%} retour"
        f"{f' / DR {dr2:.2f}' if dr2 else ''}"
        f"{f' / indice {ic2:.0%}' if ic2 else ''} "
        f"→ points au service {pa:.1%} vs {pb:.1%} "
        f"→ hold {bc_proba_jeu(pa):.1%} vs {bc_proba_jeu(pb):.1%} "
        f"→ proba match {j1} {proba_j1:.1%}"
    )
    return proba_j1, details


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
    # v7.5.3 : en fenêtre NOCTURNE (session SOIR, ex: 19:30→05:00), le
    # LENDEMAIN est aussi accepté pour les matchs d'après minuit — sinon le
    # filtre date rejetait silencieusement tous les matchs 00:00-05:00
    # (typiquement les Challengers américains) que la fenêtre autorisait.
    fenetre_nocturne = heure_fin < heure_debut  # ex: 22:50 → 05:00
    if date_jour:
        date_lendemain = None
        if fenetre_nocturne:
            try:
                d = datetime.strptime(date_jour, "%d/%m/%Y")
                date_lendemain = (d + timedelta(days=1)).strftime("%d/%m/%Y")
            except Exception:
                pass

        def _bonne_date(m):
            dm = str(m.get("date_match", "")).strip()
            if not dm:
                return True  # date absente → on ne filtre pas ici (prudence), l'heure filtrera
            # Normaliser : extraire JJ/MM/AAAA
            md = re.search(r"(\d{2})[/-](\d{2})[/-](\d{4})", dm)
            if not md:
                return True  # format inattendu → garder, ne pas casser
            date_normalisee = f"{md.group(1)}/{md.group(2)}/{md.group(3)}"
            if date_normalisee == date_jour:
                return True
            # Lendemain accepté UNIQUEMENT si fenêtre nocturne ET heure ≤ heure_fin
            # (un match du lendemain à 14:00 reste hors session, il sera rejoué demain)
            if date_lendemain and date_normalisee == date_lendemain:
                mh = re.search(r"(\d{2}):(\d{2})", str(m.get("heure_match", "")))
                if mh and mh.group(0) <= heure_fin:
                    return True
            return False
        avant = len(matchs)
        matchs = [m for m in matchs if _bonne_date(m)]
        retires_date = avant - len(matchs)
        if retires_date:
            logging.warning(
                f"Filtre DATE : {retires_date} match(s) à une date ≠ {date_jour} retiré(s) "
                f"(probable hallucination de date par Gemini)."
            )

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


# Marqueurs des circuits ITF/Futures — Winamax ne les propose pas, et leurs
# stats (Hold%, forme) sont introuvables → la règle anti-surestimation les
# bloque de toute façon. Constat empirique runs du 10/07/2026.
MARQUEURS_ITF = ("itf", "futures", "m15", "m25", "w15", "w25",
                 "w35", "w50", "w75", "w100", "utr")

def filtrer_json_itf(donnees_json):
    """
    Filtre ITF (v7.4) : retire les matchs ITF/Futures du pipeline.
    On ne garde que ATP / WTA / Challenger / Grand Chelem (+ qualifs GC).
    Économie : moins de tokens Gemini/Claude sur des matchs injouables.
    """
    try:
        data = json.loads(donnees_json)
    except Exception:
        return donnees_json
    matchs = data.get("matchs", [])
    if not matchs:
        return donnees_json
    motif = re.compile(r"\b(" + "|".join(MARQUEURS_ITF) + r")\b")

    def _est_itf(m):
        return bool(motif.search(str(m.get("tournoi", "")).lower()))

    gardes  = [m for m in matchs if not _est_itf(m)]
    retires = len(matchs) - len(gardes)
    if retires:
        logging.info(f"Filtre ITF : {retires} match(s) ITF/Futures retiré(s), {len(gardes)} gardé(s).")
    data["matchs"] = gardes
    return json.dumps(data, ensure_ascii=False, indent=2)


def filtrer_json_matchs_non_confirmes(donnees_json, fixtures, rapid_matchs):
    """
    v8.4 — Un match doit être CONFIRMÉ par au moins une source officielle
    (fixtures OddsPapi ou calendrier RapidAPI). Gemini seul ne suffit pas.
    Constat du 28/07 : « Aleksandar Vukic vs Zachary Svajda à 03:00 » analysé
    alors que Vukic jouait en réalité le lendemain contre Musetti — match déjà
    disputé, ressorti par Gemini avec une heure inventée. Quatre des six matchs
    inexistants de ce run étaient justement introuvables dans les 269 fixtures
    OddsPapi : l'absence des sources officielles est un signal fiable.
    FAIL-OPEN si les deux sources sont vides (mode relais : Gemini est alors la
    seule source disponible, on ne peut pas tout écarter).
    """
    if not fixtures and not rapid_matchs:
        return donnees_json
    try:
        data = json.loads(donnees_json)
    except Exception:
        return donnees_json
    matchs = data.get("matchs", [])
    if not matchs:
        return donnees_json

    # Index des paires de joueurs connues des sources officielles
    paires = []
    for f in fixtures or []:
        p = f.get("participants", {})
        paires.append((str(p.get("participant1Name", "")), str(p.get("participant2Name", ""))))
    for r in rapid_matchs or []:
        paires.append((str(r.get("joueur1", "")), str(r.get("joueur2", ""))))

    gardes, retires = [], []
    for m in matchs:
        j1, j2 = m.get("joueur1", ""), m.get("joueur2", "")
        confirme = False
        for n1, n2 in paires:
            direct = (_sim_noms(j1, n1) + _sim_noms(j2, n2)) / 2
            croise = (_sim_noms(j1, n2) + _sim_noms(j2, n1)) / 2
            if max(direct, croise) >= 0.70:
                confirme = True
                break
        (gardes if confirme else retires).append(m)
        if not confirme:
            logging.warning(f"Match NON CONFIRMÉ par les sources officielles : "
                            f"'{j1} vs {j2}' ({m.get('tournoi', 'tournoi ?')}, "
                            f"{m.get('heure_match', '?')}) — probablement inexistant "
                            f"dans cette fenêtre, écarté.")
    if retires:
        logging.info(f"Filtre CONFIRMATION : {len(retires)} match(s) non confirmé(s) "
                     f"écarté(s), {len(gardes)} gardé(s).")
        data["matchs"] = gardes
        return json.dumps(data, ensure_ascii=False, indent=2)
    return donnees_json


def dedupliquer_matchs_json(donnees_json):
    """
    v8.2 — Retire les matchs remontés DEUX FOIS par Gemini sous des orthographes
    différentes. Constat du 28/07 : « Martin Damm vs Ben Shelton » et « Martin
    Damm Jr. vs Ben Shelton » analysés comme deux matchs distincts (stats et
    probabilité identiques). Risque réel : deux tickets sur le même match, donc
    une mise doublée à l'insu du parieur — la déduplication existante travaille
    sur le hash du TICKET, elle ne peut pas voir que ce sont les mêmes joueurs.
    On garde l'occurrence la plus complète (celle qui a le plus de stats).
    """
    try:
        data = json.loads(donnees_json)
    except Exception:
        return donnees_json
    matchs = data.get("matchs", [])
    if len(matchs) < 2:
        return donnees_json

    def _richesse(m):
        """Nombre de champs de stats réellement renseignés."""
        n = 0
        for cle in ("stats_surface_j1", "stats_surface_j2"):
            for v in (m.get(cle) or {}).values():
                if v is not None and "non trouv" not in str(v).lower():
                    n += 1
        return n

    gardes = []
    for m in matchs:
        j1, j2 = m.get("joueur1", ""), m.get("joueur2", "")
        doublon_de = None
        for k, g in enumerate(gardes):
            g1, g2 = g.get("joueur1", ""), g.get("joueur2", "")
            direct = (_sim_noms(j1, g1) + _sim_noms(j2, g2)) / 2
            croise = (_sim_noms(j1, g2) + _sim_noms(j2, g1)) / 2
            if max(direct, croise) >= 0.75:
                doublon_de = k
                break
        if doublon_de is None:
            gardes.append(m)
            continue
        g = gardes[doublon_de]
        logging.warning(f"Doublon de match retiré : '{j1} vs {j2}' ≡ "
                        f"'{g.get('joueur1')} vs {g.get('joueur2')}'")
        if _richesse(m) > _richesse(g):
            gardes[doublon_de] = m   # on garde la version la mieux documentée

    if len(gardes) != len(matchs):
        logging.info(f"Déduplication : {len(matchs) - len(gardes)} match(s) en double retiré(s), "
                     f"{len(gardes)} gardé(s).")
        data["matchs"] = gardes
        return json.dumps(data, ensure_ascii=False, indent=2)
    return donnees_json


def filtrer_json_sans_cote(donnees_json):
    """
    Filtre COTES (v7.3) : retire les matchs SANS cote réelle exploitable.
    Un match sans cote est mathématiquement injouable (pas de delta calculable,
    pas de pari possible sur Winamax) — Claude les skippait déjà un par un.
    Les retirer ICI évite de gonfler le prompt (tokens gaspillés) et surtout
    de déclencher Opus à tort (ex: 20 matchs comptés dont 17 ITF sans cote).
    ⚠️ NE retire PAS les matchs aux stats partielles (Hold%, H2H, forme
    manquants) : ceux-là restent analysés et classés ÉLEVÉE/MODÉRÉE/BASSE.
    Seule l'ABSENCE DE COTE élimine un match.
    """
    try:
        data = json.loads(donnees_json)
    except Exception:
        return donnees_json
    matchs = data.get("matchs", [])
    if not matchs:
        return donnees_json

    def _cote_ok(v):
        # Cote valide = convertible en nombre > 1.0 (écarte None, "", "non trouvé", 0)
        try:
            return float(v) > 1.0
        except (TypeError, ValueError):
            return False

    def _source_winamax(m):
        # v7.8 : les cotes complétées par le repli Pinnacle sont acceptées —
        # la jouabilité est garantie par le filtre TOURNOIS, et le marquage
        # "à vérifier sur Winamax" figure sur le ticket.
        if m.get("cote_pinnacle_repli"):
            return True
        # v7.6.1 : une cote NUMÉRIQUEMENT valide ne suffit pas — elle doit venir
        # de WINAMAX. Constat du 17/07 : ticket Galan/Coppejans envoyé sur le
        # Bunschoten Challenger, tournoi ABSENT de Winamax (cote trouvée chez un
        # autre bookmaker) → pari injouable pour les abonnés. Le prompt
        # autorisait ce repli ("autre cote bookmaker EU") : plus maintenant.
        s = str(m.get("source_cote", "")).lower()
        # v7.8 : les cotes de RÉFÉRENCE (TennisAPI/Pinnacle) sont acceptées à la
        # condition — garantie par le marqueur — que le tournoi soit dans la
        # liste Winamax hebdomadaire (jouabilité assurée par le filtre TOURNOIS).
        return "winamax" in s or "tournoi liste winamax" in s

    gardes, sans_cote, hors_winamax = [], 0, []
    for m in matchs:
        if not (_cote_ok(m.get("cote_j1")) and _cote_ok(m.get("cote_j2"))):
            sans_cote += 1
            continue
        if not _source_winamax(m):
            hors_winamax.append(
                f"{m.get('joueur1','?')} vs {m.get('joueur2','?')} "
                f"({m.get('tournoi','?')} — source : {m.get('source_cote','absente')})")
            continue
        gardes.append(m)
    if sans_cote:
        logging.info(f"Filtre COTES : {sans_cote} match(s) sans cote retiré(s).")
    for detail in hors_winamax:
        logging.warning(f"Filtre WINAMAX : match écarté — cote non-Winamax : {detail}")
    if hors_winamax:
        logging.info(f"Filtre WINAMAX : {len(hors_winamax)} match(s) injouable(s) retiré(s), "
                     f"{len(gardes)} gardé(s).")
    data["matchs"] = gardes
    return json.dumps(data, ensure_ascii=False, indent=2)


COMPLETION_PINNACLE_MAX = 3   # plafond de lookups par run (budget direct : 250/mois)


def completer_cotes_pinnacle(donnees_json, fixtures):
    """
    v7.8 — REPLI DE COTES : un match validé par le filtre TOURNOIS (donc jouable
    sur Winamax — la liste est relevée chaque semaine sur l'app) mais dont Gemini
    n'a pas trouvé la cote Winamax n'est plus JETÉ : on récupère sa moneyline
    Pinnacle (oddspapi_cotes, qui gère déjà RapidAPI→direct) et on le garde,
    marqué explicitement "Pinnacle (repli)".
    POURQUOI : constat du 20/07 — couverture bornée à 2-3 matchs/run parce que
    Gemini ne parvient à vérifier une cote Coteur/Winamax que pour une poignée de
    matchs, quel que soit son budget de recherche. Le filtre TOURNOIS garantit
    déjà la jouabilité ; la cote Winamax exacte sera vérifiée par le parieur
    (marquage sur le ticket). Pinnacle est de toute façon la référence sharp.
    GARDE-FOUS : plafond COMPLETION_PINNACLE_MAX lookups/run · alignement des
    joueurs par similarité de noms (l'ordre fixture peut différer) · sanity
    checks (cotes 1.01-25, somme des probas implicites 100-115%).
    """
    if not fixtures:
        return donnees_json
    try:
        data = json.loads(donnees_json)
    except Exception:
        return donnees_json
    matchs = data.get("matchs", [])
    if not matchs:
        return donnees_json

    def _cote_valide(c):
        try:
            return 1.01 <= float(c) <= 25.0
        except (TypeError, ValueError):
            return False

    lookups = 0
    for m in matchs:
        deja_ok = (_cote_valide(m.get("cote_j1")) and _cote_valide(m.get("cote_j2"))
                   and "winamax" in str(m.get("source_cote", "")).lower())
        if deja_ok:
            continue
        if lookups >= COMPLETION_PINNACLE_MAX:
            logging.info(f"Repli Pinnacle : plafond {COMPLETION_PINNACLE_MAX} lookups atteint — matchs restants non complétés.")
            break
        j1, j2 = m.get("joueur1", ""), m.get("joueur2", "")
        fx = oddspapi_trouver_fixture(j1, j2, fixtures)
        if not fx:
            logging.info(f"Repli Pinnacle : '{j1} vs {j2}' sans cote et non apparié dans les fixtures — restera écarté par le filtre COTES.")
            continue
        fid = fx.get("fixtureId") or fx.get("id")
        if not fid:
            continue
        lookups += 1
        cotes = oddspapi_cotes(fid)
        ml = (cotes or {}).get("moneyline", {})
        c_home, c_away = ml.get("home"), ml.get("away")
        if not (_cote_valide(c_home) and _cote_valide(c_away)):
            logging.info(f"Repli Pinnacle : '{j1} vs {j2}' apparié (fid={fid}) mais moneyline absente/invalide ({ml}) — écarté.")
            continue
        somme_probas = 1.0 / float(c_home) + 1.0 / float(c_away)
        if not (1.00 <= somme_probas <= 1.15):
            logging.warning(f"Repli Pinnacle : '{j1} vs {j2}' probas implicites suspectes ({somme_probas:.2f}) — écarté.")
            continue
        # Alignement : home = participant1 du fixture, mais est-ce joueur1 du match ?
        parts = fx.get("participants", {})
        n1 = str(parts.get("participant1Name", ""))
        n2 = str(parts.get("participant2Name", ""))
        direct = (_sim_noms(j1, n1) + _sim_noms(j2, n2)) / 2
        croise = (_sim_noms(j1, n2) + _sim_noms(j2, n1)) / 2
        if croise > direct:
            c_home, c_away = c_away, c_home  # l'ordre du fixture est inversé
        m["cote_j1"], m["cote_j2"] = float(c_home), float(c_away)
        m["source_cote"] = "Pinnacle (repli — cote Winamax à vérifier avant mise)"
        m["cote_pinnacle_repli"] = True
        logging.info(f"Repli Pinnacle : '{j1} vs {j2}' complété — cotes {c_home}/{c_away} "
                     f"(alignement {'croisé' if croise > direct else 'direct'}, marge {somme_probas:.2f}).")
    data["matchs"] = matchs
    return json.dumps(data, ensure_ascii=False, indent=2)


def _tournoi_dans_liste(nom_tournoi, tournois_actifs, defaut_si_absent=True):
    """v7.9 : cœur du matching tournoi, factorisé (utilisé par le pré-filtre
    AMONT sur rapid_matchs ET le filtre AVAL sur le JSON Gemini). Matching
    tolérant : accents, noms commerciaux, anglais (ratio ≥ 0.85). nom absent
    → True (fail-open, un filtre en aval tranchera)."""
    import unicodedata
    from difflib import SequenceMatcher
    if not nom_tournoi:
        # v8.3 : le comportement dépend de l'appelant. En AMONT (calendrier
        # RapidAPI) le nom est toujours présent. En AVAL (JSON Gemini) un nom
        # absent signifie qu'on ne PEUT PAS vérifier la jouabilité → on exclut.
        # Constat du 28/07 : 6 matchs inexistants sur Winamax analysés parce que
        # Gemini n'avait pas renseigné le tournoi et que le filtre laissait
        # passer par défaut. C'est le retour du bug Galan/Coppejans du 17/07.
        return defaut_si_absent
    def _norm(txt):
        txt = unicodedata.normalize("NFD", str(txt))
        txt = "".join(c for c in txt if unicodedata.category(c) != "Mn")
        return re.sub(r"[^a-z0-9 ]", " ", txt.lower())
    MOTS_GENERIQUES = {"atp", "wta", "open", "masters", "cup", "tour", "tennis", "challenger"}
    def _jetons(entree):
        return [t for t in _norm(entree).split() if t and t not in MOTS_GENERIQUES]
    n = _norm(nom_tournoi)
    mots = n.split()
    for t in tournois_actifs:
        for j in (_jetons(t) or [_norm(t).strip()]):
            if j in n:
                return True
            if any(SequenceMatcher(None, j, m).ratio() >= 0.85 for m in mots):
                return True
    return False


def prefiltrer_rapid_matchs_par_tournoi(rapid_matchs, tournois_actifs):
    """
    v7.9 — Pré-filtre les matchs par tournoi AVANT enrichissement et Gemini.
    Constat du 21/07 : 92 matchs en fenêtre → Gemini n'en documente que 9, pris
    "au hasard" dans la masse (majoritairement des Challengers) → les matchs des
    tournois actifs étaient NOYÉS et perdus avant même d'être analysés. En ne
    passant à Gemini QUE les matchs des tournois jouables, il concentre son
    budget de recherche sur les bons matchs → couverture bien meilleure.
    FAIL-OPEN : liste vide → aucun filtrage (rétrocompatible).
    """
    if not tournois_actifs or not rapid_matchs:
        return rapid_matchs
    gardes = [m for m in rapid_matchs if _tournoi_dans_liste(m.get("tournoi", ""), tournois_actifs)]
    retires = len(rapid_matchs) - len(gardes)
    if retires:
        logging.info(f"Pré-filtre TOURNOIS (amont Gemini) : {retires} match(s) hors tournois "
                     f"actifs écarté(s), {len(gardes)} envoyé(s) à l'enrichissement.")
        # v8.5 : nommer les tournois écartés quand RIEN ne passe. Constat du 30/07 :
        # 108 matchs sur 108 rejetés parce que le nom RapidAPI ("Mubadala DC Open")
        # ne partage aucun jeton avec la liste ("ATP Washington") → perte de
        # couverture totale et invisible. Sans ce log, impossible de diagnostiquer.
        if not gardes and rapid_matchs:
            _noms = sorted({str(r.get("tournoi", "?")).split(" — ")[0] for r in rapid_matchs})
            logging.warning(
                f"Pré-filtre TOURNOIS : AUCUN match retenu sur {len(rapid_matchs)}. "
                f"Tournois vus : {' · '.join(_noms[:12])}{' …' if len(_noms) > 12 else ''}. "
                f"Liste active : {' · '.join(tournois_actifs)}. → si l'un de ces tournois "
                f"est jouable sur Winamax, ajoute un mot distinctif de son nom à "
                f"tournois_winamax.json."
            )
    return gardes


def filtrer_json_hors_tournois(donnees_json, tournois_actifs):
    """
    Filtre TOURNOIS (v7.6.1) : ne garde que les matchs des tournois listés dans
    tournois_winamax.json (tournois_actifs) — la liste relevée chaque semaine
    sur l'app Winamax. C'est la vérité terrain la plus fiable disponible sur ce
    qui est RÉELLEMENT jouable.
    Constat du 17/07 : ticket envoyé sur le Bunschoten Challenger, absent de
    Winamax — la liste existait mais ne servait que de guide de recherche à
    Gemini, jamais de filtre. Correction de conception.
    Matching TOLÉRANT : "ATP Bastad" reconnaît "Nordea Open - Bastad",
    "WTA Athenes" reconnaît "Athens Open" (accents/anglais, ratio ≥ 0.85).
    FAIL-OPEN : liste vide/inaccessible → aucun filtrage (on ne tue pas le bot
    sur un fichier GitHub manquant) ; le filtre source_cote reste en garde.
    """
    if not tournois_actifs:
        logging.warning("Filtre TOURNOIS : liste vide/indisponible → filtre désactivé (fail-open).")
        return donnees_json
    try:
        data = json.loads(donnees_json)
    except Exception:
        return donnees_json
    matchs = data.get("matchs", [])
    if not matchs:
        return donnees_json

    gardes, retires = [], []
    for m in matchs:
        _nom_trn = str(m.get("tournoi", "") or "").strip()
        if not _nom_trn:
            logging.warning(f"Filtre TOURNOIS : tournoi NON RENSEIGNÉ pour "
                            f"'{m.get('joueur1')} vs {m.get('joueur2')}' — "
                            f"jouabilité invérifiable, match écarté (v8.3).")
            retires.append(m)
            continue
        if _tournoi_dans_liste(_nom_trn, tournois_actifs, defaut_si_absent=False):
            gardes.append(m)
        else:
            retires.append(f"{m.get('joueur1','?')} vs {m.get('joueur2','?')} ({m.get('tournoi','?')})")
    for r in retires:
        logging.warning(f"Filtre TOURNOIS : hors liste Winamax — {r}")
    if retires:
        logging.info(f"Filtre TOURNOIS : {len(retires)} match(s) hors tournois actifs retiré(s), "
                     f"{len(gardes)} gardé(s).")
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
        # v2.7/v7.6.5 : diviser par le nom le plus COURT — 'Tabilo' est un
        # sous-ensemble parfait de 'Tabilo, Alejandro' (score 1.0), alors que
        # l'ancien /max donnait 0.5 et faisait échouer l'appariement dès que
        # le ticket abrégeait les noms (constat du 17/07 : règlement
        # déterministe désactivé sur le ticket Tabilo/Tirante).
        score = max(score, len(communs) / min(len(mots_a), len(mots_b)))
    return score


def oddspapi_fixtures_jour(exclure_termines=True):
    """
    Récupère les vrais matchs tennis du jour (SRL exclu). [] si erreur.
    exclure_termines=True (défaut) : filtre les matchs finis/en cours (pipeline paris).
    exclure_termines=False : garde tout, y compris les terminés (commande 'regler'
    qui a justement besoin des scores finaux).
    """
    # v7.6.2 : réessai sur 429. Constat des 15 et 17/07 : trois runs d'affilée
    # coupés par un 429 alors que le compteur n'était qu'à 38/1000 → ce n'est PAS
    # le quota mensuel mais une limite de DÉBIT côté RapidAPI. Résultat : les
    # marchés alternatifs étaient morts en pratique depuis 3 runs.
    # ⚠️ Si le 429 persiste après réessai, c'est le plan RapidAPI lui-même qui
    # est saturé → à vérifier sur le tableau de bord RapidAPI (notre compteur ne
    # voit que NOS appels, pas la limite réelle du plan).
    data = None
    for tentative in (1, 2):
        try:
            _quota_inc("oddspapi")  # budget séparé : 1000 req/mois
            r = requests.get(f"https://{ODDSPAPI_HOST}/fixtures/today",
                             headers=_oddspapi_headers(), params={"sportId": 12}, timeout=15)
            r.raise_for_status()
            data = r.json()
            if tentative == 2:
                logging.info("OddsPapi : réessai réussi.")
            break
        except Exception as e:
            est_429 = "429" in str(e)
            if tentative == 1 and est_429:
                logging.warning(f"OddsPapi fixtures : 429 (débit) — pause 4s puis nouvel essai.")
                time.sleep(4)
                continue
            logging.warning(f"OddsPapi fixtures indisponible : {e}")
            if est_429:
                logging.warning("OddsPapi : 429 persistant après réessai — bascule sur l'API directe.")
            # v7.7 : REPLI API directe (pool séparé) — ne consomme le nouveau
            # quota que si RapidAPI a réellement échoué.
            # v7.7.1 : blindé — un échec du REPLI ne doit jamais tuer le run
            # (constat 18/07 : NameError dans le repli → bot entier down).
            try:
                data = _oddspapi_fixtures_direct()
            except Exception as e_direct:
                logging.warning(f"Repli OddsPapi direct échoué : {e_direct}")
                data = None
            break
    if data is None:
        return []
    fixtures = data if isinstance(data, list) else data.get("fixtures", data.get("data", []))
    if not isinstance(fixtures, list):
        return []
    vrais = [f for f in fixtures if not _est_faux_tennis(f)]
    exclus = len(fixtures) - len(vrais)
    if exclus:
        logging.info(f"OddsPapi : {exclus} match(s) SRL exclu(s), {len(vrais)} vrai(s).")
    # v7.3.1 : exclure les matchs terminés/en cours — plus de cotes pré-match
    # disponibles, l'appel /fixtures/odds serait du quota gaspillé (constat
    # run 10/07 : Coppejans "Finished" interrogé pour rien).
    def _est_jouable(f):
        st = str((f.get("status") or {}).get("statusName") or "")
        if st in ("Finished", "Live", "In-Play", "Cancelled", "Postponed", "Retired"):
            return False
        if f.get("trueStartTime"):
            return False  # le match a déjà commencé
        return True
    if not exclure_termines:
        return vrais  # commande 'regler' : les terminés sont précisément ce qu'on veut
    jouables = [f for f in vrais if _est_jouable(f)]
    retires = len(vrais) - len(jouables)
    if retires:
        logging.info(f"OddsPapi : {retires} match(s) terminé(s)/en cours exclu(s), {len(jouables)} jouable(s).")
    return jouables


def _oddspapi_fixtures_direct():
    """
    v7.7 — Repli fixtures via l'API OddsPapi DIRECTE, converti au format que le
    reste du code attend (structure RapidAPI : participants.*, status.*).
    L'API directe renvoie les champs à PLAT (participant1Name, statusName) et
    exige une fenêtre from/to < 48h. [] si indisponible.
    """
    maintenant = datetime.now(ZoneInfo("Europe/Paris"))
    depuis = maintenant.strftime("%Y-%m-%dT00:00:00Z")
    jusqua = maintenant.strftime("%Y-%m-%dT23:59:59Z")
    brut = _oddspapi_direct_get("/v4/fixtures", {
        "sportId": 12, "from": depuis, "to": jusqua, "hasOdds": "true",
        "bookmakers": ODDSPAPI_BOOKMAKER,
    })
    if not isinstance(brut, list):
        return None
    # Adapter chaque fixture au format RapidAPI attendu par le pipeline
    convertis = []
    for f in brut:
        convertis.append({
            "id": f.get("fixtureId"),
            "fixtureId": f.get("fixtureId"),  # v7.7.5 : le consommateur lit ce nom
            "participants": {
                "participant1Name": f.get("participant1Name", ""),
                "participant2Name": f.get("participant2Name", ""),
            },
            "status": {"statusName": _oddspapi_statut_direct(f.get("statusId"),
                                                              f.get("statusName", ""))},
            "trueStartTime": f.get("trueStartTime"),
            "startTime": f.get("startTime"),
            "tournament": {"tournamentName": f.get("tournamentName", "")},
            "hasOdds": f.get("hasOdds", False),
            "_source": "direct",  # marqueur pour oddspapi_cotes (choisir la bonne voie)
        })
    logging.info(f"OddsPapi DIRECT : {len(convertis)} fixture(s) récupéré(s) en repli.")
    return convertis


def _oddspapi_statut_direct(status_id, status_name):
    """Mappe statusId (0/1/2/3) + statusName direct vers les libellés RapidAPI."""
    if status_name in ("Pre-Game", "Ended", "In-Play"):
        return {"Pre-Game": "Pre-Game", "Ended": "Finished", "In-Play": "Live"}[status_name]
    return {0: "Pre-Game", 1: "Live", 2: "Finished", 3: "Cancelled"}.get(status_id, "Pre-Game")


def oddspapi_trouver_fixture(joueur1, joueur2, fixtures):
    """
    Trouve le FIXTURE complet correspondant à un match (par noms, tolère les variantes).
    v7.3.2 : retourne l'objet fixture entier (et plus seulement l'ID) pour
    permettre de lire hasOdds/tournament AVANT d'appeler /fixtures/odds.
    """
    meilleur, score_max = None, 0.0
    for f in fixtures:
        p = f.get("participants", {})
        n1, n2 = str(p.get("participant1Name", "")), str(p.get("participant2Name", ""))
        direct  = (_sim_noms(joueur1, n1) + _sim_noms(joueur2, n2)) / 2
        inverse = (_sim_noms(joueur1, n2) + _sim_noms(joueur2, n1)) / 2
        score = max(direct, inverse)
        if score > score_max:
            score_max, meilleur = score, f
    return meilleur if score_max >= 0.7 else None


def oddspapi_cotes(fixture_id):
    """
    Récupère les cotes Pinnacle des marchés alternatifs d'un match.
    Retourne : {"total_sets": {"over":x,"under":y,"ligne":2.5},
                "total_games": {"over":x,"under":y,"ligne":~22.5}}
    PATCH B (v7.2) : collecte TOUTES les lignes de jeux du MATCH complet
    (18.5 à 26.5) et garde la plus proche de 22.5. Les totaux d'UN SEUL set
    (9.5-12.5) sont exclus par le plancher — c'est eux qui polluaient via
    la mainLine (10.5 remontait à tort comme ligne principale).
    """
    data = None
    try:
        _quota_inc("oddspapi")  # budget séparé : 1000 req/mois
        r = requests.get(f"https://{ODDSPAPI_HOST}/fixtures/odds",
                         headers=_oddspapi_headers(),
                         params={"fixtureId": fixture_id, "bookmakers": ODDSPAPI_BOOKMAKER},
                         timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logging.warning(f"OddsPapi cotes {fixture_id} (RapidAPI) : {e} — bascule API directe.")
        # v7.7 : REPLI cotes via API directe. L'endpoint /v4/odds renvoie la même
        # structure players.0.price que _oddspapi_extraire_outcomes sait déjà lire
        # (récursif). fixtureId de l'API directe (préfixe 'id...') diffère de RapidAPI,
        # mais ce repli n'est atteint QUE si le fixture vient déjà de la voie directe.
        try:
            data = _oddspapi_direct_get("/v4/odds", {
                "fixtureId": fixture_id, "bookmaker": ODDSPAPI_BOOKMAKER,
            })
        except Exception as e_direct:
            logging.warning(f"Repli cotes direct échoué : {e_direct}")
            data = None
        if data is None:
            return {}

    # MODE OBSERVATION : loguer la section utile (marchés Pinnacle), pas le début
    # de la réponse (métadonnées) — le log tronqué à 1500 chars n'atteignait
    # jamais les cotes (constat run du 10/07 : coupé avant "markets").
    if MARCHES_ALT_MODE == "observation":
        base = data[0] if isinstance(data, list) and data else data
        section = {}
        if isinstance(base, dict):
            for cle_book in ("bookmakers", "bookmakerOdds", "odds"):
                bloc = base.get(cle_book)
                if isinstance(bloc, dict) and bloc:
                    pin = bloc.get("pinnacle", bloc)
                    section = pin.get("markets", pin) if isinstance(pin, dict) else pin
                    if section:
                        break
        apercu = json.dumps(section or base, ensure_ascii=False)[:2500]
        logging.info(f"[OBS] Marchés Pinnacle bruts : {apercu}")

    outcomes = _oddspapi_extraire_outcomes(data)
    res = {}
    lignes_jeux = {}          # {ligne: {"over": prix, "under": prix}}
    for o in outcomes:
        bo = str(o.get("bookmakerOutcomeId", ""))
        price = o.get("price")
        if price is None:
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        # Moneyline Pinnacle (CLV) : outcomes "home"/"away" du marché vainqueur
        if bo == "home":
            res.setdefault("moneyline", {})["home"] = price
            continue
        if bo == "away":
            res.setdefault("moneyline", {})["away"] = price
            continue
        m = re.match(r"(\d+(?:\.\d+)?)/(over|under)", bo)
        if not m:
            continue
        ligne, sens = float(m.group(1)), m.group(2)
        if ligne == MARKET_TOTAL_SETS_LIGNE:               # Total Sets O/U 2.5
            res.setdefault("total_sets", {"ligne": MARKET_TOTAL_SETS_LIGNE})[sens] = price
        elif MARCHES_ALT_GAMES_MIN <= ligne <= MARCHES_ALT_GAMES_MAX:
            # Total Games de MATCH uniquement (18.5-26.5). Les totaux d'UN set
            # (9.5-12.5) sont exclus par le plancher — c'est eux qui polluaient.
            lignes_jeux.setdefault(ligne, {})[sens] = price

    # Choisir la ligne de jeux : priorité aux lignes COMPLÈTES (over ET under),
    # puis la plus proche de la cible ~22.5 (ligne principale de match).
    completes = {l: v for l, v in lignes_jeux.items() if "over" in v and "under" in v}
    candidates = completes or lignes_jeux
    if candidates:
        ligne_choisie = min(candidates, key=lambda l: abs(l - MARCHES_ALT_GAMES_CIBLE))
        tg = {"ligne": ligne_choisie}
        tg.update(candidates[ligne_choisie])
        res["total_games"] = tg
    return res


def _oddspapi_extraire_outcomes(data):
    """
    Normalise la réponse OddsPapi en liste d'outcomes (price + bookmakerOutcomeId).
    v7.3.1 : tolère les DEUX structures observées :
      - price directement dans l'objet outcome
      - price niché sous players."0".price (structure bookmakers.pinnacle.markets)
    """
    out = []
    def _prix(obj):
        pr = obj.get("price")
        if pr is None:
            players = obj.get("players")
            if isinstance(players, dict):
                pr = (players.get("0") or {}).get("price")
            elif isinstance(players, list) and players:
                pr = (players[0] or {}).get("price")
        return pr
    def _collecte(obj):
        if isinstance(obj, dict):
            if "bookmakerOutcomeId" in obj:
                pr = _prix(obj)
                if pr is not None:
                    out.append({"bookmakerOutcomeId": obj["bookmakerOutcomeId"],
                                "price": pr})
            else:
                for v in obj.values():
                    _collecte(v)
        elif isinstance(obj, list):
            for v in obj:
                _collecte(v)
    _collecte(data)
    return out


def corriger_heures_avec_calendrier(donnees_json, rapid_matchs):
    """
    v7.6.4 — REPLI d'heures quand OddsPapi est indisponible (429 persistant).
    Constat du 17/07 : ticket Tabilo/Tirante annoncé à 19:00 alors que le match
    se jouait à 16:00 FR. Même valeur fantaisiste (19:00) que le bug Coppejans
    du 10/07 : Gemini invente une heure de soirée générique quand il ne sait pas.
    Le garde-fou existant (corriger_heures_avec_oddspapi) ne s'exécutait pas
    faute de fixtures — le 429 OddsPapi neutralisait donc AUSSI les heures.
    Or le calendrier RapidAPI porte déjà l'heure de chaque match : s'il l'ignore,
    on la lui réimpose. Coût : 0 requête.
    Hiérarchie de confiance : OddsPapi (epoch réel) > RapidAPI > Gemini.

    Appariement par JETONS plutôt que par ratio : le format des noms RapidAPI
    n'est pas garanti ("Tabilo A." vs "Alejandro Tabilo" donne un ratio ~0.6,
    sous tout seuil raisonnable). On exige un jeton commun POUR CHAQUE joueur,
    ce qui reste discriminant tout en absorbant les abréviations.
    """
    if not rapid_matchs:
        return donnees_json
    try:
        data = json.loads(donnees_json)
    except Exception:
        return donnees_json

    def _jetons(nom):
        return {t for t in re.sub(r"[^a-zà-ÿ ]", " ", str(nom).lower()).split() if len(t) >= 3}

    def _meme_match(a1, a2, b1, b2):
        ja1, ja2, jb1, jb2 = _jetons(a1), _jetons(a2), _jetons(b1), _jetons(b2)
        direct = bool(ja1 & jb1) and bool(ja2 & jb2)
        croise = bool(ja1 & jb2) and bool(ja2 & jb1)
        return direct or croise

    corriges = 0
    for m in data.get("matchs", []):
        # v7.9.5 : ne JAMAIS écraser une heure fixée par OddsPapi (source epoch,
        # plus fiable que l'heure de programmation du calendrier RapidAPI).
        if m.get("heure_verrouillee"):
            continue
        j1, j2 = m.get("joueur1", ""), m.get("joueur2", "")
        rm = next((r for r in rapid_matchs
                   if _meme_match(j1, j2, r.get("joueur1", ""), r.get("joueur2", ""))), None)
        if not rm:
            continue
        mh = re.search(r"(\d{1,2}):(\d{2})", str(rm.get("heure", "")))
        if not mh:
            continue
        h_propre = f"{int(mh.group(1)):02d}:{mh.group(2)}"
        if m.get("heure_match") != h_propre:
            logging.info(f"Heure corrigée (calendrier RapidAPI) : {j1} vs {j2} "
                         f"{m.get('heure_match','?')} → {h_propre}")
            m["heure_match"] = h_propre
            corriges += 1
    if corriges:
        logging.info(f"Heures — calendrier RapidAPI : {corriges} match(s) corrigé(s) "
                     f"(non couverts par OddsPapi).")
    return json.dumps(data, ensure_ascii=False, indent=2)


def corriger_heures_avec_oddspapi(donnees_json, fixtures):
    """
    v7.4 : écrase l'heure Gemini par le startTime OddsPapi (epoch, déterministe)
    pour tout match apparié. Constat run 10/07 : Gemini annonçait Coppejans à
    19:00 alors que le trueStartTime réel était 16:02 FR. L'epoch OddsPapi est
    fiable, l'heure Gemini non. Coût : 0 requête (fixtures déjà récupérés).
    """
    if not fixtures:
        return donnees_json
    try:
        data = json.loads(donnees_json)
    except Exception:
        return donnees_json
    corriges = 0
    for m in data.get("matchs", []):
        fx = oddspapi_trouver_fixture(m.get("joueur1", ""), m.get("joueur2", ""), fixtures)
        if not fx:
            continue
        # v7.9.4 : accepter les DEUX formats de startTime. RapidAPI = epoch
        # (secondes) · API directe = ISO ("2026-07-22T09:00:00Z"). Bug du 22/07 :
        # int() plantait sur l'ISO → except → saut SILENCIEUX de chaque fixture
        # → la correction d'heures était morte depuis le passage au repli direct
        # (19/07), d'où les tickets à 14:00 pour des matchs de 11:00.
        start = fx.get("trueStartTime") or fx.get("startTime")
        if not start:
            continue
        try:
            s = str(start).strip()
            if s.replace(".", "", 1).isdigit():          # epoch (RapidAPI)
                dt_utc = datetime.fromtimestamp(int(float(s)), timezone.utc)
            else:                                          # ISO (API directe)
                dt_utc = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
            h_fr = dt_utc.astimezone(ZoneInfo("Europe/Paris")).strftime("%H:%M")
        except Exception as e:
            logging.info(f"Heure OddsPapi illisible pour {m.get('joueur1')} vs "
                         f"{m.get('joueur2')} (startTime={start!r}) : {e}")
            continue
        # v7.9.5 : l'heure OddsPapi est VERROUILLÉE — hiérarchie OddsPapi >
        # RapidAPI > Gemini. Bug du 23/07 : OddsPapi corrigeait 14:00 → 11:00
        # (juste), puis le calendrier RapidAPI écrasait 11:00 → 14:00 (faux,
        # heure de programmation erronée côté RapidAPI). La source la plus
        # fiable doit avoir le dernier mot.
        m["heure_verrouillee"] = True
        if m.get("heure_match") != h_fr:
            logging.info(
                f"Heure corrigée (OddsPapi) : {m.get('joueur1')} vs {m.get('joueur2')} "
                f"{m.get('heure_match')} → {h_fr}"
            )
            m["heure_match"] = h_fr
            corriges += 1
    if corriges:
        logging.info(f"Heures autoritaires OddsPapi : {corriges} match(s) corrigé(s).")
    return json.dumps(data, ensure_ascii=False, indent=2)


def analyser_marches_alternatifs(matchs_serres, date, fixtures=None):
    """
    Pour chaque match serré, récupère les cotes sets/jeux Pinnacle et (si mode actif)
    les prépare pour être injectées dans le prompt de Claude.
    Retourne une liste de données de marchés alternatifs (vide en mode observation/off).
    """
    if MARCHES_ALT_MODE == "off":
        return []
    if not RAPIDAPI_KEY:
        logging.info("Marchés alt : pas de clé RapidAPI, skip.")
        return []

    if fixtures is None:
        fixtures = oddspapi_fixtures_jour()
    if not fixtures:
        logging.info("Marchés alt : aucun fixture OddsPapi disponible.")
        return []

    logging.info(f"Marchés alt : entrée examen — {len(matchs_serres)} match(s), "
                 f"{len(fixtures)} fixtures, mode {MARCHES_ALT_MODE}.")
    tickets_alt = []
    appels_cotes = 0
    for m in matchs_serres:
        # PLAFOND (v7.3.2) : max MARCHES_ALT_MAX_APPELS appels /fixtures/odds par run
        if appels_cotes >= MARCHES_ALT_MAX_APPELS:
            logging.info(f"Marchés alt : plafond {MARCHES_ALT_MAX_APPELS} appels cotes atteint — matchs restants ignorés (économie quota).")
            break
        j1, j2 = m.get("joueur1", ""), m.get("joueur2", "")
        fx = oddspapi_trouver_fixture(j1, j2, fixtures)
        if not fx:
            # v7.7.2 : diagnostic d'appariement en TOUT mode (constat 19/07 :
            # 'Rublev vs Darderi' non apparié dans 81 fixtures alors que la
            # finale de Gstaad y est forcément — il faut VOIR les noms réels et
            # le score pour régler le seuil ou l'adaptateur).
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
            logging.info(
                f"Marchés alt : '{j1} vs {j2}' non apparié dans les {len(fixtures)} "
                f"fixtures OddsPapi — meilleur candidat : '{top[1]}' (score {top[0]:.2f}, "
                f"seuil 0.70). Cotes alt indisponibles pour ce match."
            )
            continue
        # FILTRE hasOdds (v7.3.2) : si le fixture annonce explicitement qu'il n'a
        # PAS de cotes, inutile d'appeler /fixtures/odds (constat run 10/07 :
        # 3 ITF interrogés pour rien). Clé absente → on tente quand même (prudence).
        if fx.get("hasOdds") is False:
            logging.info(f"Marchés alt : '{j1} vs {j2}' sans cotes annoncées (hasOdds=false) — appel cotes évité.")
            continue
        # v7.7.5 : lire les deux noms de champ (RapidAPI: fixtureId · adaptateur
        # direct v7.7: id). Constat du 20/07 : match apparié puis JETÉ EN SILENCE
        # car fid=None sur les fixtures directs → l'appel cotes n'avait jamais
        # lieu sur la voie directe, sans aucune trace dans les logs.
        fid = fx.get("fixtureId") or fx.get("id")
        if not fid:
            logging.info(f"Marchés alt : '{j1} vs {j2}' apparié mais fixture sans id — cotes impossibles.")
            continue
        logging.info(f"Marchés alt : '{j1} vs {j2}' apparié (fid={fid}) → appel cotes Pinnacle…")
        appels_cotes += 1
        cotes = oddspapi_cotes(fid)
        if not cotes:
            logging.info(f"Marchés alt : fixture trouvé (fid={fid}) pour '{j1} vs {j2}' mais AUCUNE cote sets/jeux extraite (match commencé, ou format Pinnacle non reconnu).")
            continue

        ts = cotes.get("total_sets", {})
        tg = cotes.get("total_games", {})

        # MODE OBSERVATION : on logue ce qu'on a trouvé, sans l'injecter à Claude
        if MARCHES_ALT_MODE == "observation":
            logging.info(
                f"[OBS] {j1} vs {j2} — "
                f"Sets O/U 2.5: Over {ts.get('over','?')}/Under {ts.get('under','?')} | "
                f"Games O/U {tg.get('ligne','?')}: Over {tg.get('over','?')}/Under {tg.get('under','?')}"
            )
            continue

        # MODE ACTIF : on stocke pour l'ajouter au prompt de Claude
        if MARCHES_ALT_MODE == "actif":
            ts_a = cotes.get("total_sets", {})
            tg_a = cotes.get("total_games", {})
            logging.info(
                f"[ACTIF] Cotes Pinnacle lues pour {j1} vs {j2} — "
                f"Sets O/U 2.5: Over {ts_a.get('over','?')}/Under {ts_a.get('under','?')} | "
                f"Games O/U {tg_a.get('ligne','?')}: Over {tg_a.get('over','?')}/Under {tg_a.get('under','?')} "
                f"→ ajout au prompt Claude."
            )
            tickets_alt.append({
                "j1": j1, 
                "j2": j2, 
                "cotes": cotes
            })

    logging.info(f"Marchés alt : sortie examen — {len(tickets_alt)} ticket(s) alt préparé(s).")
    return tickets_alt


def ajouter_instructions_marches_alternatifs(prompt_de_base, donnees_marches_list):
    """
    Intègre les instructions de raisonnement pour Claude ET lui fournit 
    les cotes réelles formatées pour les marchés alternatifs des matchs serrés.
    Les cotes sont celles de PINNACLE (référence sharp) — l'abonné vérifie la
    cote Winamax avant de jouer, d'où la mise fixe réduite MARCHES_ALT_MISE%.
    """
    if not donnees_marches_list:
        return prompt_de_base

    str_marches = ""
    for dm in donnees_marches_list:
        j1, j2 = dm["j1"], dm["j2"]
        cotes = dm["cotes"]
        ts = cotes.get("total_sets", {})
        tg = cotes.get("total_games", {})
        str_marches += f"- {j1} vs {j2} | Sets O/U 2.5: Over {ts.get('over','?')}/Under {ts.get('under','?')} | Games O/U {tg.get('ligne','?')}: Over {tg.get('over','?')}/Under {tg.get('under','?')}\n"

    instructions_sets = f"""
⚠️ ANALYSE SPÉCIFIQUE : MARCHÉS "NOMBRE DE SETS" (O/U 2.5) ET "NOMBRE DE JEUX"
Tu dois évaluer le Moneyline ET les marchés alternatifs (Sets/Jeux) de manière indépendante pour trouver la meilleure Value. Tu as le droit de proposer le marché des Sets ou des Jeux à la place du Moneyline s'il présente un meilleur edge.

📊 COTES PINNACLE EXTRAITES POUR CES MATCHS SERRÉS :
{str_marches}
⚠️ NATURE DE CES COTES — RÈGLES SPÉCIALES (dérogation encadrée à la règle Winamax) :
• Ces cotes viennent de PINNACLE, le bookmaker de référence le plus "sharp" : sa
  probabilité implicite (1/cote) est la meilleure approximation de la vraie probabilité.
• Elles servent de RÉFÉRENCE DE PROBABILITÉ pour détecter la value — PAS de cote jouable.
• Dans le ticket, tu DOIS écrire : "Cote de référence Pinnacle X.XX — vérifier la cote
  Winamax avant de jouer" (l'abonné valide la cote Winamax à la main).
• MISE FIXE : {MARCHES_ALT_MISE}% (phase de test de ce segment — pas de Kelly).

🎯 CRITÈRES DE VALIDATION D'UN PARI SETS/JEUX (contre un book sharp) :
• edge = ta probabilité estimée − probabilité implicite Pinnacle (1/cote du sens choisi)
• edge < {MARCHES_ALT_MARGE_MIN:.2f} → pas assez de value contre un book sharp → abandon
• edge > {MARCHES_ALT_ECART_MAX:.2f} → tu diverges trop de Pinnacle → c'est probablement
  TOI qui te trompes → abandon (même logique que le garde-fou Wang/Ruse)
• Ta probabilité estimée ne peut JAMAIS dépasser {MARCHES_ALT_PROBA_MAX:.0%} sur ces
  marchés dérivés (anti-surconfiance).

📈 FACTEURS FAVORISANT L'OVER 2.5 SETS (Va au 3e set) :
1. Cotes Moneyline très proches (match équilibré).
2. Hold% (pourcentage de jeux de service gagnés) élevé pour les DEUX joueurs sur cette surface. Moins il y a de breaks, plus la probabilité d'un match accroché augmente.
3. Historique H2H (confrontations directes) marqué par des matchs longs ou en 3 sets.
4. Styles de jeu similaires où personne ne parvient à imposer une domination tactique claire.

📉 FACTEURS FAVORISANT L'UNDER 2.5 SETS (Victoire nette en 2 sets secs) :
1. Supériorité manifeste d'un joueur sur LA SURFACE SPÉCIFIQUE du match, même si les cotes ML globales sont proches.
2. Écart de forme majeur récent entre les deux joueurs (l'un surperforme, l'autre sous-performe).
3. L'un des joueurs possède un très faible Hold% au service, ce qui permet à l'autre de breaker et de conclure rapidement.

🎾 RÈGLE D'ARBITRAGE MONEYLINE vs MARCHÉ ALTERNATIF (PRIORITAIRE) :
• Le marché alternatif est une OPTION supplémentaire, JAMAIS un remplacement systématique du Moneyline.
• Évalue TOUJOURS les deux indépendamment. UN SEUL ticket par match, jamais les deux.
• Si les DEUX présentent de la value → priorité au MONEYLINE (marché principal, cote Winamax
  directe, pas de vérification manuelle nécessaire), SAUF si l'edge du marché alternatif est
  NETTEMENT supérieur : au moins 3 points de % d'edge de plus que le Moneyline.
• Si seul le Moneyline a de la value → joue le Moneyline. Si seul l'alternatif → joue l'alternatif.
• Si aucun des deux → abandon du match, comme d'habitude.

🎯 RÈGLE DE SÉLECTION :
- Calcule ton estimation de probabilité d'aller en 3 sets (Over 2.5) et compare-la à la probabilité du marché Pinnacle (1 / cote).
- Si la Value est sur le Moneyline, propose le Moneyline.
- Si le marché se trompe sur la durée du match (sous-estime un match accroché ou surestime la résistance de l'outsider), propose le marché alternatif adéquat.
- PRÉCISION LIGNES DE JEUX : la ligne fournie ci-dessus est déjà une ligne de MATCH
  complet (entre 18.5 et 26.5) — les totaux d'un seul set ont été exclus à la source.
"""
    return prompt_de_base + "\n" + instructions_sets


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
    logging.info(f"AceAnalytics bot.py v{VERSION} — Session {session} — fenêtre {heure} → {heure_fin}")

    heure_utc_min      = (maintenant.astimezone(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
    odds_matchs        = precollecte_odds_api(heure_utc_min)
    rapid_matchs_tous  = precollecte_rapidapi_tennis(date)
    # Pré-filtrage horaire AVANT enrichissement — concentre les requêtes sur la fenêtre.
    # v7.9.3 : on GARDE la liste complète (rapid_matchs_tous) pour la correction
    # d'heures. Bug du 21/07 (Van De Zandschulp) : match démarré à 19:20, 10 min
    # AVANT la fenêtre → exclu du calendrier filtré → la correction d'heures
    # n'avait aucune référence → l'heure inventée par Gemini (22:15) a tenu, et
    # un ticket est parti sur un match déjà commencé.
    rapid_matchs       = filtrer_matchs_par_fenetre(rapid_matchs_tous, heure, heure_fin)
    # v7.9 : pré-filtre TOURNOIS AVANT enrichissement/Gemini — ne traiter que les
    # matchs jouables, pour que Gemini concentre sa recherche dessus (cf. 21/07).
    tournois_winamax   = charger_tournois_winamax()
    rapid_matchs       = prefiltrer_rapid_matchs_par_tournoi(rapid_matchs, tournois_winamax)
    rapid_matchs       = enrichir_matchs_rapidapi(rapid_matchs, budget_requetes=4)
    calendrier_injecte = fusionner_calendrier(odds_matchs, rapid_matchs)
    if not calendrier_injecte:
        logging.info(f"Calendrier API vide → Gemini cherchera lui-même ({len(tournois_winamax)} tournois Winamax).")
    donnees_json       = collecter_donnees_tennis(date, heure, calendrier_injecte, rapid_matchs, heure_fin, tournois_winamax)

    # ----- FIXTURES ODDSPAPI (v7.8 : fetch AVANCÉ ici — 1 seul appel, réutilisé
    # par le repli Pinnacle, les heures autoritaires et les marchés alternatifs)
    # v7.9.3 : on récupère TOUT (live/terminés inclus) — la correction d'heures
    # a besoin des matchs commencés (leur heure réelle sert à les écarter).
    # La liste "jouable" (sans live/terminés) est dérivée ensuite pour les
    # autres usages (repli Pinnacle, marchés alternatifs).
    fixtures_completes = []
    fixtures_oddspapi = []
    if RAPIDAPI_KEY:
        fixtures_completes = oddspapi_fixtures_jour(exclure_termines=False)
        _exclus_st = ("Finished", "Live", "In-Play", "Cancelled", "Postponed", "Retired")
        fixtures_oddspapi = [f for f in fixtures_completes
                             if str((f.get("status") or {}).get("statusName") or "") not in _exclus_st]
        if fixtures_completes:
            logging.info(f"OddsPapi : {len(fixtures_completes) - len(fixtures_oddspapi)} match(s) "
                         f"terminé(s)/en cours exclu(s), {len(fixtures_oddspapi)} jouable(s).")

    # Filtre DUR déterministe : retirer les matchs hors fenêtre horaire AVANT Claude.
    # Évite que Claude propose un match déjà joué (ex: 09:00 en session SOIR 22:50→05:00).
    donnees_json = filtrer_json_par_fenetre(donnees_json, heure, heure_fin, date)
    # v8.4 : aucun match analysé sur la seule parole de Gemini
    donnees_json = filtrer_json_matchs_non_confirmes(donnees_json, fixtures_completes, rapid_matchs_tous)
    # v8.2 : retirer les matchs remontés deux fois (orthographes différentes)
    donnees_json = dedupliquer_matchs_json(donnees_json)
    # Filtre TOURNOIS (v7.6.1) : hors liste Winamax = injouable → retiré avant Claude
    donnees_json = filtrer_json_hors_tournois(donnees_json, tournois_winamax)
    # REPLI PINNACLE (v7.8) : compléter la cote des matchs jouables sans cote Winamax
    donnees_json = completer_cotes_pinnacle(donnees_json, fixtures_oddspapi)
    # Filtre COTES (v7.3) : matchs sans cote = injouables → retirés avant Claude
    donnees_json = filtrer_json_sans_cote(donnees_json)
    # Filtre ITF (v7.4) : retirer les ITF/Futures (stats introuvables, injouables)
    donnees_json = filtrer_json_itf(donnees_json)

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
        donnees_json = dedupliquer_matchs_json(donnees_json)   # v8.2
        donnees_json = filtrer_json_hors_tournois(donnees_json, tournois_winamax)
        donnees_json = completer_cotes_pinnacle(donnees_json, fixtures_oddspapi)
        donnees_json = filtrer_json_sans_cote(donnees_json)
        donnees_json = filtrer_json_itf(donnees_json)

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

    # ----- FIXTURES : déjà récupérés en amont (v7.8) — réutilisation directe
    if fixtures_completes:
        # v7.4 : heures autoritaires — l'epoch OddsPapi écrase l'heure Gemini.
        # v7.9.3 : on passe les fixtures COMPLETS (live/terminés inclus) — un
        # match déjà commencé doit récupérer son heure RÉELLE pour être écarté
        # par le re-filtrage fenêtre, pas rester sur l'heure inventée par Gemini.
        donnees_json = corriger_heures_avec_oddspapi(donnees_json, fixtures_completes)
    # v7.9.3 : le calendrier RapidAPI COMPLET corrige ce qu'OddsPapi n'a pas
    # apparié (plus un simple else — les deux corrections s'enchaînent).
    donnees_json = corriger_heures_avec_calendrier(donnees_json, rapid_matchs_tous)
    # Re-filtrer par fenêtre : une heure corrigée peut sortir de la session
    donnees_json = filtrer_json_par_fenetre(donnees_json, heure, heure_fin, date)

    # v7.6.3 : références du modèle, réutilisées par le garde-fou plus bas.
    # Calculées APRÈS la correction d'heures et le filtrage final — avant ce
    # correctif, bc_refs était figé sur un état antérieur des données : le
    # garde-fou et le prompt (qui recalcule son propre bloc sur le JSON final)
    # pouvaient travailler sur deux ensembles de matchs différents.
    _, bc_refs = construire_bloc_modele(donnees_json)
    if bc_refs:
        logging.info(f"Modèle Barnett-Clarke : {len(bc_refs)} match(s) avec référence chiffrée.")
    else:
        logging.info("Modèle Barnett-Clarke : aucune référence (stats de surface absentes).")

    prompt = construire_prompt_claude(date, heure, donnees_json, heure_fin)

    # LOG DEBUG — affiche les matchs que Gemini a retournés
    try:
        donnees_debug = json.loads(donnees_json)
        matchs_debug  = donnees_debug.get("matchs", [])
        logging.info(f"DEBUG — {len(matchs_debug)} match(s) transmis à Claude :")
        for i, m in enumerate(matchs_debug, 1):
            s1 = m.get("stats_surface_j1") or {}
            s2 = m.get("stats_surface_j2") or {}
            logging.info(
                f"  [{i}] {m.get('joueur1','?')} vs {m.get('joueur2','?')} "
                f"| {m.get('tournoi','TOURNOI ABSENT')} "
                f"| {m.get('heure_match','?')} | {m.get('surface','?')} "
                f"| Cotes {m.get('cote_j1','?')} / {m.get('cote_j2','?')} "
                f"| Hold J1: {m.get('hold_pct_j1','?')} / J2: {m.get('hold_pct_j2','?')}"
            )
            # v7.6 : tracer les NOUVELLES données — sans ça, impossible de savoir
            # si Gemini les remonte, donc impossible de décider si le modèle
            # Barnett-Clarke est viable ou s'il faut alléger le schéma.
            logging.info(
                f"       ↳ [BC] service/retour {m.get('surface','?')} — "
                f"J1 : {s1.get('serve_pts_won','∅')} / {s1.get('return_pts_won','∅')} "
                f"(éch. {s1.get('echantillon','∅')}) · "
                f"J2 : {s2.get('serve_pts_won','∅')} / {s2.get('return_pts_won','∅')} "
                f"(éch. {s2.get('echantillon','∅')})"
            )
            proba_ref, detail_ref = bc_reference_match(m)
            if proba_ref is not None:
                logging.info(f"       ↳ [BC] {detail_ref}")
                try:
                    c1 = float(m.get("cote_j1") or 0)
                    if c1 > 1:
                        logging.info(
                            f"       ↳ [BC] modèle {proba_ref:.1%} vs marché "
                            f"{1/c1:.1%} → écart {(proba_ref - 1/c1)*100:+.1f} pts"
                        )
                except (TypeError, ValueError):
                    pass
            else:
                logging.info(f"       ↳ [BC] modèle silencieux : {detail_ref}")
            txt_fat, drapeaux_fat = bc_indice_fatigue(m)
            if txt_fat:
                logging.info(f"       ↳ [FATIGUE] drapeaux : {drapeaux_fat or 'aucun'}")
    except Exception as e:
        logging.warning(f"DEBUG log matchs : {e}")

    # ----- MARCHÉS ALTERNATIFS (sets / jeux) — actif seulement si MARCHES_ALT_MODE != "off"
    tickets_alt_data = []
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
                    # v7.4.2 : mémoriser l'écart de cotes pour prioriser les
                    # plus serrés (le plafond d'appels sacrifiait le mauvais
                    # match — constat 14/07 : Budkov Kjaer 2.0/1.8, le plus
                    # équilibré des cinq, était le seul ignoré).
                    matchs_serres.append((abs(c1 - c2), m))
            # Trier du plus serré au moins serré AVANT le plafond d'appels
            matchs_serres.sort(key=lambda t: t[0])
            matchs_serres = [m for _, m in matchs_serres]
            if matchs_serres:
                logging.info(f"Marchés alt ({MARCHES_ALT_MODE}) : {len(matchs_serres)} match(s) serré(s) à examiner.")
                tickets_alt_data = analyser_marches_alternatifs(matchs_serres, date, fixtures_oddspapi)

                # Injection des instructions si mode actif et cotes trouvées
                if MARCHES_ALT_MODE == "actif" and tickets_alt_data:
                    prompt = ajouter_instructions_marches_alternatifs(prompt, tickets_alt_data)
            else:
                logging.info("Marchés alt : aucun match serré aujourd'hui.")
        except Exception as e:
            logging.warning(f"Marchés alt : erreur non bloquante : {e}")

    # Bascule dynamique Sonnet → Opus selon richesse des données
    # PROTÉGÉ v7.2 : un JSON invalide ne plante plus le run ici
    try:
        nb_matchs_analyse = len(json.loads(donnees_json).get("matchs", []))
    except Exception:
        nb_matchs_analyse = 0
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
            try:
                nb = len(json.loads(donnees_json).get("matchs", []))
            except Exception:
                nb = 0
            # Extraire l'explication de Claude (500 premiers chars après AUCUN_MATCH)
            explication = ""
            idx = texte.find("AUCUN_MATCH")
            if idx != -1:
                suite = texte[idx + len("AUCUN_MATCH"):].strip()
                # v7.4.3 : synthèse BRÈVE et propre — l'ancien [:200] tranchait
                # en plein mot ("Martineau/" — Telegram 14/07). On garde 1-2 phrases
                # complètes : coupe à la dernière fin de phrase avant ~220 chars.
                explication = re.sub(r'<[^>]+>', '', suite).strip()
                if len(explication) > 220:
                    coupe = explication[:220]
                    pos_phrase = max(coupe.rfind(". "), coupe.rfind("! "), coupe.rfind("? "))
                    if pos_phrase > 80:
                        explication = coupe[:pos_phrase + 1]
                    else:
                        explication = coupe[:coupe.rfind(" ")].rstrip(",;:—-") + "…"
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
            """v7.5.2 : retourne (bool, raison) — la raison alimente le
            suivi des tickets rejetés (mesure pure, voir tickets_rejetes.json)."""
            t_lower = t.lower()
            # Signaux d'abandon explicites dans le texte
            if any(sig in t_lower for sig in ["abandon de ce ticket", "ticket abandonné",
                                               "kelly 0%", "mise : 0%", "mise 0%"]):
                return False, "abandon explicite dans le texte"

            # v8.7 — AUTO-RÉTRACTATION. Constat du 06/08 : Claude a rédigé le
            # ticket Darderi/Shang PUIS s'est corrigé dans la même réponse
            # (« l'absence de Shang dépasse 2 mois → filtre skip s'applique.
            # Je retire ce ticket. […] Seul le ticket Gibson est retenu »).
            # Le code a découpé la réponse et envoyé les DEUX tickets, dont
            # celui que Claude venait d'annuler — pari invalide publié et
            # inscrit dans pari_en_cours.json. Quand l'analyste se dédit, sa
            # rétractation prime sur le ticket qu'elle annule.
            _retractations = [
                "je retire ce ticket", "je retire donc ce ticket",
                "ce ticket est retiré", "ticket retiré", "ticket écarté",
                "j'écarte ce ticket", "je l'écarte", "n'est pas retenu",
                "ne doit pas être envoyé", "j'annule ce ticket",
                "ticket annulé", "(correction)", "→ skip", "-> skip",
                "filtre skip s'applique", "par cohérence avec le filtre",
                # v8.8 — cas du 08/08 (Pegula) : Claude a rédigé le ticket, calculé
                # l'absence de value et conclu « delta insuffisant … aucun edge.
                # Annulé. » — sans employer aucune des formules ci-dessus.
                "delta insuffisant", "aucun edge", "pas d'edge", "edge insuffisant",
                "aucune value", "pas de value", "value insuffisante",
                "annulé.", "annulé\n", "abandonné.", "→ abandon", "-> abandon",
            ]
            _trouve = next((r for r in _retractations if r in t_lower), None)
            if _trouve:
                logging.warning(f"Ticket rejeté — AUTO-RÉTRACTATION de l'analyste "
                                f"détectée (« {_trouve} »). Le ticket est annulé "
                                f"par son propre auteur.")
                return False, f"auto-rétractation de l'analyste (« {_trouve} »)"
            # Rejeter les cotes inventées/estimées — SAUF référence Pinnacle assumée
            # (marchés alternatifs : "cote de référence pinnacle" est légitime)
            est_ref_pinnacle = "référence pinnacle" in t_lower or "reference pinnacle" in t_lower
            if not est_ref_pinnacle and any(sig in t_lower for sig in
                    ["cote estimée", "estimée", "non disponible précisément",
                     "cote approximative", "estimation de cote",
                     "cote non disponible", "cote handicap estimée"]):
                logging.info("Ticket rejeté — cote estimée/inventée détectée.")
                return False, "cote estimée/inventée"
            # FILTRE HORAIRE : extraire l'heure du ticket (ligne HEURE) et vérifier la fenêtre.
            # Empêche de parier sur un match déjà joué (ex: 09:00 en session soir 22:50→05:00).
            mh = re.search(r"heure\s*:?\s*</b>?\s*(\d{1,2})[h:](\d{2})", t_lower)
            if not mh:
                mh = re.search(r"(\d{1,2})[h:](\d{2})", t_lower)
            if mh:
                hm = f"{int(mh.group(1)):02d}:{mh.group(2)}"
                if not _heure_dans_fenetre(hm, heure, heure_fin):
                    logging.warning(f"Ticket rejeté — match à {hm} hors fenêtre {heure}→{heure_fin} (déjà joué ou trop tard).")
                    return False, f"hors fenêtre horaire ({hm})"
            # Extraire le delta de la ligne VALUE : "delta +0.07" ou "delta -0.05"
            m = re.search(r"delta\s*([+-]?\d+[.,]\d+)", t_lower)
            if not m and "pinnacle" not in t_lower:
                # v8.8 — FAIL-CLOSED. Constat du 08/08 : Claude a écrit « delta
                # insuffisant » au lieu d'un nombre ; la regex n'a rien trouvé et
                # le garde-fou le PLUS IMPORTANT a été purement sauté — un ticket
                # sans aucune value est parti (Pegula à 1.38 pour une cote juste
                # de 1.60). Un delta illisible n'est pas un delta valide.
                # (Exception : marchés alternatifs Pinnacle, qui raisonnent en
                # edge et non en delta de cote.)
                logging.warning("Ticket rejeté — DELTA ILLISIBLE : aucune valeur "
                                "chiffrée trouvée dans la ligne VALUE. Le contrôle "
                                "de value ne peut pas s'appliquer → refus par sécurité.")
                return False, "delta illisible (aucune valeur chiffrée)"
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
                    return False, f"delta {delta:+.2f} < seuil {seuil_delta:.2f}"  # Delta insuffisant pour cette cote → abandon
            # v7.6.6 : référence Barnett-Clarke du joueur PARIÉ, calculée EN AMONT
            # des deux garde-fous ci-dessous (l'un s'en sert pour se calmer, l'autre
            # pour trancher). Le matching de noms est tolérant (Claude abrège).
            def _score_nom(nom, texte):
                jetons = [j for j in nom.split() if len(j) >= 3]
                if not jetons:
                    return 0
                score = 10 if jetons[-1] in texte else 0   # nom de famille = discriminant
                score += sum(1 for j in jetons[:-1] if j in texte)
                return score

            bc_ref_pari = None  # proba de référence BC pour le joueur pronostiqué
            _mprono_bc = re.search(r"prono\s*:\s*(?:</b>)?\s*(.+)", t_lower)
            if _mprono_bc:
                _prono_bc = _mprono_bc.group(1)
                for (rj1, rj2), proba_ref in bc_refs.items():
                    if not (_score_nom(rj1, t_lower) or _score_nom(rj2, t_lower)):
                        continue
                    _s1, _s2 = _score_nom(rj1, _prono_bc), _score_nom(rj2, _prono_bc)
                    if _s1 > _s2:
                        bc_ref_pari = proba_ref
                    elif _s2 > _s1:
                        bc_ref_pari = 1.0 - proba_ref
                    break

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
                    # v7.6.6 : le durcissement à 10% suppose que Claude s'écarte du
                    # marché SANS appui. Mais si le modèle Barnett-Clarke CORROBORE
                    # Claude (les deux proches, ≤ 8 pts), l'écart au marché n'est plus
                    # une survalorisation : c'est de la value chiffrée. Le modèle EST
                    # la donnée qui "manquait" → on lève le durcissement.
                    # Cas réel du 18/07 (Collignon) : Claude 71%, modèle 70.8%, marché
                    # 64% → l'ancien code rejetait un pari que le modèle validait.
                    # (Le garde-fou Barnett-Clarke ci-dessous, lui, reste actif et
                    # rejette toujours un Claude qui s'écarte DU MODÈLE — cas Altmaier.)
                    if (donnees_manquantes and bc_ref_pari is not None
                            and abs(proba_bot - bc_ref_pari) <= 0.08
                            and bc_ref_pari > proba_marche):
                        logging.info(
                            f"[écart-marché] durcissement levé : le modèle BC corrobore "
                            f"Claude ({proba_bot:.0%} vs modèle {bc_ref_pari:.0%}, tous deux "
                            f"> marché {proba_marche:.0%}) → seuil normal 30%."
                        )
                        seuil = 0.30
                    if ecart_relatif > seuil:
                        raison = "données manquantes + survalorisation" if donnees_manquantes else "faux delta probable (cf. Wang/Ruse)"
                        logging.warning(
                            f"Ticket rejeté — écart modèle-marché trop élevé : "
                            f"bot {proba_bot*100:.0f}% vs marché {proba_marche*100:.0f}% "
                            f"(+{ecart_relatif*100:.0f}%, seuil {seuil*100:.0f}%). {raison}."
                        )
                        return False, f"écart modèle-marché +{ecart_relatif*100:.0f}% ({raison})"

            # GARDE-FOU BARNETT-CLARKE (v7.6) — le contrôle passe du DÉCLARATIF au CALCULÉ.
            # Avant : le seuil dépendait du champ "données manquantes" que Claude
            # remplissait LUI-MÊME (Altmaier : "aucune donnée manquante" → seuil
            # relâché à 30% → ticket accepté). Ici, Python compare l'estimation de
            # Claude à une probabilité dérivée des chiffres. Claude ne choisit plus
            # la sévérité qu'on lui applique.
            # v7.6.2 : matching TOLÉRANT des noms. Bug constaté le 17/07 : le
            # modèle connaît "Adolfo Daniel Vallejo" (nom du JSON), Claude écrit
            # "Vallejo vainqueur" dans le PRONO → `rj1 in prono_txt` était FAUX
            # → le code sortait par `break` SANS vérifier. Le garde-fou était
            # donc silencieusement inerte dès que Claude abrégeait un nom (soit
            # presque toujours). _score_nom est défini plus haut (v7.6.6).
            for (rj1, rj2), proba_ref in bc_refs.items():
                if not (_score_nom(rj1, t_lower) or _score_nom(rj2, t_lower)):
                    continue
                # À quel joueur se rapporte le pari ? Celui nommé dans le PRONO.
                mprono = re.search(r"prono\s*:\s*(?:</b>)?\s*(.+)", t_lower)
                if not mprono:
                    break
                prono_txt = mprono.group(1)
                s1, s2 = _score_nom(rj1, prono_txt), _score_nom(rj2, prono_txt)
                # v7.9.1 : bug du 21/07 (Rus/Avanesyan). Si le PRONO ne tranche pas
                # (s1==s2, ex. les deux noms présents, ou libellé bruité), on ne
                # DÉSACTIVE PAS le garde-fou — on essaie de trancher autrement avant
                # d'abandonner. Un garde-fou qui se tait laisse passer un pari à
                # contre-modèle (Avanesyan pariée à 34% par le modèle, envoyée quand
                # même). Fallback : chercher le joueur parié par sa position dans le
                # prono (le nom du parié apparaît AVANT tout adversaire éventuel).
                if s1 == s2:
                    pos1 = prono_txt.find(rj1.split()[-1])  # position du nom de famille
                    pos2 = prono_txt.find(rj2.split()[-1])
                    # Un seul des deux est présent → c'est lui le parié
                    if pos1 >= 0 and pos2 < 0:
                        s1 = 1  # force J1
                    elif pos2 >= 0 and pos1 < 0:
                        s2 = 1  # force J2
                    elif pos1 >= 0 and pos2 >= 0:
                        # Les deux présents (prono bruité) → le premier cité est le parié
                        if pos1 < pos2:
                            s1 = 1
                        else:
                            s2 = 1
                if s1 > s2:
                    ref_pari = proba_ref
                elif s2 > s1:
                    ref_pari = 1.0 - proba_ref
                else:
                    # Vraiment aucun joueur identifiable (marché alternatif type
                    # "Over 2.5 sets") → hors périmètre du garde-fou Moneyline.
                    # v7.9.1 : par SÉCURITÉ, on ne fait plus un simple break silencieux
                    # sur un ticket Moneyline — si le marché est moneyline mais qu'on
                    # n'a pas pu identifier le joueur, on REJETTE (le garde-fou ne peut
                    # pas faire son travail → on ne prend pas le risque).
                    est_alternatif = any(x in t_lower for x in
                                         ["over", "under", "o/u", "sets", "jeux", "games"])
                    if not est_alternatif:
                        logging.warning("[BC] REJET sécurité : ticket Moneyline mais joueur "
                                        f"parié non identifiable dans le prono ({prono_txt[:60]!r}) "
                                        "→ garde-fou aveugle, on ne prend pas le risque.")
                        return False, "garde-fou BC aveugle (joueur non identifié) — rejet sécurité"
                    logging.info("[BC] garde-fou non applicable : marché alternatif "
                                 f"({prono_txt[:40]!r}).")
                    break
                if mp:  # proba annoncée par Claude, déjà extraite plus haut
                    ecart_pts = (proba_bot - ref_pari) * 100
                    logging.info(
                        f"[BC] Claude {proba_bot:.1%} vs modèle {ref_pari:.1%} "
                        f"→ écart {ecart_pts:+.1f} pts (seuil {BC_ECART_MAX_POINTS:.0f})"
                    )
                    if ecart_pts > BC_ECART_MAX_POINTS:
                        logging.warning(
                            f"Ticket rejeté — Claude surestime de {ecart_pts:+.1f} pts "
                            f"vs le modèle chiffré ({proba_bot:.1%} contre {ref_pari:.1%}). "
                            f"C'est le motif Altmaier : le récit écrase les chiffres."
                        )
                        return False, f"écart modèle Barnett-Clarke {ecart_pts:+.1f} pts"
                break
            return True, None

        tickets_valides = []
        for t in tickets:
            if not (t.startswith("🎾") or t.startswith("🔴")):
                continue  # Pas un ticket structuré valide
            valide, raison_rejet = _ticket_valide(t)
            if not valide:
                logging.info("Ticket rejeté (delta < seuil ou abandon explicite) — non envoyé.")
                # v7.5.2 : trace le rejet pour mesure future — AUCUN impact sur
                # les stats officielles, AUCUN envoi Telegram, juste un journal
                # pour un jour évaluer si les garde-fous rejettent net plus de
                # gagnants que de perdants (voir historique Etcheverry/Merida
                # Aguilar du 15/07 — un rejet peut être une fausse alerte, la
                # question est de savoir si c'est la tendance ou l'exception).
                sauvegarder_ticket_rejete(t, raison_rejet, date)
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

        # v7.5 : lookup surface par match (mesure seule — n'influence AUCUNE
        # décision d'analyse). Reparse défensif de donnees_json, indépendant
        # de matchs_debug qui peut ne pas exister si son bloc try a échoué.
        surface_par_match = {}
        try:
            for m in json.loads(donnees_json).get("matchs", []):
                j1n = str(m.get("joueur1", "")).lower()
                j2n = str(m.get("joueur2", "")).lower()
                if j1n and j2n:
                    surface_par_match[(j1n, j2n)] = m.get("surface", "")
        except Exception:
            pass

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
                pari_rec = {
                    "pari":   ticket_propre,
                    "date":   date,
                    "marche": _detecter_marche(ticket_propre),
                    "niveau": _detecter_niveau(ticket_propre),
                    "modele": "opus" if modele_choisi == CLAUDE_OPUS else "sonnet",
                }
                # v7.4 : cote et mise pour le calcul de YIELD (profit/mises)
                tpl = ticket_propre.lower()
                mcote = re.search(r"cote\s*:?\s*(?:</b>)?\s*(\d+[.,]\d+)", tpl)
                mmise = re.search(r"mise\s*:?\s*(?:</b>)?\s*(\d+(?:[.,]\d+)?)\s*%", tpl)
                if mcote:
                    pari_rec["cote"] = float(mcote.group(1).replace(",", "."))
                if mmise:
                    pari_rec["mise_pct"] = float(mmise.group(1).replace(",", "."))
                # v7.5 : surface du match (segment de mesure — voir _normaliser_surface)
                for (j1n, j2n), surf in surface_par_match.items():
                    if j1n in tpl or j2n in tpl:
                        pari_rec["surface"] = _normaliser_surface(surf)
                        break
                # v7.4 : CLV — joindre la moneyline Pinnacle du moment si dispo
                for dm in tickets_alt_data:
                    if dm["j1"].lower() in tpl or dm["j2"].lower() in tpl:
                        ml = dm.get("cotes", {}).get("moneyline")
                        if ml:
                            pari_rec["pinnacle_ml_ouverture"] = ml
                        break
                sauvegarder_pari_pour_suivi(pari_rec)
                hashes_connus.add(h)
                nouveaux_hashes.append(h)
                paris_envoyes += 1
                if i < len(tickets):
                    time.sleep(1)

        # CORRIGÉ v7.2 : conserver l'ordre chronologique (historique + nouveaux).
        # Avant : list(set) → ordre aléatoire → le [-20:] pouvait jeter les hashes
        # récents et garder des vieux → déduplication non fiable.
        if nouveaux_hashes and not DRY_RUN:
            sauvegarder_historique(historique + nouveaux_hashes, hist_sha)
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

def regler_paris_interactif():
    """
    Règlement SEMI-AUTOMATIQUE des paris en cours (v7.4) :
    1. Charge la file pari_en_cours.json
    2. Récupère les matchs du jour OddsPapi AVEC les terminés (1 requête)
    3. Pour chaque pari : apparie le match, affiche le score final et le vainqueur
    4. TOI tu confirmes v/d (ou s pour passer) — le bot ne décide JAMAIS seul
       si le pari est gagné (un score ne suffit pas pour juger un écart de jeux
       ou un over/under sans risque d'erreur).
    Limite assumée : OddsPapi ne couvre que les matchs DU JOUR → lancer 'regler'
    le soir même. Les paris plus anciens restent réglables via 'resultat v/d N'.
    """
    paris, _ = _gh_get("pari_en_cours.json")
    if not paris:
        print("File vide — aucun pari à régler.")
        return
    if not RAPIDAPI_KEY:
        print("RAPIDAPI_KEY absente — impossible de récupérer les scores.")
        return

    fixtures = oddspapi_fixtures_jour(exclure_termines=False)
    if not fixtures:
        print("OddsPapi indisponible — réessaie plus tard ou utilise 'resultat v/d N'.")
        return

    def _extraire_joueurs(p):
        txt = re.sub(r"<[^>]+>", "", p.get("pari", ""))
        m = re.search(r"matchs?\s*:?\s*(.+?)\s+vs\s+(.+)", txt, re.IGNORECASE)
        if not m:
            return None, None
        return m.group(1).strip()[:40], m.group(2).strip().split("\n")[0][:40]

    # Parcourir en ordre INVERSE : enregistrer_resultat retire le pari de la
    # file par index — en remontant, les index des paris restants ne bougent pas.
    for idx in range(len(paris), 0, -1):
        p = paris[idx - 1]
        j1, j2 = _extraire_joueurs(p)
        resume = re.sub(r"<[^>]+>", "", p.get("pari", "")).replace("\n", " ")[:90]
        print(f"\n[{idx}] {p.get('date','?')} | {resume}")
        if not j1 or not j2:
            print("   ⚠️ Joueurs non identifiables dans le ticket — passe (règle via 'resultat v/d N').")
            continue
        fx = oddspapi_trouver_fixture(j1, j2, fixtures)
        if not fx:
            print(f"   ⚠️ Match '{j1} vs {j2}' introuvable chez OddsPapi (autre jour ?) — passé.")
            continue
        statut = str((fx.get("status") or {}).get("statusName") or "?")
        scores = fx.get("scores", {}) or {}
        resultat = scores.get("result", {})
        if statut != "Finished" or not resultat:
            print(f"   ⏳ Match pas terminé (statut : {statut}) — passé.")
            continue
        pn = fx.get("participants", {})
        n1 = pn.get("participant1Name", "?")
        n2 = pn.get("participant2Name", "?")
        s1 = resultat.get("participant1Score", "?")
        s2 = resultat.get("participant2Score", "?")
        sets_detail = []
        for per in ("p1", "p2", "p3"):
            sd = scores.get(per)
            if sd:
                sets_detail.append(f"{sd.get('participant1Score','?')}-{sd.get('participant2Score','?')}")
        vainqueur = n1 if str(s1) > str(s2) else n2
        print(f"   🏁 Score final : {n1} {s1}-{s2} {n2} ({', '.join(sets_detail)}) — Vainqueur : {vainqueur}")
        try:
            rep_u = input("   → Ton pari : [v]ictoire / [d]éfaite / [s]auter ? ").strip().lower()
        except EOFError:
            print("   Mode non interactif — utilise 'resultat v/d N'.")
            return
        if rep_u in ("v", "victoire", "1"):
            enregistrer_resultat(True, index_pari=idx)
        elif rep_u in ("d", "defaite", "0"):
            enregistrer_resultat(False, index_pari=idx)
        else:
            print("   Passé.")
    _quota_persister()
    print("\n✅ Règlement terminé.")


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

    # v7.4 : YIELD global (la vraie métrique de rentabilité, pas le win rate)
    roi = s.get("roi", {})
    if roi.get("mise_totale"):
        y = roi["profit"] / roi["mise_totale"] * 100
        lignes.append(
            f"💶 <b>Profit : {roi['profit']:+.2f}u</b> sur {roi['mise_totale']:.2f}u misées "
            f"— <b>Yield {y:+.1f}%</b>"
        )

    # Détail par marché (si v2 disponible)
    par_marche = s.get("par_marche", {})
    if par_marche:
        détails = []
        for marche, vd in par_marche.items():
            v, d = vd.get("v", 0), vd.get("d", 0)
            if v + d > 0:
                wr_m = v / (v + d) * 100
                extra = ""
                if vd.get("mise"):
                    extra = f" · yield {vd.get('profit', 0) / vd['mise'] * 100:+.0f}%"
                détails.append(f"  • {marche} : {v}V/{d}D ({wr_m:.0f}%){extra}")
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
                extra = ""
                if vd.get("mise"):
                    extra = f" · yield {vd.get('profit', 0) / vd['mise'] * 100:+.0f}%"
                détails_n.append(f"  • {niveau} : {v}V/{d}D ({wr_n:.0f}%){extra}")
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

    # v7.5 : détail par surface (Terre/Dur/Gazon) — mesure pure, aucune
    # décision d'analyse n'en dépend actuellement.
    par_surface = s.get("par_surface", {})
    if par_surface:
        détails_s = []
        for surface, vd in par_surface.items():
            v, d = vd.get("v", 0), vd.get("d", 0)
            if v + d > 0:
                wr_s = v / (v + d) * 100
                extra = ""
                if vd.get("mise"):
                    extra = f" · yield {vd.get('profit', 0) / vd['mise'] * 100:+.0f}%"
                détails_s.append(f"  • {surface.capitalize()} : {v}V/{d}D ({wr_s:.0f}%){extra}")
        if détails_s:
            lignes.append("\n🎾 <b>Par surface :</b>")
            lignes.extend(détails_s)

    # Quota RapidAPI du mois
    quota, _ = _gh_get("quota_rapidapi.json")
    if isinstance(quota, dict):
        lignes.append(
            f"\n⚙️ Quotas {quota.get('mois','?')} — "
            f"TennisAPI : {quota.get('tennisapi_mois', quota.get('rapidapi_utilise', 0))} req/mois "
            f"({quota.get('tennisapi_jour', '?')}/50 aujourd'hui) · "
            f"OddsPapi : {quota.get('oddspapi_mois', 0)}/1000."
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


# =====================================================================
# HARNAIS DE TEST HORS-LIGNE — `python bot.py test`
# =====================================================================
# v7.8 — Rejoue les validations gagnées durant le développement + contrôles
# d'intégrité structurelle. Objectif : attraper AVANT le push les 2 classes de
# bugs qui ont atteint la production cette semaine — fonctions fantômes
# (_maintenant_paris, _oddspapi_traiter_fixtures) et doublons de définition
# (completer_cotes_pinnacle défini 2×). Aucun appel réseau : 100% déterministe.

def _selftest():
    import ast, io, contextlib
    echecs = []
    oks = []
    def check(nom, cond, detail=""):
        (oks if cond else echecs).append(nom + (f" — {detail}" if detail and not cond else ""))

    # ---- A. INTÉGRITÉ STRUCTURELLE (sur le source lui-même) ----
    src = open(__file__).read()
    tree = ast.parse(src)

    # A1. Pas de fonction de module définie deux fois (bug completer_cotes 20/07)
    noms_mod = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    doublons = sorted({x for x in noms_mod if noms_mod.count(x) > 1})
    check("A1 aucun doublon de fonction", not doublons, f"doublons: {doublons}")

    # A2. Pas d'appel vers une fonction inexistante (bugs 'fantômes')
    defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    imports = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imports |= {a.asname or a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imports |= {a.asname or a.name for a in n.names}
    import builtins as _b
    connus = defs | imports | set(dir(_b))
    appels = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    fantomes = sorted(a for a in appels - connus
                      if not __import__("re").search(rf"(\b{a}\s*=|def {a}\(|as {a}\b|for {a}[ ,(])", src))
    check("A2 aucune fonction fantôme", not fantomes, f"fantômes: {fantomes}")

    # A3. Cohérence signature/appels de completer_cotes_pinnacle
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "completer_cotes_pinnacle"]
    if fns:
        np = len(fns[0].args.args)
        apps = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name) and c.func.id == "completer_cotes_pinnacle"]
        check("A3 signature completer_cotes_pinnacle", all(len(c.args) == np for c in apps),
              f"{np} params, appels {[len(c.args) for c in apps]}")

    # ---- B. MODÈLE BARNETT-CLARKE (valeurs connues) ----
    check("B1 jeu(0.50)=0.5 exact", abs(bc_proba_jeu(0.5) - 0.5) < 1e-12)
    check("B2 jeu(0.65)≈0.83 ATP", 0.828 < bc_proba_jeu(0.65) < 0.831)
    check("B3 match symétrique=0.5", abs(bc_proba_match(0.62, 0.62) - 0.5) < 1e-9)
    check("B4 antisymétrie M(A,B)+M(B,A)=1",
          abs(bc_proba_match(0.68, 0.61) + bc_proba_match(0.61, 0.68) - 1.0) < 1e-9)
    for surf, fm in BC_SERVE_MOYEN_SURFACE.items():
        if abs(bc_dominance_ratio(fm, 1 - fm) - 1.0) > 1e-9:
            check(f"B5 DR moyen=1.0 ({surf})", False); break
    else:
        check("B5 DR joueur moyen = 1.00", True)

    # ---- B-BIS. GRILLE PAR SURFACE (v8.0) ----
    # Validation croisée : le hold ATP impliqué par la moyenne de service réelle
    # doit tomber dans la fourchette annoncée par la matrice d'analyse.
    for surf in ("terre", "dur", "dur_indoor", "gazon"):
        p = BC_SERVE_MOYEN_CIRCUIT["atp"][surf]
        h = bc_proba_jeu(p)
        lo, hi = BC_NORMES_SURFACE[surf]["hold"]
        check(f"B6 hold ATP {surf} dans la norme {lo:.0%}-{hi:.0%}",
              lo <= h <= hi, f"hold obtenu {h:.1%}")
    # Hiérarchie des surfaces : terre < dur < dur_indoor < gazon
    check("B7 hiérarchie des surfaces",
          BC_SERVE_MOYEN_SURFACE["terre"] < BC_SERVE_MOYEN_SURFACE["dur"]
          < BC_SERVE_MOYEN_SURFACE["dur_indoor"] < BC_SERVE_MOYEN_SURFACE["gazon"])
    # Normes complètes pour chaque surface
    for surf in ("terre", "gazon", "dur", "dur_indoor"):
        n = BC_NORMES_SURFACE.get(surf, {})
        check(f"B8 normes {surf} complètes",
              all(k in n for k in ("hold", "break", "serve1", "serve2",
                                   "prioritaires", "piege")))
    # Détection du dur indoor
    check("B9 dur indoor détecté (flag)", _bc_cle_surface({"surface": "Hard", "indoor": True}) == "dur_indoor")
    check("B10 dur extérieur par défaut",  _bc_cle_surface({"surface": "Hard"}) == "dur")
    check("B11 terre insensible à indoor", _bc_cle_surface({"surface": "Clay", "indoor": True}) == "terre")

    # ---- B-TER. PORTIER 4 : plausibilité de la SORTIE (v8.0.1) ----
    def _hold_calc(f, g_adv, surf="dur"):
        return bc_proba_jeu(bc_proba_point_service(f, g_adv, surf))
    _seuil = BC_NORMES_SURFACE["dur"]["hold"][0] - 0.20
    check("B12 hold absurde détecté (Bucsa 54%→30%)", _hold_calc(0.540, 0.476) < _seuil)
    check("B13 hold sain conservé (Fils 65%→83%)",    _hold_calc(0.650, 0.350) > _seuil)
    check("B14 seuil dur = 60%", abs(_seuil - 0.60) < 1e-9)

    # ---- B-QUATER. CIRCUIT ATP/WTA (v8.1) ----
    check("B15 écart ATP/WTA entre 6 et 10 pts",
          all(0.06 < BC_SERVE_MOYEN_CIRCUIT["atp"][s] - BC_SERVE_MOYEN_CIRCUIT["wta"][s] < 0.10
              for s in ("terre", "dur", "dur_indoor", "gazon")))
    check("B16 hiérarchie des surfaces en WTA",
          BC_SERVE_MOYEN_CIRCUIT["wta"]["terre"] < BC_SERVE_MOYEN_CIRCUIT["wta"]["dur"]
          < BC_SERVE_MOYEN_CIRCUIT["wta"]["dur_indoor"] < BC_SERVE_MOYEN_CIRCUIT["wta"]["gazon"])
    check("B17 circuit via champ explicite", _bc_circuit({"circuit": "WTA"}) == "wta")
    check("B18 circuit via nom de tournoi",  _bc_circuit({"tournoi": "WTA Washington"}) == "wta")
    check("B19 défaut ATP",                  _bc_circuit({"tournoi": "Citi DC Open"}) == "atp")
    _h_atp = bc_proba_jeu(bc_proba_point_service(0.540, 0.476, "dur", "atp"))
    _h_wta = bc_proba_jeu(bc_proba_point_service(0.540, 0.476, "dur", "wta"))
    check("B20 normes WTA relèvent le hold (bug 28/07)", _h_wta - _h_atp > 0.15,
          f"atp {_h_atp:.1%} · wta {_h_wta:.1%}")
    check("B21 hold WTA plausible dans son circuit",
          _h_wta > BC_NORMES_CIRCUIT["wta"]["dur"]["hold"][0] - 0.20)

    # ---- B-QUINQUIES. DÉDUPLICATION DES MATCHS (v8.2) ----
    _d = {"matchs": [
        {"joueur1": "Martin Damm", "joueur2": "Ben Shelton",
         "stats_surface_j1": {"serve_pts_won": "70.4%"}, "stats_surface_j2": {}},
        {"joueur1": "Trevor Svajda", "joueur2": "Jakub Mensik",
         "stats_surface_j1": {}, "stats_surface_j2": {}},
        {"joueur1": "Martin Damm Jr.", "joueur2": "Ben Shelton",
         "stats_surface_j1": {"serve_pts_won": "70.4%", "return_pts_won": "33.6%"},
         "stats_surface_j2": {}},
    ]}
    _r = json.loads(dedupliquer_matchs_json(json.dumps(_d)))["matchs"]
    check("B22 doublon 'Damm' / 'Damm Jr.' retiré", len(_r) == 2, f"{len(_r)} matchs")
    check("B23 version la mieux documentée conservée",
          any("return_pts_won" in (m.get("stats_surface_j1") or {}) for m in _r))
    _d2 = {"matchs": [{"joueur1": "A Un", "joueur2": "B Deux",
                       "stats_surface_j1": {}, "stats_surface_j2": {}},
                      {"joueur1": "C Trois", "joueur2": "D Quatre",
                       "stats_surface_j1": {}, "stats_surface_j2": {}}]}
    check("B24 matchs distincts non fusionnés",
          len(json.loads(dedupliquer_matchs_json(json.dumps(_d2)))["matchs"]) == 2)

    # ---- B-SEXIES. CONFIRMATION PAR SOURCE OFFICIELLE (v8.4) ----
    _fx = [{"participants": {"participant1Name": "Humbert, Ugo", "participant2Name": "Martin, Andres"}}]
    _rp = [{"joueur1": "Kamil Majchrzak", "joueur2": "Tommy Paul"}]
    _dc = {"matchs": [
        {"joueur1": "Ugo Humbert", "joueur2": "Andres Martin"},
        {"joueur1": "Kamil Majchrzak", "joueur2": "Tommy Paul"},
        {"joueur1": "Aleksandar Vukic", "joueur2": "Zachary Svajda"},
    ]}
    _rc = json.loads(filtrer_json_matchs_non_confirmes(json.dumps(_dc), _fx, _rp))["matchs"]
    check("B25 match fantôme écarté (Vukic 28/07)", len(_rc) == 2, f"{len(_rc)} gardés")
    check("B26 confirmé par OddsPapi gardé", any(m["joueur1"] == "Ugo Humbert" for m in _rc))
    check("B27 confirmé par calendrier gardé", any(m["joueur1"] == "Kamil Majchrzak" for m in _rc))
    check("B28 mode relais : filtre désactivé si sources vides",
          len(json.loads(filtrer_json_matchs_non_confirmes(json.dumps(_dc), [], []))["matchs"]) == 3)

    # ---- B-SEPTIES. AUTO-RÉTRACTATION (v8.7) ----
    def _est_retracte(txt):
        tl = txt.lower()
        return any(r in tl for r in [
            "je retire ce ticket", "je retire donc ce ticket", "ce ticket est retiré",
            "ticket retiré", "ticket écarté", "j'écarte ce ticket", "je l'écarte",
            "n'est pas retenu", "ne doit pas être envoyé", "j'annule ce ticket",
            "ticket annulé", "(correction)", "→ skip", "-> skip",
            "filtre skip s'applique", "par cohérence avec le filtre"])
    _cas_reel = ("PRONO : Over 22.5 jeux\nCOTE : Référence Pinnacle 1.917\n"
                 "Note : l'absence de Shang dépasse 2 mois → filtre skip s'applique "
                 "normalement. Je retire ce ticket par cohérence avec le filtre absence.")
    check("B29 auto-rétractation détectée (cas Darderi 06/08)", _est_retracte(_cas_reel))
    check("B30 ticket normal non impacté",
          not _est_retracte("PRONO : Talia Gibson (Vainqueur)\nCOTE : 2.0\nMISE : 1%\n"
                            "POURQUOI ? Hold supérieur sur dur, value nette."))
    check("B31 mention 'correction' captée", _est_retracte("PRONOSTIC SIMPLE (CORRECTION)"))

    # ---- B-OCTIES. TICKET SANS VALUE (v8.8, cas Pegula 08/08) ----
    _pegula = ("PRONO : Jessica Pegula (Vainqueur)\nCOTE : 1.38\nMISE : 3%\n"
               "VALUE : 62.6% → juste 1.60 → réelle 1.38... delta insuffisant\n"
               "POURQUOI ? Le modèle donne 62.6% (cote juste 1.60), la cote réelle "
               "1.38 est en-dessous → aucun edge. Annulé.")
    _tl = _pegula.lower()
    check("B32 rétractation 'delta insuffisant' captée", "delta insuffisant" in _tl)
    check("B33 rétractation 'aucun edge' captée", "aucun edge" in _tl)
    check("B34 delta non chiffré → illisible",
          re.search(r"delta\s*([+-]?\d+[.,]\d+)", _tl) is None)
    _ok_delta = "value : 62.6% → juste 1.60 → réelle 2.10 → delta +0.50"
    check("B35 delta chiffré toujours lu",
          re.search(r"delta\s*([+-]?\d+[.,]\d+)", _ok_delta).group(1) == "+0.50")

    # ---- C. PARSEUR (bug '60% ou non trouvé' du 20/07) ----
    check("C1 '60.3% ou non trouvé'→0.603", abs(_bc_parse_pct("60.3% ou non trouvé") - 0.603) < 1e-9)
    check("C2 'non trouvé'→None", _bc_parse_pct("non trouvé") is None)
    check("C3 '64,2'→0.642", abs(_bc_parse_pct("64,2") - 0.642) < 1e-9)

    # ---- D. GARDE-FOU BC : matching de noms tolérant (bug Vallejo 17/07) ----
    def _sc(nom, texte):
        jet = [j for j in nom.split() if len(j) >= 3]
        if not jet: return 0
        return (10 if jet[-1] in texte else 0) + sum(1 for j in jet[:-1] if j in texte)
    check("D1 'vallejo' matche 'adolfo daniel vallejo'",
          _sc("adolfo daniel vallejo", "vallejo vainqueur") > 0)
    check("D2 prénom partagé discriminé",
          _sc("daniel elahi galan", "daniel elahi galan vainqueur")
          > _sc("adolfo daniel vallejo", "daniel elahi galan vainqueur"))
    # D3 : bug Avanesyan (21/07) — le joueur parié doit être identifié même quand
    # son nom de famille est le seul discriminant, sinon garde-fou aveugle.
    check("D3 joueur parié identifié (Avanesyan)",
          _sc("elina avanesyan", "elina avanesyan (vainqueur)")
          > _sc("arantxa rus", "elina avanesyan (vainqueur)"))

    # ---- E. CLAUSE DE CORROBORATION (dossier rejets validé le 20/07) ----
    def decide(pb, cote, dm, ref, BC=20.0):
        pm = 1 / cote; er = (pb - pm) / pm
        seuil = 0.10 if dm else 0.30
        if dm and ref is not None and abs(pb - ref) <= 0.08 and ref > pm:
            seuil = 0.30
        if er > seuil: return "REJET_MARCHE"
        if ref is not None and (pb - ref) * 100 > BC: return "REJET_BC"
        return "ACCEPTE"
    check("E1 Collignon (modèle corrobore) accepté",
          decide(0.71, 1.56, True, 0.708) == "ACCEPTE")
    check("E2 Altmaier (Claude délire) rejeté BC",
          decide(0.45, 2.50, False, 0.152) == "REJET_BC")
    check("E3 Sherif +71% rejeté malgré corrobo",
          decide(0.671, 2.55, True, 0.671) == "REJET_MARCHE")
    check("E4 survalorisation sans appui rejetée",
          decide(0.60, 2.50, True, 0.42) == "REJET_MARCHE")

    # ---- F. REPLI PINNACLE : inversion d'ordre (test du 20/07) ----
    check("F1 filtre source accepte cote Pinnacle marquée",
          _selftest_source_ok())

    # ---- G. PRÉ-FILTRE TOURNOIS AMONT (v7.9, constat couverture 21/07) ----
    _actifs = ["ATP Kitzbuhel", "WTA Prague"]
    check("G1 tournoi actif reconnu (nom commercial)",
          _tournoi_dans_liste("Generali Open - Kitzbuhel", _actifs) is True)
    check("G2 Challenger hors liste écarté",
          _tournoi_dans_liste("Tampere Challenger", _actifs) is False)
    _pf = prefiltrer_rapid_matchs_par_tournoi(
        [{"tournoi": "Generali Open - Kitzbuhel"}, {"tournoi": "Zug Challenger"}], _actifs)
    check("G3 pré-filtre ne garde que les jouables", len(_pf) == 1)
    check("G4 pré-filtre fail-open sur liste vide",
          len(prefiltrer_rapid_matchs_par_tournoi([{"tournoi": "X"}], [])) == 1)

    # ---- Rapport ----
    print(f"\n{'='*56}\n  HARNAIS DE TEST — bot.py v7.9\n{'='*56}")
    for o in oks:     print(f"  ✅ {o}")
    for e in echecs:  print(f"  ❌ {e}")
    print(f"{'-'*56}\n  {len(oks)} réussis · {len(echecs)} échoués")
    if echecs:
        print("  ⛔ NE PAS POUSSER — corriger les échecs d'abord.\n")
        return 1
    print("  ✅ TOUT VERT — sûr à pousser.\n")
    return 0


def _selftest_source_ok():
    # reproduit la logique de _source_winamax sans dépendre de sa portée locale
    def src_ok(m):
        if m.get("cote_pinnacle_repli"):
            return True
        return "winamax" in str(m.get("source_cote", "")).lower()
    return (src_ok({"cote_pinnacle_repli": True, "source_cote": "Pinnacle (repli)"}) is True
            and src_ok({"source_cote": "bet365"}) is False
            and src_ok({"source_cote": "Winamax (Coteur)"}) is True)



if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if not args:
        run_bot_autonome()
    elif args[0] == "test":
        sys.exit(_selftest())
    elif args[0] == "recap":
        envoyer_recap_hebdo()
    elif args[0] == "file":
        # Liste la file des paris en cours avec leurs numéros
        lister_file_paris()
    elif args[0] == "regler":
        # Règlement semi-auto : scores OddsPapi + confirmation manuelle v/d
        regler_paris_interactif()
    elif args[0] == "resultat" and len(args) in (2, 3):
        # resultat v          → victoire, stats globales seulement
        # resultat v 2        → victoire du pari n°2 de la file
        #                        (stats segmentées + retrait de la file)
        flag = args[1].lower()
        idx  = None
        if len(args) == 3:
            try:
                idx = int(args[2])
            except ValueError:
                print("❌ Le numéro de pari doit être un entier (cf. 'python bot.py file').")
                sys.exit(1)
        if flag in ("v", "victoire", "win", "1"):
            enregistrer_resultat(True, index_pari=idx)
        elif flag in ("d", "defaite", "lose", "0"):
            enregistrer_resultat(False, index_pari=idx)
        else:
            print("❌ Utilise 'v' ou 'd' [+ numéro du pari dans la file].")
            sys.exit(1)
    elif args[0] in ("--help", "-h"):
        print(__doc__)
    else:
        print(f"❌ Commande inconnue.")
        sys.exit(1)
