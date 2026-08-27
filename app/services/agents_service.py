"""
Service Agents — vérification d'identité (matricule/nom/prénom/date de
naissance) contre la base du personnel, et vérification des délais
réglementaires d'avancement grade/échelon via les tables de référence
(corps.csv, classe.csv, echelon.csv, corps_classe_echelon.csv).

Ce module était vide dans l'API déployée : toute cette logique existait
dans le notebook (cellules 11 et 12) mais n'avait jamais été portée ici,
ce qui explique l'absence de vérification matricule/nom/prénom et de
calcul de délai d'avancement dans les résultats de l'API.
"""

import difflib
import json
import logging
import os
import re
import unicodedata

import pandas as pd

from app.config import get_settings
from app.services.regles_service import calcul_delai_annees, get_delai_reglementaire

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
#  1. BASE DES AGENTS
# ═══════════════════════════════════════════════════════════

def _normaliser_texte_identite(texte) -> str:
    """Normalise pour comparaison : majuscules, sans accents, espaces uniques."""
    if not texte:
        return ""
    t = str(texte).upper().strip()
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii")
    return " ".join(t.split())


def _normaliser_date(date_str) -> str:
    """Normalise une date en JJ/MM/AAAA, quel que soit le séparateur d'origine."""
    if not date_str:
        return ""
    chiffres = re.findall(r"\d+", str(date_str))
    if len(chiffres) != 3:
        return _normaliser_texte_identite(date_str)
    j, m, a = chiffres
    if len(a) == 2:
        a = ("19" + a) if int(a) > 30 else ("20" + a)
    return f"{int(j):02d}/{int(m):02d}/{a}"


def _construire_index_matricule(base_agents: list) -> dict:
    return {_normaliser_texte_identite(a.get("matricule")): a for a in base_agents if a.get("matricule")}


def _charger_base_agents() -> list:
    settings = get_settings()
    chemin = os.path.join(settings.data_dir, "base_agents.json")
    try:
        with open(chemin, "r", encoding="utf-8") as f:
            base = json.load(f)
        if not isinstance(base, list):
            logger.error(f"{chemin} doit contenir une LISTE d'agents — base ignorée.")
            return []
        logger.info(f"✅ Base agents chargée : {len(base)} agent(s) depuis {chemin}")
        return base
    except FileNotFoundError:
        logger.warning(f"⚠️ {chemin} introuvable — vérification identité désactivée (aucun agent en base).")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"⚠️ {chemin} contient du JSON invalide : {e} — vérification identité désactivée.")
        return []


BASE_AGENTS = _charger_base_agents()
INDEX_AGENTS_PAR_MATRICULE = _construire_index_matricule(BASE_AGENTS)


def recharger_base_agents():
    """Permet de recharger la base sans redémarrer l'API (ex: après mise à jour du fichier)."""
    global BASE_AGENTS, INDEX_AGENTS_PAR_MATRICULE
    BASE_AGENTS = _charger_base_agents()
    INDEX_AGENTS_PAR_MATRICULE = _construire_index_matricule(BASE_AGENTS)
    return len(BASE_AGENTS)


# ═══════════════════════════════════════════════════════════
#  2. EXTRACTION DE L'IDENTITÉ DEPUIS LE TEXTE DE L'ACTE
# ═══════════════════════════════════════════════════════════

_RE_MATRICULE = re.compile(r"\b(\d{6}[A-Z])\b")
_RE_NOM_NARRATIF = re.compile(
    r"(?:Monsieur|Madame|Mademoiselle)\s+((?:[A-ZÀ-Ü][a-zà-ÿ\-]+\s+){1,3})([A-ZÀ-Ü]{2,}(?:[\-\s][A-ZÀ-Ü]{2,})*)\s*,?\s*$"
)
_RE_NOM_TABLEAU = re.compile(
    r"((?:[A-ZÀ-Ü][a-zà-ÿ\-]+\s+){1,3})([A-ZÀ-Ü]{2,}(?:[\-\s][A-ZÀ-Ü]{2,})*)\s*\n?\s*$"
)
_RE_DATE_NAISSANCE = re.compile(
    r"n[ée]\(?e?\)?\s+le\s+(\d{1,2}\s*[/\-.]\s*\d{1,2}\s*[/\-.]\s*\d{2,4})", re.IGNORECASE
)
_RE_PREFIXE_MATRICULE = re.compile(r"matricule\s*(?:de\s*solde)?\s*n?°?\s*:?\s*$", re.IGNORECASE)


def extraire_identite_agent(acte_text: str) -> list:
    """Repère chaque matricule (6 chiffres + 1 lettre) dans l'acte et tente
    d'identifier le nom/prénom juste avant, ainsi que la date de naissance
    à proximité. Retourne une liste (un acte peut concerner plusieurs agents)."""
    agents = []
    matches = list(_RE_MATRICULE.finditer(acte_text))
    for i, m in enumerate(matches):
        matricule = m.group(1)
        fenetre_avant = acte_text[max(0, m.start() - 220):m.start()]
        fenetre_avant = _RE_PREFIXE_MATRICULE.sub("", fenetre_avant)

        nom, prenom = None, None
        m_nom = _RE_NOM_NARRATIF.search(fenetre_avant) or _RE_NOM_TABLEAU.search(fenetre_avant)
        if m_nom:
            prenom = " ".join(m_nom.group(1).split())
            nom = m_nom.group(2).strip()

        date_naissance = None
        fenetre_large = acte_text[max(0, m.start() - 300):m.start() + 300]
        m_date = _RE_DATE_NAISSANCE.search(fenetre_large)
        if m_date:
            date_naissance = m_date.group(1).strip()

        fin_bloc = matches[i + 1].start() if i + 1 < len(matches) else min(len(acte_text), m.end() + 600)
        bloc_progression = acte_text[m.end():fin_bloc]

        agents.append({
            "matricule": matricule, "nom": nom, "prenom": prenom, "date_naissance": date_naissance,
            "bloc_progression": bloc_progression,
        })
    return agents


# ═══════════════════════════════════════════════════════════
#  3. VÉRIFICATION IDENTITÉ ACTE <-> BASE AGENTS
# ═══════════════════════════════════════════════════════════

def verifier_identite_agent(acte_text: str, etape: int, profil: str, agents_externes: list = None, corps_acte: str = None):
    """Extrait le/les agent(s) de l'acte, les recherche par matricule, puis
    compare nom / prénom / date de naissance / corps.

    agents_externes : si fourni (liste de dicts au format de base_agents.json,
    envoyée par GIRAFE à chaque appel), c'est CETTE liste qui sert de source
    de vérité — pas le fichier local base_agents.json. Le fichier local ne
    sert que de repli pour les tests autonomes (dashboard), quand aucune
    donnée n'est fournie par l'appelant.

    corps_acte : le corps déjà extrait du TEXTE de l'acte par
    regles_service.extraire_infos_acte() — comparé au corps déclaré dans
    agent_info/base_agents.json, pour détecter une incohérence (ex: l'acte
    dit "Professeurs" mais la fiche agent dit "Médecins").

    Retourne (anomalies: list[dict], checks: dict) — même format que
    verifier_points_abc, pour fusion directe dans workflow_service."""
    from app.services.workflow_service import determiner_criticite  # import différé (anti-cycle)

    anomalies = []
    checks = {}

    if agents_externes:
        base_a_utiliser = agents_externes
        index_a_utiliser = _construire_index_matricule(agents_externes)
        source = "fournie par l'appelant"
    else:
        base_a_utiliser = BASE_AGENTS
        index_a_utiliser = INDEX_AGENTS_PAR_MATRICULE
        source = "locale (base_agents.json)"

    if not base_a_utiliser:
        checks["Identification agent(s)"] = f"ℹ Aucune base d'agents disponible ({source} vide)"
        return anomalies, checks

    agents_acte = extraire_identite_agent(acte_text)

    if not agents_acte:
        code = "MATRICULE_NON_TROUVE_DANS_ACTE"
        checks["Identification agent(s)"] = "❌ AUCUN MATRICULE TROUVÉ DANS L'ACTE"
        anomalies.append({
            "code": code,
            "description": "Aucun matricule n'a pu être identifié dans le texte de l'acte.",
            "criticite": determiner_criticite(code).value,
            "profil_concerne": profil, "etape": etape,
            "recommandation": "Vérifier que le(s) matricule(s) de(s) agent(s) figure(nt) bien et lisiblement dans l'acte.",
        })
        return anomalies, checks

    plusieurs = len(agents_acte) > 1

    for idx, identite_acte in enumerate(agents_acte, start=1):
        prefixe = f"Agent {idx}" if plusieurs else "Agent"
        cle = _normaliser_texte_identite(identite_acte["matricule"])
        agent_ref = index_a_utiliser.get(cle)

        if agent_ref is None:
            code = "AGENT_INCONNU_BASE"
            checks[f"{prefixe} — Identification"] = f"❌ MATRICULE INCONNU DANS LA BASE ({identite_acte['matricule']})"
            anomalies.append({
                "code": code,
                "description": f"Le matricule '{identite_acte['matricule']}' cité dans l'acte n'existe pas dans la base des agents.",
                "criticite": determiner_criticite(code).value,
                "profil_concerne": profil, "etape": etape,
                "recommandation": "Vérifier le matricule (erreur de saisie possible) ou signaler un agent non répertorié.",
            })
            continue

        checks[f"{prefixe} — Identification"] = f"✅ TROUVÉ — {agent_ref['nom']} {agent_ref['prenom']} (matricule {agent_ref['matricule']})"
        checks[f"{prefixe} — Matricule (acte vs base)"] = f"✅ CONFORME — '{identite_acte['matricule']}' trouvé dans la base des agents"

        if identite_acte["nom"] and _normaliser_texte_identite(identite_acte["nom"]) != _normaliser_texte_identite(agent_ref.get("nom")):
            code = "IDENTITE_NOM_INCORRECT"
            checks[f"{prefixe} — Nom (acte vs base)"] = f"❌ Acte: '{identite_acte['nom']}' / Base: '{agent_ref.get('nom')}'"
            anomalies.append({
                "code": code,
                "description": f"Nom incohérent : acte='{identite_acte['nom']}', base='{agent_ref.get('nom')}' pour le matricule {agent_ref['matricule']}.",
                "criticite": determiner_criticite(code).value,
                "profil_concerne": profil, "etape": etape,
                "recommandation": "Vérifier l'orthographe du nom ou l'exactitude du matricule utilisé.",
            })
        elif identite_acte["nom"]:
            checks[f"{prefixe} — Nom (acte vs base)"] = "✅ CONFORME"

        if identite_acte["prenom"] and _normaliser_texte_identite(identite_acte["prenom"]) != _normaliser_texte_identite(agent_ref.get("prenom")):
            code = "IDENTITE_PRENOM_INCORRECT"
            checks[f"{prefixe} — Prénom (acte vs base)"] = f"❌ Acte: '{identite_acte['prenom']}' / Base: '{agent_ref.get('prenom')}'"
            anomalies.append({
                "code": code,
                "description": f"Prénom incohérent : acte='{identite_acte['prenom']}', base='{agent_ref.get('prenom')}' pour le matricule {agent_ref['matricule']}.",
                "criticite": determiner_criticite(code).value,
                "profil_concerne": profil, "etape": etape,
                "recommandation": "Vérifier l'orthographe du prénom ou l'exactitude du matricule utilisé.",
            })
        elif identite_acte["prenom"]:
            checks[f"{prefixe} — Prénom (acte vs base)"] = "✅ CONFORME"

        if identite_acte["date_naissance"]:
            d_acte = _normaliser_date(identite_acte["date_naissance"])
            d_base = _normaliser_date(agent_ref.get("date_naissance"))
            if d_acte != d_base:
                code = "IDENTITE_DATE_NAISSANCE_INCORRECTE"
                checks[f"{prefixe} — Date de naissance (acte vs base)"] = f"❌ Acte: '{identite_acte['date_naissance']}' / Base: '{agent_ref.get('date_naissance')}'"
                anomalies.append({
                    "code": code,
                    "description": f"Date de naissance incohérente : acte='{identite_acte['date_naissance']}', base='{agent_ref.get('date_naissance')}' pour le matricule {agent_ref['matricule']}.",
                    "criticite": determiner_criticite(code).value,
                    "profil_concerne": profil, "etape": etape,
                    "recommandation": "Vérifier la date de naissance ou l'exactitude du matricule utilisé.",
                })
            else:
                checks[f"{prefixe} — Date de naissance (acte vs base)"] = "✅ CONFORME"

        if agent_ref.get("corps"):
            if corps_acte:
                corps_acte_norm = _normaliser_texte_identite(corps_acte)
                corps_base_norm = _normaliser_texte_identite(agent_ref["corps"])
                # Comparaison tolérante : correspondance exacte, inclusion
                # mutuelle (ex: base a un suffixe "NF REF" en plus), ou
                # forte similarité — pour absorber les petites variations
                # de formulation sans générer de faux rejets.
                ratio = difflib.SequenceMatcher(None, corps_acte_norm, corps_base_norm).ratio()
                correspond = (
                    corps_acte_norm == corps_base_norm
                    or corps_acte_norm in corps_base_norm
                    or corps_base_norm in corps_acte_norm
                    or ratio >= 0.6
                )
                if correspond:
                    checks[f"{prefixe} — Corps (acte vs base)"] = f"✅ CONFORME — {agent_ref['corps']}"
                else:
                    code = "IDENTITE_CORPS_INCORRECT"
                    checks[f"{prefixe} — Corps (acte vs base)"] = f"❌ Acte: '{corps_acte}' / Base: '{agent_ref['corps']}'"
                    anomalies.append({
                        "code": code,
                        "description": f"Corps incohérent : acte='{corps_acte}', base='{agent_ref['corps']}' pour le matricule {agent_ref['matricule']}.",
                        "criticite": determiner_criticite(code).value,
                        "profil_concerne": profil, "etape": etape,
                        "recommandation": "Vérifier le corps mentionné dans l'acte ou l'exactitude du matricule utilisé.",
                    })
            else:
                checks[f"{prefixe} — Corps (base)"] = f"ℹ {agent_ref['corps']} ({agent_ref.get('hierarchie') or 'n/c'}) — corps non détecté dans le texte de l'acte, comparaison impossible"

    return anomalies, checks


def verifier_visa_coherent(acte_text: str, etape: int, profil: str) -> tuple:
    """Vérifie que les références légales (loi/décret) citées dans l'acte
    correspondent bien au VRAI statut (FONCT/NON_FONCT) du corps détecté,
    selon la base de référence corps.csv — pas seulement à ce que le texte
    de l'acte prétend lui-même. Conforme à la RÈGLE V-01 de la base de
    connaissance métier :
      - Fonctionnaire  → doit citer la Loi n°61-33
      - Non-fonctionnaire → doit citer la Loi n°97-17 et/ou le Décret n°74-347
    """
    from app.services.workflow_service import determiner_criticite  # import différé (anti-cycle)

    anomalies = []
    checks = {}

    if CORPS_DF is None:
        return anomalies, checks

    code_corps = detecter_corps_depuis_texte(acte_text)
    if not code_corps:
        return anomalies, checks

    infos_corps = CPS_INFOS_PAR_CODE.get(code_corps, {})
    type_reel = infos_corps.get("cps_typecorps_code")  # "FONCT" ou "NON_FONCT"
    libelle_corps = infos_corps.get("cps_libelle", code_corps)

    if type_reel not in ("FONCT", "NON_FONCT"):
        return anomalies, checks  # donnée de référence incomplète pour ce corps, on ne bloque pas

    texte_norm = unicodedata.normalize("NFKD", acte_text.lower()).encode("ascii", "ignore").decode("ascii")
    cite_loi_non_fonctionnaire = "97-17" in texte_norm or "74-347" in texte_norm
    cite_loi_fonctionnaire = "61-33" in texte_norm

    # On vérifie uniquement que LA LOI ATTENDUE pour ce corps est bien
    # présente dans l'acte — peu importe si d'autres références légales
    # apparaissent aussi (un agent peut légitimement ajouter des décrets
    # spécifiques à son corps, non répertoriés dans notre base ; ce n'est
    # pas une erreur).
    loi_attendue_presente = cite_loi_fonctionnaire if type_reel == "FONCT" else cite_loi_non_fonctionnaire

    if loi_attendue_presente:
        attendu = "Loi n°61-33 (fonctionnaires)" if type_reel == "FONCT" else "Loi n°97-17 / Décret n°74-347 (non-fonctionnaires)"
        checks["Visa (loi/décret) vs corps"] = f"✅ CONFORME — {libelle_corps} ({type_reel}), {attendu} bien présent(e)"
    else:
        code = "VISA_INCOHERENT"
        attendu = "la Loi n°61-33 (fonctionnaires)" if type_reel == "FONCT" else "la Loi n°97-17 / le Décret n°74-347 (non-fonctionnaires)"
        checks["Visa (loi/décret) vs corps"] = f"ℹ Corps '{libelle_corps}' ({type_reel}) — {attendu} non trouvée dans l'acte, à vérifier"
        anomalies.append({
            "code": code,
            "description": (
                f"Pour le corps '{libelle_corps}' ({type_reel}), la référence légale "
                f"attendue ({attendu}) n'a pas été retrouvée dans le texte de l'acte."
            ),
            "criticite": determiner_criticite(code).value,
            "profil_concerne": profil, "etape": etape,
            "recommandation": "Vérifier que la référence légale attendue pour ce corps figure bien dans l'acte.",
        })

    return anomalies, checks


# ═══════════════════════════════════════════════════════════
#  4. TABLES DE RÉFÉRENCE CORPS / CLASSE / ÉCHELON (délai d'avancement)
# ═══════════════════════════════════════════════════════════

def _normaliser_ref(t):
    if t is None or isinstance(t, float):
        return ""
    t = str(t).upper().strip()
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii")
    return " ".join(t.split())


def _charger_tables_reference():
    settings = get_settings()
    d = settings.data_dir
    try:
        corps_df = pd.read_csv(os.path.join(d, "corps.csv"))
        classe_df = pd.read_csv(os.path.join(d, "classe.csv"))
        echelon_df = pd.read_csv(os.path.join(d, "echelon.csv"))
        cce_df = pd.read_csv(os.path.join(d, "corps_classe_echelon.csv"))
        logger.info(
            f"✅ Tables de référence chargées : corps={len(corps_df)}, classe={len(classe_df)}, "
            f"echelon={len(echelon_df)}, corps_classe_echelon={len(cce_df)}"
        )
        return corps_df, classe_df, echelon_df, cce_df
    except FileNotFoundError as e:
        logger.warning(f"⚠️ Table de référence introuvable ({e}) — calcul de délai en mode repli uniquement.")
        return None, None, None, None


CORPS_DF, CLASSE_DF, ECHELON_DF, CCE_DF = _charger_tables_reference()

CLS_LABEL_VERS_CODE = {
    "1CL": "1_SG", "2CL": "2_SG", "3CL": "3_SG", "4CL": "4_SG",
    "5CL": "5_SG", "6CL": "6_SG",
    "CEX": "7_SG", "PPL CEX": "8_SG", "PPL": "9_SG",
}
ECH_LABEL_VERS_CODE = {
    "1ECH": "1_SG", "2ECH": "2_SG", "3ECH": "3_SG", "4ECH": "4_SG",
}

CPS_LIBELLE_VERS_CODES = {}
_LIBELLES_TRIES = []
CPS_INFOS_PAR_CODE = {}
DUREE_LOOKUP = {}

if CORPS_DF is not None:
    for _, _row in CORPS_DF.iterrows():
        for _lib in (_row.get("cps_libelle"), _row.get("cps_libelle_singulier")):
            _cle = _normaliser_ref(_lib)
            if _cle and len(_cle) >= 5:
                CPS_LIBELLE_VERS_CODES.setdefault(_cle, []).append(_row["cps_code"])
                _LIBELLES_TRIES.append((_cle, _row["cps_code"], _row.get("cps_typecorps_code")))
    _LIBELLES_TRIES = sorted(set(_LIBELLES_TRIES), key=lambda x: -len(x[0]))
    CPS_INFOS_PAR_CODE = CORPS_DF.set_index("cps_code").to_dict(orient="index")

if CCE_DF is not None:
    for _, _row in CCE_DF.iterrows():
        _cle = (str(_row["cce_cps_code"]), str(_row["cce_cls_code"]), str(_row["cce_ech_code"]))
        DUREE_LOOKUP[_cle] = int(_row["cce_duree"])

_HIER_RE = re.compile(r'\b(AS|A1|A2|A3|B1|B2|B3|B4|C1|C2|C3|C4|D1|D2|D3|D4)\b')


def _pretraiter_texte_corps(texte):
    texte = re.sub(r'\b([A-D])\s+(\d)\b', r'\1\2', texte)
    texte = re.sub(r'\bNF([A-D][1-4]|AS)\b', r'NF \1', texte)
    return texte


def detecter_corps_depuis_texte(acte_text: str, statut: str = ""):
    if CORPS_DF is None:
        return None
    texte_norm = _normaliser_ref(_pretraiter_texte_corps(acte_text))
    candidats = [(lib, code, typ) for lib, code, typ in _LIBELLES_TRIES if lib in texte_norm]

    if not candidats:
        hierarchies_trouvees = _HIER_RE.findall(texte_norm)
        mots = [m for m in texte_norm.split() if len(m) >= 5]
        for mot in mots[:5]:
            famille = [(lib, code, typ) for lib, code, typ in _LIBELLES_TRIES if lib.split()[0] == mot]
            if not famille:
                continue
            trouve = []
            if hierarchies_trouvees:
                for h in hierarchies_trouvees:
                    for lib, code, typ in famille:
                        if (" " + lib + " ").endswith(" " + h + " "):
                            trouve = [(lib, code, typ)]
                            break
                    if trouve:
                        break
            if not trouve and len(famille) == 1:
                trouve = famille
            if trouve:
                candidats = trouve
                break

    if not candidats:
        return None

    max_len = max(len(c[0]) for c in candidats)
    meilleurs = [c for c in candidats if len(c[0]) == max_len]
    if len(meilleurs) == 1:
        return meilleurs[0][1]
    type_attendu = "NON_FONCT" if statut == "NF" else "FONCT"
    for lib, code, typ in meilleurs:
        if typ == type_attendu:
            return code
    return meilleurs[0][1]


def parser_grade_depart(grade_label: str):
    label = " ".join(str(grade_label).upper().split())

    if label.startswith("PPL") and "CEX" not in label:
        cls_code = CLS_LABEL_VERS_CODE.get("PPL")
        m = re.search(r"(\d+ECH)", label)
        ech_code = ECH_LABEL_VERS_CODE.get(m.group(1)) if m else None
        return (cls_code, ech_code) if ech_code else None

    m = re.match(r"(\d+CL)\s+(\d+ECH)", label)
    if m:
        cls_code = CLS_LABEL_VERS_CODE.get(m.group(1))
        ech_code = ECH_LABEL_VERS_CODE.get(m.group(2))
        return (cls_code, ech_code) if (cls_code and ech_code) else None

    return None


def get_delai_reglementaire_v2(acte_text: str, statut: str, corps_texte_extrait: str, grade_depart_label: str):
    """Priorité à la table corps_classe_echelon.csv (source de vérité).
    Repli sur get_delai_reglementaire() (règles générales) si le corps
    n'y figure pas — dans ce cas confirme_par_table=False."""
    cps_code = detecter_corps_depuis_texte(acte_text, statut) or detecter_corps_depuis_texte(corps_texte_extrait or "", statut)

    if cps_code is not None:
        parse = parser_grade_depart(grade_depart_label)
        if parse is not None:
            cls_code, ech_code = parse
            duree = DUREE_LOOKUP.get((str(cps_code), cls_code, ech_code))
            if duree is not None:
                libelle_corps = CPS_INFOS_PAR_CODE.get(cps_code, {}).get("cps_libelle", cps_code)
                return duree, f"Table corps_classe_echelon (corps {cps_code} — {libelle_corps})", True

    hierarchie = ""
    m = _HIER_RE.search(_normaliser_ref(acte_text))
    if m:
        hierarchie = m.group(1)
    classe_depart = ""
    for c in ("1CL", "2CL", "3CL", "4CL"):
        if c in grade_depart_label.upper():
            classe_depart = c
            break
    duree, regle = get_delai_reglementaire(statut, hierarchie, classe_depart, corps_texte_extrait or "")
    return duree, f"{regle} (repli — corps non trouvé dans corps_classe_echelon.csv, non confirmé par la table)", False


# ═══════════════════════════════════════════════════════════
#  5. VÉRIFICATION DES DÉLAIS D'AVANCEMENT
# ═══════════════════════════════════════════════════════════

_RE_GRADE_PROGRESSION = re.compile(r"(?:\d+CL|PPL|HC)\s+\d+ECH|CEX", re.IGNORECASE)
_RE_DATE_PROGRESSION = re.compile(r"\d{2}[./]\d{2}[./]\d{4}")


def _extraire_paires_brutes(texte: str):
    """Extrait toutes les paires (grade, date) trouvées dans le texte
    donné, dans leur ordre d'apparition physique — sans jugement sur leur
    cohérence."""
    grades = [g.upper().replace("  ", " ").strip() for g in _RE_GRADE_PROGRESSION.findall(texte)]
    dates = [d.replace(".", "/") for d in _RE_DATE_PROGRESSION.findall(texte)]
    if len(grades) != len(dates):
        return []
    return list(zip(grades, dates))


def _tronquer_a_la_premiere_incoherence(paires: list):
    """Garde-fou : dans une vraie progression de carrière, les dates
    avancent TOUJOURS dans le temps. Une date qui recule ou n'avance pas
    est le signe fiable d'une confusion d'extraction (mélange entre deux
    agents) — on tronque la séquence à ce point-là plutôt que de produire
    des comparaisons absurdes en aval."""
    if not paires:
        return None
    paires_valides = [paires[0]]
    for grade, date in paires[1:]:
        derniere_date = paires_valides[-1][1]
        if calcul_delai_annees(derniere_date, date) <= 0:
            break
        paires_valides.append((grade, date))
    return paires_valides


def extraire_progression_grade(bloc_texte: str):
    """Conservé pour compatibilité — extraction + garde-fou sur UN bloc de
    texte donné (utilisé pour les actes à un seul agent)."""
    paires = _extraire_paires_brutes(bloc_texte)
    if len(paires) < 2:
        return None
    return _tronquer_a_la_premiere_incoherence(paires)


def verifier_delais_avancement(acte_text: str, agents_acte: list, statut: str, hierarchie: str, corps: str, etape: int, profil: str):
    """Pour chaque agent, vérifie chaque étape de sa progression grade/échelon
    par rapport au délai réglementaire (table corps_classe_echelon.csv en
    priorité, repli sur les règles générales sinon)."""
    from app.services.workflow_service import determiner_criticite  # import différé (anti-cycle)

    anomalies = []
    checks = {}

    if not agents_acte:
        return anomalies, checks

    plusieurs = len(agents_acte) > 1

    # Détermine, pour chaque agent, sa liste de paires (grade, date) —
    # deux stratégies selon le cas :
    #
    # 1) Acte à PLUSIEURS agents : le nom/matricule d'un agent peut
    #    apparaître APRÈS ses propres données de progression dans le texte
    #    extrait du PDF (mise en page en tableau, cellule de nom sur deux
    #    lignes qui décale l'ordre de lecture) — chercher "après le
    #    matricule" échoue alors pour certains agents. On extrait donc
    #    TOUTES les paires du document en une fois, dans leur ordre
    #    d'apparition physique, et on les répartit équitablement entre les
    #    agents dans l'ordre où ILS apparaissent (même hypothèse validée
    #    sur un vrai cas : chaque agent = un nombre égal de paires).
    #
    # 2) Acte à UN SEUL agent : on garde l'extraction par bloc individuel,
    #    qui gère bien le cas d'un seul agent avec plusieurs échelons
    #    successifs (chaînage complet dans l'ordre).
    groupes_par_agent = []
    if plusieurs:
        toutes_paires = _extraire_paires_brutes(acte_text)
        if toutes_paires and len(toutes_paires) % len(agents_acte) == 0:
            par_agent = len(toutes_paires) // len(agents_acte)
            for i in range(len(agents_acte)):
                sous_groupe = toutes_paires[i * par_agent:(i + 1) * par_agent]
                groupes_par_agent.append(_tronquer_a_la_premiere_incoherence(sous_groupe))
        else:
            # Répartition non régulière : repli sur l'extraction par bloc
            # individuel (moins fiable ici, mais reste un filet de sécurité).
            for agent in agents_acte:
                groupes_par_agent.append(extraire_progression_grade(agent.get("bloc_progression", "")))
    else:
        for agent in agents_acte:
            groupes_par_agent.append(extraire_progression_grade(agent.get("bloc_progression", "")))

    for idx, (agent, etapes) in enumerate(zip(agents_acte, groupes_par_agent), start=1):
        prefixe = f"Agent {idx}" if plusieurs else "Agent"

        if not etapes:
            code = "DELAI_NON_VERIFIABLE"
            checks[f"{prefixe} — Délai avancement"] = "ℹ Tableau de progression non exploitable (à vérifier manuellement)"
            anomalies.append({
                "code": code,
                "description": f"Impossible d'extraire un tableau grade/échelon exploitable pour l'agent (matricule {agent.get('matricule')}).",
                "criticite": determiner_criticite(code).value,
                "profil_concerne": profil, "etape": etape,
                "recommandation": "Vérifier manuellement le calcul du délai d'avancement pour cet agent.",
            })
            continue

        for i in range(1, len(etapes)):
            grade_avant = " ".join(etapes[i - 1][0].split())
            date_avant = etapes[i - 1][1]
            grade_apres = " ".join(etapes[i][0].split())
            date_apres = etapes[i][1]

            delai_constate = calcul_delai_annees(date_avant, date_apres)

            # Garde-fou : un délai négatif ou nul est impossible dans une
            # vraie progression de carrière — c'est le signe que le tableau
            # a été mal extrait (dates/grades de deux agents mélangés), pas
            # une vraie violation de règle métier. On le signale comme "à
            # vérifier manuellement" plutôt que comme une fausse anomalie.
            if delai_constate <= 0:
                code = "DELAI_NON_VERIFIABLE"
                checks[libelle_etape] = f"⚠ Délai constaté incohérent ({delai_constate} an(s)) — extraction du tableau probablement erronée, à vérifier manuellement"
                anomalies.append({
                    "code": code,
                    "description": (
                        f"Délai incohérent (négatif ou nul) détecté pour l'agent "
                        f"{agent.get('nom') or ''} (matricule {agent.get('matricule')}) : "
                        f"{grade_avant} ({date_avant}) → {grade_apres} ({date_apres}). "
                        f"Cela indique probablement une erreur d'extraction du tableau, pas une vraie anomalie."
                    ),
                    "criticite": determiner_criticite(code).value,
                    "profil_concerne": profil, "etape": etape,
                    "recommandation": "Vérifier manuellement le tableau de progression de cet agent dans l'acte original.",
                })
                continue

            delai_reg, regle_appliquee, confirme_par_table = get_delai_reglementaire_v2(
                acte_text, statut, corps, grade_avant
            )

            libelle_etape = f"{prefixe} — {grade_avant} → {grade_apres}"

            if delai_reg is None:
                checks[libelle_etape] = "ℹ Délai réglementaire indéterminé (corps/grade non reconnu) — à vérifier manuellement"
                code = "DELAI_NON_VERIFIABLE"
                anomalies.append({
                    "code": code,
                    "description": f"Impossible de déterminer le délai réglementaire pour {grade_avant} → {grade_apres} (agent {agent.get('nom') or ''}, matricule {agent.get('matricule')}).",
                    "criticite": determiner_criticite(code).value,
                    "profil_concerne": profil, "etape": etape,
                    "recommandation": "Vérifier manuellement dans corps_classe_echelon.csv ou regles_metier_actes_RH.",
                })
                continue

            if delai_constate == delai_reg:
                checks[libelle_etape] = f"✅ CONFORME ({delai_constate} an(s), {regle_appliquee})"
            else:
                code = "DELAI_AVANCEMENT_INCORRECT" if confirme_par_table else "DELAI_AVANCEMENT_A_VERIFIER"
                emoji = "❌" if confirme_par_table else "⚠"
                checks[libelle_etape] = f"{emoji} Constaté {delai_constate} an(s) / Réglementaire {delai_reg} an(s) ({regle_appliquee})"
                anomalies.append({
                    "code": code,
                    "description": (
                        f"Délai d'avancement {'incorrect' if confirme_par_table else 'à vérifier'} pour l'agent "
                        f"{agent.get('nom') or ''} (matricule {agent.get('matricule')}) : {grade_avant} ({date_avant}) → "
                        f"{grade_apres} ({date_apres}) = {delai_constate} an(s) constaté(s), {delai_reg} an(s) attendu(s) "
                        f"selon {regle_appliquee}."
                    ),
                    "criticite": determiner_criticite(code).value,
                    "profil_concerne": profil, "etape": etape,
                    "recommandation": "Corriger la date d'effet ou vérifier le grade/l'échelon inscrits dans le tableau.",
                })

    return anomalies, checks