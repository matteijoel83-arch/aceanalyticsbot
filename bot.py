import os
import sys
import requests
import json
import subprocess
from datetime import datetime, timedelta, timezone
from google import genai
from google.genai import types

# =====================================================================
# ⚙️ CONFIGURATION & SÉCURITÉ
# =====================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
STATS_FILE = "stats.json"

if not GEMINI_API_KEY or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
    print("❌ Erreur : Clés secrètes manquantes.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

# =====================================================================
# 📊 MODULE DE SUIVI
# =====================================================================
def charger_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r") as f:
                return json.load(f)
        except:
            return {"victoires": 0, "defaites": 0}
    return {"victoires": 0, "defaites": 0}

def calculer_winrate(stats):
    total = stats["victoires"] + stats["defaites"]
    return (stats["victoires"] / total * 100) if total > 0 else 0.0

def enregistrer_resultat(victoire: bool):
    stats = charger_stats()
    if victoire: stats["victoires"] += 1
    else: stats["defaites"] += 1
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f)
    subprocess.run(["git", "config", "--global", "user.name", "bot-stats"])
    subprocess.run(["git", "config", "--global", "user.email", "bot@github.com"])
    subprocess.run(["git", "add", STATS_FILE])
    subprocess.run(["git", "commit", "-m", "Maj stats"])
    subprocess.run(["git", "push"])

def envoyer_sur_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    stats = charger_stats()
    wr = calculer_winrate(stats)
    signature = f"\n\n📊 <b>BILAN ACEANALYTICS</b>\n✅ V: {stats['victoires']} | ❌ D: {stats['defaites']}\n📈 <b>Win Rate : {wr:.1f}%</b>"
    payload = {"chat_id": TELEGRAM_CHANNEL_ID, "text": message + signature, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"❌ Erreur Telegram : {e}")

# =====================================================================
# 🧠 CŒUR DU BOT : STRATÉGIE ET ANALYSE
# =====================================================================

def obtenir_heure_france_exacte():
    """Récupère l'heure en France (UTC+2) de manière conforme."""
    return datetime.now(timezone.utc) + timedelta(hours=2)

def sauvegarder_pari_pour_suivi(pari_info):
    """Ajout : Enregistre le pari en cours pour permettre la vérification ultérieure."""
    with open("pari_en_cours.json", "w") as f:
        json.dump(pari_info, f)

def run_bot_autonome():
    maintenant = obtenir_heure_france_exacte()
    date_du_jour = maintenant.strftime("%d/%m/%Y")
    heure_actuelle = maintenant.strftime("%H:%M")
    
    instructions_agent = f"""
    Tu es l'assistant personnel d'un parieur expert en tennis. Analyse les matchs ATP/WTA du {date_du_jour}. 
    Note : Il est actuellement {heure_actuelle} (heure locale France).
    
    RÈGLE DE CORRÉLATION TEMPORELLE :
    - Compare l'heure de début de chaque match avec {heure_actuelle}.
    - SI LE MATCH A DÉJÀ COMMENCÉ OU EST TERMINÉ, EXCLUS-LE DE TES ANALYSES.
    - Ne propose QUE des matchs dont le début est futur par rapport à {heure_actuelle}.

    BIBLIOTHÈQUE DE SOURCES OBLIGATOIRES (Pour la Triangulation) :
    1. Calendriers/Résultats : https://www.atptour.com/, https://www.wtatennis.com/, https://www.flashscore.fr/
    2. Cotes & Marché (Référence Winamax) : https://www.winamax.fr/paris-sportifs, https://www.sportytrader.com/
    3. Stats Avancées : https://www.sofascore.com/fr/, https://www.flashscore.fr/

    PROTOCOLE D'ANALYSE EXPERT (Les 5 Points Cruciaux) :
    1. SURFACE : Analyse la performance spécifique sur la surface du jour (Terre/Dur/Gazon).
    2. H2H (Style de jeu) : Analyse les confrontations directes et les blocages tactiques (gaucher vs droitier, frappeur vs contreur).
    3. FORME PHYSIQUE : Analyse les 10 derniers matchs (facilité, sets concédés, fatigue accumulée).
    4. STATS DE SERVICE/RETOUR : Analyse le % de points gagnés derrière la 1ère balle et le taux de balles de break sauvées.
    5. CONTEXTE/ENJEU : Analyse les points à défendre, la préparation d'un tournoi majeur à venir, et les conditions météo.

    PROTOCOLE DE SÉCURITÉ (DOUBLE TRIPLE VÉRIFICATION) :
    1. VALIDATION D'EXISTENCE : Croise les informations entre les sources (ATP/WTA, Winamax, Flashscore). Si le match n'est pas concordant, ignore-le.
    2. VALIDATION DES DONNÉES : Force la lecture de la cote réelle sur Winamax ou Sportytrader. Utilise l'arbre de décision pour calculer la probabilité réelle.

    STRATÉGIE DE SÉLECTION (CHOIX DU FORMAT & VOLUME) :
    - VOLUME MAXIMAL : 3 paris maximum par session (Matin ou Après-midi).
    - CONDITIONS DE SÉLECTION :
        a) Pari simple : Vainqueur sec, handicap (sets/jeux), ou Over/Under (total jeux/sets).
        b) Pari combiné : Uniquement si 2 matchs ont chacun une probabilité > 60%.
    - Sélectionne la structure qui offre la meilleure sécurité mathématique.

    RÈGLES DE SORTIE :
    - Langue : FRANÇAIS uniquement.
    - Format : HTML simple (utilise <b> et <i>).
    - Si aucun pari n'est solide, réponds uniquement : PAS_DE_VALUE
    
    FORMAT TICKET :
    🔴 <b>PRONOSTIC [SIMPLE OU COMBINÉ]</b> 🔴
    🏟 <b>MATCHS :</b> [Match 1] vs [Match 2 si combiné]
    🏆 <b>COMPÉTITION :</b> [Nom du tournoi]
    ⏰ <b>HEURE :</b> [Heure du match] (Vérifié futur par rapport à {heure_actuelle})
    ✅ <b>PRONO :</b> [Pronostic précis]
    📈 <b>COTE :</b> [Cote réelle Winamax]
    💰 <b>MISE :</b> [2.0% pour simple / 1.0% pour combiné]
    🛡 <b>CONFIANCE :</b> [ÉLEVÉE / MODÉRÉE]
    📌 <b>POURQUOI ?</b> [Analyse courte intégrant le H2H/Forme/Surface et la justification du type de pari choisi]
    """

    try:
        reponse = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Effectue la double triple vérification, analyse les matchs à venir après {heure_actuelle} et propose les meilleurs paris pour le {date_du_jour}.",
            config=types.GenerateContentConfig(
                system_instruction=instructions_agent,
                tools=[{"google_search": {}}],
                temperature=0.1,
            ),
        )
        
        texte = reponse.text.strip()
        if "PAS_DE_VALUE" not in texte and len(texte) > 20:
            sauvegarder_pari_pour_suivi({"pari": texte, "date": date_du_jour})
            envoyer_sur_telegram(texte)
            print("✅ Analyse finale envoyée.")
        else:
            print("📅 Aucune opportunité trouvée.")
            
    except Exception as e:
        print(f"❌ Erreur : {e}")

if __name__ == "__main__":
    run_bot_autonome()
