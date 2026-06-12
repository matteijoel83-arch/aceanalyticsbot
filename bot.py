import os
import sys
import requests
import json
import subprocess
import logging
import re
from datetime import datetime, timezone, timedelta
from google import genai
from google.genai import types

# =====================================================================
# ⚙️ CONFIGURATION, SÉCURITÉ & LOGS
# =====================================================================
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
# 📊 MODULE DE SUIVI & SYNCHRONISATION GIT
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
    """Met à jour les statistiques et synchronise la suppression du pari sur GitHub"""
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
        
        # On stage la mise à jour des statistiques
        subprocess.run(["git", "add", STATS_FILE], check=True)
        
        # On indique à Git si le fichier pari_en_cours.json a été supprimé localement
        if not os.path.exists("pari_en_cours.json"):
            subprocess.run(["git", "rm", "pari_en_cours.json"], check=False)
            
        subprocess.run(["git", "commit", "-m", "🔄 Maj stats et nettoyage du pari terminé"], check=True)
        subprocess.run(["git", "push"], check=True)
        logging.info("Statistiques et nettoyage validés avec succès sur GitHub.")
    except subprocess.CalledProcessError as e:
        logging.error(f"Erreur lors de la synchronisation Git (Résultats) : {e}")

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
    texte_complet = message_propre + signature
    
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID, 
        "text": texte_complet, 
        "parse_mode": "HTML"
    }
    
    if len(texte_complet) > 4000:
        logging.warning("Message trop long détecté. Nettoyage et troncature de sécurité...")
        texte_sans_html = re.sub('<[^<]+?>', '', message_propre)
        texte_complet = texte_sans_html[:3500] + "\n\n... [Analyse tronquée car trop longue] ..." + signature
        payload = {
            "chat_id": TELEGRAM_CHANNEL_ID, 
            "text": texte_complet
        }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logging.info("Message envoyé avec succès sur Telegram.")
    except requests.exceptions.HTTPError as http_err:
        logging.error(f"Erreur HTTP Telegram : {http_err} - Réponse : {response.text}")
    except requests.exceptions.Timeout:
        logging.error("Erreur : La requête vers Telegram a expiré.")
    except Exception as e:
        logging.error(f"Erreur inattendue Telegram : {e}")

# =====================================================================
# 🧠 CŒUR DU BOT : ANALYSE EXPERTE & SESSIONS
# =====================================================================
def obtenir_heure_france_exacte():
    return datetime.now(timezone.utc) + timedelta(hours=2)

def sauvegarder_pari_pour_suivi(pari_info):
    """Ajoute le nouveau pari à la liste sans écraser les matchs en cours"""
    paris_en_attente = []
    if os.path.exists("pari_en_cours.json"):
        try:
            with open("pari_en_cours.json", "r") as f:
                contenu = json.load(f)
                if isinstance(contenu, list):
                    paris_en_attente = contenu
                else:
                    paris_en_attente = [contenu] if contenu else []
        except Exception:
            paris_en_attente = []
            
    paris_en_attente.append(pari_info)
    with open("pari_en_cours.json", "w") as f:
        json.dump(paris_en_attente, f)

def run_bot_autonome():
    maintenant = obtenir_heure_france_exacte()
    date_du_jour = maintenant.strftime("%d/%m/%Y")
    heure_actuelle = maintenant.strftime("%H:%M")
    
    instructions_agent = f"""
    Tu es l'assistant personnel d'un parieur expert en tennis. Analyse les matchs ATP/WTA du {date_du_jour}. 
    Note : Il est actuellement {heure_actuelle} (heure locale France).
    
    RÈGLE DES SESSIONS ET FLEXIBILITÉ (CRUCIAL) :
    - Détermine ta session selon l'heure actuelle :
      * Si l'heure est AVANT 14:00 : Tu analyses la SESSION MATIN (matchs de la mi-journée et du début d'après-midi).
      * Si l'heure est APRÈS 14:00 : Tu analyses la SESSION APRÈS-MIDI (matchs de fin d'après-midi, soirée et nuit).
    - Tu as carte blanche pour proposer entre 0 et 3 TICKETS MAXIMUM par session.
    - Tu choisis librement le format selon les opportunités réelles (Value-bets) : uniquement des simples, uniquement des combinés, ou un mélange des deux (ex: 2 simples et 1 combiné, 2 combinés, etc.). S'il n'y a pas d'opportunité, ne propose rien.
    
    RÈGLE DE SÉPARATION MULTI-TICKETS :
    - SI TU PROPOSES PLUSIEURS TICKETS DISTINCTS, tu dois ABSOLUMENT insérer le délimiteur exact === sur une ligne isolée entre chaque ticket. Ne mets aucun autre texte sur cette ligne de séparation.
    
    RÈGLE DE CONCISION ABSOLUE (CRUCIAL) :
    - Ton analyse de la section "POURQUOI ?" doit être ultra-courte, percutante et faire maximum 100 à 150 mots par ticket.
    - Va droit au but. Pas de phrases de transition inutiles. Le ticket entier DOIT être concis pour ne pas saturer l'affichage.
    
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
    
    FORMAT DE CHAQUE TICKET :
    🔴 <b>PRONOSTIC [SIMPLE OU COMBINÉ]</b> 🔴
    🏟 <b>MATCHS :</b> [Match 1] vs [Match 2]
    🏆 <b>COMPÉTITION :</b> [Tournoi]
    ⏰ <b>HEURE :</b> [Heure]
    ✅ <b>PRONO :</b> [Pronostic précis]
    📈 <b>COTE :</b> [Cote réelle Winamax]
    💰 <b>MISE :</b> [2% simple / 1% combiné]
    🛡 <b>CONFIANCE :</b> [ÉLEVÉE / MODÉRÉE]
    📌 <b>POURQUOI ?</b> [Analyse très courte et synthétique]
    """

    # AJOUT SÉCURITÉ ANTI-HALLUCINATION (CONSIGNE AGENT)
    instructions_agent += """
    RÈGLE ABSOLUE ANTI-HALLUCINATION :
    - SI ET SEULEMENT SI la recherche Google Search ne renvoie aucun match réel pour le tennis aujourd'hui, ou si aucun match ne commence après l'heure actuelle, tu ne dois ABSOLUMENT RIEN INVENTER.
    - Ne te base pas sur tes connaissances passées pour imaginer des rencontres fictives ou passées (comme Draper vs Berrettini).
    - Si la liste réelle est vide, réponds UNIQUEMENT et STRICTEMENT par le mot : AUCUN_MATCH
    - Ne mets aucun format HTML, aucune analyse, aucun autre mot. Juste : AUCUN_MATCH
    """

    # AJOUT PROTOCOLE MATHÉMATIQUE VALUE-BET
    instructions_agent += """
    PROTOCOLE DE CALCUL DE LA VALUE (OBLIGATOIRE) :
    Pour chaque match validé par tes 5 points d'analyse, tu dois obligatoirement réaliser ce calcul avant de générer le ticket :
    1. Évalue la probabilité de réussite en pourcentage (ex: 65%) basée strictement sur tes conclusions de l'analyse.
    2. Convertis cette probabilité en cote juste avec la formule : Cote Juste = 1 / (Pourcentage / 100). (ex: 1 / 0.65 = 1.53).
    3. Compare ta Cote Juste avec la cote réelle disponible sur Winamax :
       - SI Cote Winamax > Cote Juste : C'est une Value. Le ticket est validé.
       - SI Cote Winamax <= Cote Juste : Ce n'est PAS une Value. Tu dois abandonner ce match, même si le joueur est le grand favori.
    - Si aucun match de la session ne contient de Value mathématique, réponds STRICTEMENT : AUCUN_MATCH
    """

    # AJOUT DIRECTIVE TYPES DE PARIS (CONFIANCE MODÉRÉE / ÉLEVÉE)
    instructions_agent += """
    ORIENTATION DES MARCHÉS SELON LE RISQUE :
    - Lorsque ton niveau de confiance se situe dans la tranche MODÉRÉE ou ÉLEVÉE, privilégie les paris alternatifs basés sur le scénario et le contenu du match plutôt que sur la victoire sèche (Moneyline).
    - Oriente tes propositions en priorité vers des marchés du style : "Plus de 2.5 sets", "Moins de 2.5 sets", ou sur le nombre de jeux total (Over/Under jeux), ainsi que les handicaps, si l'analyse montre un match particulièrement serré ou au contraire totalement déséquilibré.
    - Ces types de paris doivent évidemment être validés par ton PROTOCOLE DE CALCUL DE LA VALUE avant d'être proposés.
    """

    # AJOUT FILTRE REPRISE DE BLESSURE / ABSENCE PROLONGÉE
    instructions_agent += """
    FILTRE REPRISE DE BLESSURE / MANQUE DE RYTHME (VIGILANCE PRO) :
    - Lors de tes recherches, vérifie systématiquement si un joueur ou une joueuse effectue son match de reprise après une absence pour blessure ou une pause de plus de 2 mois (en dehors de l'intersaison habituelle).
    - Si c'est le cas, ÉLIMINE purement et simplement ce match de tes sélections (n'analyse aucun pari sur ou contre ce joueur). Le manque de repères physiques ou le risque d'abandon introduit une variance impossible à modéliser mathématiquement. Dans le doute, on passe notre chemin.
    """

    try:
        logging.info("Lancement de l'analyse Gemini...")
        reponse = client.models.generate_content(
            model='gemini-2.5-pro',
            contents=f"Effectue la double triple vérification et propose les meilleurs paris (max 3) pour la session du {date_du_jour}.",
            config=types.GenerateContentConfig(
                system_instruction=instructions_agent,
                tools=[{"google_search": {}}],
                temperature=0.1,
            ),
        )
        
        texte = reponse.text.strip()
        
        if "AUCUN_MATCH" in texte:
            logging.info("Session annulée proprement : Aucun match réel disponible à cette heure-ci (Détection AUCUN_MATCH).")
            return

        if "PAS_DE_VALUE" not in texte and len(texte) > 20:
            tickets = [t.strip() for t in texte.split("===") if len(t.strip()) > 20]
            
            pari_envoye = False
            for ticket in tickets:
                if not est_deja_envoye(ticket):
                    sauvegarder_pari_pour_suivi({"pari": ticket, "date": date_du_jour})
                    sauvegarder_dans_historique(ticket)
                    envoyer_sur_telegram(ticket)
                    pari_envoye = True
                else:
                    logging.warning("Doublon détecté pour un des tickets, sauté.")
            
            if pari_envoye:
                try:
                    subprocess.run(["git", "config", "--global", "user.name", "bot-pari"], check=True)
                    subprocess.run(["git", "config", "--global", "user.email", "bot@github.com"], check=True)
                    subprocess.run(["git", "add", "pari_en_cours.json", "historique.json"], check=True)
                    subprocess.run(["git", "commit", "-m", "📌 Sauvegarde des nouveaux paris de la session et historique"], check=True)
                    subprocess.run(["git", "push"], check=True)
                    logging.info("Nouveaux paris sauvegardés avec succès sur GitHub.")
                except subprocess.CalledProcessError as e:
                    logging.error(f"Erreur lors du push des nouveaux paris : {e}")
        else:
            logging.info("Aucune opportunité de value-bet trouvée pour cette session.")
    except Exception as e:
        logging.error(f"Erreur lors de l'exécution globale du bot : {e}")

if __name__ == "__main__":
    run_bot_autonome()
