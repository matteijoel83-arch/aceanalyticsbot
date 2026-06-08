import os
import sys
import requests
from datetime import datetime
from google import genai
from google.genai import types

# =====================================================================
# ⚙️ CONFIGURATION & SÉCURITÉ
# =====================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")

if not GEMINI_API_KEY or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
    print("❌ Erreur : Configuration manquante.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

def envoyer_sur_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHANNEL_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"❌ Erreur connexion : {e}")

# =====================================================================
# 🧠 CŒUR DU BOT : ANALYSE AVEC FORCE READING & COMBINÉS
# =====================================================================
def run_bot_autonome():
    date_du_jour = datetime.now().strftime("%d/%m/%Y")
    
    instructions_agent = f"""
    Tu es l'assistant personnel d'un parieur récréatif. Analyse les matchs ATP/WTA du {date_du_jour}.
    
    RÈGLES D'ANALYSE (FORCE READING) :
    1. Pour chaque match, utilise Google Search pour trouver la page spécifique "cotes Winamax" sur Sportytrader.
    2. FORCE LA LECTURE : Extrais uniquement le chiffre de la cote Winamax affiché. Si le chiffre est ambigu, ignore le match.
    3. CALCUL : Utilise cet arbre  pour ta logique.
    
    LOGIQUE DE COMBINÉS :
    - Tu peux proposer UN combiné de 2 matchs MAXIMUM.
    - RÈGLE : Seulement si les 2 matchs ont une probabilité individuelle > 60% et que la cote globale offre une réelle Value.
    - Si combiné, la mise conseillée doit être fixée à 1.0%.
    
    RÈGLES DE SORTIE :
    - Langue : FRANÇAIS uniquement.
    - Format : HTML simple (utilise <b> et <i>).
    - Si aucun pari n'est solide, réponds uniquement : PAS_DE_VALUE
    
    FORMAT TICKET :
    🔴 <b>PRONOSTIC [SIMPLE OU COMBINÉ]</b> 🔴
    🏟 <b>MATCHS :</b> [Match 1] vs [Match 2 si combiné]
    ✅ <b>PRONO :</b> [Pronostic]
    📈 <b>COTE :</b> [Cote réelle]
    💰 <b>MISE :</b> [2.0% pour simple / 1.0% pour combiné]
    🛡 <b>CONFIANCE :</b> [ÉLEVÉE / MODÉRÉE]
    📌 <b>POURQUOI ?</b> [Une phrase simple et directe]
    """

    try:
        reponse = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Analyse les matchs du {date_du_jour}, récupère les cotes réelles par force reading, détecte les opportunités simples et combinés (max 3 tickets).",
            config=types.GenerateContentConfig(
                system_instruction=instructions_agent,
                tools=[{"google_search": {}}],
                temperature=0.1,
            ),
        )
        
        texte = reponse.text.strip()
        if "PAS_DE_VALUE" not in texte and len(texte) > 20:
            envoyer_sur_telegram(texte)
            print("✅ Analyse envoyée sur Telegram.")
        else:
            print("📅 Aucune opportunité trouvée.")
            
    except Exception as e:
        print(f"❌ Erreur : {e}")

if __name__ == "__main__":
    run_bot_autonome()
