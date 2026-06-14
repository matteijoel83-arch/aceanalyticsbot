"""
â•”â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•—
â•‘          BOT TENNIS ACEANALYTICS â€” v5.0                             â•‘
â•‘  Auteur  : AceAnalytics                                              â•‘
â•‘  ModÃ¨le  : Claude Sonnet 4.6 (Anthropic) + web_search tool         â•‘
â•‘            + The Odds API (cotes temps rÃ©el)                        â•‘
â•‘                                                                      â•‘
â•‘  Variables d'environnement requises :                                â•‘
â•‘    ANTHROPIC_API_KEY    â†’ ClÃ© API Anthropic (claude.ai)             â•‘
â•‘    TELEGRAM_BOT_TOKEN   â†’ Token du bot Telegram                     â•‘
â•‘    TELEGRAM_CHANNEL_ID  â†’ ID du canal Telegram (ex: @moncanal)      â•‘
â•‘    GITHUB_TOKEN         â†’ Personal Access Token (scope: repo)       â•‘
â•‘    GITHUB_REPO          â†’ "username/nom-du-repo"                    â•‘
â•‘                                                                      â•‘
â•‘  Variable optionnelle :                                              â•‘
â•‘    ODDS_API_KEY         â†’ The Odds API (cotes temps rÃ©el)           â•‘
â•‘                           Gratuit : https://the-odds-api.com        â•‘
â•‘                                                                      â•‘
â•‘  Usage :                                                             â•‘
â•‘    python bot_tennis.py              â†’ Analyse normale               â•‘
â•‘    python bot_tennis.py --dry-run    â†’ Simulation (pas d'envoi)     â•‘
â•‘    python bot_tennis.py resultat v   â†’ Enregistrer une victoire      â•‘
â•‘    python bot_tennis.py resultat d   â†’ Enregistrer une dÃ©faite       â•‘
â•šâ•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
"""

import os
import sys
import json
import hashlib
import logging
import re
import time
import base64
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from logging.handlers import RotatingFileHandler
import anthropic

# =====================================================================
# âš™ï¸  1. CONFIGURATION & LOGGING
# =====================================================================

DRY_RUN = "--dry-run" in sys.argv   # Mode simulation : aucun envoi rÃ©el

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        # Rotation automatique : 3 fichiers de 5 Mo max
        RotatingFileHandler("bot.log", maxBytes=5 * 1024 * 1024, backupCount=3),
        logging.StreamHandler(),
    ],
)

# --- Variables d'environnement ---
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
GITHUB_TOKEN        = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO         = os.environ.get("GITHUB_REPO")

MISSING = [k for k, v in {
    "ANTHROPIC_API_KEY":   ANTHROPIC_API_KEY,
    "TELEGRAM_BOT_TOKEN":  TELEGRAM_BOT_TOKEN,
    "TELEGRAM_CHANNEL_ID": TELEGRAM_CHANNEL_ID,
    "GITHUB_TOKEN":        GITHUB_TOKEN,
    "GITHUB_REPO":         GITHUB_REPO,
}.items() if not v]

if MISSING:
    logging.critical(f"ClÃ©s secrÃ¨tes manquantes : {', '.join(MISSING)}")
    sys.exit(1)

# Client Anthropic â€” le SDK gÃ¨re automatiquement ANTHROPIC_API_KEY si prÃ©sente
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ModÃ¨le utilisÃ©
CLAUDE_MODEL = "claude-sonnet-4-6"

GITHUB_API     = "https://api.github.com"
GITHUB_HEADERS = {
    "Authorization":        f"Bearer {GITHUB_TOKEN}",
    "Accept":               "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# SchÃ©ma attendu pour stats.json â€” versionnÃ© pour migrations futures
STATS_VERSION  = 1
STATS_DEFAUT   = {"version": STATS_VERSION, "victoires": 0, "defaites": 0}

# DÃ©limiteur de tickets â€” moins ambigu que ===
TICKET_SEP     = "[SEPARATEUR]"
MAX_TICKETS    = 3

# =====================================================================
# ðŸ—„ï¸  2. COUCHE D'ACCÃˆS GITHUB (atomique, sans subprocess Git)
# =====================================================================

def _github_get_file(path: str) -> tuple[dict | list | None, str | None]:
    """
    RÃ©cupÃ¨re un fichier JSON depuis GitHub.
    Retourne (contenu_parsÃ©, sha) ou (None, None) si absent ou erreur.
    """
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    try:
        r = requests.get(url, headers=GITHUB_HEADERS, timeout=10)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        data   = r.json()
        sha    = data["sha"]
        # GitHub encode le contenu en base64 avec des sauts de ligne â€” on les retire
        contenu = json.loads(base64.b64decode(data["content"].replace("\n", "")).decode())
        return contenu, sha
    except Exception as e:
        logging.error(f"GitHub GET '{path}' Ã©chouÃ© : {e}")
        return None, None


def _github_put_file(
    path: str,
    contenu: dict | list,
    message: str,
    sha: str | None = None,
    retries: int = 2,
) -> bool:
    """
    CrÃ©e ou met Ã  jour un fichier JSON sur GitHub de faÃ§on atomique.
    GÃ¨re le conflit 409 (SHA pÃ©rimÃ©) avec un re-fetch automatique.
    """
    url     = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    encoded = base64.b64encode(
        json.dumps(contenu, ensure_ascii=False, indent=2).encode()
    ).decode()
    payload = {"message": message, "content": encoded}
    if sha:
        payload["sha"] = sha

    for tentative in range(1, retries + 1):
        try:
            r = requests.put(url, headers=GITHUB_HEADERS, json=payload, timeout=15)

            # 409 Conflict : SHA pÃ©rimÃ© â†’ on re-fetch et on retente
            if r.status_code == 409 and tentative < retries:
                logging.warning(f"GitHub 409 sur '{path}' â€” re-fetch du SHA et nouvelle tentative.")
                _, sha_frais = _github_get_file(path)
                if sha_frais:
                    payload["sha"] = sha_frais
                time.sleep(1)
                continue

            r.raise_for_status()
            logging.info(f"GitHub : '{path}' mis Ã  jour â€” {message}")
            return True

        except Exception as e:
            logging.error(f"GitHub PUT '{path}' (tentative {tentative}/{retries}) Ã©chouÃ© : {e}")
            if tentative < retries:
                time.sleep(2)

    return False


def _github_delete_file(path: str, message: str, sha: str) -> bool:
    """Supprime un fichier sur GitHub."""
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    try:
        r = requests.delete(
            url, headers=GITHUB_HEADERS,
            json={"message": message, "sha": sha}, timeout=10
        )
        r.raise_for_status()
        logging.info(f"GitHub : '{path}' supprimÃ©.")
        return True
    except Exception as e:
        logging.error(f"GitHub DELETE '{path}' Ã©chouÃ© : {e}")
        return False

# =====================================================================
# ðŸ“Š  3. MODULE STATISTIQUES
# =====================================================================

def _migrer_stats(stats: dict) -> dict:
    """Migration automatique si le schÃ©ma Ã©volue dans les futures versions."""
    version = stats.get("version", 0)
    if version < 1:
        # v0 â†’ v1 : ajout du champ version
        stats["version"] = STATS_VERSION
    return stats


def charger_stats() -> dict:
    stats, _ = _github_get_file("stats.json")
    # Validation de la structure â€” corrompu ou format inattendu â†’ dÃ©faut
    if not isinstance(stats, dict) or "victoires" not in stats or "defaites" not in stats:
        logging.warning("stats.json absent ou invalide â€” rÃ©initialisation aux valeurs par dÃ©faut.")
        return dict(STATS_DEFAUT)
    return _migrer_stats(stats)


def calculer_winrate(stats: dict) -> float:
    total = stats["victoires"] + stats["defaites"]
    return (stats["victoires"] / total * 100) if total > 0 else 0.0


def enregistrer_resultat(victoire: bool, pari_termine: str | None = None):
    """
    Met Ã  jour les stats et retire proprement le pari terminÃ©.
    Toutes les Ã©critures sont atomiques via l'API GitHub.
    """
    # --- Mise Ã  jour stats ---
    stats, stats_sha = _github_get_file("stats.json")
    if not isinstance(stats, dict) or "victoires" not in stats:
        stats = dict(STATS_DEFAUT)
    stats["victoires" if victoire else "defaites"] += 1
    stats = _migrer_stats(stats)

    if DRY_RUN:
        logging.info(f"[DRY-RUN] Stats simulÃ©es : {stats}")
    else:
        _github_put_file("stats.json", stats, "ðŸ”„ Maj stats", sha=stats_sha)

    # --- Nettoyage chirurgical du pari terminÃ© ---
    if pari_termine and not DRY_RUN:
        paris, paris_sha = _github_get_file("pari_en_cours.json")
        if isinstance(paris, list):
            paris_restants = [p for p in paris if p.get("pari") != pari_termine]
            if paris_restants:
                _github_put_file(
                    "pari_en_cours.json", paris_restants,
                    "ðŸ§¹ Nettoyage partiel des paris", sha=paris_sha
                )
            elif paris_sha:
                _github_delete_file(
                    "pari_en_cours.json",
                    "ðŸ—‘ï¸ Suppression pari_en_cours (liste vide)", sha=paris_sha
                )

    resultat_str = "âœ… VICTOIRE" if victoire else "âŒ DÃ‰FAITE"
    logging.info(f"RÃ©sultat enregistrÃ© : {resultat_str} â€” Bilan : {stats['victoires']}V / {stats['defaites']}D")

# =====================================================================
# ðŸ”  4. DÃ‰DUPLICATION PAR HASH SHA-256
# =====================================================================

def _hash_ticket(ticket: str) -> str:
    """SHA-256 des 300 premiers caractÃ¨res normalisÃ©s â€” insensible aux espaces."""
    normalise = re.sub(r"\s+", " ", ticket.strip().lower())[:300]
    return hashlib.sha256(normalise.encode()).hexdigest()


def charger_historique() -> tuple[list, str | None]:
    """Charge l'historique des hashes depuis GitHub. Retourne (liste, sha)."""
    historique, sha = _github_get_file("historique.json")
    if not isinstance(historique, list):
        return [], sha
    return historique, sha


def sauvegarder_historique(hashes: list, sha: str | None):
    """Persiste la liste de hashes (20 derniers max) sur GitHub en un seul appel."""
    _github_put_file(
        "historique.json", hashes[-20:],
        "ðŸ“š Mise Ã  jour historique", sha=sha
    )

# =====================================================================
# ðŸ“¬  5. MODULE TELEGRAM â€” RETRY + BACKOFF EXPONENTIEL
# =====================================================================

def _tronquer_proprement(texte: str, limite: int = 3500) -> str:
    """
    Tronque sur le dernier saut de ligne avant la limite.
    Si le texte est monolithique (pas de \\n), tronque Ã  la limite brute.
    """
    if len(texte) <= limite:
        return texte
    coupe = texte.rfind("\n", 0, limite)
    coupe = coupe if coupe != -1 else limite
    return texte[:coupe] + "\n\nâ€¦ [Analyse tronquÃ©e pour raison de longueur]"


def envoyer_sur_telegram(message: str, retries: int = 3) -> bool:
    stats = charger_stats()
    wr    = calculer_winrate(stats)

    signature = (
        f"\n\nðŸ“Š <b>BILAN ACEANALYTICS</b>\n"
        f"âœ… V: {stats['victoires']} | âŒ D: {stats['defaites']}\n"
        f"ðŸ“ˆ <b>Win Rate : {wr:.1f}%</b>"
    )

    texte_html = message + signature

    if len(texte_html) > 4000:
        logging.warning("Message trop long â€” troncature propre (suppression balises HTML).")
        # On retire toutes les balises HTML avant de tronquer
        # â†’ Ã©vite les balises orphelines qui feraient rejeter le message par Telegram
        texte_brut = re.sub(r"<[^>]+>", "", message)
        texte_html = _tronquer_proprement(texte_brut, 3500) + signature
        parse_mode = None   # Pas de HTML si le message a Ã©tÃ© dÃ©pouillÃ©
    else:
        parse_mode = "HTML"

    if DRY_RUN:
        logging.info(f"[DRY-RUN] Message Telegram simulÃ© ({len(texte_html)} chars) :\n{texte_html}")
        return True

    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHANNEL_ID, "text": texte_html}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    for tentative in range(1, retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=10)

            if r.status_code == 429:
                retry_after = r.json().get("parameters", {}).get("retry_after", 5)
                logging.warning(f"Telegram rate-limit. Attente {retry_after}s (tentative {tentative}/{retries}).")
                time.sleep(retry_after)
                continue

            r.raise_for_status()
            logging.info("âœ… Message envoyÃ© avec succÃ¨s sur Telegram.")
            return True

        except requests.exceptions.Timeout:
            logging.warning(f"Telegram timeout (tentative {tentative}/{retries}).")
        except requests.exceptions.HTTPError as e:
            logging.error(f"Telegram HTTP error : {e} â€” {r.text}")
            break   # Erreur 4xx permanente : inutile de retenter
        except Exception as e:
            logging.error(f"Erreur inattendue Telegram : {e}")

        if tentative < retries:
            attente = 2 ** tentative   # Backoff exponentiel : 2s puis 4s
            logging.info(f"Nouvelle tentative Telegram dans {attente}sâ€¦")
            time.sleep(attente)

    logging.error("âŒ Ã‰chec dÃ©finitif Telegram aprÃ¨s tous les retries â€” message perdu.")
    return False

# =====================================================================
# ðŸ’¾  6. GESTION DES PARIS EN COURS
# =====================================================================

def sauvegarder_pari_pour_suivi(pari_info: dict):
    """Ajoute le pari Ã  la liste persistante sans Ã©craser les paris en cours."""
    if "pari" not in pari_info or "date" not in pari_info:
        logging.error(f"Structure pari_info invalide, sauvegarde ignorÃ©e : {pari_info}")
        return

    if DRY_RUN:
        logging.info(f"[DRY-RUN] Pari simulÃ© (non sauvegardÃ©) : {pari_info['date']}")
        return

    paris, sha = _github_get_file("pari_en_cours.json")
    if not isinstance(paris, list):
        paris = []
    paris.append(pari_info)
    _github_put_file("pari_en_cours.json", paris, "ðŸ“Œ Ajout nouveau pari", sha=sha)

# =====================================================================
# ðŸ’±  7. MODULE COTES TEMPS RÃ‰EL (The Odds API)
# =====================================================================
# Variable d'environnement optionnelle : ODDS_API_KEY
# Gratuit jusqu'Ã  500 req/mois â€” https://the-odds-api.com
# Si absente, le bot fonctionne sans injection de cotes (dÃ©gradÃ©).

ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/tennis/odds"


def recuperer_cotes_tennis() -> str:
    """
    RÃ©cupÃ¨re les cotes Moneyline tennis en temps rÃ©el via The Odds API.
    Retourne un bloc texte formatÃ© Ã  injecter dans le prompt,
    ou une chaÃ®ne vide si la clÃ© est absente ou l'appel Ã©choue.
    """
    if not ODDS_API_KEY:
        logging.info("ODDS_API_KEY absente â€” injection de cotes dÃ©sactivÃ©e (mode dÃ©gradÃ©).")
        return ""

    params = {
        "apiKey":   ODDS_API_KEY,
        "regions":  "eu",           # Cotes europÃ©ennes (incluant Winamax)
        "markets":  "h2h",          # MarchÃ© Moneyline (Head-to-Head)
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }

    try:
        r = requests.get(ODDS_API_URL, params=params, timeout=10)
        r.raise_for_status()
        matchs = r.json()

        if not matchs:
            return ""

        lignes = ["ðŸ“‹ COTES TEMPS RÃ‰EL (source : The Odds API / bookmakers EU) :"]
        for m in matchs[:20]:   # On limite Ã  20 matchs pour ne pas saturer le prompt
            heure = m.get("commence_time", "")[:16].replace("T", " ")
            j1    = m.get("home_team", "?")
            j2    = m.get("away_team", "?")

            # On cherche le bookmaker Winamax en prioritÃ©, sinon le premier disponible
            bookmakers = m.get("bookmakers", [])
            cote_j1, cote_j2 = None, None

            for bk in bookmakers:
                if "winamax" in bk.get("key", "").lower() or not cote_j1:
                    for market in bk.get("markets", []):
                        if market.get("key") == "h2h":
                            outcomes = {o["name"]: o["price"] for o in market.get("outcomes", [])}
                            cote_j1  = outcomes.get(j1)
                            cote_j2  = outcomes.get(j2)
                    if "winamax" in bk.get("key", "").lower():
                        break   # Winamax trouvÃ©, on arrÃªte

            if cote_j1 and cote_j2:
                lignes.append(f"  â€¢ {heure} UTC | {j1} ({cote_j1:.2f}) vs {j2} ({cote_j2:.2f})")

        quota = r.headers.get("x-requests-remaining", "?")
        logging.info(f"Cotes rÃ©cupÃ©rÃ©es : {len(matchs)} matchs. Quota restant : {quota} req.")
        return "\n".join(lignes)

    except Exception as e:
        logging.warning(f"The Odds API indisponible : {e} â€” analyse sans injection de cotes.")
        return ""


# =====================================================================
# ðŸ§   8. PROMPT SYSTÃˆME â€” v4
# =====================================================================

def construire_prompt_systeme(date_du_jour: str, heure_actuelle: str, cotes_injectees: str = "") -> str:

    bloc_cotes = ""
    if cotes_injectees:
        bloc_cotes = f"""
COTES CERTIFIÃ‰ES INJECTÃ‰ES (source externe fiable â€” NE PAS rechercher de cotes ailleurs) :
{cotes_injectees}

â†’ Ces cotes sont les valeurs de rÃ©fÃ©rence officielles pour ton calcul de value.
â†’ Si un match n'apparaÃ®t pas dans cette liste, tu peux chercher sa cote via Google Search.
â†’ Ne jamais inventer une cote absente de cette liste ou des rÃ©sultats de recherche.
"""
    else:
        bloc_cotes = """
AVERTISSEMENT COTES : Aucune source de cotes certifiÃ©e n'est disponible ce cycle.
â†’ Recherche les cotes via Google Search (Winamax, Sportytrader).
â†’ Si tu ne trouves pas une cote avec certitude, indique "cote non vÃ©rifiÃ©e" dans le ticket
  et rÃ©duis automatiquement la mise Ã  0.5% quelle que soit la confiance.
â†’ Ne jamais inventer une cote.
"""

    return f"""
Tu es l'assistant personnel d'un parieur expert en tennis. Analyse les matchs ATP/WTA du {date_du_jour}.
Note : Il est actuellement {heure_actuelle} (heure locale France).

â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
RÃˆGLE DES SESSIONS
â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
- AVANT 14:00 â†’ SESSION MATIN  (mi-journÃ©e et dÃ©but d'aprÃ¨s-midi).
- APRÃˆS 14:00 â†’ SESSION APRÃˆS-MIDI  (fin d'aprÃ¨s-midi, soirÃ©e, nuit).
- Maximum {MAX_TICKETS} tickets par session. Pas de ticket si aucune value rÃ©elle.

â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
RÃˆGLE DE SÃ‰PARATION MULTI-TICKETS
â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
- InsÃ¨re EXACTEMENT ce dÃ©limiteur sur une ligne isolÃ©e entre chaque ticket : {TICKET_SEP}
- N'utilise JAMAIS ce dÃ©limiteur ailleurs dans ton texte.

â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
RÃˆGLE DE CORRÃ‰LATION TEMPORELLE
â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
- Si un match a dÃ©jÃ  commencÃ© ou est terminÃ© Ã  {heure_actuelle} â†’ EXCLUS-LE immÃ©diatement.

â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
SOURCES DE RECHERCHE (classÃ©es par fiabilitÃ©)
â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
TIER 1 â€” Calendriers (trÃ¨s bien indexÃ©s, fiables) :
  â†’ https://www.atptour.com/  |  https://www.wtatennis.com/  |  https://www.flashscore.fr/

TIER 2 â€” Stats rÃ©centes (partiellement indexÃ©s) :
  â†’ https://www.sofascore.com/fr/  |  https://www.flashscore.fr/
  IMPORTANT : Les stats fines (Hold %, % balles de break) ne sont pas toujours disponibles
  via Google Search. Si une statistique n'est pas trouvÃ©e avec certitude dans les rÃ©sultats,
  tu dois l'OMETTRE de ton analyse plutÃ´t que l'estimer ou l'inventer.
  Une analyse incomplÃ¨te mais honnÃªte vaut mieux qu'une analyse complÃ¨te mais partiellement fausse.

TIER 3 â€” Cotes (voir bloc ci-dessous) :
{bloc_cotes}

â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
PROTOCOLE D'ANALYSE EN 2 Ã‰TAPES (anti-biais de confirmation)
â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
Pour chaque match candidat, tu dois impÃ©rativement respecter cet ordre :

Ã‰TAPE 1 â€” ANALYSE BRUTE (sans Ã©mettre de pronostic) :
  Identifie et liste les facteurs POUR et CONTRE chaque joueur sur les 5 axes suivants.
  Ne tire aucune conclusion Ã  ce stade.

  1. SURFACE & CONDITIONS : Bilan historique sur la surface (Terre/Dur/Gazon), indoor/outdoor,
     altitude. Si le Court Pace Index (CPI) du tournoi est disponible, indique-le.
     Sinon, omet-le â€” ne l'invente pas.

  2. FORME & CHARGE PHYSIQUE : Dynamique sur les 10 derniers matchs. Heures de jeu dans
     les 72h prÃ©cÃ©dentes, longs trajets, dÃ©calage horaire, contrecoup d'un titre la semaine passÃ©e.

  3. CONTEXTE & PSYCHOLOGIE : Points Ã  dÃ©fendre au classement, motivation rÃ©elle,
     Grand Chelem dans les 7 jours suivants (vigilance gestion d'effort maximale).

  4. STATS AVANCÃ‰ES : % de Hold et % de balles de break converties/sauvÃ©es â€” UNIQUEMENT
     si trouvÃ©s dans les rÃ©sultats de recherche. Si Hold % > 83% des deux cÃ´tÃ©s avec
     historique de Tie-breaks confirmÃ© â†’ privilÃ©gier les marchÃ©s de jeux (Over/Under).

  5. H2H & TACTIQUE : Historique direct sous angle stylistique (serveur puissant vs rameur,
     contre-attaquant vs attaquant). VÃ©rifier si l'un des joueurs est gaucher et chercher
     le bilan spÃ©cifique de son adversaire face aux gauchers.

Ã‰TAPE 2 â€” PROBABILITÃ‰ & DÃ‰CISION (uniquement aprÃ¨s avoir listÃ© tous les facteurs) :
  Sur la base EXCLUSIVE des facteurs listÃ©s Ã  l'Ã‰tape 1 :
  â†’ Assigne une probabilitÃ© en % Ã  ton pronostic.
  â†’ Applique le PROTOCOLE DE CALCUL DE LA VALUE ci-dessous.
  â†’ Tire ta conclusion.

â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
PROTOCOLE DE CALCUL DE LA VALUE
â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
1. ProbabilitÃ© estimÃ©e (issue de l'Ã‰tape 2) â†’ ex: 67%
2. Cote Juste = 1 / (ProbabilitÃ© / 100) â†’ ex: 1 / 0.67 = 1.49
3. Calcul Kelly partiel pour la mise :
   Kelly = (ProbabilitÃ© Ã— Cote - 1) / (Cote - 1)
   Mise appliquÃ©e = Kelly Ã— 0.25  (Kelly quart â€” conservateur)
   â†’ Arrondie Ã  0.5% prÃ¨s, plafonnÃ©e Ã  2% (simple) ou 1% (combinÃ©).
4. Comparaison avec la cote rÃ©elle :
   - Cote rÃ©elle > Cote Juste + 0.10 â†’ VALUE confirmÃ©e âœ… â†’ Ticket validÃ©.
   - Cote rÃ©elle â‰¤ Cote Juste + 0.10 â†’ Pas de value âŒ â†’ Match abandonnÃ©.
- Si aucun match ne passe ce filtre â†’ rÃ©ponds STRICTEMENT : AUCUN_MATCH

â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
ORIENTATION DES MARCHÃ‰S SELON LE SCÃ‰NARIO
â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
- CONFIANCE Ã‰LEVÃ‰E  : Moneyline si value nette. Sinon, Handicap Jeux Favori ou Victoire 2-0.
- CONFIANCE MODÃ‰RÃ‰E : Moneyline INTERDIT. MarchÃ©s alternatifs uniquement :
  * Match serrÃ© (Holds Ã©levÃ©s confirmÃ©s) â†’ Plus de 21.5/22.5 jeux. WTA : Plus de 2.5 sets.
  * Match Ã  sens unique                  â†’ Moins de 20.5/19.5 jeux ou 2-0 Score Exact.
  * Favori prenable / Outsider fragile   â†’ Handicap Jeux Positif outsider (+4.5 jeux).

RÃˆGLE RETRAIT EN COURS DE MATCH :
- Si un joueur prÃ©sente des signaux physiques rÃ©cents (soins mÃ©dicaux visibles lors du
  dernier match, crampes, dÃ©clarations publiques de douleur dans les 48h) :
  â†’ INTERDIT de jouer un marchÃ© de jeux (Over/Under, Score Exact, Handicap de jeux).
  â†’ Moneyline uniquement si confiance Ã‰LEVÃ‰E, ou passe ton chemin.
  â†’ Raison : un retrait invalide ou fait perdre ces marchÃ©s selon les rÃ¨gles bookmaker.

Tous les marchÃ©s alternatifs doivent passer le PROTOCOLE DE CALCUL DE LA VALUE.

â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
STRATÃ‰GIE DE MISE â€” MATRICE BANKROLL
â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
La mise finale est le MIN entre le Kelly quart calculÃ© et ce plafond :
- Simple   + Ã‰LEVÃ‰E   â†’ plafond 2%
- Simple   + MODÃ‰RÃ‰E  â†’ plafond 1%
- CombinÃ©  + Ã‰LEVÃ‰E   â†’ plafond 1%
- CombinÃ©  + MODÃ‰RÃ‰E  â†’ INTERDIT. N'Ã©mets pas ce ticket.
- Cote non vÃ©rifiÃ©e (toute confiance) â†’ plafond 0.5% sans exception.

â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
FILTRE BLESSURE & DISPONIBILITÃ‰ (2 niveaux)
â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
NIVEAU 1 â€” Ã‰LIMINATION TOTALE (ne pas analyser ce match) :
  - Retour de blessure aprÃ¨s absence > 2 mois.
  - Doute public sur la participation dans les 48h (dÃ©claration joueur ou staff).

NIVEAU 2 â€” VIGILANCE RENFORCÃ‰E (analyse autorisÃ©e, contraintes strictes) :
  - Soins mÃ©dicaux visibles lors du dernier match.
  - Match de plus de 3h jouÃ© dans les 24h prÃ©cÃ©dentes.
  - Retour aprÃ¨s absence entre 3 et 8 semaines.
  â†’ Dans ce cas : marchÃ©s alternatifs UNIQUEMENT + mise plafonnÃ©e Ã  0.5%.

â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
RÃˆGLE ABSOLUE ANTI-HALLUCINATION
â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
- Si aucun match rÃ©el et Ã  venir n'est trouvÃ© â†’ rÃ©ponds STRICTEMENT : AUCUN_MATCH
- Aucune rencontre fictive ni extrapolation depuis tes connaissances passÃ©es.
- Toute statistique non trouvÃ©e dans les rÃ©sultats de recherche â†’ omise, jamais estimÃ©e.

â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
FORMAT DE CHAQUE TICKET (respecter exactement)
â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”
ðŸ”´ <b>PRONOSTIC [SIMPLE OU COMBINÃ‰]</b> ðŸ”´
ðŸŸ <b>MATCHS :</b> [Joueur A vs Joueur B]
ðŸ† <b>COMPÃ‰TITION :</b> [Tournoi]
â° <b>HEURE :</b> [Heure exacte du match]
âœ… <b>PRONO :</b> [Pronostic prÃ©cis]
ðŸ“ˆ <b>COTE :</b> [Cote rÃ©elle â€” indiquer "non vÃ©rifiÃ©e" si incertaine]
ðŸ’° <b>MISE :</b> [% Kelly quart, plafonnÃ© selon matrice]
ðŸ›¡ <b>CONFIANCE :</b> [Ã‰LEVÃ‰E / MODÃ‰RÃ‰E]
ðŸ§® <b>VALUE :</b> [Proba X% â†’ Cote juste Y.YY â†’ Cote rÃ©elle Z.ZZ â†’ Kelly quart W%]
ðŸ“Œ <b>POURQUOI ?</b> [Max 150 mots â€” facteurs clÃ©s uniquement, pas de transition]
âš ï¸ <b>DONNÃ‰ES MANQUANTES :</b> [Liste des stats non trouvÃ©es, ou "Aucune" si analyse complÃ¨te]
"""

# =====================================================================
# ðŸš€  8. CÅ’UR DU BOT â€” ORCHESTRATION PRINCIPALE
# =====================================================================

def run_bot_autonome():
    debut          = time.time()
    maintenant     = datetime.now(ZoneInfo("Europe/Paris"))
    date_du_jour   = maintenant.strftime("%d/%m/%Y")
    heure_actuelle = maintenant.strftime("%H:%M")

    if DRY_RUN:
        logging.info("=" * 60)
        logging.info("MODE DRY-RUN ACTIVÃ‰ â€” aucun envoi rÃ©el ne sera effectuÃ©.")
        logging.info("=" * 60)

    # Injection des cotes temps rÃ©el avant l'appel Claude
    cotes_injectees = recuperer_cotes_tennis()
    prompt_systeme  = construire_prompt_systeme(date_du_jour, heure_actuelle, cotes_injectees)

    try:
        logging.info(f"Lancement de l'analyse Claude ({CLAUDE_MODEL}) pour le {date_du_jour} Ã  {heure_actuelle}â€¦")

        # ----------------------------------------------------------------
        # Appel API Anthropic avec l'outil web_search natif (beta)
        # Le SDK gÃ¨re automatiquement la boucle outil â†’ rÃ©ponse â†’ outil
        # jusqu'Ã  ce que Claude produise sa rÃ©ponse finale.
        # ----------------------------------------------------------------
        reponse = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=prompt_systeme,
            tools=[
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Effectue l'analyse complÃ¨te et propose les meilleurs paris "
                        f"(max {MAX_TICKETS}) pour la session du {date_du_jour}. "
                        f"Il est actuellement {heure_actuelle} heure de France. "
                        f"Commence par rechercher les matchs ATP/WTA du jour avant d'analyser."
                    ),
                }
            ],
        )

        # Extraction du texte final â€” Claude peut retourner plusieurs blocs
        # (text + tool_use + tool_result) ; on ne garde que les blocs texte
        texte = "\n".join(
            bloc.text
            for bloc in reponse.content
            if hasattr(bloc, "text") and bloc.text
        ).strip()

        logging.info(f"RÃ©ponse Claude reÃ§ue ({len(texte)} caractÃ¨res). Tokens : {reponse.usage.input_tokens} in / {reponse.usage.output_tokens} out.")

        # --- Sorties propres sans ticket ---
        if "AUCUN_MATCH" in texte:
            logging.info("Session annulÃ©e proprement : aucun match rÃ©el disponible (AUCUN_MATCH).")
            return

        if len(texte) <= 20:
            logging.info("RÃ©ponse Claude trop courte ou vide â€” aucun ticket Ã©mis.")
            return

        # --- DÃ©coupage des tickets sur le dÃ©limiteur explicite ---
        tickets_bruts = [t.strip() for t in texte.split(TICKET_SEP) if len(t.strip()) > 20]

        # SÃ©curitÃ© stricte : jamais plus de MAX_TICKETS, quoi qu'il arrive
        tickets = tickets_bruts[:MAX_TICKETS]

        if len(tickets_bruts) > MAX_TICKETS:
            logging.warning(
                f"Claude a gÃ©nÃ©rÃ© {len(tickets_bruts)} tickets â€” troncature Ã  {MAX_TICKETS} appliquÃ©e."
            )

        if not tickets:
            logging.warning("RÃ©ponse Claude non vide mais aucun ticket valide extrait.")
            return

        # --- DÃ©duplication : chargement unique de l'historique ---
        historique, historique_sha = charger_historique()
        hashes_connus = set(historique)
        nouveaux_hashes = []

        paris_envoyes = 0
        for i, ticket in enumerate(tickets, 1):
            h = _hash_ticket(ticket)

            if h in hashes_connus:
                logging.warning(f"Ticket {i}/{len(tickets)} : doublon dÃ©tectÃ© (hash), ignorÃ©.")
                continue

            logging.info(f"Envoi du ticket {i}/{len(tickets)}â€¦")
            succes = envoyer_sur_telegram(ticket)

            if succes:
                sauvegarder_pari_pour_suivi({"pari": ticket, "date": date_du_jour})
                hashes_connus.add(h)
                nouveaux_hashes.append(h)
                paris_envoyes += 1

                # Petite pause entre tickets pour ne pas saturer Telegram
                if i < len(tickets):
                    time.sleep(1)

        # --- Persistance de l'historique en un seul appel GitHub ---
        if nouveaux_hashes and not DRY_RUN:
            sauvegarder_historique(list(hashes_connus), historique_sha)

        if paris_envoyes:
            logging.info(f"âœ… {paris_envoyes} ticket(s) envoyÃ©(s) et sauvegardÃ©(s) avec succÃ¨s.")
        else:
            logging.warning("Aucun ticket envoyÃ© (tous doublons ou erreurs Telegram).")

    except Exception as e:
        logging.error(f"Erreur critique lors de l'exÃ©cution du bot : {e}", exc_info=True)

    finally:
        duree = time.time() - debut
        logging.info(f"ExÃ©cution terminÃ©e en {duree:.1f}s.")

# =====================================================================
# ðŸ–¥ï¸  9. POINT D'ENTRÃ‰E CLI
# =====================================================================

def afficher_aide():
    print(__doc__)


if __name__ == "__main__":
    args = sys.argv[1:]

    # Filtrage du flag --dry-run pour ne pas le confondre avec des commandes
    args_filtres = [a for a in args if a != "--dry-run"]

    if not args_filtres:
        # Lancement normal (ou dry-run)
        run_bot_autonome()

    elif args_filtres[0] == "resultat" and len(args_filtres) == 2:
        # Enregistrement manuel d'un rÃ©sultat
        # Usage : python bot_tennis.py resultat v
        #         python bot_tennis.py resultat d
        flag = args_filtres[1].lower()
        if flag in ("v", "victoire", "win", "1"):
            enregistrer_resultat(victoire=True)
        elif flag in ("d", "defaite", "lose", "0"):
            enregistrer_resultat(victoire=False)
        else:
            print(f"âŒ Argument inconnu : '{flag}'. Utilise 'v' (victoire) ou 'd' (dÃ©faite).")
            sys.exit(1)

    elif args_filtres[0] in ("--help", "-h", "help"):
        afficher_aide()

    else:
        print(f"âŒ Commande inconnue : {' '.join(args_filtres)}")
        afficher_aide()
        sys.exit(1)
