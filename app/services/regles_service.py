"""Service de vérification des règles métier des actes RH."""

import re
import unicodedata
import logging

logger = logging.getLogger(__name__)

SIGNATAIRE_OFFICIEL = "LE MINISTRE DE LA FONCTION PUBLIQUE, DU TRAVAIL ET DE LA REFORME DU SERVICE PUBLIC,"


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
        r'(LE MINISTRE. ?SERVICE PUBLIC[,\.]?)', acte, re.IGNORECASE | re.DOTALL
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

    # 4) CORPS
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
    ok_A = (
        len(e) >= 3
        and "REPUBLIQUE DU SENEGAL" in entete_joined
        and "FONCTION PUBLIQUE" in entete_joined
        and "TRAVAIL" in entete_joined
        and has_dashes
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


def detecter_type_acte(acte_text: str) -> str:
    """Détecte le type d'acte : retraite / avancement / autre."""
    texte_normalise = unicodedata.normalize("NFKD", acte_text.lower()).encode("ascii", "ignore").decode("ascii")

    if "retraite" in texte_normalise and ("fonctionnaire" in texte_normalise or "61-33" in texte_normalise):
        return "retraite_fonctionnaire"
    if "avancement" in texte_normalise and "echelon" in texte_normalise:
        return "avancement_echelon"
    return "autre"