"""
verification_resultats.py — v2.1
Vérifie automatiquement les résultats des paris en cours via Claude + web_search.
Notifie Telegram du résultat (gagné/perdu) et met à jour stats + file d'attente via API GitHub.

v2.1 (13/07/2026) — alignement sur bot.py v7.4 :
  • YIELD : lit les champs 'cote' et 'mise_pct' des paris (écrits par bot.py)
    et accumule profit/mises dans stats.json (global 'roi' + par segment).
    Sans cela, le suivi de rentabilité construit dans bot.py restait à zéro.
  • Notification Telegram enrichie du yield global.
  • _migrer_stats complète aussi 'par_modele' (manquait en v2.0).

v2.2 (13/07/2026) — protection du temps d'exécution :
  • Constat : job GitHub Actions tué à 15m14s. Chaque vérification = un appel
    Claude+web_search (~40-60s) ; les paris EN_COURS qui traînent sont
    revérifiés à CHAQUE run → durée non bornée.
  • MAX_VERIFS_PAR_RUN : plafond de vérifications par run (le reste attend
    le run suivant, 3 occasions/jour).
  • AGE_MAX_JOURS : un pari trop vieux est retiré de la file avec notification
    Telegram "à régler manuellement" — SANS toucher aux stats (pas de V/D
    deviné). Évite de payer Sonnet 3×/jour pour un match introuvable à vie.
"""

import os, sys, json, logging, base64, time, re, requests
from datetime import datetime
from zoneinfo import ZoneInfo
import anthropic
from logging.handlers import RotatingFileHandler

# v2.7 : Gemini comme SECONDE source de vérification (consensus avec Claude).
# Import tolérant : si la lib n'est pas installée, on retombe sur Claude seul.
try:
    from google import genai
    from google.genai import types
    _GEMINI_DISPO = True
except Exception:
    _GEMINI_DISPO = False

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
CLAUDE_MODEL = "claude-sonnet-4-6"  # Sonnet pour fiabilité — vérification critique

# v2.7 : client Gemini (2e source). GEMINI_API_KEY optionnel — sans lui,
# le consensus se dégrade proprement en "Claude seul".
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
gemini_client = None
if _GEMINI_DISPO and GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        logging.warning(f"Gemini indisponible pour la vérification : {e}")
GEMINI_MODEL = "gemini-3.5-flash"


def _maintenant_paris():
    """v2.4 : heure de PARIS, pas UTC — le runner GitHub Actions est en UTC,
    or toutes les dates des paris (JJ/MM/AAAA) sont en heure française.
    Sans ça : contrôle 'jour même' raté entre minuit et 2h Paris, et
    expiration à 3 jours décalée. bot.py utilise déjà ZoneInfo partout."""
    return datetime.now(ZoneInfo("Europe/Paris"))

# v2.2 — bornes de temps d'exécution
MAX_VERIFS_PAR_RUN = 8   # ~8 × 60s max ≈ 8 min — le reste attend le run suivant
AGE_MAX_JOURS      = 3   # au-delà : retrait de la file + notification (règlement manuel)

GITHUB_API     = "https://api.github.com"
GITHUB_HEADERS = {
    "Authorization":        f"Bearer {GITHUB_TOKEN}",
    "Accept":               "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

STATS_DEFAUT = {
    "version":   2,
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


def _normaliser_surface(surface_brute):
    """Même logique que bot.py v7.5 — segment de mesure, aucune décision d'analyse."""
    s = str(surface_brute or "").lower()
    if "clay" in s or "terre" in s:
        return "terre"
    if "grass" in s or "gazon" in s:
        return "gazon"
    if "hard" in s or "dur" in s:
        return "dur"
    return "autre"


def _migrer_stats(s):
    if s.get("version", 0) < 2:
        s["version"] = 2
        if "par_marche" not in s:
            s["par_marche"] = dict(STATS_DEFAUT["par_marche"])
        if "par_niveau" not in s:
            s["par_niveau"] = dict(STATS_DEFAUT["par_niveau"])
    # v2.1 : par_modele pouvait manquer sur d'anciens stats.json
    if "par_modele" not in s:
        s["par_modele"] = dict(STATS_DEFAUT["par_modele"])
    # v2.2 : par_surface (aligné sur bot.py v7.5)
    if "par_surface" not in s:
        s["par_surface"] = dict(STATS_DEFAUT["par_surface"])
    return s


def _maj_stats_detail(stats, victoire, marche, niveau, modele="autre",
                      mise=None, profit=None, surface="autre"):
    """
    Met à jour les stats par marché, par niveau, par modèle et (v2.2) par surface.
    v2.1 : accumule aussi mise/profit par segment si fournis (calcul de yield,
    même logique que bot.py v7.4 — enregistrer_resultat).
    """
    cle = "v" if victoire else "d"
    # Par marché
    if "par_marche" not in stats:
        stats["par_marche"] = dict(STATS_DEFAUT["par_marche"])
    marche_cle = marche if marche in stats["par_marche"] else "autre"
    stats["par_marche"][marche_cle][cle] += 1
    # Par niveau
    if "par_niveau" not in stats:
        stats["par_niveau"] = dict(STATS_DEFAUT["par_niveau"])
    niveau_cle = niveau if niveau in stats["par_niveau"] else "autre"
    if niveau_cle in stats["par_niveau"]:
        stats["par_niveau"][niveau_cle][cle] += 1
    # Par modèle (opus / sonnet)
    if "par_modele" not in stats:
        stats["par_modele"] = dict(STATS_DEFAUT["par_modele"])
    if modele in stats["par_modele"]:
        stats["par_modele"][modele][cle] += 1
    # v2.2 : Par surface (terre/dur/gazon/autre)
    if "par_surface" not in stats:
        stats["par_surface"] = dict(STATS_DEFAUT["par_surface"])
    surface_cle = surface if surface in stats["par_surface"] else "autre"
    stats["par_surface"][surface_cle][cle] += 1
    # v2.1 : YIELD par segment (mise et profit en unités de % de bankroll)
    if mise is not None and profit is not None:
        for section, cle_seg in [("par_marche", marche_cle),
                                 ("par_niveau", niveau_cle),
                                 ("par_modele", modele),
                                 ("par_surface", surface_cle)]:
            seg = stats.get(section, {}).get(cle_seg)
            if isinstance(seg, dict):
                seg["mise"]   = round(seg.get("mise", 0.0) + mise, 4)
                seg["profit"] = round(seg.get("profit", 0.0) + profit, 4)
    return stats


def _maj_roi_global(stats, mise, profit):
    """v2.1 : accumule le ROI global (même structure que bot.py v7.4)."""
    roi = stats.setdefault("roi", {"mise_totale": 0.0, "profit": 0.0})
    roi["mise_totale"] = round(roi.get("mise_totale", 0.0) + mise, 4)
    roi["profit"]      = round(roi.get("profit", 0.0) + profit, 4)
    return stats

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
                logging.warning(f"GitHub 409 '{path}' — re-fetch SHA.")
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


def _gh_delete(path: str, message: str, sha: str) -> bool:
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
# 3. TELEGRAM
# =====================================================================

def _envoyer_telegram(texte: str, parse_mode: str = "HTML") -> bool:
    """Envoi simple avec 2 tentatives — utilisé pour les notifications de résultat."""
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
    logging.error("Telegram : échec envoi notification résultat.")
    return False


def notifier_resultat(statut: str, pari_texte: str, stats: dict):
    """Envoie une notification Telegram formatée du résultat."""
    total = stats["victoires"] + stats["defaites"]
    wr    = (stats["victoires"] / total * 100) if total > 0 else 0.0

    if statut == "GAGNE":
        header = "🎾 🏆 <b>ACEANALYTICS TENNIS — PRONOSTIC VALIDÉ !</b> ✅"
        emoji  = "✅"
    else:
        header = "🎾 ❌ <b>ACEANALYTICS TENNIS — PRONOSTIC PERDU</b>"
        emoji  = "❌"

    resume = next(
        (re.sub(r"<[^>]+>", "", l).strip() for l in pari_texte.splitlines() if "MATCH" in l.upper()),
        pari_texte.strip().split("\n")[0]
    )[:200]

    # v2.1 : ligne de yield global si des paris avec cote/mise ont été réglés
    ligne_yield = ""
    roi = stats.get("roi", {})
    if roi.get("mise_totale"):
        y = roi["profit"] / roi["mise_totale"] * 100
        ligne_yield = f"\n💶 Profit : {roi['profit']:+.2f}u | Yield : {y:+.1f}%"

    message = (
        f"{header}\n\n"
        f"📌 {resume}\n\n"
        f"📊 <b>BILAN ACEANALYTICS 🎾 TENNIS</b>\n"
        f"{emoji} V: {stats['victoires']} | ❌ D: {stats['defaites']}\n"
        f"📈 <b>Win Rate : {wr:.1f}%</b>{ligne_yield}"
    )
    _envoyer_telegram(message)

# =====================================================================
# 4. VÉRIFICATION VIA CLAUDE + WEB_SEARCH
# =====================================================================

INSTRUCTIONS_VERIFICATION = (
    "Tu es un vérificateur de résultats de paris sportifs. "
    "Cherche le résultat via web_search sur Flashscore, Sofascore ou BBC Sport. "
    "\n\nRÈGLE ABSOLUE : Tu dois vérifier le PRONOSTIC EXACT, pas seulement le vainqueur."
    "\n- Si le prono est 'Victoire Moneyline' → vérifier qui a gagné le match"
    "\n- Si le prono est 'Score 2-1' → vérifier le score exact des sets"
    "\n- Si le prono est 'Over X jeux' → vérifier le nombre total de jeux"
    "\n- Si le prono est 'BTTS' → vérifier si les deux équipes ont marqué"
    "\n- Si le prono est 'Over 2.5 buts' → vérifier le nombre de buts"
    "\n- Si le prono est un combiné → TOUS les pronostics doivent être validés"
    "\n\n⛔ RÈGLE DE FIN DE MATCH — LA PLUS IMPORTANTE :"
    "\nUn match n'est TERMINÉ que si tu as une PREUVE EXPLICITE :"
    "\n  • mention 'Terminé' / 'Finished' / 'Final' / 'FT' / score final officiel"
    "\n  • un score complet de tennis (ex: 6-3 6-4, soit 2 sets gagnés par un joueur)"
    "\nSi tu vois un score PARTIEL (1 set joué, match en cours, 'Live', 'en direct',"
    "\nset en cours, ou AUCUNE mention claire de fin) → c'est EN_COURS, JAMAIS PERDU."
    "\nNe conclus JAMAIS PERDU sur la base d'un score intermédiaire où le joueur est mené."
    "\nUn joueur mené 6-3 4-3 peut encore gagner : ce match est EN_COURS, pas PERDU."
    "\nSi tu ne trouves PAS le score final explicite → EN_COURS."
    "\n\n⛔ AVANT TOUT VERDICT GAGNE OU PERDU — CITATION OBLIGATOIRE :"
    "\nTu DOIS d'abord écrire une ligne 'PREUVE DE FIN: ...' citant la preuve exacte"
    "\ntrouvée (ex: 'PREUVE DE FIN: Flashscore affiche Terminé, score final 4-6 6-3 6-4')."
    "\nSi tu ne peux PAS citer une preuve de fin explicite (mot Terminé/FT/Final + score"
    "\ncomplet) → tu écris 'PREUVE DE FIN: aucune' et le verdict est OBLIGATOIREMENT EN_COURS."
    "\nUn verdict GAGNE ou PERDU SANS ligne 'PREUVE DE FIN' valide est INTERDIT."
    "\n\n⚠️ VÉRIFICATION DU BON MATCH :"
    "\nAttention aux homonymes. Vérifie que le tournoi, la date et l'adversaire correspondent"
    "\nEXACTEMENT au ticket. Un autre joueur du même nom dans un autre tournoi ≠ ton match."
    "\nSi tu n'es pas certain que c'est le bon match → EN_COURS."
    "\n\nPROCÉDURE :"
    "\n1. Cherche le score FINAL du match (avec preuve de fin : Terminé/FT/score complet)"
    "\n2. Si pas de preuve de fin claire → EN_COURS immédiatement"
    "\n3. Si match terminé : compare avec le pronostic exact indiqué"
    "\n\n⚠️⚠️ FORMAT DE RÉPONSE — RÈGLE ABSOLUE, VIOLATION = ÉCHEC ⚠️⚠️"
    "\nTa réponse doit être COURTE et se terminer OBLIGATOIREMENT par DEUX lignes,"
    "\nchacune seule sur sa ligne, dans CET ORDRE EXACT :"
    "\nPREUVE DE FIN: [score final cité, ex: Flashscore Terminé 6-4 6-2] ou 'aucune'"
    "\nVERDICT: GAGNE   (ou PERDU, ou EN_COURS, ou ANNULE si forfait/abandon)"
    "\n\n⛔ INTERDICTIONS :"
    "\n- NE PAS faire de tableaux markdown, d'analyse longue, d'emojis décoratifs."
    "\n- NE PAS écrire 'RÉSULTAT', 'ANALYSE' ou autre en guise de conclusion."
    "\n- La SEULE conclusion valide = la ligne 'VERDICT: X'. Sans elle = ÉCHEC total."
    "\n- Réponds en 3-4 lignes maximum : preuve trouvée + les 2 lignes finales."
    "\n\nEXEMPLE DE RÉPONSE CORRECTE (à imiter EXACTEMENT) :"
    "\nMatch trouvé sur Flashscore, terminé. Ruse bat Navarro 6-4 6-2."
    "\nLe pronostic était Victoire Navarro → non validé."
    "\nPREUVE DE FIN: Flashscore affiche Terminé, score final 6-4 6-2"
    "\nVERDICT: PERDU"
    "\n\n⛔ FORFAIT / ABANDON — VERDICT: ANNULE (règle du 17/07/2026) :"
    "\nUn match gagné par FORFAIT (walkover, w.o., 'forfait', joueur déclaré forfait"
    "\navant le match, AUCUN jeu joué) n'est PAS une victoire : le bookmaker rembourse"
    "\nla mise. → VERDICT: ANNULE"
    "\nUn match interrompu par ABANDON (retirement, 'ab.', joueur qui se retire en"
    "\ncours de match) → VERDICT: ANNULE également (les conditions de remboursement"
    "\ndépendent du set en cours et du type de tournoi — un humain tranchera)."
    "\nMatch annulé, reporté, ou interrompu définitivement → VERDICT: ANNULE"
    "\n⚠️ Ne JAMAIS conclure GAGNE sur un forfait ou un abandon, même si ton joueur"
    "\nest déclaré vainqueur : l'argent n'est pas gagné pour autant."
    "\n\nRÈGLES STRICTES :"
    "\n- Match pas encore terminé OU score partiel OU doute → VERDICT: EN_COURS"
    "\n- Match terminé (preuve explicite) + pronostic validé → VERDICT: GAGNE"
    "\n- Match terminé (preuve explicite) + pronostic non validé → VERDICT: PERDU"
    "\n- Le moindre doute sur la fin du match → VERDICT: EN_COURS"
    "\n\n⚠️ RAPPEL FINAL : quoi que tu écrives avant, tu DOIS terminer par la ligne"
    "\n'VERDICT: GAGNE/PERDU/EN_COURS/ANNULE'. C'est la SEULE chose que le système lit."
)


def _extraire_resume_ticket(pari_texte: str) -> str:
    """
    Extrait les infos nécessaires à la vérification :
    match, tournoi, heure ET pronostic exact.
    """
    champs_utiles = ("MATCH", "COMPÉTITION", "HEURE", "PRONO", "COTE")
    lignes = []
    for ligne in pari_texte.splitlines():
        ligne_clean = ligne.strip()
        if any(c in ligne_clean.upper() for c in champs_utiles):
            ligne_clean = re.sub(r"<[^>]+>", "", ligne_clean)
            lignes.append(ligne_clean)
    return "\n".join(lignes) if lignes else "\n".join(pari_texte.splitlines()[:5])


def interroger_claude_statut(pari_texte: str, date_pari: str = "") -> str:
    # On n'envoie que le résumé minimal — pas le ticket complet
    resume = _extraire_resume_ticket(pari_texte)
    contexte_date = f"\nDate du match : {date_pari}" if date_pari else ""
    try:
        reponse = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            system=INSTRUCTIONS_VERIFICATION,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{
                "role": "user",
                "content": f"Résultat de ce pari ?{contexte_date}\n{resume}",
            }],
        )
        verdict = "\n".join(
            b.text for b in reponse.content if hasattr(b, "text") and b.text
        ).strip().upper()
        logging.info(f"Verdict brut : '{verdict[-200:]}'")

        # Parser UNIQUEMENT la ligne 'VERDICT:' (évite les faux positifs
        # quand 'PERDU'/'GAGNE' apparaît dans le raisonnement de Claude)
        m = re.search(r"VERDICT\s*:\s*(GAGNE|PERDU|EN_COURS|ANNULE)", verdict)
        if m:
            resultat = m.group(1)
            if resultat == "ANNULE":
                logging.info("Verdict extrait : ANNULE (forfait/abandon) — aucun impact stats.")
                return "ANNULE"

            # Protection anti-faux-positif : un verdict GAGNE/PERDU exige une
            # ligne 'PREUVE DE FIN' qui n'est PAS 'aucune'. Sinon → EN_COURS.
            if resultat in ("GAGNE", "PERDU"):
                preuve = re.search(r"PREUVE DE FIN\s*:\s*(.+)", verdict)
                preuve_txt = preuve.group(1).strip() if preuve else ""
                preuve_valide = bool(preuve_txt) and "AUCUNE" not in preuve_txt[:20]
                # La preuve doit mentionner un indicateur de fin réel
                indices_fin = any(mot in preuve_txt for mot in
                                  ["TERMIN", "FINISHED", "FINAL", "FT", "6-", "7-", "7/", "6/"])
                if not (preuve_valide and indices_fin):
                    logging.warning(
                        f"Verdict {resultat} SANS preuve de fin valide "
                        f"('{preuve_txt[:60]}') → forcé EN_COURS par sécurité."
                    )
                    return "EN_COURS"

            logging.info(f"Verdict extrait : {resultat}")
            return resultat

        # Pas de ligne VERDICT trouvée → sécurité maximale : EN_COURS
        # (on ne devine PAS à partir du texte libre, pour ne pas clôturer à tort)
        logging.warning(
            "Aucune ligne VERDICT trouvée → EN_COURS par sécurité. "
            "Causes possibles : réponse coupée (max_tokens) ou format ignoré par Claude. "
            f"Fin de réponse : '...{verdict[-120:]}'"
        )
        return "EN_COURS"
    except Exception as e:
        logging.error(f"Erreur Claude : {e}")
        return "EN_COURS"

# =====================================================================
# 4ter. VÉRIFICATION VIA GEMINI + google_search (2e source, v2.7)
# =====================================================================
# Constat des 15/07 (Altmaier) et 18/07 (Collignon) : Claude+web_search a
# HALLUCINÉ un score final plausible mais faux, en citant "ATP Tour officiel".
# Le garde-fou PREUVE DE FIN ne protège pas contre une citation inventée.
# Parade : demander le MÊME résultat à Gemini (google_search RÉEL) et n'accepter
# un verdict que si les DEUX sources sont d'accord. Sur désaccord → EN_COURS
# (règlement manuel), jamais un score deviné.

def interroger_gemini_statut(pari_texte: str, date_pari: str = "") -> str:
    """2e source de vérification. Retourne GAGNE/PERDU/EN_COURS/ANNULE.
    EN_COURS par défaut si Gemini indisponible ou réponse ambiguë."""
    if gemini_client is None:
        return "INDISPONIBLE"
    resume = _extraire_resume_ticket(pari_texte)
    contexte_date = f"\nDate du match : {date_pari}" if date_pari else ""
    prompt = (
        "Tu vérifies le résultat RÉEL d'un pari tennis. Cherche le score FINAL "
        "officiel via google_search (Flashscore, Sofascore, ATP/WTA, BBC Sport).\n"
        f"{contexte_date}\n{resume}\n\n"
        "RÈGLES :\n"
        "- Match pas terminé, score partiel, ou doute → EN_COURS\n"
        "- Forfait (walkover, aucun jeu joué) ou abandon → ANNULE\n"
        "- Vérifie le PRONOSTIC EXACT (le bon joueur, le bon marché)\n"
        "- Attention aux homonymes : bon tournoi, bonne date, bon adversaire\n\n"
        "Réponds en 2 lignes maximum, en terminant OBLIGATOIREMENT par :\n"
        "PREUVE: [score final cité] ou aucune\n"
        "VERDICT: GAGNE (ou PERDU, ou EN_COURS, ou ANNULE)"
    )
    try:
        rep = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.0,
            ),
        )
        texte = (rep.text or "").strip().upper()
        logging.info(f"Verdict Gemini brut : '{texte[-160:]}'")
        m = re.search(r"VERDICT\s*:\s*(GAGNE|PERDU|EN_COURS|ANNULE)", texte)
        if not m:
            return "EN_COURS"
        resultat = m.group(1)
        # Même exigence de preuve que pour Claude sur GAGNE/PERDU
        if resultat in ("GAGNE", "PERDU"):
            preuve = re.search(r"PREUVE\s*:\s*(.+)", texte)
            ptxt = preuve.group(1).strip() if preuve else ""
            if not ptxt or "AUCUNE" in ptxt[:20] or not any(
                    x in ptxt for x in ["6-", "7-", "6/", "7/", "TERMIN", "FINAL", "FINISHED"]):
                return "EN_COURS"
        return resultat
    except Exception as e:
        logging.warning(f"Gemini vérification échouée : {e}")
        return "INDISPONIBLE"


def verdict_par_consensus(pari_texte: str, date_pari: str = "") -> str:
    """
    v2.7 — Règle un pari par CONSENSUS de deux sources indépendantes quand
    OddsPapi est muet. Claude+web_search ET Gemini+google_search doivent être
    D'ACCORD pour clôturer un pari en GAGNE/PERDU. Toute divergence → EN_COURS.
    """
    v_claude = interroger_claude_statut(pari_texte, date_pari)
    v_gemini = interroger_gemini_statut(pari_texte, date_pari)
    logging.info(f"Consensus — Claude: {v_claude} | Gemini: {v_gemini}")

    # Gemini indisponible (pas de clé/lib) → on retombe sur Claude seul, MAIS
    # uniquement pour EN_COURS/ANNULE (sûrs). Un GAGNE/PERDU de Claude SEUL
    # reste autorisé par rétrocompatibilité — c'est le comportement d'avant v2.7,
    # avec le risque connu. Si tu veux le durcir, mets GEMINI_API_KEY en secret.
    if v_gemini == "INDISPONIBLE":
        logging.info("Gemini indisponible → Claude seul (comportement d'avant v2.7).")
        return v_claude

    # ANNULE prime dès qu'une source le détecte (forfait = fait objectif)
    if "ANNULE" in (v_claude, v_gemini):
        return "ANNULE"

    # Accord parfait GAGNE/PERDU → on clôture
    if v_claude == v_gemini and v_claude in ("GAGNE", "PERDU"):
        return v_claude

    # Tout le reste (désaccord, l'un EN_COURS, GAGNE vs PERDU...) → prudence
    if v_claude != v_gemini and {v_claude, v_gemini} & {"GAGNE", "PERDU"}:
        logging.warning(
            f"⚠️ DÉSACCORD Claude ({v_claude}) vs Gemini ({v_gemini}) → EN_COURS. "
            f"C'est exactement le cas qui a corrompu les stats les 15 et 18/07 : "
            f"on NE tranche PAS, un humain règlera à la main."
        )
    return "EN_COURS"


# =====================================================================
# 4bis. VÉRIFICATION DÉTERMINISTE VIA ODDSPAPI (v2.3, priorité sur Claude)
# =====================================================================
# Constat du 15/07/2026 : Claude+web_search a annoncé "GAGNE" en citant un
# score INVENTÉ ("Altmaier bat Darderi 6-2 7-5" sourcé "ATP Tour officiel")
# alors que Darderi avait réellement gagné 6-4 6-4 (confirmé Winamax/app).
# La garde-fou "PREUVE DE FIN" ne protège pas contre une citation qui SONNE
# vraie mais est fausse — c'est une limite structurelle du web_search LLM.
#
# Pour les paris MONEYLINE réglés le jour même, on utilise à la place le
# score OddsPapi — la même source de données que la commande 'regler' de
# bot.py, donc déterministe et déjà validée en production. Claude+web_search
# ne sert plus alors que de FILET DE SECOURS quand OddsPapi n'a pas le match
# (autre jour, tournoi non couvert) ou pour les marchés non-Moneyline
# (score exact, écart de jeux...) que ce contrôle ne couvre pas encore.

ODDSPAPI_HOST = "odds-api1.p.rapidapi.com"
RAPIDAPI_KEY  = os.environ.get("RAPIDAPI_KEY")  # optionnel — repli Claude si absent


def _sim_noms_verif(a, b):
    """Similarité de noms tolérante à l'ordre/virgules (même logique que bot.py)."""
    from difflib import SequenceMatcher
    def norm(x):
        x = str(x).lower().replace(",", " ").replace("-", " ")
        return " ".join(sorted(w for w in x.split() if w))
    na, nb = norm(a), norm(b)
    score = SequenceMatcher(None, na, nb).ratio()
    mots_a, mots_b = set(na.split()), set(nb.split())
    communs = mots_a & mots_b
    if communs:
        # v2.7/v7.6.5 : diviser par le nom le plus COURT — 'Tabilo' est un
        # sous-ensemble parfait de 'Tabilo, Alejandro' (score 1.0), alors que
        # l'ancien /max donnait 0.5 et faisait échouer l'appariement dès que
        # le ticket abrégeait les noms (constat du 17/07 : règlement
        # déterministe désactivé sur le ticket Tabilo/Tirante).
        score = max(score, len(communs) / min(len(mots_a), len(mots_b)))
    return score


def _quota_oddspapi_compter(n=1):
    """v2.4 : incrémente oddspapi_mois dans quota_rapidapi.json — les requêtes
    de CE script étaient invisibles du compteur (~90/mois non comptées).
    Écriture directe simple : ce script tourne AVANT bot.py dans le même job
    GitHub Actions (séquentiel), donc pas de conflit d'écriture possible."""
    try:
        mois_actuel = _maintenant_paris().strftime("%Y-%m")
        data, sha = _gh_get("quota_rapidapi.json")
        if not isinstance(data, dict) or data.get("mois") != mois_actuel:
            data = {"mois": mois_actuel, "tennisapi_mois": 0, "oddspapi_mois": 0,
                    "jour": _maintenant_paris().strftime("%Y-%m-%d"), "tennisapi_jour": 0}
        data["oddspapi_mois"] = data.get("oddspapi_mois", 0) + n
        _gh_put("quota_rapidapi.json", data, "📊 Quota OddsPapi (vérification)", sha=sha)
    except Exception as e:
        logging.warning(f"Comptage quota vérification échoué (non bloquant) : {e}")


def _oddspapi_fixtures_today_verif():
    """Fixtures du jour (mêmes règles que bot.py : SRL exclu). [] si erreur."""
    if not RAPIDAPI_KEY:
        return []
    # v2.5 : réessai sur 429 (même correctif que bot.py v7.6.2 — limite de débit
    # RapidAPI, pas de quota). Enjeu ici : si ce fetch échoue, le règlement
    # retombe sur Claude+web_search, donc sur le risque d'hallucination du 15/07.
    data = None
    for tentative in (1, 2):
        try:
            r = requests.get(
                f"https://{ODDSPAPI_HOST}/fixtures/today",
                headers={"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": ODDSPAPI_HOST},
                params={"sportId": 12}, timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            if tentative == 1 and "429" in str(e):
                logging.info("OddsPapi (vérif) : 429 — pause 4s puis nouvel essai.")
                time.sleep(4)
                continue
            logging.info(f"OddsPapi (vérif déterministe) indisponible : {e}")
            return []
    if data is None:
        return []
    fixtures = data if isinstance(data, list) else data.get("fixtures", data.get("data", []))
    if not isinstance(fixtures, list):
        return []
    return [f for f in fixtures
            if "srl" not in str((f.get("tournament") or {}).get("tournamentName", "")).lower()]


def _extraire_match_et_prono(pari_texte):
    """Extrait (joueur1, joueur2, texte_ligne_prono) depuis un ticket."""
    txt = re.sub(r"<[^>]+>", "", pari_texte)
    mm = re.search(r"matchs?\s*:?\s*(.+?)\s+vs\s+(.+)", txt, re.IGNORECASE)
    pm = re.search(r"prono\s*:\s*(.+)", txt, re.IGNORECASE)
    j1 = mm.group(1).strip()[:50] if mm else None
    j2 = mm.group(2).strip().split("\n")[0][:50] if mm else None
    prono = pm.group(1).strip().split("\n")[0][:100] if pm else ""
    return j1, j2, prono


def _est_pari_moneyline_simple(prono_texte, marche_detectee):
    """True si le pari est un Moneyline simple ('X (Vainqueur)') — seul cas
    où 'qui a gagné le match' suffit à trancher le pari sans ambiguïté."""
    if marche_detectee != "moneyline":
        return False
    p = prono_texte.lower()
    # Exclure toute trace d'un autre marché qui aurait pu glisser dans la ligne
    return not any(x in p for x in ["over", "under", "écart", "ecart", "handicap",
                                     "score", "combin", "tiebreak", "set"])


def verifier_moneyline_oddspapi(item, marche_detectee, fixtures):
    """
    Retourne 'GAGNE' / 'PERDU' / None (None = inconclusif, repli sur Claude).
    Ne s'applique qu'aux paris Moneyline réglés le jour même de leur émission
    ET dont le match est trouvé Finished chez OddsPapi.
    """
    if item.get("date") != _maintenant_paris().strftime("%d/%m/%Y"):
        return None  # seul le jour même est couvert par /fixtures/today
    j1, j2, prono = _extraire_match_et_prono(item.get("pari", ""))
    if not j1 or not j2 or not _est_pari_moneyline_simple(prono, marche_detectee):
        return None

    meilleur, score_max = None, 0.0
    for f in fixtures:
        p = f.get("participants", {})
        n1, n2 = str(p.get("participant1Name", "")), str(p.get("participant2Name", ""))
        s = max((_sim_noms_verif(j1, n1) + _sim_noms_verif(j2, n2)) / 2,
               (_sim_noms_verif(j1, n2) + _sim_noms_verif(j2, n1)) / 2)
        if s > score_max:
            score_max, meilleur = s, f
    if not meilleur or score_max < 0.7:
        return None

    statut = str((meilleur.get("status") or {}).get("statusName") or "")
    resultat = (meilleur.get("scores") or {}).get("result") or {}

    # v2.6 : FORFAIT / ABANDON → le pari est ANNULÉ, pas gagné.
    # Constat du 17/07 : Tabilo bat Tirante par FORFAIT (aucun jeu joué, le match
    # n'a jamais commencé) → Winamax rembourse la mise. Le bot a pourtant compté
    # une VICTOIRE et crédité un profit jamais encaissé, parce qu'aucun verdict
    # "annulé" n'existait : Claude devait choisir entre GAGNE/PERDU/EN_COURS.
    # Règles Winamax : forfait = mise remboursée. Abandon pendant le 1er set =
    # remboursé. Abandon APRÈS le 1er set = payé si ton joueur gagne, mais
    # UNIQUEMENT sur ATP/WTA simple (Garantie Abandon — exclut les Challengers).
    # Trop de conditions pour trancher sans risque → on annule et on alerte.
    if statut in ("Walkover", "Cancelled", "Postponed", "Abandoned", "Interrupted"):
        logging.warning(f"OddsPapi : statut '{statut}' → pari ANNULÉ (mise remboursée).")
        return "ANNULE"
    if statut == "Retired":
        logging.warning("OddsPapi : abandon détecté → pari ANNULÉ + règlement manuel "
                        "(Garantie Abandon Winamax : payé seulement si ton joueur gagne "
                        "après le 1er set, ATP/WTA simple uniquement).")
        return "ANNULE"
    if statut != "Finished" or not resultat:
        return None  # pas encore terminé → Claude tranchera plus tard

    p = meilleur.get("participants", {})
    n1, n2 = p.get("participant1Name", ""), p.get("participant2Name", "")
    try:
        s1 = int(resultat.get("participant1Score"))
        s2 = int(resultat.get("participant2Score"))
    except (TypeError, ValueError):
        return None  # scores absents ou illisibles → repli Claude
    if s1 == s2:
        return None
    vainqueur = n1 if s1 > s2 else n2

    # Le joueur pronostiqué est-il j1 ou j2 du ticket ? Puis : a-t-il gagné ?
    score_j1 = max(_sim_noms_verif(prono, j1), _sim_noms_verif(j1, prono))
    score_j2 = max(_sim_noms_verif(prono, j2), _sim_noms_verif(j2, prono))
    joueur_pronostique = j1 if score_j1 >= score_j2 else j2
    a_gagne = _sim_noms_verif(joueur_pronostique, vainqueur) >= 0.6

    logging.info(
        f"OddsPapi (déterministe) — {n1} vs {n2} : score final {s1}-{s2}, "
        f"vainqueur '{vainqueur}'. Pronostiqué : '{joueur_pronostique}' → "
        f"{'GAGNE' if a_gagne else 'PERDU'}."
    )
    return "GAGNE" if a_gagne else "PERDU"

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
        logging.info("Aucun pari en cours — rien à vérifier.")
        return

    # --- Chargement stats ---
    stats, stats_sha = _gh_get("stats.json")
    if not isinstance(stats, dict) or "victoires" not in stats:
        stats     = dict(STATS_DEFAUT)
        stats_sha = None
    else:
        stats = _migrer_stats(stats)

    logging.info(f"Vérification de {len(paris)} pari(s)…")

    # DÉDUPLICATION de sécurité : si des doublons existent déjà dans le fichier
    # (match+date+prono identiques), ne traiter qu'une seule occurrence pour
    # éviter le double comptage dans les stats.
    def _sig(item):
        txt = re.sub(r"<[^>]+>", "", item.get("pari", "")).lower()
        mm = re.search(r"match\s*:?\s*(.+)", txt)
        pm = re.search(r"prono\s*:\s*(.+)", txt)
        ms = mm.group(1).strip()[:60] if mm else ""
        ps = pm.group(1).strip()[:40] if pm else ""
        return f"{item.get('date','')}|{ms}|{ps}"

    vus = set()
    paris_uniques = []
    for item in paris:
        s = _sig(item)
        if s in vus:
            logging.warning(f"Doublon ignoré dans pari_en_cours : {s}")
            continue
        vus.add(s)
        paris_uniques.append(item)
    if len(paris_uniques) != len(paris):
        logging.warning(f"{len(paris) - len(paris_uniques)} doublon(s) retiré(s) avant vérification.")
    paris = paris_uniques

    # v2.2 — EXPIRATION : les paris trop vieux sortent de la file SANS verdict
    # (aucun impact stats — à régler manuellement via 'python bot.py resultat v/d N')
    def _age_jours(item):
        try:
            d = datetime.strptime(str(item.get("date", "")), "%d/%m/%Y")
            return (_maintenant_paris().replace(tzinfo=None) - d).days
        except Exception:
            return 0  # date illisible → on ne l'expire pas (prudence)

    frais, expires = [], []
    for item in paris:
        (expires if _age_jours(item) > AGE_MAX_JOURS else frais).append(item)
    if expires:
        for item in expires:
            resume_exp = next(
                (re.sub(r"<[^>]+>", "", l).strip() for l in item.get("pari", "").splitlines()
                 if "MATCH" in l.upper()), item.get("date", "?"))[:150]
            logging.warning(f"Pari expiré (> {AGE_MAX_JOURS}j) retiré de la file : {resume_exp}")
            _envoyer_telegram(
                f"⏰ <b>Pari expiré ({item.get('date','?')})</b> — introuvable après "
                f"{AGE_MAX_JOURS} jours de vérifications.\n📌 {resume_exp}\n"
                f"→ À régler manuellement : <code>python bot.py file</code> puis "
                f"<code>resultat v/d N</code> (il a été retiré de la file automatique)."
            )
    paris = frais

    # v2.2 — PLAFOND : borne le nombre d'appels Claude par run (durée maîtrisée).
    # Les paris au-delà restent en file et seront vérifiés au run suivant.
    a_verifier = paris[:MAX_VERIFS_PAR_RUN]
    reportes   = paris[MAX_VERIFS_PAR_RUN:]
    if reportes:
        logging.info(f"Plafond {MAX_VERIFS_PAR_RUN} vérifications/run : "
                     f"{len(reportes)} pari(s) reporté(s) au prochain run.")

    restants        = list(reportes)  # les reportés restent en file tels quels
    stats_modifiees = False

    # v2.3 : fixtures OddsPapi du jour, récupérées UNE fois pour tout le run
    # (sert au contrôle déterministe des paris Moneyline réglés aujourd'hui).
    # v2.4 : fetch PARESSEUX — seulement si au moins un pari peut en profiter
    # (Moneyline daté d'aujourd'hui), sinon la requête était gaspillée.
    fixtures_jour = []
    date_paris = _maintenant_paris().strftime("%d/%m/%Y")
    if any(it.get("marche") == "moneyline" and it.get("date") == date_paris
           for it in a_verifier):
        fixtures_jour = _oddspapi_fixtures_today_verif()
        if fixtures_jour:
            _quota_oddspapi_compter(1)  # v2.4 : cette requête compte aussi

    for i, item in enumerate(a_verifier, 1):
        pari_texte = item.get("pari", "")
        if not pari_texte:
            continue

        # Récupérer marché, niveau, modèle ET (v2.1) cote/mise depuis la file
        marche  = item.get("marche", "autre")
        niveau  = item.get("niveau", "autre")
        modele  = item.get("modele", "autre")
        cote    = item.get("cote")       # v2.1 — écrit par bot.py v7.4
        mise    = item.get("mise_pct")   # v2.1 — écrit par bot.py v7.4
        surface = _normaliser_surface(item.get("surface"))  # v2.2 — écrit par bot.py v7.5

        logging.info(f"─── Ticket {i}/{len(a_verifier)} — {marche} / {niveau} ───")

        # v2.3 : contrôle DÉTERMINISTE en priorité (score OddsPapi réel) pour
        # les Moneyline du jour même — élimine le risque d'hallucination du
        # web_search LLM constaté le 15/07 (score inventé, source citée fictive).
        statut = verifier_moneyline_oddspapi(item, marche, fixtures_jour)
        if statut:
            logging.info(f"Verdict (OddsPapi déterministe) : {statut}")
        else:
            # v2.7 : OddsPapi muet → CONSENSUS Claude + Gemini (2 sources
            # indépendantes). Sur désaccord → EN_COURS, jamais un score deviné.
            statut = verdict_par_consensus(pari_texte, item.get("date", ""))
            logging.info(f"Verdict (consensus 2 sources) : {statut}")

        if statut == "ANNULE":
            # v2.6 : forfait/abandon → mise remboursée. Le pari sort de la file
            # SANS aucun impact sur les stats : ni V, ni D, ni profit, ni mise.
            # Un pari remboursé n'a jamais existé du point de vue du yield.
            resume_ann = next(
                (re.sub(r"<[^>]+>", "", l).strip() for l in pari_texte.splitlines()
                 if "MATCH" in l.upper()), item.get("date", "?"))[:150]
            logging.info("↩️ ANNULÉ (forfait/abandon) — retiré de la file, stats inchangées.")
            _envoyer_telegram(
                f"↩️ <b>PARI ANNULÉ — {item.get('date','?')}</b>\n\n"
                f"📌 {resume_ann}\n\n"
                f"Forfait ou abandon : la mise est remboursée par Winamax, "
                f"le pari ne compte ni en victoire ni en défaite.\n"
                f"⚠️ En cas d'ABANDON après le 1er set sur un tournoi ATP/WTA, la "
                f"Garantie Abandon peut s'appliquer (pari payé) — vérifie ton compte, "
                f"et si c'est le cas règle-le à la main : "
                f"<code>python bot.py resultat v</code>."
            )
            continue  # ni stats, ni file : le pari disparaît proprement

        if statut in ("GAGNE", "PERDU"):
            victoire = statut == "GAGNE"
            stats["victoires" if victoire else "defaites"] += 1
            # v2.1 : YIELD — profit en unités de mise (% de bankroll),
            # même formule que bot.py v7.4 (enregistrer_resultat)
            profit = None
            if cote and mise:
                profit = round((cote - 1) * mise, 4) if victoire else round(-mise, 4)
                stats = _maj_roi_global(stats, mise, profit)
                logging.info(f"Yield — cote {cote} / mise {mise}% → profit {profit:+.2f}u.")
            else:
                logging.info("Yield — cote/mise absentes du pari (ancien format) : V/D seulement.")
            stats = _maj_stats_detail(stats, victoire, marche, niveau, modele,
                                      mise=mise if profit is not None else None,
                                      profit=profit, surface=surface)
            stats_modifiees = True
            logging.info("🏆 Victoire enregistrée." if victoire else "❌ Défaite enregistrée.")
            notifier_resultat(statut, pari_texte, stats)
        else:
            restants.append(item)
            logging.info("⏳ En cours — maintenu dans la file.")

        if i < len(a_verifier):
            time.sleep(2)

    # --- Persistance stats ---
    if stats_modifiees:
        _gh_put("stats.json", stats, "🔄 MAJ stats après vérification", sha=stats_sha)

    # --- Persistance file d'attente ---
    # (comparaison avec la taille d'ORIGINE : expirés + réglés déclenchent l'écriture)
    if len(restants) != len(paris) + len(expires):
        if restants:
            _gh_put("pari_en_cours.json", restants,
                    "🧹 Nettoyage file après résultats", sha=paris_sha)
        elif paris_sha:
            _gh_delete("pari_en_cours.json", "🗑️ File vide — suppression", sha=paris_sha)
        logging.info(f"File mise à jour : {len(restants)} pari(s) restant(s).")
    else:
        logging.info("Aucun changement — tous les matchs encore en cours.")

    logging.info(f"Vérification terminée en {time.time() - debut:.1f}s.")


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
                      "text": f"⚠️ verification_resultats.py a planté :\n{e}"},
                timeout=5,
            )
        except Exception:
            pass
        sys.exit(1)
