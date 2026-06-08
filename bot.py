import os
import json
import requests
from datetime import datetime
from google import genai
from google.genai import types

# =====================================================================
# ⚙️ RECUPERATION AUTOMATIQUE DES CLES DEPUIS GITHUB ACTIONS
# =====================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
# =====================================================================

# Sécurité si les clés sont manquantes
if not GEMINI_API_KEY or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
    print("❌ Erreur : Les clés secrètes ne sont pas configurées sur GitHub Settings.")
    exit(1)

# Initialisation du client Google GenAI
os.environ["GEMINI_API_KEY"] = GEMINI_API_KEY
client = genai.Client()

def envoyer_sur_telegram(message: str):
    """Envoie le ticket de pari directement sur ton canal Telegram privé."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload)
        if response.status_code != 200:
            print(f"❌ Erreur Telegram : {response.text}")
    except Exception as e:
        print(f"❌ Erreur de connexion Telegram : {e}")

def run_bot_autonome():
    date_du_jour = datetime.now().strftime("%d/%m/%Y")
    print(f"🚀 Démarrage du Bot en autonomie totale pour la journée du {date_du_jour}...")
    
    # Instructions ultra-précises pour que l'IA gère TOUT de A à Z sans inventer de données
    instructions_agent = f"""
    Tu es un Agent IA 100% autonome spécialisé dans les paris sportifs sur le tennis (Stratégie des Branches Brisées).
    Aujourd'hui nous sommes le {date_du_jour}.
    
    Ta mission :
    1. Utilise Google Search pour trouver les principaux matchs de tennis ATP ou WTA prévus aujourd'hui (ou qui vont démarrer sous peu).
    2. Pour ces matchs, cherche sur le web (TennisAbstract, Flashscore) le % de points gagnés au SERVICE et au RETOUR des deux joueurs sur la surface concernée.
    3. Cherche sur internet s'il y a des actus majeures sur ces joueurs (fatigue, historique récent H2H, alerte blessure).
    4. Calcule l'arbre de probabilités (Modèle de Markov) : 
       - P(Point Serve A) = (% Serve A + (100% - % Return B)) / 2
       - Applique un malus de 3% sur le serveur si l'actualité indique une grosse fatigue ou un retour de blessure récent.
       - Calcule la probabilité du score exact 2-1 ou du 'Plus de 2,5 sets'.
    5. Cherche la vraie cote en direct sur Winamax pour ces marchés précis.
    6. Compare : Si ta cote calculée (1 / Probabilité) est inférieure à la cote Winamax avec une marge de 10%, il y a une VALUE.
    
    Règles de filtrage strictes pour éviter les bugs :
    - Si un joueur a abandonné au tournoi précédent, IGNORE le match (Écris : PAS_DE_VALUE).
    - Si Winamax n'a pas encore sorti les cotes pour le match, IGNORE le match.
    
    Si tu trouves un match avec une vraie value, rédige LE TICKET au format exact suivant, sans aucun autre texte :

    🔴 **PRONOSTIC WINAMAX** 🔴

    🏟 **MATCH :** [Joueur A] vs [Joueur B]
    🏆 **COMPÉTITION :** [Tournoi + Surface]
    ✅ **PRONO :** [ex: Score Exact : Joueur A gagne 2-1 OU Nombre total de sets : 3]

    📈 **COTE :** [La vraie cote trouvée sur Winamax]
    💰 **MISE CONSEILLÉE :** 2.0% de votre bankroll

    📌 **ANALYSE :**
    [Une phrase courte expliquant pourquoi l'arbre de probabilités détecte une valeur (ex: fatigue du favori sur terre battue, historique H2H serré)].
    
    Si aucun match ne présente de Value aujourd'hui, écris exactement : PAS_DE_VALUE
    """

    try:
        # Lancement de l'agent avec recherche Google intégrée
        reponse = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=" Trouve les matchs de tennis du jour, analyse-les et publie uniquement s'il y a une value.",
            config=types.GenerateContentConfig(
                system_instruction=instructions_agent,
                tools=[{"google_search": {}}],
                temperature=0.1,
            ),
        )
        
        texte_final = reponse.text.strip()
        
        if "PAS_DE_VALUE" in texte_final or len(texte_final) < 20:
            print("📅 Analyse terminée : Aucun match ne correspondait aux critères de rentabilité aujourd'hui.")
            # Optionnel : Envoi d'un petit message pour te rassurer que le bot a bien tourné
            envoyer_sur_telegram("🤖 *Bot Tennis* : Analyse du jour effectuée. Aucun pari safe trouvé aujourd'hui.")
        else:
            print("✅ Value trouvée ! Envoi du ticket sur Telegram.")
            envoyer_sur_telegram(texte_final)
            
    except Exception as e:
        print(f"❌ Erreur lors de l'exécution de l'IA : {e}")

if __name__ == "__main__":
    run_bot_autonome()
