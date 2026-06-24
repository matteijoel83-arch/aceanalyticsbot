"""
verification_football.py — v1.0
Vérifie automatiquement les résultats des paris football via Claude + web_search.
"""

import os, sys, json, logging, base64, time, re, requests
import anthropic
from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler("verification_football.log", maxBytes=2*1024*1024, backupCount=2),
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
    "version": 1, "victoires": 0, "defaites": 0,
    "par_marche": {
        "1n2": {"v": 0, "d": 0}, "score_exact": {"v": 0, "d": 0},
        "over_under": {"v": 0, "d": 0}, "handicap": {"v": 0, "d": 0},
        "btts": {"v": 0, "d": 0}, "combine": {"v": 0, "d": 0}, "autre": {"v": 0, "d": 0},
    },
    "par_niveau": {
        "elevee": {"v": 0, "d": 0}, "moderee": {"v": 0, "d": 0}, "basse": {"v": 0, "d": 0},
    }
}

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

def _gh_put(path, contenu, message, sha=None):
    url     = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(json.dumps(contenu, ensure_ascii=False, indent=2).encode()).decode(),
    }
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(url, headers=GITHUB_HEADERS, json=payload, timeout=15)
        r.raise_for_status()
        logging.info(f"GitHub '{path}' OK")
        return True
    except Exception as e:
        logging.error(f"GitHub PUT '{path}' : {e}")
        return False

def _gh_delete(path, message, sha):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    try:
        r = requests.delete(url, headers=GITHUB_HEADERS,
                            json={"message": message, "sha": sha}, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        logging.error(f"GitHub DELETE '{path}' : {e}")
        return False

def _migrer_stats(s):
    if s.get("version", 0) < 1:
        s["version"] = 1
        if "par_marche" not in s:
            s["par_marche"] = dict(STATS_DEFAUT["par_marche"])
        if "par_niveau" not in s:
            s["par_niveau"] = dict(STATS_DEFAUT["par_niveau"])
    return s

def _maj_stats_detail(stats, victoire, marche, niveau):
    cle = "v" if victoire else "d"
    if "par_marche" not in stats:
        stats["par_marche"] = dict(STATS_DEFAUT["par_marche"])
    marche_cle = marche if marche in stats["par_marche"] else "autre"
    stats["par_marche"][marche_cle][cle] += 1
    if "par_niveau" not in stats:
        stats["par_niveau"] = dict(STATS_DEFAUT["par_niveau"])
    if niveau in stats["par_niveau"]:
        stats["par_niveau"][niveau][cle] += 1
    return stats

def _envoyer_telegram(texte, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for t in range(1, 3):
        try:
            r = requests.post(url, json={"chat_id": TELEGRAM_CHANNEL_ID,
                                          "text": texte, "parse_mode": parse_mode}, timeout=10)
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
                continue
            r.raise_for_status()
            return True
        except Exception as e:
            logging.warning(f"Telegram tentative {t} : {e}")
            time.sleep(2)
    return False

def notifier_resultat(statut, pari_texte, stats):
    total = stats["victoires"] + stats["defaites"]
    wr    = (stats["victoires"] / total * 100) if total > 0 else 0.0

    if statut == "GAGNE":
        header = "⚽ 🏆 <b>ACEANALYTICS FOOTBALL — PRONOSTIC VALIDÉ !</b> ✅"
        emoji  = "✅"
    else:
        header = "⚽ ❌ <b>ACEANALYTICS FOOTBALL — PRONOSTIC PERDU</b>"
        emoji  = "❌"

    resume = next(
        (re.sub(r"<[^>]+>", "", l).strip() for l in pari_texte.splitlines() if "MATCH" in l.upper()),
        pari_texte.strip().split("\n")[0]
    )[:200]

    message = (
        f"{header}\n\n"
        f"📌 {resume}\n\n"
        f"📊 <b>BILAN ACEANALYTICS ⚽ FOOTBALL</b>\n"
        f"{emoji} V: {stats['victoires']} | ❌ D: {stats['defaites']}\n"
        f"📈 <b>Win Rate : {wr:.1f}%</b>"
    )
    _envoyer_telegram(message)

INSTRUCTIONS = (
    "Tu es un vérificateur de résultats de paris football. "
    "Cherche le résultat via web_search sur Flashscore, Sofascore ou BBC Sport. "
    "\n\nRÈGLE ABSOLUE : Tu dois vérifier le PRONOSTIC EXACT, pas seulement le vainqueur."
    "\n- Si le prono est '1N2' → vérifier le résultat exact (victoire dom/nul/ext)"
    "\n- Si le prono est 'Over 2.5 buts' → vérifier le nombre total de buts"
    "\n- Si le prono est 'BTTS' → vérifier si les deux équipes ont marqué"
    "\n- Si le prono est 'Handicap -1' → vérifier l'écart de buts"
    "\n- Si le prono est un combiné → TOUS les pronostics doivent être validés"
    "\n\n⛔ RÈGLE DE FIN DE MATCH — LA PLUS IMPORTANTE :"
    "\nUn match n'est TERMINÉ que si tu as une PREUVE EXPLICITE :"
    "\n  • mention 'Terminé' / 'Finished' / 'Final' / 'FT' / 'Temps plein' / score final officiel"
    "\nSi tu vois un score en direct ('Live', 'en direct', minute de jeu affichée,"
    "\nmi-temps, ou AUCUNE mention claire de fin) → c'est EN_COURS, JAMAIS PERDU."
    "\nNe conclus JAMAIS PERDU sur la base d'un score intermédiaire : une équipe menée"
    "\npeut encore égaliser ou gagner. Match non fini = EN_COURS."
    "\nSi tu ne trouves PAS le score final explicite → EN_COURS."
    "\n\n⛔ AVANT TOUT VERDICT GAGNE OU PERDU — CITATION OBLIGATOIRE :"
    "\nTu DOIS d'abord écrire une ligne 'PREUVE DE FIN: ...' citant la preuve exacte"
    "\n(ex: 'PREUVE DE FIN: Flashscore affiche Terminé, score final 2-1')."
    "\nSi tu ne peux PAS citer de preuve de fin explicite (Terminé/FT/Temps plein +"
    "\nscore final) → écris 'PREUVE DE FIN: aucune' et le verdict est OBLIGATOIREMENT EN_COURS."
    "\nUn verdict GAGNE ou PERDU SANS ligne 'PREUVE DE FIN' valide est INTERDIT."
    "\n\n⚠️ VÉRIFICATION DU BON MATCH :"
    "\nAttention aux homonymes. Vérifie que la compétition, la date et l'adversaire"
    "\ncorrespondent EXACTEMENT au ticket. Une autre équipe du même nom dans une autre"
    "\ncompétition ≠ ton match. Si tu n'es pas certain que c'est le bon match → EN_COURS."
    "\n\nPROCÉDURE :"
    "\n1. Cherche le score FINAL (preuve de fin : Terminé/FT/Temps plein)"
    "\n2. Si pas de preuve de fin claire → EN_COURS immédiatement"
    "\n3. Si match terminé : compare avec le pronostic exact"
    "\n\nFORMAT DE RÉPONSE OBLIGATOIRE :"
    "\nTermine ta réponse par EXACTEMENT une de ces lignes, seule sur la dernière ligne :"
    "\nVERDICT: GAGNE"
    "\nVERDICT: PERDU"
    "\nVERDICT: EN_COURS"
    "\n\nRÈGLES STRICTES :"
    "\n- Match pas terminé OU score en direct OU doute → VERDICT: EN_COURS"
    "\n- Match terminé (preuve) + pronostic validé → VERDICT: GAGNE"
    "\n- Match terminé (preuve) + pronostic non validé → VERDICT: PERDU"
)

def _extraire_resume(pari_texte):
    champs = ("MATCH", "COMPÉTITION", "HEURE", "PRONO")
    lignes = []
    for ligne in pari_texte.splitlines():
        if any(c in ligne.upper() for c in champs):
            lignes.append(re.sub(r"<[^>]+>", "", ligne).strip())
    return "\n".join(lignes) if lignes else "\n".join(pari_texte.splitlines()[:3])

def interroger_claude_statut(pari_texte, date_pari=""):
    resume = _extraire_resume(pari_texte)
    contexte_date = f"\nDate du match : {date_pari}" if date_pari else ""
    try:
        reponse = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=256, system=INSTRUCTIONS,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": f"Résultat de ce pari football ?{contexte_date}\n{resume}"}],
        )
        verdict = "\n".join(
            b.text for b in reponse.content if hasattr(b, "text") and b.text
        ).strip().upper()
        logging.info(f"Verdict brut : '{verdict[-200:]}'")
        m = re.search(r"VERDICT\s*:\s*(GAGNE|PERDU|EN_COURS)", verdict)
        if m:
            resultat = m.group(1)

            # Protection anti-faux-positif : verdict GAGNE/PERDU exige une preuve de fin
            if resultat in ("GAGNE", "PERDU"):
                preuve = re.search(r"PREUVE DE FIN\s*:\s*(.+)", verdict)
                preuve_txt = preuve.group(1).strip() if preuve else ""
                preuve_valide = bool(preuve_txt) and "AUCUNE" not in preuve_txt[:20]
                indices_fin = any(mot in preuve_txt for mot in
                                  ["TERMIN", "FINISHED", "FINAL", "FT", "TEMPS PLEIN"])
                if not (preuve_valide and indices_fin):
                    logging.warning(
                        f"Verdict {resultat} SANS preuve de fin valide "
                        f"('{preuve_txt[:60]}') → forcé EN_COURS par sécurité."
                    )
                    return "EN_COURS"

            logging.info(f"Verdict extrait : {resultat}")
            return resultat
        logging.warning("Aucune ligne VERDICT trouvée → EN_COURS par sécurité.")
        return "EN_COURS"
    except Exception as e:
        logging.error(f"Erreur Claude : {e}")
        return "EN_COURS"

def main():
    debut = time.time()
    paris, paris_sha = _gh_get("pari_en_cours_football.json")
    if not isinstance(paris, list):
        paris = [paris] if paris else []
    if not paris:
        logging.info("Aucun pari football en cours.")
        return

    stats, stats_sha = _gh_get("stats_football.json")
    if not isinstance(stats, dict) or "victoires" not in stats:
        stats     = dict(STATS_DEFAUT)
        stats_sha = None
    else:
        stats = _migrer_stats(stats)

    logging.info(f"Vérification de {len(paris)} pari(s) football…")
    restants        = []
    stats_modifiees = False

    for i, item in enumerate(paris, 1):
        pari_texte = item.get("pari", "")
        if not pari_texte:
            continue
        marche = item.get("marche", "autre")
        niveau = item.get("niveau", "autre")
        logging.info(f"─── Ticket {i}/{len(paris)} — {marche} / {niveau} ───")
        statut = interroger_claude_statut(pari_texte, item.get("date", ""))
        if statut == "GAGNE":
            stats["victoires"] += 1
            stats = _maj_stats_detail(stats, True, marche, niveau)
            stats_modifiees = True
            notifier_resultat("GAGNE", pari_texte, stats)
        elif statut == "PERDU":
            stats["defaites"] += 1
            stats = _maj_stats_detail(stats, False, marche, niveau)
            stats_modifiees = True
            notifier_resultat("PERDU", pari_texte, stats)
        else:
            restants.append(item)
            logging.info("⏳ En cours.")
        if i < len(paris):
            time.sleep(2)

    if stats_modifiees:
        _gh_put("stats_football.json", stats, "🔄 MAJ stats football", sha=stats_sha)

    if len(restants) != len(paris):
        if restants:
            _gh_put("pari_en_cours_football.json", restants, "🧹 Nettoyage paris football", sha=paris_sha)
        elif paris_sha:
            _gh_delete("pari_en_cours_football.json", "🗑️ File vide", sha=paris_sha)

    logging.info(f"Vérification football terminée en {time.time() - debut:.1f}s.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"Erreur critique : {e}", exc_info=True)
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHANNEL_ID,
                      "text": f"⚠️ verification_football.py a planté : {e}"},
                timeout=5,
            )
        except Exception:
            pass
        sys.exit(1)
