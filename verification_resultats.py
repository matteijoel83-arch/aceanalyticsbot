import json
import os
import sys
import logging
from google import genai
from google.genai import types

# Protection d'importation
sys.path.append(os.getcwd())
try:
    from bot import enregistrer_resultat
except ImportError:
    logging.critical("Impossible d'importer 'enregistrer_resultat' depuis bot.py.")
    sys.exit(1)

# Configuration du logging (aligné sur bot.log)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    logging.critical("Erreur : Clé API GEMINI_API_KEY manquante.")
    sys.exit(1)

client = genai.Client(api_key=api_key)

def verifier_et_mettre_a_jour():
    FICHIER_PARI = "pari_en_cours.json"

    if not os.path.exists(FICHIER_PARI):
        logging.info("Aucun pari en cours à vérifier.")
        return

    # 1. Lecture sécurisée du fichier JSON
    try:
        with open(FICHIER_PARI, "r", encoding="utf-8") as f:
            data = json.load(f)
        pari_texte = data.get("pari", "")
    except json.JSONDecodeError:
        logging.error(f"Le fichier {FICHIER_PARI} est corrompu. Suppression pour éviter un blocage.")
        os.remove(FICHIER_PARI)
        return
    except Exception as e:
        logging.error(f"Erreur lors de la lecture du fichier : {e}")
        return

    if not pari_texte:
        logging.warning("Le fichier existe mais est vide. Nettoyage.")
        os.remove(FICHIER_PARI)
        return

    # 2. Appel déterministe à l'IA pour classification
    try:
        logging.info(f"Recherche du résultat internet pour : {pari_texte[:60]}...")
        
        system_prompt = (
            "Tu es un agent automatique de vérification de scores de tennis. Tu dois chercher sur le web "
            "le résultat final du match. Tu as une obligation stricte : "
            "Répondre UNIQUEMENT par le mot 'GAGNÉ' ou 'PERDU' selon l'issue du pronostic fourni."
        )

        verif = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Analyse ce pronostic et donne l'issue (GAGNÉ/PERDU) : '{pari_texte}'",
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[{"google_search": {}}],
                temperature=0.0, # Neutralise la créativité de l'IA
            ),
        )
        
        resultat = verif.text.strip().upper()
        logging.info(f"Verdict rendu par l'IA : {resultat}")
        
        # 3. Traitement binaire et suppression locale avant synchronisation globale
        if "GAGNÉ" in resultat:
            logging.info("🏆 Pronostic validé ! Suppression locale et mise à jour des stats...")
            os.remove(FICHIER_PARI)
            enregistrer_resultat(True) # Le push Git global a lieu ici
        elif "PERDU" in resultat:
            logging.info("❌ Pronostic perdu. Suppression locale et mise à jour des stats...")
            os.remove(FICHIER_PARI)
            enregistrer_resultat(False) # Le push Git global a lieu ici
        else:
            logging.warning("⚠️ Résultat incertain ou match non terminé. Le fichier est conservé pour le prochain cycle.")
            return

    except Exception as e:
        logging.error(f"Erreur critique lors du processus de vérification : {e}")

if __name__ == "__main__":
    verifier_et_mettre_a_jour()
