import json
import os
import sys
import logging
from google import genai
from google.genai import types

# Protection d'import et alignement du chemin
sys.path.append(os.getcwd())
try:
    from bot import enregistrer_resultat
except ImportError:
    logging.critical("❌ Impossible d'importer 'enregistrer_resultat' depuis bot.py. Vérifiez l'emplacement du fichier.")
    sys.exit(1)

# Configuration du logging (partagée avec bot.log)
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
        logging.warning("Le fichier pari_en_cours.json existe mais ne contient aucun texte de pari.")
        os.remove(FICHIER_PARI)
        return

    # 2. Appel à l'IA avec configuration stricte
    try:
        logging.info(f"Recherche du résultat pour le pari : {pari_texte[:50]}...")
        
        system_prompt = (
            "Tu es un agent de vérification de scores sportifs. Tu dois chercher sur le web "
            "le résultat final du match mentionné. Tu as une obligation stricte : "
            "Répondre UNIQUEMENT par le mot 'GAGNÉ' ou 'PERDU' selon l'issue du pronostic fourni."
        )

        verif = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Analyse ce pronostic et donne le résultat : '{pari_texte}'",
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[{"google_search": {}}],
                temperature=0.0,  # Supprime toute créativité de l'IA
            ),
        )
        
        resultat = verif.text.strip().upper()
        logging.info(f"Résultat retourné par l'IA : {resultat}")
        
        # 3. Traitement du résultat et mise à jour des stats via bot.py
        if "GAGNÉ" in resultat:
            logging.info("🏆 Pari GAGNÉ. Enregistrement des statistiques...")
            enregistrer_resultat(True)
        elif "PERDU" in resultat:
            logging.info("❌ Pari PERDU. Enregistrement des statistiques...")
            enregistrer_resultat(False)
        else:
            logging.warning("⚠️ Impossible de déterminer le résultat avec certitude. Le fichier est conservé pour le prochain run.")
            return

        # 4. Nettoyage après succès du processus complet
        os.remove(FICHIER_PARI)
        logging.info("✅ Fichier 'pari_en_cours.json' nettoyé avec succès.")

    except Exception as e:
        logging.error(f"Erreur critique lors du processus de vérification : {e}")

if __name__ == "__main__":
    verifier_et_mettre_a_jour()
