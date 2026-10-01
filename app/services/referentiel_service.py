"""
Service Référentiel — gestion DYNAMIQUE de la base de référence (app/data)
===========================================================================

Ce module est partagé entre :
  - l'interface d'administration (admin_app.py), qui l'utilise pour LIRE,
    VALIDER, ENREGISTRER, SAUVEGARDER et RESTAURER les fichiers de la base ;
  - l'API, qui l'utilise pour savoir si un fichier a changé depuis son
    dernier chargement (signature_fichiers), afin de le recharger à chaud.

Principe : quand le ministère publie une nouvelle règle (nouveau corps,
nouvelle durée d'échelon, nouvelle classe, nouveau type d'acte → circuit,
nouvelle référence légale de visa...), l'administrateur la saisit dans
l'interface. Le fichier CSV/TXT correspondant est réécrit ici de façon
ATOMIQUE (fichier temporaire + remplacement), l'ancienne version est
archivée dans app/data/_historique/, et l'opération est tracée dans
app/data/_historique/journal_modifications.jsonl. À la vérification d'acte
suivante, l'API constate que le fichier a changé (date de modification /
taille) et recharge ses tables — aucune modification du code, aucun
redémarrage.

Ce module ne dépend que de pandas et de la bibliothèque standard, pour
pouvoir être importé aussi bien par Streamlit que par FastAPI.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

# ═══════════════════════════════════════════════════════════
#  CHEMINS
# ═══════════════════════════════════════════════════════════

_BASE_DIR = Path(__file__).resolve().parent.parent.parent  # .../rag_rh_api


def dossier_data() -> Path:
    """Même logique que app/config.py : DATA_DIR si défini, sinon app/data.
    Un chemin relatif (ex: './app/data' dans le .env) est résolu par rapport
    à la racine du projet, pour que l'API et l'interface admin pointent
    TOUJOURS vers le même dossier, quel que soit le répertoire de lancement."""
    # Comme l'API (load_dotenv(override=True) dans app/config.py), la valeur
    # du fichier .env est prioritaire sur la variable d'environnement.
    d = None
    try:
        from dotenv import dotenv_values
        d = dotenv_values(_BASE_DIR / ".env").get("DATA_DIR")
    except Exception:
        pass
    d = d or os.getenv("DATA_DIR")
    if d:
        p = Path(d)
        return p if p.is_absolute() else (_BASE_DIR / p).resolve()
    return _BASE_DIR / "app" / "data"


def dossier_historique() -> Path:
    p = dossier_data() / "_historique"
    p.mkdir(parents=True, exist_ok=True)
    return p


def chemin_journal() -> Path:
    return dossier_historique() / "journal_modifications.jsonl"


# ═══════════════════════════════════════════════════════════
#  DESCRIPTION DES TABLES CONFIGURABLES
#
#  Pour ajouter un jour une NOUVELLE table configurable, il suffit de la
#  décrire ici : l'interface admin la proposera automatiquement.
# ═══════════════════════════════════════════════════════════

TYPES_CORPS = ["FONCT", "NON_FONCT", "ENS_FONCT", "ENS_NON_FONCT"]
CIRCUITS = ["APAE", "APAG", "RET"]
NATURES_ACTE = ["ARRETE", "DECISION", "DECIDE"]

TABLES: dict[str, dict] = {
    "corps.csv": {
        "titre": "Corps de la fonction publique",
        "description": (
            "Liste officielle des corps. Sert à reconnaître le corps cité dans "
            "l'acte, à déterminer s'il s'agit d'un fonctionnaire ou d'un "
            "non-fonctionnaire (visa attendu) et à retrouver les durées "
            "d'avancement dans la table corps / classe / échelon."
        ),
        "colonnes": [
            "cps_code", "cps_libelle", "cps_age_retraite", "cps_description",
            "cps_hierarchie_code", "cps_cadre_code", "cps_typecorps_code",
            "cps_libelle_singulier",
        ],
        "libelles": {
            "cps_code": "Code corps", "cps_libelle": "Libellé (pluriel, tel qu'écrit dans les actes)",
            "cps_age_retraite": "Âge de retraite", "cps_description": "Description",
            "cps_hierarchie_code": "Code hiérarchie", "cps_cadre_code": "Code cadre",
            "cps_typecorps_code": "Type de corps", "cps_libelle_singulier": "Libellé singulier",
        },
        "cle": ["cps_code"],
        "obligatoires": ["cps_code", "cps_libelle", "cps_typecorps_code"],
        "entiers": ["cps_age_retraite"],
        "choix": {"cps_typecorps_code": TYPES_CORPS},
        "majuscules": ["cps_typecorps_code"],
        "fk": {},
        "auto_id": None,
    },
    "classe.csv": {
        "titre": "Classes",
        "description": (
            "Classes de grade. La colonne « Sigle dans les actes » (ex : 1CL, "
            "2CL, CEX, PPL) est celle que l'IA recherche dans le tableau de "
            "progression de l'acte pour retrouver le code classe."
        ),
        "colonnes": ["cls_code", "cls_libelle", "cls_description"],
        "libelles": {
            "cls_code": "Code classe", "cls_libelle": "Libellé",
            "cls_description": "Sigle dans les actes (ex : 1CL)",
        },
        "cle": ["cls_code"],
        "obligatoires": ["cls_code", "cls_libelle"],
        "entiers": [],
        "choix": {},
        "majuscules": [],
        "fk": {},
        "auto_id": None,
    },
    "echelon.csv": {
        "titre": "Échelons",
        "description": (
            "Échelons. La colonne « Sigle dans les actes » (ex : 1ECH, 2ECH) "
            "est celle que l'IA recherche dans le tableau de progression."
        ),
        "colonnes": ["ech_code", "ech_libelle", "ech_description"],
        "libelles": {
            "ech_code": "Code échelon", "ech_libelle": "Libellé",
            "ech_description": "Sigle dans les actes (ex : 1ECH)",
        },
        "cle": ["ech_code"],
        "obligatoires": ["ech_code", "ech_libelle"],
        "entiers": [],
        "choix": {},
        "majuscules": [],
        "fk": {},
        "auto_id": None,
    },
    "corps_classe_echelon.csv": {
        "titre": "Durées d'avancement (corps / classe / échelon)",
        "description": (
            "Source de vérité du contrôle « délai réglementaire d'avancement » : "
            "pour un corps, une classe et un échelon de départ, la durée (en "
            "années) avant le passage au grade suivant."
        ),
        "colonnes": [
            "cce_id", "cce_cps_code", "cce_cls_code", "cce_ech_code", "cce_duree",
            "cce_fin_grade", "cce_fin_echelon", "cce_operation",
            "cce_appelation_grade", "cce_appelation_echelon", "cce_suivant", "cce_fisrt",
        ],
        "libelles": {
            "cce_id": "N° ligne (auto)", "cce_cps_code": "Code corps",
            "cce_cls_code": "Code classe", "cce_ech_code": "Code échelon",
            "cce_duree": "Durée (années)", "cce_fin_grade": "Fin de grade (0/1)",
            "cce_fin_echelon": "Fin d'échelon (0/1)", "cce_operation": "Opération",
            "cce_appelation_grade": "Appellation grade", "cce_appelation_echelon": "Appellation échelon",
            "cce_suivant": "Ligne suivante", "cce_fisrt": "Première",
        },
        "cle": ["cce_id"],
        "unique_avertissement": ["cce_cps_code", "cce_cls_code", "cce_ech_code"],
        "obligatoires": ["cce_cps_code", "cce_cls_code", "cce_ech_code", "cce_duree"],
        "entiers": ["cce_id", "cce_duree", "cce_fin_grade", "cce_fin_echelon"],
        "choix": {},
        "majuscules": [],
        "fk": {
            "cce_cps_code": ("corps.csv", "cps_code"),
            "cce_cls_code": ("classe.csv", "cls_code"),
            "cce_ech_code": ("echelon.csv", "ech_code"),
        },
        "valeurs_defaut": {"cce_fin_grade": "0", "cce_fin_echelon": "0", "cce_operation": "="},
        "auto_id": "cce_id",
    },
    "parametrage_type_acte_workflow.csv": {
        "titre": "Type d'acte GIRAFE → circuit",
        "description": (
            "Table officielle qui associe un couple (nature, type d'acte GIRAFE) "
            "au circuit de validation à appliquer : APAE (avancement d'échelon, "
            "9 étapes), APAG (autre acte, 12 étapes) ou RET (retraite "
            "fonctionnaire, 13 étapes). Utilisée quand GIRAFE envoie 'nature' et "
            "'type_acte' avec l'acte ; sinon l'IA détecte le circuit par mots-clés."
        ),
        "colonnes": ["nature", "type_acte", "ref_engine", "libelle"],
        "libelles": {
            "nature": "Nature de l'acte", "type_acte": "Code type d'acte GIRAFE",
            "ref_engine": "Circuit", "libelle": "Libellé (facultatif)",
        },
        "cle": ["nature", "type_acte"],
        "obligatoires": ["nature", "type_acte", "ref_engine"],
        "entiers": ["type_acte"],
        "choix": {"nature": NATURES_ACTE, "ref_engine": CIRCUITS},
        "majuscules": ["nature", "ref_engine"],
        "fk": {},
        "auto_id": None,
        "creer_si_absent": True,
    },
}

# Dépendances inverses : quand on SUPPRIME une ligne d'une table, quelles
# autres tables y font référence (pour avertir l'administrateur).
REFERENCES_INVERSES: dict[str, list[tuple[str, str, str]]] = {}
for _nom_t, _spec in TABLES.items():
    for _col, (_t_cible, _col_cible) in _spec["fk"].items():
        REFERENCES_INVERSES.setdefault(_t_cible, []).append((_nom_t, _col, _col_cible))

FICHIER_REFERENCES_VISA = "corps_references_RAG.txt"
FICHIER_REGLES_METIER = "regles_metier_actes_RH_v7.txt"

TEXTES: dict[str, dict] = {
    FICHIER_REFERENCES_VISA: {
        "titre": "Références légales (visa) par corps",
        "description": "Lois et décrets attendus dans le visa de l'acte, corps par corps.",
    },
    FICHIER_REGLES_METIER: {
        "titre": "Règles métier (base de connaissance RAG)",
        "description": "Texte des règles métier interrogé par l'assistant (questions/réponses RAG).",
    },
}


# ═══════════════════════════════════════════════════════════
#  SIGNATURE DES FICHIERS (utilisée par l'API pour le rechargement à chaud)
# ═══════════════════════════════════════════════════════════

def signature_fichiers(noms, data_dir: Optional[str] = None) -> tuple:
    """Empreinte légère (date de modification en ns + taille) d'une liste de
    fichiers. Si elle change, au moins un fichier a été modifié."""
    d = Path(data_dir) if data_dir else dossier_data()
    sig = []
    for nom in noms:
        try:
            st = (d / nom).stat()
            sig.append((nom, st.st_mtime_ns, st.st_size))
        except FileNotFoundError:
            sig.append((nom, None, None))
    return tuple(sig)


# ═══════════════════════════════════════════════════════════
#  LECTURE
# ═══════════════════════════════════════════════════════════

def chemin_fichier(nom: str) -> Path:
    return dossier_data() / nom


def charger_table(nom: str) -> pd.DataFrame:
    """Lit une table en TEXTE BRUT (aucune conversion de type) pour la
    réécrire à l'identique : '01' reste '01', une cellule vide reste vide."""
    spec = TABLES.get(nom, {})
    chemin = chemin_fichier(nom)
    if not chemin.exists():
        return pd.DataFrame(columns=spec.get("colonnes", []), dtype=str)
    df = pd.read_csv(chemin, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df.columns = [str(c).strip() for c in df.columns]
    # Colonnes attendues absentes du fichier : on les ajoute (vides)
    for col in spec.get("colonnes", []):
        if col not in df.columns:
            df[col] = ""
    return df.astype(str)


def lire_csv_televerse(contenu: bytes) -> pd.DataFrame:
    """Lit un CSV envoyé par l'administrateur (import), séparateur , ou ;"""
    texte = contenu.decode("utf-8-sig", errors="replace")
    try:
        dialecte = csv.Sniffer().sniff(texte[:5000], delimiters=",;\t")
        sep = dialecte.delimiter
    except csv.Error:
        sep = ","
    df = pd.read_csv(io.StringIO(texte), dtype=str, keep_default_na=False, sep=sep)
    df.columns = [str(c).strip() for c in df.columns]
    return df.astype(str)


# ═══════════════════════════════════════════════════════════
#  PRÉPARATION & VALIDATION
# ═══════════════════════════════════════════════════════════

def _vers_texte(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if pd.isna(v):
            return ""
        if v.is_integer():
            return str(int(v))
    s = str(v)
    return "" if s.lower() in ("nan", "none", "<na>") else s


def _colonnes_codifiees(spec: dict) -> set:
    """Colonnes de codes/nombres, où un espace parasite en début ou fin de
    valeur ne peut être qu'une erreur de saisie (on le retire). Les
    colonnes de libellés libres sont laissées strictement intactes."""
    return set(spec["cle"]) | set(spec["entiers"]) | set(spec["choix"]) | set(spec["fk"]) | set(spec.get("majuscules", []))


def preparer_table(nom: str, df: pd.DataFrame) -> pd.DataFrame:
    """Nettoyage avant validation/enregistrement : texte partout, espaces
    retirés, lignes entièrement vides supprimées, majuscules sur les
    colonnes codifiées, valeurs par défaut et numéro auto pour les
    nouvelles lignes."""
    spec = TABLES[nom]
    df = df.copy()
    codifiees = _colonnes_codifiees(spec)
    for col in df.columns:
        df[col] = df[col].map(_vers_texte)
        if col in codifiees:
            df[col] = df[col].str.strip()
    df = df[[c for c in df.columns if not str(c).startswith("_")]]
    if len(df):
        masque_vide = (df.apply(lambda s: s.str.strip()) == "").all(axis=1)
        df = df[~masque_vide]
    for col in spec.get("majuscules", []):
        if col in df.columns:
            df[col] = df[col].str.upper()
    for col, defaut in spec.get("valeurs_defaut", {}).items():
        if col in df.columns:
            df.loc[df[col] == "", col] = defaut
    col_id = spec.get("auto_id")
    if col_id and col_id in df.columns:
        existants = pd.to_numeric(df[col_id], errors="coerce")
        prochain = int(existants.max()) + 1 if existants.notna().any() else 1
        for idx in df.index[df[col_id] == ""]:
            df.at[idx, col_id] = str(prochain)
            prochain += 1
    # Ordre des colonnes : celui du schéma, puis les éventuelles colonnes en plus
    ordre = [c for c in spec["colonnes"] if c in df.columns] + [c for c in df.columns if c not in spec["colonnes"]]
    return df[ordre].reset_index(drop=True)


def _cle_ligne(row, cle) -> tuple:
    return tuple(row[c] for c in cle)


def diff_tables(nom: str, ancien: pd.DataFrame, nouveau: pd.DataFrame) -> dict:
    """Compare deux versions d'une table par sa clé. Retourne les clés
    ajoutées, supprimées et modifiées."""
    cle = TABLES[nom]["cle"] if nom in TABLES else list(nouveau.columns[:1])
    cols = [c for c in nouveau.columns if c in ancien.columns]

    def _index(df):
        res = {}
        cles = df[cle].to_numpy().tolist() if len(df) else []
        valeurs = df[cols].to_numpy().tolist() if len(df) else []
        for k, v in zip(cles, valeurs):
            res.setdefault(tuple(k), tuple(v))
        return res

    a, n = _index(ancien), _index(nouveau)
    ajoutees = [k for k in n if k not in a]
    supprimees = [k for k in a if k not in n]
    modifiees = [k for k in n if k in a and n[k] != a[k]]
    return {"ajoutees": ajoutees, "supprimees": supprimees, "modifiees": modifiees}


def valider_table(nom: str, df: pd.DataFrame, ancien: Optional[pd.DataFrame] = None) -> tuple[list, list]:
    """Retourne (erreurs bloquantes, avertissements).

    Les contrôles de cohérence avec les AUTRES tables (code corps / classe /
    échelon inexistant) ne portent que sur les lignes ajoutées ou modifiées :
    la base historique contient déjà quelques références orphelines, qu'on
    ne veut pas voir bloquer chaque enregistrement."""
    spec = TABLES[nom]
    erreurs, avertissements = [], []
    if df.empty:
        if ancien is not None and not ancien.empty:
            avertissements.append("La table est vide : toutes les lignes seront supprimées.")
        return erreurs, avertissements

    def _num(i):  # numéro de ligne lisible (1 = première ligne de données)
        return i + 1

    for col in spec["obligatoires"]:
        if col not in df.columns:
            erreurs.append(f"Colonne obligatoire absente : « {col} ».")
            continue
        vides = df.index[df[col].str.strip() == ""].tolist()
        if vides:
            erreurs.append(
                f"« {spec['libelles'].get(col, col)} » est obligatoire — vide sur "
                f"{len(vides)} ligne(s) (ex : ligne {', '.join(str(_num(i)) for i in vides[:5])})."
            )

    for col in spec["entiers"]:
        if col not in df.columns:
            continue
        mauvais = df.index[(df[col] != "") & (~df[col].str.fullmatch(r"-?\d+"))].tolist()
        if mauvais:
            exemples = ", ".join(f"ligne {_num(i)} = '{df.at[i, col]}'" for i in mauvais[:5])
            erreurs.append(f"« {spec['libelles'].get(col, col)} » doit être un nombre entier ({exemples}).")

    for col, valeurs in spec["choix"].items():
        if col not in df.columns:
            continue
        mauvais = df.index[(df[col] != "") & (~df[col].isin(valeurs))].tolist()
        if mauvais:
            exemples = ", ".join(f"ligne {_num(i)} = '{df.at[i, col]}'" for i in mauvais[:5])
            erreurs.append(
                f"« {spec['libelles'].get(col, col)} » doit valoir {' / '.join(valeurs)} ({exemples})."
            )

    cle = [c for c in spec["cle"] if c in df.columns]
    if cle:
        doublons = df[df.duplicated(cle, keep=False)]
        if not doublons.empty:
            valeurs = sorted({" / ".join(v) for v in doublons[cle].to_numpy().tolist()})
            erreurs.append(
                f"Identifiant en double ({' + '.join(spec['libelles'].get(c, c) for c in cle)}) : "
                f"{', '.join(valeurs[:8])}{' …' if len(valeurs) > 8 else ''}."
            )

    # Lignes nouvelles/modifiées (pour les contrôles « doux »)
    if ancien is not None and not ancien.empty:
        cols_communes = [c for c in df.columns if c in ancien.columns]
        anciennes = set(map(tuple, ancien[cols_communes].to_numpy().tolist()))
        masque_change = pd.Series(
            [tuple(v) not in anciennes for v in df[cols_communes].to_numpy().tolist()], index=df.index
        )
    else:
        masque_change = pd.Series(True, index=df.index)
    lignes_changees = df[masque_change]

    unique_av = spec.get("unique_avertissement")
    if unique_av and not lignes_changees.empty:
        dup = df[df.duplicated(unique_av, keep=False)]
        cles_dup = set(map(tuple, dup[unique_av].to_numpy().tolist()))
        cles_dup_changees = sorted(set(map(tuple, lignes_changees[unique_av].to_numpy().tolist())) & cles_dup)
        if cles_dup_changees:
            avertissements.append(
                "Plusieurs durées pour le même (corps, classe, échelon) : "
                + ", ".join(" / ".join(k) for k in cles_dup_changees[:8])
                + " — seule la dernière ligne sera retenue par l'IA."
            )

    for col, (table_cible, col_cible) in spec["fk"].items():
        if col not in df.columns or lignes_changees.empty:
            continue
        try:
            valeurs_valides = set(charger_table(table_cible)[col_cible])
        except Exception:
            continue
        inconnues = sorted(set(lignes_changees[col]) - valeurs_valides - {""})
        if inconnues:
            avertissements.append(
                f"« {spec['libelles'].get(col, col)} » : code(s) inexistant(s) dans "
                f"{TABLES[table_cible]['titre']} ({table_cible}) : {', '.join(inconnues[:10])}"
                f"{' …' if len(inconnues) > 10 else ''}. Ces lignes ne seront jamais utilisées "
                f"tant que le code n'y est pas créé."
            )

    # Suppressions référencées par d'autres tables
    if ancien is not None and nom in REFERENCES_INVERSES and spec["cle"]:
        cle_col = spec["cle"][0]
        supprimes = set(ancien[cle_col]) - set(df[cle_col])
        if supprimes:
            for table_src, col_src, _ in REFERENCES_INVERSES[nom]:
                try:
                    src = charger_table(table_src)
                except Exception:
                    continue
                n = int(src[col_src].isin(supprimes).sum())
                if n:
                    avertissements.append(
                        f"{len(supprimes)} code(s) supprimé(s) sont encore utilisés par {n} ligne(s) "
                        f"de {TABLES[table_src]['titre']} ({table_src}) — ces lignes deviendront inutilisables."
                    )

    return erreurs, avertissements


# ═══════════════════════════════════════════════════════════
#  ÉCRITURE ATOMIQUE, SAUVEGARDE, JOURNAL
# ═══════════════════════════════════════════════════════════

def _horodatage() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _archiver(nom: str) -> Optional[str]:
    """Copie la version ACTUELLE du fichier dans _historique/<nom>/ avant
    qu'elle soit remplacée. Retourne le nom de la copie (ou None)."""
    source = chemin_fichier(nom)
    if not source.exists():
        return None
    dossier = dossier_historique() / nom
    dossier.mkdir(parents=True, exist_ok=True)
    cible = dossier / f"{_horodatage()}__{nom}"
    shutil.copy2(source, cible)
    return cible.name


def _ecrire_atomique(chemin: Path, contenu: bytes):
    """Écrit dans un fichier temporaire du même dossier puis le renomme :
    l'API ne peut jamais lire un fichier à moitié écrit."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(chemin.parent), prefix=f".{chemin.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(contenu)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, chemin)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def journaliser(entree: dict):
    entree = {"date": datetime.now().isoformat(timespec="seconds"), **entree}
    with open(chemin_journal(), "a", encoding="utf-8") as f:
        f.write(json.dumps(entree, ensure_ascii=False) + "\n")


def lire_journal(limite: int = 200, fichier: Optional[str] = None) -> list[dict]:
    chemin = chemin_journal()
    if not chemin.exists():
        return []
    entrees = []
    with open(chemin, "r", encoding="utf-8") as f:
        for ligne in f:
            ligne = ligne.strip()
            if not ligne:
                continue
            try:
                e = json.loads(ligne)
            except json.JSONDecodeError:
                continue
            if fichier and e.get("fichier") != fichier:
                continue
            entrees.append(e)
    return list(reversed(entrees))[:limite]


def table_vers_csv(df: pd.DataFrame) -> bytes:
    tampon = io.StringIO()
    df.to_csv(tampon, index=False, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    return tampon.getvalue().encode("utf-8")


def sauvegarder_table(nom: str, df: pd.DataFrame, auteur: str = "admin",
                      commentaire: str = "", action: str = "modification") -> dict:
    """Prépare, valide, archive l'ancienne version, écrit la nouvelle et
    journalise. Lève ValueError si des erreurs bloquantes subsistent."""
    ancien = charger_table(nom)
    df = preparer_table(nom, df)
    erreurs, avertissements = valider_table(nom, df, ancien)
    if erreurs:
        raise ValueError("\n".join(erreurs))
    d = diff_tables(nom, ancien, df)
    archive = _archiver(nom)
    _ecrire_atomique(chemin_fichier(nom), table_vers_csv(df))
    resume = {
        "fichier": nom, "action": action, "auteur": auteur or "admin",
        "commentaire": commentaire,
        "ajoutees": len(d["ajoutees"]), "modifiees": len(d["modifiees"]),
        "supprimees": len(d["supprimees"]),
        "exemples_ajoutees": [" / ".join(k) for k in d["ajoutees"][:10]],
        "exemples_modifiees": [" / ".join(k) for k in d["modifiees"][:10]],
        "exemples_supprimees": [" / ".join(k) for k in d["supprimees"][:10]],
        "nb_lignes": len(df), "archive": archive,
    }
    journaliser(resume)
    return {**resume, "avertissements": avertissements}


def charger_texte(nom: str) -> str:
    chemin = chemin_fichier(nom)
    if not chemin.exists():
        return ""
    with open(chemin, "r", encoding="utf-8-sig", newline="") as f:
        return f.read()


def sauvegarder_texte(nom: str, texte: str, auteur: str = "admin", commentaire: str = "",
                      action: str = "modification", details: Optional[dict] = None) -> dict:
    archive = _archiver(nom)
    _ecrire_atomique(chemin_fichier(nom), texte.encode("utf-8"))
    resume = {
        "fichier": nom, "action": action, "auteur": auteur or "admin",
        "commentaire": commentaire, "archive": archive, **(details or {}),
    }
    journaliser(resume)
    return resume


def lister_sauvegardes(nom: str) -> list[dict]:
    dossier = dossier_historique() / nom
    if not dossier.exists():
        return []
    res = []
    for p in sorted(dossier.iterdir(), reverse=True):
        if not p.is_file():
            continue
        horo = p.name.split("__", 1)[0]
        try:
            date = datetime.strptime(horo, "%Y%m%d-%H%M%S-%f")
        except ValueError:
            date = datetime.fromtimestamp(p.stat().st_mtime)
        res.append({"fichier": p.name, "chemin": str(p), "date": date, "taille": p.stat().st_size})
    return res


def restaurer_sauvegarde(nom: str, nom_archive: str, auteur: str = "admin") -> dict:
    """Remet en service une ancienne version (la version actuelle est elle-
    même archivée avant, donc une restauration est toujours annulable)."""
    source = dossier_historique() / nom / Path(nom_archive).name
    if not source.exists():
        raise FileNotFoundError(f"Sauvegarde introuvable : {nom_archive}")
    contenu = source.read_bytes()
    if nom in TABLES:
        ancien = charger_table(nom)
        nouveau = lire_csv_televerse(contenu)
        d = diff_tables(nom, ancien, nouveau)
        details = {"ajoutees": len(d["ajoutees"]), "modifiees": len(d["modifiees"]), "supprimees": len(d["supprimees"])}
    else:
        details = {}
    archive = _archiver(nom)
    _ecrire_atomique(chemin_fichier(nom), contenu)
    resume = {"fichier": nom, "action": "restauration", "auteur": auteur or "admin",
              "commentaire": f"Restauration de la version {nom_archive}", "archive": archive, **details}
    journaliser(resume)
    return resume


# ═══════════════════════════════════════════════════════════
#  RÉFÉRENCES LÉGALES (VISA) PAR CORPS — corps_references_RAG.txt
#
#  Format d'un bloc :
#     === CORPS : <LIBELLÉ> ===
#     Code : <code> | Type : <FONCT|NON_FONCT|...> | Âge de retraite : <n> ans
#     Références juridiques :
#       - <texte complet de la référence 1>
#       - <texte complet de la référence 2>
#
#  L'API ne vérifie que les NUMÉROS de texte (ex : 61-33, 97-17) repérés
#  dans chaque ligne par le motif « n° XX-YY ».
# ═══════════════════════════════════════════════════════════

RE_NUMERO_REFERENCE = re.compile(r"n°\s*(\d{2,4}-\d{1,4})")
_RE_TITRE_BLOC = re.compile(r"^(?:=== CORPS)?\s*:\s*(.+?)\s*===\s*$", re.MULTILINE)


def numeros_reference(ligne: str) -> Optional[str]:
    m = RE_NUMERO_REFERENCE.search(" ".join(ligne.split()))
    return m.group(1) if m else None


def parser_references(texte: str) -> list[dict]:
    texte = texte.replace("\r\n", "\n")
    morceaux = _RE_TITRE_BLOC.split(texte)
    corps = []
    for i in range(1, len(morceaux), 2):
        libelle = morceaux[i].strip()
        corps_bloc = morceaux[i + 1] if i + 1 < len(morceaux) else ""
        m_code = re.search(r"Code\s*:\s*([^|\n]+)", corps_bloc)
        m_type = re.search(r"Type\s*:\s*([^|\n]+)", corps_bloc)
        m_age = re.search(r"retraite\s*:\s*(\d+)", corps_bloc)
        refs = [" ".join(l.split()) for l in re.findall(r"^\s*-\s*(.+)$", corps_bloc, re.MULTILINE)]
        corps.append({
            "libelle": libelle,
            "code": (m_code.group(1).strip() if m_code else ""),
            "type": (m_type.group(1).strip() if m_type else ""),
            "age_retraite": (m_age.group(1) if m_age else ""),
            "references": [r for r in refs if r],
        })
    return corps


def generer_references(corps: list[dict], fin_ligne: str = "\r\n") -> str:
    blocs = []
    for c in corps:
        lignes = [
            f"=== CORPS : {c['libelle'].strip()} ===",
            f"Code : {c['code'].strip()} | Type : {c['type'].strip()} | Âge de retraite : {str(c.get('age_retraite') or '60').strip()} ans",
            "Références juridiques :",
        ]
        lignes += [f"  - {r.strip()}" for r in c.get("references", []) if r.strip()]
        blocs.append(fin_ligne.join(lignes))
    return (fin_ligne + fin_ligne).join(blocs) + fin_ligne


def charger_references_visa() -> tuple[list[dict], str]:
    """Retourne (liste des corps, fin de ligne utilisée dans le fichier)."""
    texte = charger_texte(FICHIER_REFERENCES_VISA)
    fin_ligne = "\r\n" if "\r\n" in texte else "\n"
    return parser_references(texte), fin_ligne


def valider_corps_reference(c: dict, tous: list[dict], code_original: Optional[str] = None) -> tuple[list, list]:
    erreurs, avertissements = [], []
    if not c["libelle"].strip():
        erreurs.append("Le libellé du corps est obligatoire.")
    if not c["code"].strip():
        erreurs.append("Le code du corps est obligatoire.")
    if c["type"] not in TYPES_CORPS:
        erreurs.append(f"Le type doit valoir {' / '.join(TYPES_CORPS)}.")
    if c.get("age_retraite") and not str(c["age_retraite"]).isdigit():
        erreurs.append("L'âge de retraite doit être un nombre entier.")
    autres = [x for x in tous if x["code"] != (code_original if code_original is not None else c["code"])]
    if c["code"].strip() and any(x["code"] == c["code"].strip() for x in autres):
        erreurs.append(f"Le code {c['code']} possède déjà un bloc de références.")
    if not c["references"]:
        avertissements.append("Aucune référence : l'IA retombera sur la règle générale FONCT / NON_FONCT pour ce corps.")
    sans_numero = [r for r in c["references"] if not numeros_reference(r)]
    if sans_numero:
        avertissements.append(
            f"{len(sans_numero)} référence(s) sans numéro au format « n°XX-YY » : elles seront "
            "conservées dans le texte mais NE SERONT PAS contrôlées dans l'acte."
        )
    try:
        codes_corps = set(charger_table("corps.csv")["cps_code"])
        if c["code"].strip() and c["code"].strip() not in codes_corps:
            avertissements.append(
                f"Le code {c['code']} n'existe pas dans la table des corps (corps.csv) : "
                "ce bloc ne sera utilisé que lorsque le corps y aura été créé."
            )
    except Exception:
        pass
    return erreurs, avertissements


def sauvegarder_references_visa(corps: list[dict], fin_ligne: str, auteur: str, commentaire: str,
                                action: str, code: str) -> dict:
    texte = generer_references(corps, fin_ligne)
    return sauvegarder_texte(
        FICHIER_REFERENCES_VISA, texte, auteur=auteur, commentaire=commentaire,
        action=action, details={"corps": code, "nb_corps": len(corps)},
    )