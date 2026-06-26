"""
verification_resultats.py — v2.0
Vérifie automatiquement les résultats des paris en cours via Claude + web_search.
Notifie Telegram du résultat (gagné/perdu) et met à jour stats + file d'attente via API GitHub.
"""

import os, sys, json, logging, base64, time, re, requests
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
CLAUDE_MODEL = "claude-sonnet-4-6"  # Sonnet pour fiabilité — vérification critique

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
    }
}


def _migrer_stats(s):
    if s.get("version", 0) < 2:
        s["version"] = 2
        if "par_marche" not in s:
            s["par_marche"] = dict(STATS_DEFAUT["par_marche"])
        if "par_niveau" not in s:
            s["par_niveau"] = dict(STATS_DEFAUT["par_niveau"])
    return s


def _maj_stats_detail(stats, victoire, marche, niveau, modele="autre"):
    """Met à jour les stats par marché, par niveau et par modèle."""
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

    message = (
        f"{header}\n\n"
        f"📌 {resume}\n\n"
        f"📊 <b>BILAN ACEANALYTICS 🎾 TENNIS</b>\n"
        f"{emoji} V: {stats['victoires']} | ❌ D: {stats['defaites']}\n"
        f"📈 <b>Win Rate : {wr:.1f}%</b>"
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
    "\nVERDICT: GAGNE   (ou PERDU, ou EN_COURS)"
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
    "\n\nRÈGLES STRICTES :"
    "\n- Match pas encore terminé OU score partiel OU doute → VERDICT: EN_COURS"
    "\n- Match terminé (preuve explicite) + pronostic validé → VERDICT: GAGNE"
    "\n- Match terminé (preuve explicite) + pronostic non validé → VERDICT: PERDU"
    "\n- Le moindre doute sur la fin du match → VERDICT: EN_COURS"
    "\n\n⚠️ RAPPEL FINAL : quoi que tu écrives avant, tu DOIS terminer par la ligne"
    "\n'VERDICT: GAGNE/PERDU/EN_COURS'. C'est la SEULE chose que le système lit."
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
        m = re.search(r"VERDICT\s*:\s*(GAGNE|PERDU|EN_COURS)", verdict)
        if m:
            resultat = m.group(1)

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

    restants        = []
    stats_modifiees = False

    for i, item in enumerate(paris, 1):
        pari_texte = item.get("pari", "")
        if not pari_texte:
            continue

        # Récupérer marché et niveau depuis pari_en_cours.json
        marche = item.get("marche", "autre")
        niveau = item.get("niveau", "autre")
        modele = item.get("modele", "autre")

        logging.info(f"─── Ticket {i}/{len(paris)} — {marche} / {niveau} ───")
        statut = interroger_claude_statut(pari_texte, item.get("date", ""))
        logging.info(f"Verdict : {statut}")

        if statut == "GAGNE":
            stats["victoires"] += 1
            stats = _maj_stats_detail(stats, True, marche, niveau, modele)
            stats_modifiees = True
            logging.info("🏆 Victoire enregistrée.")
            notifier_resultat("GAGNE", pari_texte, stats)
        elif statut == "PERDU":
            stats["defaites"] += 1
            stats = _maj_stats_detail(stats, False, marche, niveau, modele)
            stats_modifiees = True
            logging.info("❌ Défaite enregistrée.")
            notifier_resultat("PERDU", pari_texte, stats)
        else:
            restants.append(item)
            logging.info("⏳ En cours — maintenu dans la file.")

        if i < len(paris):
            time.sleep(2)

    # --- Persistance stats ---
    if stats_modifiees:
        _gh_put("stats.json", stats, "🔄 MAJ stats après vérification", sha=stats_sha)

    # --- Persistance file d'attente ---
    if len(restants) != len(paris):
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
