"""
verification_resultats.py â€” v2.0
VÃ©rifie automatiquement les rÃ©sultats des paris en cours via Claude + web_search.
Notifie Telegram du rÃ©sultat (gagnÃ©/perdu) et met Ã  jour stats + file d'attente via API GitHub.
"""

import os, sys, json, logging, base64, time, requests
import anthropic
from logging.handlers import RotatingFileHandler

# =====================================================================
# 1. CONFIGURATION & LOGGING
# =====================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler("verification.log", maxBytes=2 * 1024 * 1024, backupCount=2),
        logging.StreamHandler(),
    ],
)

ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY")
GITHUB_TOKEN        = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO         = os.environ.get("GITHUB_REPO")
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")

MISSING = [k for k, v in {
    "ANTHROPIC_API_KEY":   ANTHROPIC_API_KEY,
    "GITHUB_TOKEN":        GITHUB_TOKEN,
    "GITHUB_REPO":         GITHUB_REPO,
    "TELEGRAM_BOT_TOKEN":  TELEGRAM_BOT_TOKEN,
    "TELEGRAM_CHANNEL_ID": TELEGRAM_CHANNEL_ID,
}.items() if not v]

if MISSING:
    logging.critical(f"Secrets manquants : {', '.join(MISSING)}")
    sys.exit(1)

client       = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
CLAUDE_MODEL = "claude-sonnet-4-6"

GITHUB_API     = "https://api.github.com"
GITHUB_HEADERS = {
    "Authorization":        f"Bearer {GITHUB_TOKEN}",
    "Accept":               "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

STATS_DEFAUT = {"version": 1, "victoires": 0, "defaites": 0}

# =====================================================================
# 2. COUCHE GITHUB
# =====================================================================

def _gh_get(path: str) -> tuple:
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


def _gh_put(path: str, contenu, message: str, sha=None, retries: int = 2) -> bool:
    url     = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(json.dumps(contenu, ensure_ascii=False, indent=2).encode()).decode(),
    }
    if sha:
        payload["sha"] = sha
    for t in range(1, retries + 1):
        try:
            r = requests.put(url, headers=GITHUB_HEADERS, json=payload, timeout=15)
            if r.status_code == 409 and t < retries:
                logging.warning(f"GitHub 409 '{path}' â€” re-fetch SHA.")
                _, sha_frais = _gh_get(path)
                if sha_frais:
                    payload["sha"] = sha_frais
                time.sleep(1)
                continue
            r.raise_for_status()
            logging.info(f"GitHub '{path}' OK â€” {message}")
            return True
        except Exception as e:
            logging.error(f"GitHub PUT '{path}' tentative {t} : {e}")
            if t < retries:
                time.sleep(2)
    return False


def _gh_delete(path: str, message: str, sha: str) -> bool:
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    try:
        r = requests.delete(url, headers=GITHUB_HEADERS,
                            json={"message": message, "sha": sha}, timeout=10)
        r.raise_for_status()
        logging.info(f"GitHub '{path}' supprimÃ©.")
        return True
    except Exception as e:
        logging.error(f"GitHub DELETE '{path}' : {e}")
        return False

# =====================================================================
# 3. TELEGRAM
# =====================================================================

def _envoyer_telegram(texte: str, parse_mode: str = "HTML") -> bool:
    """Envoi simple avec 2 tentatives â€” utilisÃ© pour les notifications de rÃ©sultat."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHANNEL_ID, "text": texte}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    for t in range(1, 3):
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
                continue
            r.raise_for_status()
            return True
        except Exception as e:
            logging.warning(f"Telegram tentative {t} : {e}")
            time.sleep(2)
    logging.error("Telegram : Ã©chec envoi notification rÃ©sultat.")
    return False


def notifier_resultat(statut: str, pari_texte: str, stats: dict):
    """Envoie une notification Telegram formatÃ©e du rÃ©sultat."""
    total = stats["victoires"] + stats["defaites"]
    wr    = (stats["victoires"] / total * 100) if total > 0 else 0.0

    if statut == "GAGNE":
        header = "ðŸ† <b>PRONOSTIC VALIDÃ‰ !</b> âœ…"
        emoji  = "âœ…"
    else:
        header = "âŒ <b>PRONOSTIC PERDU</b>"
        emoji  = "âŒ"

    # On extrait la premiÃ¨re ligne du ticket comme rÃ©sumÃ©
    resume = pari_texte.strip().split("\n")[0][:200]

    message = (
        f"{header}\n\n"
        f"ðŸ“Œ {resume}\n\n"
        f"ðŸ“Š <b>BILAN MIS Ã€ JOUR</b>\n"
        f"{emoji} V: {stats['victoires']} | âŒ D: {stats['defaites']}\n"
        f"ðŸ“ˆ <b>Win Rate : {wr:.1f}%</b>"
    )
    _envoyer_telegram(message)

# =====================================================================
# 4. VÃ‰RIFICATION VIA CLAUDE + WEB_SEARCH
# =====================================================================

INSTRUCTIONS_VERIFICATION = """
Tu es un agent expert de vÃ©rification de scores de tennis.
DÃ©termine le rÃ©sultat RÃ‰EL du pronostic fourni.

Utilise web_search pour chercher les rÃ©sultats officiels sur :
Flashscore, Sofascore, ATP Tour, WTA Tennis.

RÃ©ponds STRICTEMENT par un seul de ces 3 mots, sans aucun autre texte :
- GAGNE  : matchs terminÃ©s ET pronostic correct.
- PERDU  : matchs terminÃ©s ET pronostic incorrect.
- EN_COURS : match non commencÃ©, en cours, ou rÃ©sultat introuvable/incomplet.

RÃˆGLE D'OR : moindre doute = EN_COURS. Ne devine jamais.
"""


def interroger_claude_statut(pari_texte: str) -> str:
    try:
        reponse = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=256,
            system=INSTRUCTIONS_VERIFICATION,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{
                "role": "user",
                "content": f"VÃ©rifie le rÃ©sultat rÃ©el pour ce ticket :\n\n{pari_texte}",
            }],
        )
        verdict = "\n".join(
            b.text for b in reponse.content if hasattr(b, "text") and b.text
        ).strip().upper()
        logging.info(f"Verdict Claude brut : '{verdict}'")
        if "GAGNE" in verdict:
            return "GAGNE"
        if "PERDU" in verdict:
            return "PERDU"
        return "EN_COURS"
    except Exception as e:
        logging.error(f"Erreur Claude : {e}")
        return "EN_COURS"  # SÃ©curitÃ© : on ne supprime rien en cas d'erreur

# =====================================================================
# 5. LOGIQUE PRINCIPALE
# =====================================================================

def main():
    debut = time.time()

    # --- Chargement paris en cours ---
    paris, paris_sha = _gh_get("pari_en_cours.json")
    if not isinstance(paris, list):
        paris = [paris] if paris else []
    if not paris:
        logging.info("Aucun pari en cours â€” rien Ã  vÃ©rifier.")
        return

    # --- Chargement stats ---
    stats, stats_sha = _gh_get("stats.json")
    if not isinstance(stats, dict) or "victoires" not in stats:
        stats     = dict(STATS_DEFAUT)
        stats_sha = None

    logging.info(f"VÃ©rification de {len(paris)} pari(s)â€¦")

    restants       = []
    stats_modifiees = False

    for i, item in enumerate(paris, 1):
        pari_texte = item.get("pari", "")
        if not pari_texte:
            continue

        logging.info(f"â”€â”€â”€ Ticket {i}/{len(paris)} â”€â”€â”€")
        statut = interroger_claude_statut(pari_texte)
        logging.info(f"Verdict : {statut}")

        if statut == "GAGNE":
            stats["victoires"] += 1
            stats_modifiees = True
            logging.info("ðŸ† Victoire enregistrÃ©e.")
            notifier_resultat("GAGNE", pari_texte, stats)
        elif statut == "PERDU":
            stats["defaites"] += 1
            stats_modifiees = True
            logging.info("âŒ DÃ©faite enregistrÃ©e.")
            notifier_resultat("PERDU", pari_texte, stats)
        else:
            restants.append(item)
            logging.info("â³ En cours â€” maintenu dans la file.")

        # Pause entre vÃ©rifications pour Ã©viter les rate limits
        if i < len(paris):
            time.sleep(2)

    # --- Persistance stats ---
    if stats_modifiees:
        _gh_put("stats.json", stats, "ðŸ”„ MAJ stats aprÃ¨s vÃ©rification", sha=stats_sha)

    # --- Persistance file d'attente ---
    if len(restants) != len(paris):
        if restants:
            _gh_put("pari_en_cours.json", restants,
                    "ðŸ§¹ Nettoyage file aprÃ¨s rÃ©sultats", sha=paris_sha)
        elif paris_sha:
            _gh_delete("pari_en_cours.json", "ðŸ—‘ï¸ File vide â€” suppression", sha=paris_sha)
        logging.info(f"File mise Ã  jour : {len(restants)} pari(s) restant(s).")
    else:
        logging.info("Aucun changement â€” tous les matchs encore en cours.")

    logging.info(f"VÃ©rification terminÃ©e en {time.time() - debut:.1f}s.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"Erreur critique verification_resultats.py : {e}", exc_info=True)
        # Alerte Telegram en cas de plantage total
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHANNEL_ID,
                      "text": f"âš ï¸ verification_resultats.py a plantÃ© :\n{e}"},
                timeout=5,
            )
        except Exception:
            pass
        sys.exit(1)
