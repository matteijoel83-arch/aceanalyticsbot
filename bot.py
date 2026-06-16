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
    if s.get("version", 0) < 1:
        s["version"] = STATS_VERSION
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
    texte = texte.replace('\\"', '"')
    texte = re.sub(r'\\(?![nrt"\'\\])', '', texte)
    texte = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', texte, flags=re.DOTALL)
    texte = re.sub(r'<(?!/?(b|i|u|s|code|pre|a)(\s[^>]*)?>)[^>]+>', '', texte)
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


def _envoyer_notification_sans_ticket(raison):
    stats = charger_stats()
    msg   = (
        f"📊 <b>ACEANALYTICS — Analyse du jour</b>\n\n{raison}\n\n"
        f"✅ V: {stats['victoires']} | ❌ D: {stats['defaites']} | "
        f"📈 Win Rate : {calculer_winrate(stats):.1f}%"
    )
    if DRY_RUN:
        logging.info(f"[DRY-RUN] Notification sans ticket")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHANNEL_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        logging.info("Notification 'sans ticket' envoyée.")
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
                        "joueur1": n1.strip(),
                        "joueur2": n2.strip(),
                        "heure":   h,
                        "tournoi": f"{nom_trn} — {round_n}".strip(" —"),
                        "surface": surface,
                        "odd1":    m.get("odd1"),
                        "odd2":    m.get("odd2"),
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

def fusionner_calendrier(odds_matchs, rapid_matchs):
    lignes = ["📋 CALENDRIER TENNIS PRÉ-COLLECTÉ :\n"]
    odds_index = {}
    for cle, m in odds_matchs.items():
        odds_index[m["joueur1"].lower()] = m
        odds_index[m["joueur2"].lower()] = m

    affiches = set()

    for m in rapid_matchs:
        j1, j2 = m["joueur1"], m["joueur2"]
        cle = f"{j1}|{j2}"
        if cle in affiches:
            continue
        affiches.add(cle)
        cote_info = ""
        for nom, data in odds_index.items():
            if j1.lower() in nom or nom in j1.lower() or j2.lower() in nom or nom in j2.lower():
                if data.get("cote_j1") and data.get("cote_j2"):
                    cote_info = (f" | Cotes {data['source_cote']}: "
                                 f"{data['joueur1']} {data['cote_j1']:.2f} / "
                                 f"{data['joueur2']} {data['cote_j2']:.2f}")
                    break
        lignes.append(f"• {m['heure']} | {j1} vs {j2} | {m['tournoi']}{cote_info}")

    for cle, m in odds_matchs.items():
        j1, j2 = m["joueur1"], m["joueur2"]
        if f"{j1}|{j2}" not in affiches:
            affiches.add(f"{j1}|{j2}")
            cote_info = ""
            if m.get("cote_j1") and m.get("cote_j2"):
                cote_info = (f" | Cotes {m['source_cote']}: "
                             f"{j1} {m['cote_j1']:.2f} / {j2} {m['cote_j2']:.2f}")
            lignes.append(f"• {m['heure_utc']} | {j1} vs {j2}{cote_info}")

    total = len(affiches)
    logging.info(f"Calendrier fusionné : {total} match(s) (RapidAPI + Odds API).")
    if total == 0:
        return ""
    lignes.append(f"\nTotal : {total} match(s).")
    return "\n".join(lignes)

# =====================================================================
# 10. COLLECTE GEMINI (stats + H2H + contexte)
# =====================================================================

def collecter_donnees_tennis(date, heure, calendrier_injecte):
    bloc = (
        f"{calendrier_injecte}\n\n"
        f"→ Calendrier COMPLET. Ne pas le re-vérifier.\n"
        f"→ Filtrer les matchs commençant APRÈS {heure} (heure France).\n"
        f"→ Pour cotes manquantes : sportytrader.com ou compare-bet.fr."
        if calendrier_injecte else
        f"Cherche les matchs du {date} après {heure} sur flashscore.fr et atptour.com."
    )

    prompt = f"""
Tu es un agent de collecte tennis. Date : {date}. Heure : {heure} France.

MISSION : Enrichir les données avec stats, H2H et contexte.
Tu NE cherches PAS le calendrier — il est fourni ci-dessous.
Tes 10 requêtes Google : UNIQUEMENT stats, H2H, blessures, contexte.

{bloc}

RECHERCHES (max 10 requêtes) :
1. Forme J1 et J2 (5 derniers matchs) + Hold% — 1 requête/match sur flashscore.fr
2. H2H global et par surface — flashscore.fr ou atptour.com
3. Charge physique — heures jouées 72h, titre récent, matchs enchaînés
4. Blessures/forfaits (1 requête globale) — eurosport.fr ou tennis.com

PRIORITÉ : matchs avec cotes disponibles en premier.
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
        rep   = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.1,
            ),
        )
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

def construire_prompt_claude(date, heure, donnees_json):
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
• absence_recente > 2 mois → skip
• alertes_physiques → marchés de jeux interdits + mise 0.5%
• Retour 3-8 semaines → marchés alternatifs + mise 0.5%
• Qualifications ou hors tableau principal → skip
• Cote non trouvée → analyser + mise 0.5% + "non vérifiée"

CALIBRATION PROBABILITÉS :
• Cote < 1.50  → MAX 75%
• 1.50-1.80    → MAX 68%
• 1.80-2.20    → MAX 58%
• > 2.20       → MAX 52%

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

MISES : Simple ÉLEVÉE 2% · Modérée 1% · Combiné 1% · Non vérifiée 0.5%

FORMAT (max {MAX_TICKETS} tickets, [SEPARATEUR] entre chaque) :
HTML uniquement <b>texte</b>. JAMAIS **texte**. POURQUOI max 60 mots.

🔴 <b>PRONOSTIC [SIMPLE/COMBINÉ]</b> 🔴
🏟 <b>MATCHS :</b> [A vs B]
🏆 <b>COMPÉTITION :</b> [Tournoi]
⏰ <b>HEURE :</b> [Heure]
✅ <b>PRONO :</b> [Pronostic]
📈 <b>COTE :</b> [Cote ou non vérifiée]
💰 <b>MISE :</b> [% Kelly]
🛡 <b>CONFIANCE :</b> [ÉLEVÉE/MODÉRÉE]
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

    heure_utc_min = (maintenant.astimezone(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")

    odds_matchs        = precollecte_odds_api(heure_utc_min)
    rapid_matchs       = precollecte_rapidapi_tennis(date)
    calendrier_injecte = fusionner_calendrier(odds_matchs, rapid_matchs)
    donnees_json       = collecter_donnees_tennis(date, heure, calendrier_injecte)

    try:
        donnees = json.loads(donnees_json)
        if not donnees.get("matchs"):
            session = "matin" if heure < "14:00" else "après-midi"
            _envoyer_notification_sans_ticket(
                f"🔍 Aucun match à venir pour la session {session}.\n"
                f"Le bot reprendra à la prochaine session."
            )
            return
    except Exception:
        pass

    prompt = construire_prompt_claude(date, heure, donnees_json)

    try:
        logging.info(f"Claude analyse — {date} {heure}")
        rep = claude_client.messages.create(
            model=CLAUDE_MODEL, max_tokens=4096, system=prompt,
            messages=[{"role": "user", "content":
                f"Analyse et propose les meilleurs paris (max {MAX_TICKETS}) — {date} {heure}."}],
        )
        texte = "\n".join(b.text for b in rep.content if hasattr(b, "text") and b.text).strip()
        logging.info(f"Claude OK ({len(texte)} chars) — {rep.usage.input_tokens} in / {rep.usage.output_tokens} out")

        if "AUCUN_MATCH" in texte:
            session = "matin" if heure < "14:00" else "après-midi"
            _envoyer_notification_sans_ticket(
                f"🔎 Session {session} — {len(json.loads(donnees_json).get('matchs', []))} match(s) analysé(s).\n"
                f"Aucune value suffisante. On passe notre chemin. 💼"
            )
            return
        if len(texte) <= 20:
            return

        tickets = [t.strip() for t in texte.split(TICKET_SEP) if len(t.strip()) > 20][:MAX_TICKETS]
        if not tickets:
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
                sauvegarder_pari_pour_suivi({"pari": ticket, "date": date})
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
