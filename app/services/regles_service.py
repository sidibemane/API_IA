"""Service de vérification des règles métier des actes RH."""

import os
import re
import unicodedata
import logging

import pandas as pd

logger = logging.getLogger(__name__)

SIGNATAIRE_OFFICIEL = "LE MINISTRE DE LA FONCTION PUBLIQUE, DU TRAVAIL ET DE LA REFORME DU SERVICE PUBLIC,"


# ═══════════════════════════════════════════════════════════
#  TABLE DE PARAMÉTRAGE OFFICIELLE type_acte → circuit (GIRAFE)
#
#  Fichier : parametrage_type_acte_workflow.csv, colonnes :
#    - nature      : "ARRETE", "DECISION", "DECIDE" (nature juridique de l'acte)
#    - type_acte   : code numérique interne GIRAFE identifiant précisément
#                    le type d'acte (ex: 5, 191, 198...)
#    - ref_engine  : le circuit à appliquer, un des 3 codes suivants :
#        APAE = circuit COURT de l'avancement d'échelon (9 étapes)
#        APAG = circuit LONG ("autre" type d'acte — 12 étapes)
#        RET  = circuit de la RETRAITE fonctionnaire (13 étapes)
#
#  Quand GIRAFE fournit le couple (nature, type_acte) à l'appel de l'API,
#  on consulte CETTE table directement — 100% fiable, car basée sur le
#  vrai référentiel GIRAFE. La détection par mots-clés dans le texte de
#  l'acte (plus bas dans ce fichier) ne sert plus que de REPLI, pour les
#  cas où (nature, type_acte) n'est pas fourni (ex: tests via le dashboard
#  sans passer par GIRAFE).
# ═══════════════════════════════════════════════════════════

_REF_ENGINE_VERS_TYPE_ACTE = {
    "APAE": "avancement_echelon",
    "APAG": "autre",
    "RET": "retraite_fonctionnaire",
}

_PARAMETRAGE_WORKFLOW_INDEX: dict = {}  # (nature_normalisee, type_acte_int) -> "avancement_echelon" / "autre" / "retraite_fonctionnaire"


def _charger_parametrage_workflow():
    """Charge parametrage_type_acte_workflow.csv en mémoire, une seule
    fois au démarrage. Si le fichier est absent, on continue sans lui —
    la détection retombe alors entièrement sur les mots-clés (repli)."""
    global _PARAMETRAGE_WORKFLOW_INDEX
    try:
        chemin = os.path.join(os.getenv("DATA_DIR", "./app/data"), "parametrage_type_acte_workflow.csv")
        df = pd.read_csv(chemin)
        index = {}
        for _, ligne in df.iterrows():
            nature_norm = str(ligne["nature"]).strip().upper()
            try:
                type_acte_int = int(ligne["type_acte"])
            except (TypeError, ValueError):
                continue
            ref_engine = str(ligne["ref_engine"]).strip().upper()
            type_circuit = _REF_ENGINE_VERS_TYPE_ACTE.get(ref_engine)
            if type_circuit:
                index[(nature_norm, type_acte_int)] = type_circuit
        _PARAMETRAGE_WORKFLOW_INDEX = index
        logger.info(f"✅ Paramétrage type_acte → circuit chargé : {len(index)} entrées")
    except Exception as e:
        logger.warning(f"⚠️ Paramétrage type_acte → circuit non chargé ({e}) — repli sur les mots-clés uniquement.")
        _PARAMETRAGE_WORKFLOW_INDEX = {}


_charger_parametrage_workflow()


def detecter_type_acte_par_parametrage(nature: str, type_acte) -> str:
    """Cherche le circuit applicable dans la VRAIE table de paramétrage
    GIRAFE, à partir du couple (nature, type_acte). Retourne None si le
    couple n'est pas fourni ou n'est pas trouvé dans la table — dans ce
    cas, l'appelant doit se rabattre sur la détection par mots-clés
    (detecter_type_acte, plus bas)."""
    if not nature or type_acte is None:
        return None
    try:
        type_acte_int = int(type_acte)
    except (TypeError, ValueError):
        return None
    nature_norm = str(nature).strip().upper()
    return _PARAMETRAGE_WORKFLOW_INDEX.get((nature_norm, type_acte_int))


def calcul_delai_annees(d1: str, d2: str) -> int:
    try:
        d1_clean = d1.replace(".", "/").replace("  ", " ")
        d2_clean = d2.replace(".", "/").replace("  ", " ")
        return int(d2_clean.split("/")[2]) - int(d1_clean.split("/")[2])
    except Exception:
        return -1


def get_delai_reglementaire(statut: str, hierarchie: str, classe: str, corps: str = ""):
    if statut == "NF":
        if hierarchie in ("B1", "B2") and classe == "2CL":
            return 3, "RÈGLE PAT3-01 (Pattern 3 : NF hiérarchie B en 2CL — 3 ans)"
        if classe == "1CL" and hierarchie in ("A1", "A2", "A3", "AS"):
            return 3, "RÈGLE AE-02b (NF hiérarchie A en 1CL — 3 ans)"
        return 2, "RÈGLE AE-02 (NF hiérarchie B, C, D ou A en 2CL/3CL/4CL — 2 ans)"
    else:
        if classe in ("4CL", "3CL"):
            return 2, "RÈGLE V-03 (FONCTIONNAIRE en 4CL/3CL — 2 ans)"
        else:
            return 3, "RÈGLE V-03 (FONCTIONNAIRE en 2CL/1CL/CEX — 3 ans)"


def extraire_infos_acte(acte: str) -> dict:
    """Extraction complète des infos d'un acte (en-tête, type, signataire, corps, dates)."""
    infos = {
        "corps": "", "statut": "INCONNU", "hierarchie": "", "classe": "",
        "type_acte": "", "signataire": "",
        "date_ancien_grade": "", "date_nouveau_grade": "",
        "entete_lignes": [], "ancien_grade_label": "", "nouveau_grade_label": "",
    }

    texte_upper = acte.upper()
    texte_normalise = "  ".join(texte_upper.split())
    lignes = acte.split("\n")

    # 1) EN-TÊTE
    entete_candidates = []
    for l in lignes:
        ls = l.strip()
        if not ls or len(ls) < 3:
            continue
        if re.match(r'^[A-Z]{1,3}$|^\d+$|^N°|^\(?\d', ls):
            continue
        if "Objet" in ls or "LE MINISTRE" in ls.upper() or ls.upper().startswith("VU "):
            break
        entete_candidates.append(ls)
        if len(entete_candidates) >= 5:
            break
    infos["entete_lignes"] = entete_candidates

    # 2) SIGNATAIRE
    match_sign = re.search(
        r'(LE MINISTRE.*?SERVICE PUBLIC[,\.]?)', acte, re.IGNORECASE | re.DOTALL
    )
    if match_sign:
        infos["signataire"] = " ".join(match_sign.group(1).split()).rstrip(",.")
    else:
        for ligne in lignes:
            if "LE MINISTRE" in ligne.upper() and len(ligne.strip()) > 20:
                infos["signataire"] = " ".join(ligne.strip().split()).rstrip(",.")
                break

    # 3) TYPE D'ACTE
    for ligne in lignes:
        l_low = ligne.strip().lower()
        if "objet" in l_low or "portant" in l_low:
            if "décision" in l_low:
                infos["type_acte"] = "DÉCISION"
                break
            if "arrêté" in l_low or "arrete" in l_low:
                infos["type_acte"] = "ARRÊTÉ"
                break
    if not infos["type_acte"]:
        if "DÉCISION" in texte_upper or "DECISION" in texte_upper:
            infos["type_acte"] = "DÉCISION"
        elif "ARRÊTÉ" in texte_upper or "ARRETE" in texte_upper:
            infos["type_acte"] = "ARRÊTÉ"

    # 4) CORPS — recherche EN PRIORITÉ dans la vraie table de référence des
    # corps (corps.csv), via le même mécanisme robuste déjà utilisé pour la
    # vérification du visa (detecter_corps_depuis_texte). Beaucoup plus
    # fiable qu'une liste codée en dur limitée à une vingtaine de corps :
    # cette table couvre l'intégralité des corps officiels de la fonction
    # publique, quel que soit le corps mentionné dans l'acte.
    try:
        from app.services.agents_service import detecter_corps_depuis_texte, CPS_INFOS_PAR_CODE  # import différé (anti-cycle)
        code_corps = detecter_corps_depuis_texte(acte)
        if code_corps:
            infos["corps"] = CPS_INFOS_PAR_CODE.get(code_corps, {}).get("cps_libelle", "")
    except Exception as e:
        logger.warning(f"Détection du corps via la table de référence indisponible ({e}) — repli sur l'ancienne méthode.")

    # Repli : ancienne méthode (liste codée en dur + regex), utilisée
    # uniquement si la table de référence n'a rien trouvé — par exemple un
    # environnement de test minimal sans les fichiers CSV chargés.
    corps_cibles = [
        "INSTITUTEURS ADJOINTS NF REF", "INSTITUTEURS ADJOINTS", "INSTITUTEUR ADJOINT",
        "PROFESSEURS DE CEM", "PROFESSEUR DE CEM", "PROFESSEURS DES CEM",
        "ADMINISTRATEURS CIVILS", "ATTACHES D'ADMINISTRATION",
        "JURISTES NF", "JURISTES CONSEILS", "STATISTICIEN NF", "STATISTICIENS NF",
        "SOCIOLOGUES", "SOCIO-ECONOMISTE", "COMMIS D'ADMINISTRATION",
        "AGENTS ADMINISTRATIFS", "ADJOINTS INTENDANTS",
        "TECHNICIENS MEDICAUX", "CHAUFFEUR ET CONDUCTEUR",
        "COMPTABLES NF", "SECRETAIRES D'ADMINISTRATION",
        "MAITRES CONTRACTUELS", "MAÎTRES CONTRACTUELS",
        "SPECIALISTES FONCTION PUBLIQUE", "CONSEILLERS JURIDIQUES",
    ]
    if not infos["corps"]:
        for corps_ref in corps_cibles:
            if corps_ref in texte_normalise:
                infos["corps"] = corps_ref
                break

    if not infos["corps"]:
        m_corps = re.search(
            r'([A-ZÉÈ][A-ZÉÈ\s\-]{3,40}?)\s+(?:hiérarchie\b|NF\b|REF\b|matricule\b|née\b)',
            texte_normalise,
        )
        if m_corps:
            infos["corps"] = m_corps.group(1).strip()

    # 5) STATUT
    is_nf = " NF" in infos["corps"].upper() or infos["corps"].upper().endswith("NF")
    has_nf_laws = "97-17" in acte or "74-347" in acte
    infos["statut"] = "NF" if (has_nf_laws or is_nf) else "FONCTIONNAIRE"

    # 6) HIÉRARCHIE
    for h in ("AS", "A1", "A2", "A3", "B1", "B2", "B3", "B4",
              "C1", "C2", "C3", "C4", "D1", "D2", "D3", "D4"):
        if re.search(rf'\b{h}\b', acte):
            infos["hierarchie"] = h
            break

    # 7) GRADES ET DATES
    match_situation = re.search(r'est régularisée ainsi qu[\'’]il suit', acte, re.IGNORECASE)
    bloc_sit = acte[match_situation.start():match_situation.start() + 1500] if match_situation else acte
    bloc_sit_clean = re.sub(r'(?:née?|né)\s+le\s+\d{2}[./]\d{2}[./]\d{4}', '', bloc_sit, flags=re.IGNORECASE)

    pattern_grade = r'(\d+CL\s+\d+ECH|(?:Première|Deuxième|Troisième|Quatrième|Principale|Stagiaire)\s+(?:Classe|Échelon)?)'
    grades_bruts = re.findall(pattern_grade, bloc_sit_clean, re.IGNORECASE)
    grades_uniques = []
    for g in grades_bruts:
        g_clean = g.strip().upper().replace("   ", " ")
        if g_clean not in grades_uniques and len(g_clean) >= 5:
            grades_uniques.append(g_clean)

    if len(grades_uniques) >= 2:
        infos["ancien_grade_label"] = grades_uniques[0]
        infos["nouveau_grade_label"] = grades_uniques[-1]
        for cl in ("1CL", "2CL", "3CL", "4CL"):
            if cl in infos["nouveau_grade_label"]:
                infos["classe"] = cl
                break

    dates_brutes = re.findall(r'(\d{2}[./]\d{2}[./]\d{4})', bloc_sit_clean)
    dates_propres = []
    for d in dates_brutes:
        d_clean = d.replace(".", "/")
        try:
            j, m, a = map(int, d_clean.split("/"))
            if 1950 <= a <= 2050 and 1 <= m <= 12 and 1 <= j <= 31:
                if d_clean not in dates_propres:
                    dates_propres.append(d_clean)
        except:
            continue
    if len(dates_propres) >= 2:
        infos["date_ancien_grade"] = dates_propres[0]
        infos["date_nouveau_grade"] = dates_propres[-1]

    return infos


def verifier_points_abc(acte_text: str) -> dict:
    """Vérifie les points A (en-tête), B (type), C (signataire)."""
    infos = extraire_infos_acte(acte_text)

    # Point A — En-tête
    e = infos["entete_lignes"]
    entete_joined = " ".join(x.upper() for x in e)
    has_dashes = "UN PEUPLE – UN BUT – UNE FOI" in entete_joined or "UN PEUPLE - UN BUT - UNE FOI" in entete_joined
    has_devise = "UN PEUPLE" in entete_joined and "UN BUT" in entete_joined and "UNE FOI" in entete_joined
    ok_A = (
        len(e) >= 3
        and "REPUBLIQUE DU SENEGAL" in entete_joined
        and "FONCTION PUBLIQUE" in entete_joined
        and "TRAVAIL" in entete_joined
        and has_devise
    )

    # Point B — Type d'acte
    statut = infos["statut"]
    type_constate = infos["type_acte"].split()[0].upper() if infos["type_acte"] else ""
    type_attendu = "DÉCISION" if statut == "NF" else "ARRÊTÉ"
    ok_B = type_attendu[:4] in type_constate.upper() if type_constate else False

    # Point C — Signataire
    sign_constate = infos["signataire"]
    sign_c_norm = " ".join(sign_constate.upper().split()).rstrip(",.")
    sign_a_norm = " ".join(SIGNATAIRE_OFFICIEL.upper().split()).rstrip(",.")
    ok_C = sign_c_norm == sign_a_norm

    logger.info(
        f"DIAGNOSTIC point_A/C — entete_joined={entete_joined!r} | has_dashes={has_dashes} "
        f"| ok_A={ok_A} | sign_constate={sign_constate!r} | sign_c_norm={sign_c_norm!r} "
        f"| sign_a_norm={sign_a_norm!r} | ok_C={ok_C}"
    )

    return {
        "point_A": {"conforme": ok_A, "constate": entete_joined[:200]},
        "point_B": {"conforme": ok_B, "constate": type_constate, "attendu": type_attendu, "statut": statut},
        "point_C": {"conforme": ok_C, "constate": sign_constate, "attendu": SIGNATAIRE_OFFICIEL},
        "infos": infos,
    }


def detecter_type_acte(acte_text: str, nature: str = None, type_acte=None) -> str:
    """Détecte le type d'acte (circuit à appliquer) : retraite
    (fonctionnaire uniquement) / avancement_echelon / autre.

    PRIORITÉ 1 — table de paramétrage officielle : si GIRAFE fournit
    'nature' (ARRETE/DECISION/DECIDE) et 'type_acte' (code numérique
    interne), on consulte directement parametrage_type_acte_workflow.csv
    — 100% fiable, basé sur le vrai référentiel GIRAFE, aucune ambiguïté
    possible contrairement à une recherche de mots-clés.

    PRIORITÉ 2 — repli par mots-clés : utilisé uniquement si (nature,
    type_acte) n'est pas fourni, ou si le couple n'existe pas dans la
    table (ex: tests via le dashboard, sans passer par GIRAFE).

    La distinction fonctionnaire / non-fonctionnaire (pour le repli
    uniquement) se base EN PRIORITÉ sur les références légales
    obligatoires citées dans l'acte (RÈGLE V-01 de la base de
    connaissance métier) :
      - Non-fonctionnaire (NF) : Loi n°97-17 (Code du travail) et/ou
        Décret n°74-347
      - Fonctionnaire : Loi n°61-33 (statut général des fonctionnaires)

    C'est plus fiable qu'une simple recherche du mot "fonctionnaire", qui
    apparaît aussi à l'intérieur de "non fonctionnaire". Les mots-clés
    textuels ne servent qu'en tout dernier recours, si aucune des deux
    références légales n'est trouvée dans le texte.

    Important : un acte de retraite pour un NON-fonctionnaire ne doit PAS
    être classé "retraite_fonctionnaire" (circuit à 13 étapes avec tampon
    DP) — il doit tomber sur le circuit "autre" (12 étapes, sans DP).

    Important (2) : la détection par mots-clés s'ancre sur la vraie ligne
    "Objet :" de l'acte, PAS sur tout le corps du texte. Un acte de
    régularisation peut très bien mentionner "l'avancement de grade et
    d'échelon" dans une phrase administrative sans que ce soit lui-même
    un acte d'avancement — chercher ces mots dans tout le texte le
    classerait alors, à tort, dans le circuit "avancement_echelon"."""

    # ── PRIORITÉ 1 : table de paramétrage officielle ──
    type_via_parametrage = detecter_type_acte_par_parametrage(nature, type_acte)
    if type_via_parametrage:
        return type_via_parametrage

    # ── PRIORITÉ 2 : repli par mots-clés (nature/type_acte absent(s) ──
    texte_normalise = unicodedata.normalize("NFKD", acte_text.lower()).encode("ascii", "ignore").decode("ascii")

    m_objet = re.search(r"objet\s*:?\s*(.{0,150})", texte_normalise)
    objet = m_objet.group(1) if m_objet else texte_normalise[:200]

    est_retraite = "retraite" in objet

    cite_loi_non_fonctionnaire = "97-17" in texte_normalise or "74-347" in texte_normalise
    cite_loi_fonctionnaire = "61-33" in texte_normalise

    if cite_loi_fonctionnaire and not cite_loi_non_fonctionnaire:
        est_fonctionnaire = True
    elif cite_loi_non_fonctionnaire and not cite_loi_fonctionnaire:
        est_fonctionnaire = False
    else:
        # Repli sur les mots-clés textuels si aucune référence légale
        # claire (ou les deux à la fois, cas ambigu) n'a été trouvée.
        est_non_fonctionnaire_mot = "non fonctionnaire" in texte_normalise or "non-fonctionnaire" in texte_normalise
        est_fonctionnaire = "fonctionnaire" in texte_normalise and not est_non_fonctionnaire_mot

    if est_retraite and est_fonctionnaire:
        return "retraite_fonctionnaire"
    if "avancement" in objet and "echelon" in objet:
        return "avancement_echelon"
    return "autre"