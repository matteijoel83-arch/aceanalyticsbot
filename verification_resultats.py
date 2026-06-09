import json
import os
import sys
from google import genai
from google.genai import types
from bot import enregistrer_resultat

api_key = os.environ.get("GEMINI_API_KEY")

if not api_key:
    print("❌ Erreur : Clé API manquante.")
    sys.exit(1)

client = genai.Client(api_key=api_key)

def verifier_et_mettre_a_jour():
    if not os.path.exists("pari_en_cours.json"):
        print("❌ Aucun pari à vérifier.")
        return

    with open("pari_en_cours.json", "r") as f:
        data = json.load(f)
    
    pari_texte = data["pari"]
    
    try:
        verif = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=f"Analyse ce pronostic : '{pari_texte}'. Identifie le vainqueur sur Google. Réponds uniquement : GAGNÉ ou PERDU.",
            config=types.GenerateContentConfig(tools=[{"google_search": {}}]),
        )
        
        resultat = verif.text.strip()
        if "GAGNÉ" in resultat:
            enregistrer_resultat(True)
        else:
            enregistrer_resultat(False)
            
        os.remove("pari_en_cours.json")
        print("✅ Stats mises à jour.")
    except Exception as e:
        print(f"❌ Erreur : {e}")

if __name__ == "__main__":
    verifier_et_mettre_a_jour()
