import os
import sys
import json
import subprocess
import logging
from google import genai
from google.genai import types

# Configuration des logs
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
STATS_FILE = "stats.json"
PARI_FILE = "pari_en_cours.json"

if not GEMINI_API_KEY:
    logging.critical("Erreur : Clé API GEMINI_API_KEY manquante.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

def charger_json(fichier, par_defaut):
    if os.path.exists(fichier):
        try:
            with open(fichier, "r") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"Erreur lecture {fichier}: {e}")
            return par_defaut
    return par_defaut

def sauvegarder_json(fichier, donnees):
    with open(fichier, "w") as f:
        json.dump(donnees, f, indent=2)

def interroger_ia_statut(pari_texte):
    """Demande à Gemini Pro de vérifier le résultat réel sur le web"""
    instructions = """
    Tu es un agent expert de vérification de scores de tennis. Ton rôle est de déterminer le résultat RÉEL du pronostic fourni.
    Utilise l'outil Google Search pour chercher les résultats officiels sur Flashscore, Sofascore, ATP Tour ou WTA Tennis.

    Tu dois STRICTEMENT répondre par l'un de ces 3 mots :
    - GAGNE : Si les matchs sont terminés et que le pronostic est correct (Validé).
    - PERDU : Si les matchs sont terminés et que le pronostic est faux (Échoué).
    - EN_COURS : Si le match n'a pas commencé, est en train de se jouer, ou si les résultats réels sont encore introuvables/incomplets sur le web.

    RÈGLE D'OR : Ne devine jamais. Si tu as le moindre doute ou si tu ne trouves pas de preuve absolue du score, réponds EN_COURS.
    Ne donne aucune explication, aucun texte additionnel. Juste un mot : GAGNE, PERDU, ou EN_COURS.
    """

    try:
        reponse = client.models.generate_content(
            model='gemini-2.5-pro',
            contents=f"Vérifie le résultat réel pour ce ticket :\n\n{pari_texte}",
            config=types.GenerateContentConfig(
                system_instruction=instructions,
                # MAJ : Utilisation de la syntaxe native officielle pour le grounding Google Search
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.1,
            ),
        )
        verdict = reponse.text.strip().upper()
        if "GAGNE" in verdict: return "GAGNE"
        if "PERDU" in verdict: return "PERDU"
        return "EN_COURS"
    except Exception as e:
        logging.error(f"Erreur lors de l'appel Gemini : {e}")
        return "EN_COURS"

def main():
    paris = charger_json(PARI_FILE, [])
    
    # Sécurité si le fichier n'était pas une liste
    if not isinstance(paris, list):
        paris = [paris] if paris else []

    if not paris:
        logging.info("Aucun pari en cours dans la file d'attente.")
        return

    stats = charger_json(STATS_FILE, {"victoires": 0, "defaites": 0})
    restants = []
    ajustement_stats = False

    logging.info(f"Vérification de {len(paris)} pari(s) en cours...")

    for item in paris:
        pari_texte = item.get("pari", "")
        if not pari_texte:
            continue

        logging.info("--- Analyse d'un ticket ---")
        statut = interroger_ia_statut(pari_texte)
        logging.info(f"Verdict rendu par l'IA Pro : {statut}")

        if statut == "GAGNE":
            stats["victoires"] += 1
            ajustement_stats = True
            logging.info("🏆 Pronostic validé ! Ajout d'une victoire.")
        elif statut == "PERDU":
            stats["defaites"] += 1
            ajustement_stats = True
            logging.info("❌ Pronostic perdu. Ajout d'une défaite.")
        else:
            restants.append(item)
            logging.info("⏳ Match toujours en cours ou non vérifiable. Maintenu dans la file d'attente.")

    # 💾 SAUVEGARDE & SYNCHRONISATION GITHUB
    if ajustement_stats:
        sauvegarder_json(STATS_FILE, stats)

    # Si la file d'attente a changé (des paris ont été validés/perdus)
    if len(restants) != len(paris):
        if restants:
            sauvegarder_json(PARI_FILE, restants)
        else:
            if os.path.exists(PARI_FILE):
                os.remove(PARI_FILE)

        try:
            subprocess.run(["git", "config", "--global", "user.name", "bot-verification"], check=True)
            subprocess.run(["git", "config", "--global", "user.email", "bot@github.com"], check=True)
            
            # MAJ SÉCURITÉ CRITIQUE : Pull avec rebase prioritaire avant d'ajouter les modifications
            # Évite les rejets de push si bot.py a poussé un nouveau match entre-temps
            subprocess.run(["git", "pull", "--rebase", "-Xours"], check=False)
            
            subprocess.run(["git", "add", STATS_FILE], check=True)
            
            if os.path.exists(PARI_FILE):
                subprocess.run(["git", "add", PARI_FILE], check=True)
            else:
                subprocess.run(["git", "rm", PARI_FILE], check=False)

            subprocess.run(["git", "commit", "-m", "🔄 MAJ automatique des résultats et de la file d'attente"], check=True)
            subprocess.run(["git", "push"], check=True)
            logging.info("Changements synchronisés avec succès sur GitHub.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Erreur lors de la synchronisation Git : {e}")
    else:
        logging.info("Aucun changement. Tous les matchs restent en cours.")

if __name__ == "__main__":
    main()
