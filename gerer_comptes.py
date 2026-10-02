"""
Gestion des comptes administrateurs en ligne de commande (sur le serveur).

    python gerer_comptes.py ajouter <identifiant>        crée un compte (le 1er compte se crée ainsi)
    python gerer_comptes.py mot-de-passe <identifiant>   réinitialise un mot de passe oublié
    python gerer_comptes.py supprimer <identifiant>      supprime un compte
    python gerer_comptes.py lister                       liste les comptes

Les mots de passe sont demandés au clavier (non affichés) et ne sont
jamais stockés en clair.
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.services import auth_service as auth  # noqa: E402


def _demander_mot_de_passe() -> str:
    while True:
        mdp = getpass.getpass("Mot de passe : ")
        erreur = auth.valider_mot_de_passe(mdp)
        if erreur:
            print(f"  ✗ {erreur}")
            continue
        if getpass.getpass("Confirmer le mot de passe : ") != mdp:
            print("  ✗ Les deux saisies ne correspondent pas.")
            continue
        return mdp


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("ajouter", "mot-de-passe", "supprimer", "lister"):
        print(__doc__)
        sys.exit(1)
    commande = sys.argv[1]

    if commande == "lister":
        comptes = auth.charger_comptes()
        if not comptes:
            print("Aucun compte. Créez-en un : python gerer_comptes.py ajouter <identifiant>")
        for ident, c in comptes.items():
            print(f"- {ident:20} {c['nom_complet']:35} dernière connexion : {c.get('derniere_connexion') or 'jamais'}")
        return

    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    identifiant = sys.argv[2].strip().lower()

    try:
        if commande == "ajouter":
            nom = input("Nom complet (affiché dans l'historique, ex : Mané Sidibé — DGFP) : ").strip()
            auth.ajouter_compte(identifiant, nom, _demander_mot_de_passe(), cree_par="ligne de commande")
            print(f"✓ Compte « {identifiant} » créé. Fichier : {auth.chemin_comptes()}")
        elif commande == "mot-de-passe":
            auth.changer_mot_de_passe(identifiant, _demander_mot_de_passe())
            print(f"✓ Mot de passe de « {identifiant} » modifié.")
        elif commande == "supprimer":
            comptes = auth.charger_comptes()
            if identifiant not in comptes:
                raise ValueError("Compte introuvable.")
            if len(comptes) <= 1:
                raise ValueError("Impossible de supprimer le dernier compte administrateur.")
            if input(f"Supprimer « {identifiant} » ? (oui/non) : ").strip().lower() == "oui":
                auth.supprimer_compte(identifiant, demande_par="")
                print(f"✓ Compte « {identifiant} » supprimé.")
    except ValueError as e:
        print(f"✗ {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()