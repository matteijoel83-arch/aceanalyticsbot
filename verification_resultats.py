import json
import os
from google import genai
from google.genai import types
from bot import enregistrer_resultat

# Initialisation du client (identique à ton bot.py)
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

def verifier_et_mettre_a_jour():
    # 1. Lire le pari en cours
    if not os.path.exists("pari_en_cours.json"):
        print("❌ Aucun pari à vérifier.")
        return

    with open("pari_en_cours.json", "r") as f:
        data = json.load(f)
    
    pari_texte = data["pari"]
    print(f"🔍 Vérification du pari : {pari_texte}")

    # 2. Utiliser l'IA pour extraire les joueurs et le résultat
    try:
        verif = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Analyse ce pronostic : '{pari_texte}'. Identifie les joueurs et va chercher sur Google le vainqueur du match. Réponds uniquement par : GAGNÉ ou PERDU.",
            config=types.GenerateContentConfig(
                tools=[{"google_search": {}}],
            ),
        )
        
        resultat = verif.text.strip()
        print(f"📊 Résultat détecté : {resultat}")

        # 3. Mettre à jour les stats et supprimer le pari traité
        if "GAGNÉ" in resultat:
            enregistrer_resultat(True)
        else:
            enregistrer_resultat(False)
            
        os.remove("pari_en_cours.json")
        print("✅ Stats mises à jour et fichier temporaire supprimé.")

    except Exception as e:
        print(f"❌ Erreur lors de la vérification : {e}")

if __name__ == "__main__":
    verifier_et_mettre_a_jour()
