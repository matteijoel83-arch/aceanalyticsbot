import os
import sys
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
    sys.exit(1)

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

# =====================================================================
# 🧠 EXÉCUTION DU BOT EN AUTONOMIE TOTALE (RECHERCHE + TRIANGULATION)
# =====================================================================
def run_bot_autonome():
    date_du_jour = datetime.now().strftime("%d/%m/%Y")
    print(f"🚀 Démarrage du Bot en autonomie totale pour la journée du {date_du_jour}...")
    
    # Consignes globales de l'Agent incluant la recherche de calendrier ET la triangulation
    instructions_agent = f"""
    Tu es un Agent IA 100% autonome spécialisé dans les paris sportifs sur le tennis (Stratégie des Branches Brisées).
    Aujourd'hui nous sommes le {date_du_jour}. Tu dois gérer l'intégralité du processus de sélection.

    ÉTAPE 1 : RECHERCHE DU CALENDRIER DU JOUR
    - Utilise Google Search pour trouver la liste des vrais matchs de tennis ATP et WTA programmés aujourd'hui ({date_du_jour}).
    - Ignore TOUS les matchs d'exhibition, les tournois secondaires Challenger / Futures, ou les matchs amicaux (Filtre anti-piège).

    ÉTAPE 2 : PROTOCOLE DE TRIPLE VÉRIFICATION & TRIANGULATION
    Pour chaque match majeur trouvé :
    1. Source 1 (Calendrier) : Identifie les joueurs et le tournoi.
    2. Source 2 (ATP/WTA Officiel) : Vérifie la surface exacte (Dur, Terre Battue, Gazon) et assure-toi qu'aucune alerte météo ou abandon de dernière minute n'est signalé.
    3. Source 3 (Winamax) : Utilise Google Search pour valider que le match est bien ouvert aux paris sur Winamax et note la cote en direct pour le vainqueur ou les marchés annexes (Over sets). Si le match n'est pas coté sur Winamax, élimine-le.

    ÉTAPE 3 : ANALYSE FUSIONNÉE (ARBRE DE PROBABILITÉS + VALUE)
    Pour les matchs ayant validé la Triple Vérification :
    - Cherche sur TennisAbstract ou Flashscore le % de points gagnés au SERVICE et au RETOUR des deux joueurs sur la surface spécifique du jour.
    - Analyse la forme récente (10 derniers matchs), la fatigue et l'historique direct (H2H).
    - Construis l'ARBRE DE PROBABILITÉS (Modèle de Markov) :
      * P(Point Serve A) = (% Serve A + (100% - % Return B)) / 2
      * Applique un malus de 3% à 5% sur le serveur en cas de fatigue ou d'historique mental défavorable.
      * Simule les trajectoires pour obtenir la Probabilité Finale (P) du scénario le plus sûr (Victoire, Score exact 2-1, ou Plus de 2.5 sets).
    - RÈGLE D'OR DE LA VALUE : Calcule la Cote Juste (1 / Probabilité). Valide le pronostic UNIQUEMENT si la cote réelle de Winamax est SUPÉRIEURE à ta Cote Juste d'au moins 10%.

    LIMITES ET FORMAT DE SORTIE :
    - Tu ne peux publier au MAXIMUM que 3 tickets pour l'ensemble de la journée.
    - Si aucun match ne présente de Value, réponds STRICTEMENT avec le mot : PAS_DE_VALUE

    Si un match est validé, rédige le ticket au format Telegram exact suivant :

    🔴 **PRONOSTIC WINAMAX** 🔴

    🏟 **MATCH :** [Joueur A] vs [Joueur B]
    🏆 **COMPÉTITION :** [Tournoi + Surface]
    ✅ **PRONO :** [ex: Score Exact : Joueur A gagne 2-1 OU Nombre total de sets : 3]

    📈 **COTE :** [La vraie cote trouvée sur Winamax]
    💰 **MISE CONSEILLÉE :** 2.0% de votre bankroll

    📊 **ARBRE DE PROBABILITÉS & VALUE :**
    * 🎾 Efficacité Surface : [Détail bref des % Serve/Return collectés]
    * 🔄 Dynamique & H2H : [Ajustement appliqué dans l'arbre]
    * 🎲 Probabilité estimée : [X]%
    * 🧮 Cote Juste calculée : [Cote calculée 1/P] 
    * 💸 Statut : VALUE DÉTECTÉE (Cote Winamax > Cote Juste)

    📌 **ANALYSE :**
    [Une phrase courte de maximum 25 mots expliquant pourquoi l'arbre mathématique valide la value].
    """

    try:
        # Lancement de l'agent 100% dynamique avec recherche Google active
        reponse = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Trouve les vrais matchs de tennis du {date_du_jour}, applique la triple vérification, calcule l'arbre de probabilités et sors maximum 3 valeurs.",
            config=types.GenerateContentConfig(
                system_instruction=instructions_agent,
                tools=[{"google_search": {}}],
                temperature=0.1,
            ),
        )
        
        texte_final = reponse.text.strip()
        
        if "PAS_DE_VALUE" in texte_final or len(texte_final) < 20:
            print("📅 Analyse terminée : Aucun match réel ne présentait de value aujourd'hui.")
            envoyer_sur_telegram("🤖 *Bot Tennis* : Analyse du jour effectuée. L'arbre de probabilités n'a détecté aucune Value aujourd'hui sur les matchs officiels.")
        else:
            print("✅ Value trouvée sur de vrais matchs ! Envoi du ticket sur Telegram.")
            envoyer_sur_telegram(texte_final)
            
    except Exception as e:
        print(f"❌ Erreur lors de l'exécution de l'IA : {e}")

if __name__ == "__main__":
    run_bot_autonome()
