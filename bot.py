"""
╔══════════════════════════════════════════════════════════════════════╗
║          BOT TENNIS ACEANALYTICS — bot.py v6.0                      ║
║  Architecture hybride : Gemini (recherche) + Claude (analyse)        ║
║                                                                      ║
║  Secrets GitHub requis :                                             ║
║    ANTHROPIC_API_KEY  · TELEGRAM_BOT_TOKEN · TELEGRAM_CHANNEL_ID    ║
║    GITHUB_TOKEN       · GITHUB_REPO · GEMINI_API_KEY                ║
║  Secret optionnel :                                                  ║
║    ODDS_API_KEY  (https://the-odds-api.com — gratuit 500 req/mois)  ║
║                                                                      ║
║  Flux :                                                              ║
║    1. Gemini 2.5 Pro  → collecte données tennis via Google Search   ║
║    2. Claude Sonnet   → analyse + calcul value + génère tickets      ║
║    3. Telegram        → envoi + sauvegarde GitHub                    ║
║                                                                      ║
║  Usage CLI :                                                         ║
║    python bot.py              → analyse + envoi Telegram             ║
║    python bot.py --dry-run    → simulation, aucun envoi réel         ║
║    python bot.py resultat v   → enregistrer une victoire             ║
║    python bot.py resultat d   → enregistrer une défaite              ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import os, sys, json, hashlib, logging, re, time, base64, requests
from datetime import datetime
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
ODDS_API_KEY        = os.environ.get("ODDS_API_KEY")  # Optionnel

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

# Clients IA
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

CLAUDE_MODEL = "claude-sonnet-4-6"
GEMINI_MODEL = "gemini-2.5-pro"

GITHUB_API     = "https://api.github.com"
GITHUB_HEADERS = {
    "Authorization":        f"Bearer {GITHUB_TOKEN}",
    "Accept":               "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

STATS_VERSION = 1
STATS_DEFAUT  = {"version": STATS_VERSION, "victoires": 0, "defaites": 0}
TICKET_SEP    = "[SEPARATEUR]"
MAX_TICKETS   = 3

# =====================================================================
# 2. COUCHE GITHUB — lecture/écriture atomique
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
# 3. STATISTIQUES
# =====================================================================

def _migrer_stats(s: dict) -> dict:
    if s.get("version", 0) < 1:
        s["version"] = STATS_VERSION
    return s


def charger_stats() -> dict:
    s, _ = _gh_get("stats.json")
    if not isinstance(s, dict) or "victoires" not in s:
        return dict(STATS_DEFAUT)
    return _migrer_stats(s)


def calculer_winrate(s: dict) -> float:
    total = s["victoires"] + s["defaites"]
    return (s["victoires"] / total * 100) if total > 0 else 0.0


def enregistrer_resultat(victoire: bool, pari_termine: str = None):
    s, sha = _gh_get("stats.json")
    if not isinstance(s, dict) or "victoires" not in s:
        s = dict(STATS_DEFAUT)
    s["victoires" if victoire else "defaites"] += 1
    s = _migrer_stats(s)
    if DRY_RUN:
        logging.info(f"[DRY-RUN] Stats simulées : {s}")
    else:
        _gh_put("stats.json", s, "🔄 Maj stats", sha=sha)
    if pari_termine and not DRY_RUN:
        paris, psha = _gh_get("pari_en_cours.json")
        if isinstance(paris, list):
            restants = [p for p in paris if p.get("pari") != pari_termine]
            if restants:
                _gh_put("pari_en_cours.json", restants, "🧹 Nettoyage paris", sha=psha)
            elif psha:
                _gh_delete("pari_en_cours.json", "🗑️ File vide", sha=psha)
    logging.info(f"{'✅ VICTOIRE' if victoire else '❌ DÉFAITE'} — {s['victoires']}V / {s['defaites']}D")

# =====================================================================
# 4. DÉDUPLICATION PAR HASH SHA-256
# =====================================================================

def _hash_ticket(ticket: str) -> str:
    return hashlib.sha256(
        re.sub(r"\s+", " ", ticket.strip().lower())[:300].encode()
    ).hexdigest()


def charger_historique() -> tuple:
    h, sha = _gh_get("historique.json")
    return (h if isinstance(h, list) else []), sha


def sauvegarder_historique(hashes: list, sha):
    _gh_put("historique.json", hashes[-20:], "📚 Maj historique", sha=sha)

# =====================================================================
# 5. TELEGRAM — retry + backoff exponentiel
# =====================================================================

def _tronquer(texte: str, limite: int = 3500) -> str:
    if len(texte) <= limite:
        return texte
    coupe = texte.rfind("\n", 0, limite)
    return texte[:coupe if coupe != -1 else limite] + "\n\n… [Analyse tronquée]"


def envoyer_sur_telegram(message: str, stats: dict = None, retries: int = 3) -> bool:
    if stats is None:
        stats = charger_stats()
    sig = (
        f"\n\n📊 <b>BILAN ACEANALYTICS</b>\n"
        f"✅ V: {stats['victoires']} | ❌ D: {stats['defaites']}\n"
        f"📈 <b>Win Rate : {calculer_winrate(stats):.1f}%</b>"
    )
    html = message + sig
    if len(html) > 4000:
        logging.warning("Message trop long — troncature propre.")
        html = _tronquer(re.sub(r"<[^>]+>", "", message), 3500) + sig
        parse_mode = None
    else:
        parse_mode = "HTML"
    if DRY_RUN:
        logging.info(f"[DRY-RUN] Telegram simulé ({len(html)} chars)")
        return True
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHANNEL_ID, "text": html}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    for t in range(1, retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 429:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                logging.warning(f"Rate-limit Telegram — attente {wait}s.")
                time.sleep(wait)
                continue
            r.raise_for_status()
            logging.info("✅ Telegram envoyé.")
            return True
        except requests.exceptions.Timeout:
            logging.warning(f"Telegram timeout tentative {t}.")
        except requests.exceptions.HTTPError as e:
            logging.error(f"Telegram HTTP {e} — {r.text}")
            break
        except Exception as e:
            logging.error(f"Telegram erreur : {e}")
        if t < retries:
            time.sleep(2 ** t)
    logging.error("❌ Telegram : échec définitif.")
    _alerter_telegram_erreur("❌ bot.py : échec envoi ticket après tous les retries.")
    return False


def _alerter_telegram_erreur(msg: str):
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

def sauvegarder_pari_pour_suivi(pari_info: dict):
    if "pari" not in pari_info or "date" not in pari_info:
        logging.error(f"Structure pari_info invalide : {pari_info}")
        return
    if DRY_RUN:
        logging.info(f"[DRY-RUN] Pari non sauvegardé : {pari_info['date']}")
        return
    paris, sha = _gh_get("pari_en_cours.json")
    if not isinstance(paris, list):
        paris = []
    paris.append(pari_info)
    _gh_put("pari_en_cours.json", paris, "📌 Ajout pari", sha=sha)

# =====================================================================
# 7. COTES TEMPS RÉEL (The Odds API — optionnel)
# =====================================================================

def recuperer_cotes_tennis() -> str:
    if not ODDS_API_KEY:
        return ""
    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/tennis/odds",
            params={"apiKey": ODDS_API_KEY, "regions": "eu",
                    "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"},
            timeout=10,
        )
        r.raise_for_status()
        matchs = r.json()
        if not matchs:
            return ""
        lignes = ["COTES WINAMAX (source The Odds API) :"]
        for m in matchs[:20]:
            heure = m.get("commence_time", "")[:16].replace("T", " ")
            j1, j2 = m.get("home_team", "?"), m.get("away_team", "?")
            c1 = c2 = None
            for bk in m.get("bookmakers", []):
                is_winamax = "winamax" in bk.get("key", "").lower()
                if is_winamax or not c1:
                    for mkt in bk.get("markets", []):
                        if mkt.get("key") == "h2h":
                            out = {o["name"]: o["price"] for o in mkt.get("outcomes", [])}
                            c1, c2 = out.get(j1), out.get(j2)
                    if is_winamax:
                        break
            if c1 and c2:
                lignes.append(f"  {heure} UTC | {j1} ({c1:.2f}) vs {j2} ({c2:.2f})")
        logging.info(f"Odds API OK : {len(matchs)} matchs.")
        return "\n".join(lignes)
    except Exception as e:
        logging.warning(f"Odds API indisponible : {e}")
        return ""

# =====================================================================
# 8. COLLECTE DES DONNÉES VIA GEMINI (rôle : chercheur)
# =====================================================================

def collecter_donnees_tennis(date: str, heure: str, cotes_injectees: str) -> str:
    """
    Gemini 2.5 Pro collecte toutes les données nécessaires via Google Search.
    Retourne un bloc de données structurées à injecter dans le prompt Claude.
    Claude ne fera AUCUNE recherche — il analyse uniquement ces données.
    """

    bloc_cotes = (
        f"Les cotes suivantes sont déjà disponibles, ne les cherche pas :\n{cotes_injectees}"
        if cotes_injectees else
        "Cherche les cotes sur winamax.fr ou sportytrader.com pour chaque match retenu."
    )

    prompt_gemini = f"""
Tu es un agent de collecte de données tennis. Date : {date}. Heure : {heure} France.

MISSION : Collecter UNIQUEMENT les faits bruts. Tu ne fais AUCUNE analyse, AUCUN pronostic.
Un autre système (Claude) se chargera de l'analyse. Ton seul rôle est de chercher et structurer.

RECHERCHES À EFFECTUER (dans cet ordre) :
1. Matchs ATP et WTA du {date} qui commencent APRÈS {heure} (heure France)
   → Sources : flashscore.fr, atptour.com, wtatennis.com
2. Pour chaque match trouvé : forme récente des 2 joueurs (5 derniers matchs)
   → Sources : flashscore.fr, sofascore.com
3. Forfaits, blessures ou absences annoncées dans les 48h
   → Sources : actualités sportives, déclarations officielles
4. {bloc_cotes}

FORMAT DE RÉPONSE OBLIGATOIRE (JSON strict, aucun autre texte) :
{{
  "heure_collecte": "{heure}",
  "matchs": [
    {{
      "heure_match": "HH:MM",
      "joueur1": "Nom Prénom",
      "joueur2": "Nom Prénom",
      "tournoi": "Nom du tournoi",
      "surface": "Terre / Dur / Gazon",
      "indoor": true/false,
      "cote_j1": 1.XX,
      "cote_j2": 1.XX,
      "source_cote": "Winamax / Sportytrader / non trouvée",
      "forme_j1": ["V", "D", "V", "V", "D"],
      "forme_j2": ["V", "V", "D", "V", "V"],
      "details_forme_j1": "Résumé en 1 ligne : adversaires, surfaces, scores clés",
      "details_forme_j2": "Résumé en 1 ligne : adversaires, surfaces, scores clés",
      "h2h_recents": "Ex: J1 mène 3-1 sur les 2 dernières années, 2-0 sur terre",
      "alertes_physiques": "Ex: J1 a reçu des soins médicaux hier | Aucune",
      "absence_recente": "Ex: Retour après 6 semaines d'absence | Aucune",
      "contexte": "Ex: Finale, points à défendre, Grand Chelem dans 5 jours"
    }}
  ],
  "avertissements": "Données non trouvées ou incertaines à signaler à l'analyseur"
}}

RÈGLES STRICTES :
- Si un champ est introuvable → mettre "non trouvé" (jamais inventer)
- Si aucun match n'est prévu après {heure} → retourner {{"matchs": [], "avertissements": "Aucun match à venir"}}
- JSON valide uniquement, sans backticks ni commentaires
"""

    try:
        logging.info(f"Gemini collecte les données pour le {date} à {heure}…")
        reponse = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt_gemini,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.1,
            ),
        )
        texte = reponse.text.strip()

        # Nettoyer les backticks éventuels que Gemini ajouterait malgré la consigne
        texte = re.sub(r"^```json\s*", "", texte)
        texte = re.sub(r"\s*```$", "", texte)

        # Valider que c'est du JSON
        donnees = json.loads(texte)
        nb_matchs = len(donnees.get("matchs", []))
        logging.info(f"Gemini OK — {nb_matchs} match(s) collecté(s).")
        return json.dumps(donnees, ensure_ascii=False, indent=2)

    except json.JSONDecodeError as e:
        logging.error(f"Gemini a retourné un JSON invalide : {e}\nRéponse brute : {texte[:500]}")
        # Fallback : on retourne quand même le texte brut pour que Claude ne soit pas bloqué
        return f'{{"matchs": [], "avertissements": "Erreur collecte Gemini : JSON invalide. Données brutes : {texte[:1000]}"}}'
    except Exception as e:
        logging.error(f"Erreur Gemini collecte : {e}")
        return '{"matchs": [], "avertissements": "Erreur collecte Gemini — analyse impossible."}'

# =====================================================================
# 9. PROMPT CLAUDE (rôle : analyste pur — zéro recherche web)
# =====================================================================

def construire_prompt_claude(date: str, heure: str, donnees_json: str) -> str:
    session = "MATIN" if heure < "14:00" else "APRÈS-MIDI"

    # Extraire les avertissements Gemini pour les mettre en évidence
    try:
        avertissements = json.loads(donnees_json).get("avertissements", "Aucun")
    except Exception:
        avertissements = "Non disponibles"

    return f"""Tu es un expert en paris tennis. Date : {date} · {heure} France · Session {session}.

DONNÉES COLLECTÉES PAR GEMINI (source unique — ne pas chercher sur internet) :
{donnees_json}

⚠️ AVERTISSEMENTS GEMINI (données incertaines ou manquantes) : {avertissements}
→ Si un match contient des avertissements sur des données manquantes (forme, H2H, cotes),
  abandonne ce match plutôt que d'analyser avec des données incomplètes.
→ Mieux vaut passer son chemin que parier sur des informations partielles.

Tu disposes de TOUTES les données disponibles ci-dessus.
Tu n'as PAS accès à internet. Analyse uniquement les données fournies.

FILTRES IMMÉDIATS (élimine sans analyser) :
• Match commencé/terminé avant {heure} → skip
• absence_recente > 2 mois ou doute participation → skip
• Données manquantes signalées par Gemini sur ce match → skip
• alertes_physiques présentes → marchés de jeux interdits + mise 0.5%
• Retour 3-8 semaines → marchés alternatifs uniquement + mise 0.5%

ANALYSE EN 2 ÉTAPES SÉQUENTIELLES :
[1] FACTEURS BRUTS — lister POUR/CONTRE chaque joueur (aucune conclusion) :
  · Surface + forme des 5 derniers matchs + charge physique récente
  · H2H + contexte (points à défendre, Grand Chelem dans 7j → vigilance max)
  · Hold% / stats avancées : uniquement si présents dans les données, sinon omettre

[2] DÉCISION — sur base exclusive de [1] :
  · Probabilité estimée en %
  · Cote Juste = 1/(prob/100)
  · Kelly quart = ((prob×cote−1)/(cote−1))×0.25 → arrondi 0.5%
  · Cote réelle > Cote Juste+0.10 → VALUE ✅ → ticket validé
  · Cote réelle ≤ Cote Juste+0.10 → ❌ → abandonné
  · Si source_cote = "non trouvée" → indiquer "non vérifiée" + mise 0.5%
  · Aucune value → AUCUN_MATCH (rien d'autre)

MARCHÉS :
ÉLEVÉE  → Moneyline | si cote trop basse : Handicap Jeux ou Victoire 2-0
MODÉRÉE → Over/Under jeux · Handicap +4.5 · 2-0 Score Exact (Moneyline interdit)
Combiné + MODÉRÉE → INTERDIT

MISES = MIN(Kelly quart, plafond) :
Simple ÉLEVÉE 2% · Simple MODÉRÉE 1% · Combiné ÉLEVÉE 1% · Non vérifiée 0.5%

FORMAT (max {MAX_TICKETS} tickets, séparés par [SEPARATEUR] sur ligne isolée) :
🔴 <b>PRONOSTIC [SIMPLE/COMBINÉ]</b> 🔴
🏟 <b>MATCHS :</b> [Joueur A vs Joueur B]
🏆 <b>COMPÉTITION :</b> [Tournoi]
⏰ <b>HEURE :</b> [Heure exacte]
✅ <b>PRONO :</b> [Pronostic précis]
📈 <b>COTE :</b> [Cote ou "non vérifiée"]
💰 <b>MISE :</b> [% Kelly plafonné]
🛡 <b>CONFIANCE :</b> [ÉLEVÉE/MODÉRÉE]
🧮 <b>VALUE :</b> [X% → juste Y.YY → réelle Z.ZZ → Kelly W%]
📌 <b>POURQUOI ?</b> [Max 120 mots — facteurs clés uniquement]
⚠️ <b>DONNÉES MANQUANTES :</b> [Champs "non trouvé" utilisés, ou "Aucune"]
"""

# =====================================================================
# 10. ORCHESTRATION PRINCIPALE
# =====================================================================

def run_bot_autonome():
    debut      = time.time()
    maintenant = datetime.now(ZoneInfo("Europe/Paris"))
    date       = maintenant.strftime("%d/%m/%Y")
    heure      = maintenant.strftime("%H:%M")

    if DRY_RUN:
        logging.info("=" * 60)
        logging.info("MODE DRY-RUN — aucun envoi réel.")
        logging.info("=" * 60)

    # --- ÉTAPE 1 : Gemini collecte les données ---
    cotes = recuperer_cotes_tennis()
    donnees_json = collecter_donnees_tennis(date, heure, cotes)

    # Vérification rapide : si Gemini n'a trouvé aucun match, on s'arrête
    try:
        donnees = json.loads(donnees_json)
        if not donnees.get("matchs"):
            logging.info(f"Aucun match trouvé par Gemini — session annulée. ({donnees.get('avertissements', '')})")
            return
    except Exception:
        pass  # On laisse Claude gérer les données même partielles

    # --- ÉTAPE 2 : Claude analyse les données ---
    prompt = construire_prompt_claude(date, heure, donnees_json)

    try:
        logging.info(f"Claude ({CLAUDE_MODEL}) analyse les données — {date} {heure}")

        reponse = claude_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=prompt,
            # Pas de web_search — Claude analyse uniquement les données de Gemini
            messages=[{
                "role": "user",
                "content": (
                    f"Analyse les données collectées et propose les meilleurs paris "
                    f"(max {MAX_TICKETS}) pour la session {date} {heure}."
                ),
            }],
        )

        texte = "\n".join(
            b.text for b in reponse.content if hasattr(b, "text") and b.text
        ).strip()

        logging.info(
            f"Claude OK ({len(texte)} chars) — "
            f"{reponse.usage.input_tokens} tokens in / {reponse.usage.output_tokens} out"
        )

        if "AUCUN_MATCH" in texte:
            logging.info("Claude : aucune value trouvée — session annulée proprement.")
            return
        if len(texte) <= 20:
            logging.info("Réponse Claude trop courte — aucun ticket émis.")
            return

        # --- ÉTAPE 3 : Envoi Telegram + sauvegarde ---
        tickets_bruts = [t.strip() for t in texte.split(TICKET_SEP) if len(t.strip()) > 20]
        tickets       = tickets_bruts[:MAX_TICKETS]

        if len(tickets_bruts) > MAX_TICKETS:
            logging.warning(f"Claude a généré {len(tickets_bruts)} tickets — tronqué à {MAX_TICKETS}.")
        if not tickets:
            logging.warning("Aucun ticket valide extrait.")
            return

        historique, hist_sha = charger_historique()
        hashes_connus   = set(historique)
        nouveaux_hashes = []
        paris_envoyes   = 0
        stats_cached    = charger_stats()

        for i, ticket in enumerate(tickets, 1):
            h = _hash_ticket(ticket)
            if h in hashes_connus:
                logging.warning(f"Ticket {i} : doublon — ignoré.")
                continue
            logging.info(f"Envoi ticket {i}/{len(tickets)}…")
            if envoyer_sur_telegram(ticket, stats=stats_cached):
                sauvegarder_pari_pour_suivi({"pari": ticket, "date": date})
                hashes_connus.add(h)
                nouveaux_hashes.append(h)
                paris_envoyes += 1
                if i < len(tickets):
                    time.sleep(1)

        if nouveaux_hashes and not DRY_RUN:
            sauvegarder_historique(list(hashes_connus), hist_sha)

        logging.info(
            f"✅ {paris_envoyes} ticket(s) envoyé(s)."
            if paris_envoyes else "Aucun ticket envoyé (doublons ou erreurs)."
        )

    except Exception as e:
        logging.error(f"Erreur critique Claude : {e}", exc_info=True)
        _alerter_telegram_erreur(f"bot.py a planté : {e}")
    finally:
        logging.info(f"Terminé en {time.time() - debut:.1f}s.")

# =====================================================================
# 11. POINT D'ENTRÉE CLI
# =====================================================================

if __name__ == "__main__":
    args         = sys.argv[1:]
    args_filtres = [a for a in args if a != "--dry-run"]

    if not args_filtres:
        run_bot_autonome()
    elif args_filtres[0] == "resultat" and len(args_filtres) == 2:
        flag = args_filtres[1].lower()
        if flag in ("v", "victoire", "win", "1"):
            enregistrer_resultat(victoire=True)
        elif flag in ("d", "defaite", "lose", "0"):
            enregistrer_resultat(victoire=False)
        else:
            print(f"❌ Argument inconnu : '{flag}'. Utilise 'v' ou 'd'.")
            sys.exit(1)
    elif args_filtres[0] in ("--help", "-h", "help"):
        print(__doc__)
    else:
        print(f"❌ Commande inconnue : {' '.join(args_filtres)}")
        print(__doc__)
        sys.exit(1)
