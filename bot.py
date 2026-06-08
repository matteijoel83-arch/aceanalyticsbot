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
    print("❌ Erreur : Clés secrètes manquantes.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

def envoyer_sur_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHANNEL_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"❌ Erreur Telegram : {e}")

# =====================================================================
# 🧠 CŒUR DU BOT : STRATÉGIE ET ANALYSE
# =====================================================================
def run_bot_autonome():
    date_du_jour = datetime.now().strftime("%d/%m/%Y")
    
    instructions_agent = f"""
    Tu es l'assistant personnel d'un parieur récréatif. Analyse les matchs ATP/WTA du {date_du_jour}.
    
    PROTOCOLE DE SÉCURITÉ (DOUBLE TRIPLE VÉRIFICATION) :
    1. VALIDATION D'EXISTENCE (Triangulation) : 
       - Vérifie la présence du match sur 3 sources : site officiel (ATP/WTA), Sportytrader et Flashscore.
       - Si le match n'est pas sur les 3, ignore-le. C'est une règle de sécurité stricte.
    2. VALIDATION DES DONNÉES (Force Reading & Arbre) :
       - Force la lecture de la cote réelle sur Sportytrader.
       - Utilise l'arbre de décision  pour calculer la probabilité réelle.
       - Si la cote est indisponible, ignore le match.
    
    LOGIQUE DE COMBINÉS :
    - Tu peux proposer UN combiné de 2 matchs MAXIMUM.
    - Seulement si les 2 matchs sont validés par les étapes ci-dessus avec une probabilité > 60% chacun.
    - Si combiné, la mise conseillée doit être fixée à 1.0%.
    
    RÈGLES DE SORTIE :
    - Langue : FRANÇAIS uniquement.
    - Format : HTML simple (utilise <b> et <i>).
    - Si aucun pari n'est solide, réponds uniquement : PAS_DE_VALUE
    
    FORMAT TICKET :
    🔴 <b>PRONOSTIC [SIMPLE OU COMBINÉ]</b> 🔴
    🏟 <b>MATCHS :</b> [Match 1] vs [Match 2 si combiné]
    ✅ <b>PRONO :</b> [Pronostic simple]
    📈 <b>COTE :</b> [Cote réelle]
    💰 <b>MISE :</b> [2.0% pour simple / 1.0% pour combiné]
    🛡 <b>CONFIANCE :</b> [ÉLEVÉE / MODÉRÉE]
    📌 <b>POURQUOI ?</b> [Une phrase simple et directe]
    """

    try:
        reponse = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Effectue la double triple vérification, analyse les matchs, propose des paris simples ou combinés pour le {date_du_jour}.",
            config=types.GenerateContentConfig(
                system_instruction=instructions_agent,
                tools=[{"google_search": {}}],
                temperature=0.1,
            ),
        )
        
        texte = reponse.text.strip()
        if "PAS_DE_VALUE" not in texte and len(texte) > 20:
            envoyer_sur_telegram(texte)
            print("✅ Analyse finale envoyée.")
        else:
            print("📅 Aucune opportunité trouvée.")
            
    except Exception as e:
        print(f"❌ Erreur : {e}")

if __name__ == "__main__":
    run_bot_autonome()
