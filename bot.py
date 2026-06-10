import os
import sys
import requests
import json
import subprocess
import logging
from datetime import datetime, timezone, timedelta
from google import genai
from google.genai import types

# =====================================================================
# ⚙️ CONFIGURATION, SÉCURITÉ & LOGS
# =====================================================================
# Configuration du logging : écrit dans "bot.log" et affiche dans la console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
STATS_FILE = "stats.json"

if not GEMINI_API_KEY or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
    logging.critical("Erreur : Clés secrètes manquantes.")
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
        except Exception as e:
            logging.warning(f"Impossible de lire les stats, réinitialisation : {e}")
            return {"victoires": 0, "defaites": 0}
    return {"victoires": 0, "defaites": 0}

def calculer_winrate(stats):
    total = stats["victoires"] + stats["defaites"]
    return (stats["victoires"] / total * 100) if total > 0 else 0.0

def enregistrer_resultat(victoire: bool):
    stats = charger_stats()
    if victoire: 
        stats["victoires"] += 1
    else: 
        stats["defaites"] += 1
        
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f)
        
    try:
        subprocess.run(["git", "config", "--global", "user.name", "bot-stats"], check=True)
        subprocess.run(["git", "config", "--global", "user.email", "bot@github.com"], check=True)
        subprocess.run(["git", "add", STATS_FILE], check=True)
        subprocess.run(["git", "commit", "-m", "Maj stats"], check=True)
        subprocess.run(["git", "push"], check=True)
        logging.info("Statistiques synchronisées avec succès sur Git.")
    except subprocess.CalledProcessError as e:
        logging.error(f"Erreur lors de la synchronisation Git : {e}")

def est_deja_envoye(nouveau_pari: str):
    if not os.path.exists("historique.json"):
        return False
    try:
        with open("historique.json", "r") as f:
            historique = json.load(f)
            for ancien_pari in historique:
                if ancien_pari in nouveau_pari or nouveau_pari in ancien_pari:
                    return True
    except Exception as e:
        logging.error(f"Erreur lors de la lecture de l'historique : {e}")
        return False
    return False

def sauvegarder_dans_historique(pari: str):
    historique = []
    if os.path.exists("historique.json"):
        try:
            with open("historique.json", "r") as f:
                historique = json.load(f)
        except:
            historique = []
    historique.append(pari)
    with open("historique.json", "w") as f:
        json.dump(historique[-10:], f)

def envoyer_sur_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    stats = charger_stats()
    wr = calculer_winrate(stats)
    
    annee_actuelle = datetime.now().strftime("%Y")
    message_propre = message.replace("2024", annee_actuelle)
    
    signature = f"\n\n📊 <b>BILAN ACEANALYTICS</b>\n✅ V: {stats['victoires']} | ❌ D: {stats['defaites']}\n📈 <b>Win Rate : {wr:.1f}%</b>"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID, 
        "text": message_propre + signature, 
        "parse_mode": "HTML"
    }
    
    try:
        # Timeout de 10 secondes pour éviter que le script ne reste bloqué
        response = requests.post(url, json=payload, timeout=10)
        # Lève une exception si le statut HTTP est une erreur (ex: 400, 404, 500)
        response.raise_for_status()
        logging.info("Message envoyé avec succès sur Telegram.")
    except requests.exceptions.HTTPError as http_err:
        logging.error(f"Erreur HTTP Telegram : {http_err} - Réponse du serveur : {response.text}")
    except requests.exceptions.Timeout:
        logging.error("Erreur : La requête vers Telegram a expiré (Timeout).")
    except Exception as e:
        logging.error(f"Erreur inattendue lors de l'envoi Telegram : {e}")

# =====================================================================
# 🧠 CŒUR DU BOT : ANALYSE EXPERTE
# =====================================================================
def obtenir_heure_france_exacte():
    return datetime.now(timezone.utc) + timedelta(hours=2)

def sauvegarder_pari_pour_suivi(pari_info):
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
    
    BIBLIOTHÈQUE DE SOURCES OBLIGATOIRES :
    1. Calendriers : https://www.atptour.com/, https://www.wtatennis.com/, https://www.flashscore.fr/
    2. Cotes : https://www.winamax.fr/paris-sportifs, https://www.sportytrader.com/
    3. Stats : https://www.sofascore.com/fr/, https://www.flashscore.fr/

    PROTOCOLE D'ANALYSE EXPERT (Les 5 Points Cruciaux) :
    1. SURFACE (Performance spécifique Terre/Dur/Gazon).
    2. H2H (Style de jeu, blocages tactiques).
    3. FORME PHYSIQUE (10 derniers matchs, fatigue).
    4. STATS SERVICE/RETOUR (% balles sauvées).
    5. CONTEXTE/ENJEU (Préparation tournoi majeur, points à défendre).

    PROTOCOLE DE SÉCURITÉ (DOUBLE TRIPLE VÉRIFICATION) :
    - Croise les sources (ATP, Winamax, Flashscore).
    - Force la lecture de la cote réelle.
    
    FORMAT TICKET :
    🔴 <b>PRONOSTIC [SIMPLE OU COMBINÉ]</b> 🔴
    🏟 <b>MATCHS :</b> [Match 1] vs [Match 2]
    🏆 <b>COMPÉTITION :</b> [Tournoi]
    ⏰ <b>HEURE :</b> [Heure]
    ✅ <b>PRONO :</b> [Pronostic précis]
    📈 <b>COTE :</b> [Cote réelle Winamax]
    💰 <b>MISE :</b> [2% simple / 1% combiné]
    🛡 <b>CONFIANCE :</b> [ÉLEVÉE / MODÉRÉE]
    📌 <b>POURQUOI ?</b> [Analyse courte intégrant le H2H/Forme/Surface et la justification]
    """

    try:
        logging.info("Lancement de l'analyse Gemini...")
        reponse = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Effectue la double triple vérification et propose les meilleurs paris pour le {date_du_jour}.",
            config=types.GenerateContentConfig(
                system_instruction=instructions_agent,
                tools=[{"google_search": {}}],
                temperature=0.1,
            ),
        )
        
        texte = reponse.text.strip()
        if "PAS_DE_VALUE" not in texte and len(texte) > 20:
            if not est_deja_envoye(texte):
                sauvegarder_pari_pour_suivi({"pari": texte, "date": date_du_jour})
                sauvegarder_dans_historique(texte)
                envoyer_sur_telegram(texte)
            else:
                logging.warning("Doublon détecté, pari déjà proposé ce jour.")
        else:
            logging.info("Aucune opportunité de value-bet trouvée aujourd'hui.")
    except Exception as e:
        logging.error(f"Erreur lors de l'exécution globale du bot : {e}")

if __name__ == "__main__":
    run_bot_autonome()
