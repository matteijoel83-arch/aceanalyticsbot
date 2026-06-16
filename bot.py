"""
╔══════════════════════════════════════════════════════════════════════╗
║          BOT TENNIS ACEANALYTICS — bot.py v7.2 (MatchStat)          ║
║  Architecture hybride : Gemini (recherche) + Claude (analyse)        ║
║  Pré-collecte : Odds API + MatchStat Tennis → calendrier complet     ║
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
ODDS_API_KEY        = os.environ.get("ODDS_API_KEY")
RAPIDAPI_KEY        = os.environ.get("RAPIDAPI_KEY")

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

CLAUDE_MODEL  = "claude-sonnet-4-6"
GEMINI_MODEL  = "gemini-2.5-pro"
GITHUB_API    = "https://api.github.com"
GITHUB_HEADERS = {
    "Authorization":        f"Bearer {GITHUB_TOKEN}",
    "Accept":               "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

STATS_VERSION = 1
STATS_DEFAUT  = {"version": STATS_VERSION, "victoires": 0, "defaites": 0}
TICKET_SEP    = "[SEPARATEUR]"
MAX_TICKETS   = 3

# [Les fonctions _gh_get, _gh_put, _gh_delete, _migrer_stats, charger_stats, calculer_winrate, enregistrer_resultat, _hash_ticket, charger_historique, sauvegarder_historique, _nettoyer_html_telegram, _tronquer, envoyer_sur_telegram, _envoyer_notification_sans_ticket, _alerter_telegram_erreur, sauvegarder_pari_pour_suivi, precollecte_odds_api RESTENT INCHANGÉES]

# =====================================================================
# 8. MODULE B — PRÉ-COLLECTE MATCHSTAT (RAPIDAPI)
# =====================================================================

def precollecte_rapidapi_tennis(date_fr):
    matchs = []
    if not RAPIDAPI_KEY:
        logging.info("RAPIDAPI_KEY absente — pré-collecte RapidAPI ignorée.")
        return matchs
    
    date_api = datetime.strptime(date_fr, "%d/%m/%Y").strftime("%Y-%m-%d")
    headers  = {
        "x-rapidapi-key":  RAPIDAPI_KEY,
        "x-rapidapi-host": "tennis-api-atp-wta-itf.p.rapidapi.com"
    }
    
    try:
        url = f"https://tennis-api-atp-wta-itf.p.rapidapi.com/tennis/v2/schedule/{date_api}"
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        
        # Adaptation à la structure MatchStat
        for m in data.get("result", []):
            matchs.append({
                "joueur1": m.get("home_player", {}).get("name", "Inconnu"),
                "joueur2": m.get("away_player", {}).get("name", "Inconnu"),
                "heure": m.get("start_time", "00:00"),
                "tournoi": m.get("tournament", {}).get("name", "ATP/WTA"),
                "surface": m.get("surface", "Non précisé")
            })
    except Exception as e:
        logging.error(f"Erreur MatchStat API : {e}")
        
    logging.info(f"MatchStat Tennis — {len(matchs)} match(s) récupérés.")
    return matchs

# [La suite du script (9 à 13) RESTE INCHANGÉE]
