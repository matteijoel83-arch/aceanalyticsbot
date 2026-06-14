"""
╔══════════════════════════════════════════════════════════════════════╗
║          BOT TENNIS ACEANALYTICS — bot.py v5.0                      ║
║  Modèle  : Claude Sonnet 4.6 + web_search + The Odds API            ║
║                                                                      ║
║  Secrets GitHub requis :                                             ║
║    ANTHROPIC_API_KEY  · TELEGRAM_BOT_TOKEN · TELEGRAM_CHANNEL_ID    ║
║    GITHUB_TOKEN       · GITHUB_REPO                                  ║
║  Secret optionnel :                                                  ║
║    ODDS_API_KEY  (https://the-odds-api.com — gratuit 500 req/mois)  ║
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
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
GITHUB_TOKEN        = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO         = os.environ.get("GITHUB_REPO")
ODDS_API_KEY        = os.environ.get("ODDS_API_KEY")  # Optionnel

MISSING = [k for k, v in {
    "ANTHROPIC_API_KEY":   ANTHROPIC_API_KEY,
    "TELEGRAM_BOT_TOKEN":  TELEGRAM_BOT_TOKEN,
    "TELEGRAM_CHANNEL_ID": TELEGRAM_CHANNEL_ID,
    "GITHUB_TOKEN":        GITHUB_TOKEN,
    "GITHUB_REPO":         GITHUB_REPO,
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

STATS_VERSION = 1
STATS_DEFAUT  = {"version": STATS_VERSION, "victoires": 0, "defaites": 0}
TICKET_SEP    = "[SEPARATEUR]"
MAX_TICKETS   = 3

# =====================================================================
# 2. COUCHE GITHUB — lecture/écriture atomique, sans Git subprocess
# =====================================================================

def _gh_get(path: str) -> tuple:
    """Retourne (contenu_dict_ou_list, sha) ou (None, None) si absent."""
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
    """Crée ou met à jour un fichier JSON — gère le conflit 409 (SHA périmé)."""
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
                logging.warning(f"GitHub 409 sur '{path}' — re-fetch SHA.")
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


def envoyer_sur_telegram(message: str, retries: int = 3) -> bool:
    s   = charger_stats()
    sig = (f"\n\n📊 <b>BILAN ACEANALYTICS</b>\n"
           f"✅ V: {s['victoires']} | ❌ D: {s['defaites']}\n"
           f"📈 <b>Win Rate : {calculer_winrate(s):.1f}%</b>")
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
    # Alerte fallback en DM si disponible (canal différent)
    _alerter_telegram_erreur("❌ bot.py : échec envoi ticket après tous les retries.")
    return False


def _alerter_telegram_erreur(msg: str):
    """Tente d'envoyer une alerte d'erreur sur le même canal (best-effort)."""
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
        logging.info("ODDS_API_KEY absente — mode dégradé (pas d'injection de cotes).")
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
        lignes = ["📋 COTES TEMPS RÉEL (The Odds API / EU) :"]
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
                lignes.append(f"  • {heure} UTC | {j1} ({c1:.2f}) vs {j2} ({c2:.2f})")
        logging.info(f"Cotes OK : {len(matchs)} matchs. Quota restant : {r.headers.get('x-requests-remaining', '?')}")
        return "\n".join(lignes)
    except Exception as e:
        logging.warning(f"Odds API indisponible : {e}")
        return ""

# =====================================================================
# 8. PROMPT SYSTÈME v4
# =====================================================================

def construire_prompt(date: str, heure: str, cotes: str = "") -> str:
    bloc_cotes = (
        f"COTES CERTIFIÉES INJECTÉES (NE PAS rechercher ailleurs) :\n{cotes}\n"
        f"→ Référence officielle. Si match absent de la liste, chercher via web_search.\n"
        f"→ Ne jamais inventer une cote."
        if cotes else
        "AVERTISSEMENT : Aucune cote certifiée disponible.\n"
        "→ Recherche via web_search (Winamax, Sportytrader).\n"
        "→ Cote non trouvée = indiquer 'non vérifiée' + mise plafonnée 0.5%.\n"
        "→ Ne jamais inventer une cote."
    )
    return f"""
Tu es l'assistant personnel d'un parieur expert en tennis.
Analyse les matchs ATP/WTA du {date}. Il est {heure} heure de France.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SESSIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• AVANT 14h00 → SESSION MATIN (mi-journée / début après-midi)
• APRÈS 14h00 → SESSION APRÈS-MIDI (fin après-midi / soirée / nuit)
• Maximum {MAX_TICKETS} tickets. Zéro ticket si aucune value réelle.
• Matchs déjà commencés ou terminés à {heure} → EXCLUS immédiatement.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SÉPARATION MULTI-TICKETS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Délimiteur OBLIGATOIRE entre chaque ticket (ligne isolée) : [SEPARATEUR]
• N'utilise JAMAIS ce délimiteur ailleurs.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOURCES (classées par fiabilité)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TIER 1 — Calendriers : atptour.com | wtatennis.com | flashscore.fr
TIER 2 — Stats : sofascore.com/fr | flashscore.fr
  ⚠ Hold% et % breaks non disponibles = OMIS, jamais inventés.
TIER 3 — Cotes :
{bloc_cotes}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ANALYSE EN 2 ÉTAPES (anti-biais de confirmation)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ÉTAPE 1 — ANALYSE BRUTE (aucun pronostic à ce stade) :
  Lister facteurs POUR / CONTRE sur 5 axes :
  1. Surface & conditions (Terre/Dur/Gazon, CPI si trouvé, indoor/outdoor, altitude)
  2. Forme & charge physique (10 derniers matchs, heures de jeu 72h, trajets, décalage)
  3. Contexte & psychologie (points à défendre, Grand Chelem dans 7j → vigilance max)
  4. Stats avancées : Hold% et % breaks UNIQUEMENT si trouvés. Hold% >83% des deux côtés
     + historique Tie-breaks → marchés de jeux (Over/Under).
  5. H2H & tactique (style de jeu, gauchers → bilan adversaire face aux gauchers)

ÉTAPE 2 — PROBABILITÉ & DÉCISION (après liste complète des facteurs) :
  Sur base EXCLUSIVE de l'Étape 1 :
  → Probabilité en % → Calcul Value → Conclusion.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROTOCOLE VALUE (obligatoire pour chaque match)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Probabilité estimée → ex: 67%
2. Cote Juste = 1 / (prob/100) → ex: 1.49
3. Kelly quart = ((prob × cote - 1) / (cote - 1)) × 0.25
   → Arrondi à 0.5%, plafonné selon matrice.
4. Cote réelle > Cote Juste + 0.10 → VALUE ✅ → ticket validé
   Cote réelle ≤ Cote Juste + 0.10 → PAS de value ❌ → abandonné
• Aucun match ne passe → répondre STRICTEMENT : AUCUN_MATCH

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ORIENTATION DES MARCHÉS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• ÉLEVÉE  → Moneyline (si value nette) sinon Handicap Jeux ou Victoire 2-0.
• MODÉRÉE → Moneyline INTERDIT. Marchés alternatifs :
  - Match serré (Holds élevés)         → +21.5/22.5 jeux | WTA : +2.5 sets
  - Match à sens unique                → -20.5/19.5 jeux | 2-0 Score Exact
  - Favori prenable / Outsider fragile → Handicap Jeux +4.5 outsider
• Signaux physiques récents (soins, crampes, déclarations 48h) → INTERDIT marchés de jeux.
  Moneyline uniquement si ÉLEVÉE, sinon passe.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MATRICE BANKROLL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Mise = MIN(Kelly quart, plafond) :
• Simple   + ÉLEVÉE  → 2%   | Simple   + MODÉRÉE → 1%
• Combiné  + ÉLEVÉE  → 1%   | Combiné  + MODÉRÉE → INTERDIT
• Cote non vérifiée           → 0.5% sans exception

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FILTRE BLESSURE (2 niveaux)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NIVEAU 1 — Élimination totale :
  • Retour blessure > 2 mois | Doute public participation 48h
NIVEAU 2 — Vigilance renforcée (marchés alternatifs + mise 0.5%) :
  • Soins médicaux dernier match | Match >3h dans les 24h | Retour 3-8 semaines

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ANTI-HALLUCINATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Aucun match trouvé → AUCUN_MATCH (strict, aucun autre texte)
• Aucune rencontre fictive ni extrapolation depuis connaissances passées
• Stat non trouvée → omise, jamais estimée

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FORMAT TICKET (respecter exactement)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔴 <b>PRONOSTIC [SIMPLE OU COMBINÉ]</b> 🔴
🏟 <b>MATCHS :</b> [Joueur A vs Joueur B]
🏆 <b>COMPÉTITION :</b> [Tournoi]
⏰ <b>HEURE :</b> [Heure exacte]
✅ <b>PRONO :</b> [Pronostic précis]
📈 <b>COTE :</b> [Cote réelle — "non vérifiée" si incertaine]
💰 <b>MISE :</b> [% Kelly quart plafonné]
🛡 <b>CONFIANCE :</b> [ÉLEVÉE / MODÉRÉE]
🧮 <b>VALUE :</b> [Proba X% → Cote juste Y.YY → Cote réelle Z.ZZ → Kelly W%]
📌 <b>POURQUOI ?</b> [Max 150 mots — facteurs clés, pas de transition]
⚠️ <b>DONNÉES MANQUANTES :</b> [Stats non trouvées, ou "Aucune"]
"""

# =====================================================================
# 9. ORCHESTRATION PRINCIPALE
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

    cotes  = recuperer_cotes_tennis()
    prompt = construire_prompt(date, heure, cotes)

    try:
        logging.info(f"Analyse Claude ({CLAUDE_MODEL}) — {date} {heure}")

        reponse = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=prompt,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{
                "role": "user",
                "content": (
                    f"Recherche et analyse les matchs ATP/WTA du {date}. "
                    f"Il est {heure} heure de France. "
                    f"Propose les meilleurs paris (max {MAX_TICKETS}) pour cette session."
                ),
            }],
        )

        texte = "\n".join(
            b.text for b in reponse.content if hasattr(b, "text") and b.text
        ).strip()

        logging.info(
            f"Réponse reçue ({len(texte)} chars) — "
            f"{reponse.usage.input_tokens} tokens in / {reponse.usage.output_tokens} out"
        )

        if "AUCUN_MATCH" in texte:
            logging.info("Aucun match disponible — session annulée proprement.")
            return
        if len(texte) <= 20:
            logging.info("Réponse trop courte — aucun ticket émis.")
            return

        tickets_bruts = [t.strip() for t in texte.split(TICKET_SEP) if len(t.strip()) > 20]
        tickets       = tickets_bruts[:MAX_TICKETS]

        if len(tickets_bruts) > MAX_TICKETS:
            logging.warning(f"Claude a généré {len(tickets_bruts)} tickets — tronqué à {MAX_TICKETS}.")
        if not tickets:
            logging.warning("Aucun ticket valide extrait.")
            return

        historique, hist_sha = charger_historique()
        hashes_connus  = set(historique)
        nouveaux_hashes = []
        paris_envoyes   = 0

        for i, ticket in enumerate(tickets, 1):
            h = _hash_ticket(ticket)
            if h in hashes_connus:
                logging.warning(f"Ticket {i} : doublon — ignoré.")
                continue
            logging.info(f"Envoi ticket {i}/{len(tickets)}…")
            if envoyer_sur_telegram(ticket):
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
        logging.error(f"Erreur critique : {e}", exc_info=True)
        _alerter_telegram_erreur(f"bot.py a planté : {e}")
    finally:
        logging.info(f"Terminé en {time.time() - debut:.1f}s.")

# =====================================================================
# 10. POINT D'ENTRÉE CLI
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
