"""
Comptes administrateurs de l'interface d'administration
=======================================================

Les comptes sont stockés dans `comptes_admin.json`, à la racine du projet
(hors du dossier app/data, et exclu de git). Les mots de passe n'y sont
JAMAIS écrits en clair : seule une empreinte PBKDF2-SHA256 salée est
conservée (bibliothèque standard Python, aucune dépendance).

Création du premier compte (sur le serveur) :
    python gerer_comptes.py ajouter <identifiant>

Les comptes suivants peuvent ensuite être gérés depuis la page « Comptes
administrateurs » de l'interface.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

_BASE_DIR = Path(__file__).resolve().parent.parent.parent  # .../rag_rh_api

ITERATIONS = 310_000
LONGUEUR_MIN_MOT_DE_PASSE = 8
MAX_ESSAIS = 5                 # essais ratés avant blocage temporaire
DUREE_BLOCAGE_SECONDES = 5 * 60
RE_IDENTIFIANT = re.compile(r"^[a-zA-Z0-9._-]{3,40}$")


def chemin_comptes() -> Path:
    return Path(os.getenv("ADMIN_COMPTES_FICHIER", str(_BASE_DIR / "comptes_admin.json")))


# ═══════════════════════════════════════════════════════════
#  EMPREINTE DES MOTS DE PASSE
# ═══════════════════════════════════════════════════════════

def _empreinte(mot_de_passe: str, sel: bytes, iterations: int = ITERATIONS) -> str:
    h = hashlib.pbkdf2_hmac("sha256", mot_de_passe.encode("utf-8"), sel, iterations)
    return base64.b64encode(h).decode("ascii")


def hacher_mot_de_passe(mot_de_passe: str) -> str:
    sel = secrets.token_bytes(16)
    return f"pbkdf2_sha256${ITERATIONS}${base64.b64encode(sel).decode('ascii')}${_empreinte(mot_de_passe, sel)}"


def verifier_mot_de_passe(mot_de_passe: str, stocke: str) -> bool:
    try:
        algo, iterations, sel_b64, attendu = stocke.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        calcule = _empreinte(mot_de_passe, base64.b64decode(sel_b64), int(iterations))
        return hmac.compare_digest(calcule, attendu)
    except Exception:
        return False


def valider_mot_de_passe(mot_de_passe: str) -> Optional[str]:
    """Retourne un message d'erreur, ou None si le mot de passe convient."""
    if len(mot_de_passe) < LONGUEUR_MIN_MOT_DE_PASSE:
        return f"Le mot de passe doit contenir au moins {LONGUEUR_MIN_MOT_DE_PASSE} caractères."
    if not re.search(r"[A-Za-z]", mot_de_passe) or not re.search(r"\d", mot_de_passe):
        return "Le mot de passe doit contenir au moins une lettre et un chiffre."
    return None


# ═══════════════════════════════════════════════════════════
#  LECTURE / ÉCRITURE DU FICHIER DES COMPTES
# ═══════════════════════════════════════════════════════════

def charger_comptes() -> dict:
    chemin = chemin_comptes()
    if not chemin.exists():
        return {}
    with open(chemin, "r", encoding="utf-8") as f:
        contenu = json.load(f)
    return contenu if isinstance(contenu, dict) else {}


def _sauvegarder_comptes(comptes: dict):
    chemin = chemin_comptes()
    chemin.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(chemin.parent), prefix=".comptes_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(comptes, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)  # lisible uniquement par l'utilisateur du serveur
        os.replace(tmp, chemin)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def existe_au_moins_un_compte() -> bool:
    return bool(charger_comptes())


# ═══════════════════════════════════════════════════════════
#  GESTION DES COMPTES
# ═══════════════════════════════════════════════════════════

def ajouter_compte(identifiant: str, nom_complet: str, mot_de_passe: str, cree_par: str = "") -> None:
    identifiant = identifiant.strip().lower()
    if not RE_IDENTIFIANT.match(identifiant):
        raise ValueError("Identifiant invalide : 3 à 40 caractères parmi lettres, chiffres, point, tiret, tiret bas.")
    if not nom_complet.strip():
        raise ValueError("Le nom complet est obligatoire (il apparaît dans l'historique des modifications).")
    erreur = valider_mot_de_passe(mot_de_passe)
    if erreur:
        raise ValueError(erreur)
    comptes = charger_comptes()
    if identifiant in comptes:
        raise ValueError(f"L'identifiant « {identifiant} » existe déjà.")
    comptes[identifiant] = {
        "nom_complet": " ".join(nom_complet.split()),
        "mot_de_passe": hacher_mot_de_passe(mot_de_passe),
        "cree_le": datetime.now().isoformat(timespec="seconds"),
        "cree_par": cree_par,
        "derniere_connexion": None,
    }
    _sauvegarder_comptes(comptes)


def changer_mot_de_passe(identifiant: str, nouveau: str) -> None:
    erreur = valider_mot_de_passe(nouveau)
    if erreur:
        raise ValueError(erreur)
    comptes = charger_comptes()
    if identifiant not in comptes:
        raise ValueError("Compte introuvable.")
    comptes[identifiant]["mot_de_passe"] = hacher_mot_de_passe(nouveau)
    comptes[identifiant]["mot_de_passe_change_le"] = datetime.now().isoformat(timespec="seconds")
    _sauvegarder_comptes(comptes)


def supprimer_compte(identifiant: str, demande_par: str) -> None:
    comptes = charger_comptes()
    if identifiant not in comptes:
        raise ValueError("Compte introuvable.")
    if identifiant == demande_par:
        raise ValueError("Vous ne pouvez pas supprimer votre propre compte.")
    if len(comptes) <= 1:
        raise ValueError("Impossible de supprimer le dernier compte administrateur.")
    del comptes[identifiant]
    _sauvegarder_comptes(comptes)


# ═══════════════════════════════════════════════════════════
#  CONNEXION (avec blocage après plusieurs échecs)
# ═══════════════════════════════════════════════════════════

_ECHECS: dict[str, list[float]] = {}  # identifiant -> horodatages des échecs récents


def secondes_de_blocage(identifiant: str) -> int:
    maintenant = time.time()
    recents = [t for t in _ECHECS.get(identifiant, []) if maintenant - t < DUREE_BLOCAGE_SECONDES]
    _ECHECS[identifiant] = recents
    if len(recents) >= MAX_ESSAIS:
        return int(DUREE_BLOCAGE_SECONDES - (maintenant - recents[0])) + 1
    return 0


def authentifier(identifiant: str, mot_de_passe: str) -> Optional[dict]:
    """Retourne {identifiant, nom_complet} si les informations sont
    correctes, None sinon. Lève PermissionError si le compte est
    temporairement bloqué après trop d'essais ratés."""
    identifiant = (identifiant or "").strip().lower()
    attente = secondes_de_blocage(identifiant)
    if attente:
        raise PermissionError(f"Trop d'essais. Réessayez dans {(attente + 59) // 60} minute(s).")
    comptes = charger_comptes()
    compte = comptes.get(identifiant)
    # Calcul d'empreinte effectué même si le compte n'existe pas, pour ne
    # pas révéler par le temps de réponse quels identifiants existent.
    stocke = compte["mot_de_passe"] if compte else hacher_mot_de_passe("leurre-0")
    if compte and verifier_mot_de_passe(mot_de_passe or "", stocke):
        _ECHECS.pop(identifiant, None)
        compte["derniere_connexion"] = datetime.now().isoformat(timespec="seconds")
        _sauvegarder_comptes(comptes)
        return {"identifiant": identifiant, "nom_complet": compte["nom_complet"]}
    _ECHECS.setdefault(identifiant, []).append(time.time())
    return None