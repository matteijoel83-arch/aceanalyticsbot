import os
import sys
import json
import requests
from datetime import datetime
from google import genai
from google.genai import types

# =====================================================================
# ⚙️ CONFIGURATION
# =====================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")

if not GEMINI_API_KEY or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
    print("❌ Erreur : Les clés secrètes ne sont pas configurées.")
    sys.exit(1)

os.environ["GEMINI_API_KEY"] = GEMINI_API_KEY
client = genai.Client()

def envoyer_sur_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHANNEL_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"❌ Erreur Telegram : {e}")

# =====================================================================
# 🧠 EXÉCUTION DU BOT AVEC RECHERCHE SPORTYTRADER
# =====================================================================
def run_bot_autonome():
    date_du_jour = datetime.now().strftime("%d/%m/%Y")
    
    instructions_agent = f"""
    Tu es un Agent IA expert en paris sportifs (Stratégie des Branches Brisées).
    Aujourd'hui nous sommes le {date_du_jour}.

    MÉTHODOLOGIE DE RÉCUPÉRATION DES COTES (RÈGLE ABSOLUE) :
    1. Identifie les matchs de tennis majeurs du jour.
    2. Pour chaque match, effectue une recherche Google : "Sportytrader cote [Joueur A] vs [Joueur B] Winamax".
    3. Entre dans le résultat SportyTrader pour extraire la VRAIE cote WINAMAX actuelle. 
       - Si la cote est indisponible ou non lisible, n'invente rien. Écris "Indisponible" et ignore le match.
    4. Calcule l'arbre de probabilités basé sur les stats réelles du jour, puis détermine la Value (1/P < Cote Winamax).
    5. Rédige tout en FRANÇAIS. Aucun mot d'anglais. Aucune justification technique.

    FORMAT DE SORTIE HTML (Utilise <b> et <i>) :
    🔴 <b>PRONOSTIC WINAMAX</b> 🔴
    🏟 <b>MATCH :</b> [Joueur A] vs [Joueur B]
    🏆 <b>COMPÉTITION :</b> [Tournoi]
    ✅ <b>PRONO :</b> [Pronostic]
    📈 <b>COTE :</b> [Cote réelle extraite de Sportytrader]
    💰 <b>MISE CONSEILLÉE :</b> 2.0%
    📊 <b>ARBRE DE PROBABILITÉS :</b>
    • Cote Juste calculée : [Cote]
    • Statut : VALUE DETECTEE
    📌 <b>ANALYSE :</b> [Phrase courte]
    """

    try:
        reponse = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Trouve les matchs, récupère les cotes réelles sur Sportytrader, calcule la value et génère les tickets.",
            config=types.GenerateContentConfig(
                system_instruction=instructions_agent,
                tools=[{"google_search": {}}],
                temperature=0.1,
            ),
        )
        
        texte_final = reponse.text.strip()
        
        if "PAS_DE_VALUE" in texte_final or len(texte_final) < 20:
            print("📅 Analyse : Aucune value réelle trouvée.")
        else:
            envoyer_sur_telegram(texte_final)
            print("✅ Ticket envoyé avec cotes réelles.")
            
    except Exception as e:
        print(f"❌ Erreur : {e}")

if __name__ == "__main__":
    run_bot_autonome()
