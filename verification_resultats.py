import os
import sys
import json
import subprocess
import logging
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    logging.critical("Clé GEMINI_API_KEY manquante.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)
STATS_FILE = "stats.json"
PARI_FILE = "pari_en_cours.json"

def charger_json(fichier, defaut):
    if os.path.exists(fichier):
        try:
            with open(fichier, "r") as f:
                return json.load(f)
        except:
            return defaut
    return defaut

def sauvegarder_json(fichier, donnees):
    with open(fichier, "w") as f:
        json.dump(donnees, f)

def verifier_un_pari(pari_texte):
    prompt = f"""
    Tu es un expert en vérification de scores de tennis. Analyse le ticket de pari suivant et détermine si le pronostic est GAGNÉ, PERDU, ou si le match est EN_COURS (ou pas encore terminé / reporté).
    
    Ticket :
    {pari_texte}
    
    Utilise l'outil de recherche Google pour trouver le résultat réel exact du ou des matchs mentionnés.
    
    RÉPONSE STRICTE ATTENDUE (RETOURNE UNIQUEMENT L'UN DE CES TROIS MOTS) :
    - GAGNÉ (si le pronostic s'est avéré exact)
    - PERDU (si le pronostic est faux)
    - EN_COURS (si le match n'a pas encore eu lieu, est en train d'être joué, ou si le résultat n'est pas encore officiel)
    """
    try:
        reponse = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[{"google_search": {}}],
                temperature=0.0,
            )
        )
        resultat = reponse.text.strip().upper()
        if "GAGNÉ" in resultat or "GAGNE" in resultat:
            return "GAGNÉ"
        elif "PERDU" in resultat:
            return "PERDU"
        else:
            return "EN_COURS"
    except Exception as e:
        logging.error(f"Erreur lors de la vérification Gemini : {e}")
        return "EN_COURS"

def main():
    stats = charger_json(STATS_FILE, {"victoires": 0, "defaites": 0})
    paris_en_cours = charger_json(PARI_FILE, [])
    
    # Compatibilité : si l'ancien fichier contenait un dictionnaire unique, on le passe en liste
    if isinstance(paris_en_cours, dict):
        paris_en_cours = [paris_en_cours] if paris_en_cours else []

    if not paris_en_cours:
        logging.info("Aucun pari en cours à vérifier.")
        return

    nouveaux_paris_en_cours = []
    maj_stats = False

    for pari_info in paris_en_cours:
        pari_texte = pari_info.get("pari", "")
        date_pari = pari_info.get("date", "")
        logging.info(f"Vérification du pari du {date_pari}...")
        
        statut = verifier_un_pari(pari_texte)
        logging.info(f"Résultat constaté : {statut}")
        
        if statut == "GAGNÉ":
            stats["victoires"] += 1
            maj_stats = True
        elif statut == "PERDU":
            stats["defaites"] += 1
            maj_stats = True
        else:
            # Match en cours ou inconnu : on le conserve pour le prochain cycle
            nouveaux_paris_en_cours.append(pari_info)

    # Sauvegarde des états locaux
    sauvegarder_json(STATS_FILE, stats)
    
    if nouveaux_paris_en_cours:
        sauvegarder_json(PARI_FILE, nouveaux_paris_en_cours)
    else:
        if os.path.exists(PARI_FILE):
            os.remove(PARI_FILE)

    # Push des modifications sur GitHub si nécessaire
    if maj_stats or len(nouveaux_paris_en_cours) != len(paris_en_cours):
        try:
            subprocess.run(["git", "config", "--global", "user.name", "bot-verification"], check=True)
            subprocess.run(["git", "config", "--global", "user.email", "bot@github.com"], check=True)
            subprocess.run(["git", "add", STATS_FILE], check=True)
            
            if os.path.exists(PARI_FILE):
                subprocess.run(["git", "add", PARI_FILE], check=True)
            else:
                subprocess.run(["git", "rm", PARI_FILE], check=False)
                
            subprocess.run(["git", "commit", "-m", "🔄 MAJ des résultats (conservation des matchs en cours)"], check=True)
            subprocess.run(["git", "push"], check=True)
            logging.info("Synchronisation GitHub effectuée avec succès.")
        except Exception as e:
            logging.error(f"Erreur lors de la synchronisation Git : {e}")

if __name__ == "__main__":
    main()
