"""Service Workflow : MoteurValidationGIRAFE complet."""

import hashlib
import json
import logging
import os
import re
import unicodedata
from datetime import datetime
from typing import Optional

from app.models import TypeActe, NiveauCriticite, StatutEtape
from app.services.regles_service import (
    extraire_infos_acte, verifier_points_abc, detecter_type_acte,
    calcul_delai_annees, get_delai_reglementaire, SIGNATAIRE_OFFICIEL,
)
from app.services.vision_service import analyser_visuel_acte

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
#  WORKFLOWS (3 circuits)
#
#  Correspondance avec les noms officiels de la table de
#  paramétrage GIRAFE (parametrage_type_acte_workflow.csv,
#  colonne "ref_engine") — repère uniquement, à titre
#  informatif. La détection du circuit applicable continue de
#  se faire par mots-clés dans le texte (voir detecter_type_acte
#  dans regles_service.py), pas via cette table.
#
#    TypeActe.AVANCEMENT_ECHELON      ↔ APAE : circuit COURT de
#                                        l'avancement d'échelon (9 étapes)
#    TypeActe.AUTRE                   ↔ APAG : circuit LONG,
#                                        "autre" type d'acte (12 étapes)
#    TypeActe.RETRAITE_FONCTIONNAIRE  ↔ RET  : circuit de la
#                                        retraite fonctionnaire (13 étapes)
# ═══════════════════════════════════════════════════════════

def _etape(nom, role, tampons=None, signature=False, numero=False):
    return {
        "nom": nom, "role": role, "type_action": ["valider"],
        "analyse_textuelle": True,
        "tampons_requis": tampons or [],
        "signature_requise": signature,
        "numero_acte_requis": numero,
    }

# ── APAE : circuit court de l'avancement d'échelon (9 étapes) ──
WORKFLOW_AVANCEMENT_ECHELON = {
    1: _etape("Agent de Bureau", "Initiation"),
    2: _etape("Chef de Bureau", "1ère validation"),
    3: _etape("Chef de Division", "Régularité admin"),
    4: _etape("Division / S-Visa", "Appose visa DGFP"),
    5: _etape("Dir. Fonction Publique", "Gestion de carrière", ["DGFP"]),
    6: _etape("SG / Dir. Cabinet", "Supervision", ["DGFP"]),
    7: _etape("Le Ministre", "Appose signature", ["DGFP"]),
    8: _etape("Secrétariat Gouv. (SGG)", "Conformité finale", ["DGFP"], signature=True),
    9: _etape("Numéroteur", "Préparation numérotation", ["DGFP"], signature=True),
}

# ── RET : circuit de la retraite fonctionnaire (13 étapes) ──
WORKFLOW_RETRAITE_FONCTIONNAIRE = {
    1: _etape("Agent de Bureau", "Initiation"),
    2: _etape("Chef de Bureau", "1ère validation"),
    3: _etape("Chef de Division", "Régularité admin"),
    4: _etape("Division / S-Visa", "Appose visa DGFP"),
    5: _etape("Dir. Pensions", "Appose tampon DP", ["DGFP"]),
    6: _etape("Dir. Solde", "Appose tampon DS", ["DGFP", "DP"]),
    7: _etape("Dir. Prog. Budgétaire", "Appose tampon DPB", ["DGFP", "DP", "DS"]),
    8: _etape("Contrôle Financier", "Appose tampon CF", ["DGFP", "DP", "DS", "DPB"]),
    9: _etape("Dir. Fonction Publique", "Gestion de carrière", ["DGFP", "DP", "DS", "DPB", "CF"]),
    10: _etape("SG / Dir. Cabinet", "Supervision", ["DGFP", "DP", "DS", "DPB", "CF"]),
    11: _etape("Le Ministre", "Appose signature", ["DGFP", "DP", "DS", "DPB", "CF"]),
    12: _etape("Secrétariat Gouv. (SGG)", "Conformité finale", ["DGFP", "DP", "DS", "DPB", "CF"], signature=True),
    13: _etape("Numéroteur", "Préparation numérotation", ["DGFP", "DP", "DS", "DPB", "CF"], signature=True),
}

# ── APAG : circuit long, "autre" type d'acte (12 étapes) ──
WORKFLOW_AUTRE_ACTE = {
    1: _etape("Agent de Bureau", "Initiation"),
    2: _etape("Chef de Bureau", "1ère validation"),
    3: _etape("Chef de Division", "Régularité admin"),
    4: _etape("Division / S-Visa", "Appose visa DGFP"),
    5: _etape("Dir. Solde", "Appose tampon DS", ["DGFP"]),
    6: _etape("Dir. Prog. Budgétaire", "Appose tampon DPB", ["DGFP", "DS"]),
    7: _etape("Contrôle Financier", "Appose tampon CF", ["DGFP", "DS", "DPB"]),
    8: _etape("Dir. Fonction Publique", "Gestion de carrière", ["DGFP", "DS", "DPB", "CF"]),
    9: _etape("SG / Dir. Cabinet", "Supervision", ["DGFP", "DS", "DPB", "CF"]),
    10: _etape("Le Ministre", "Appose signature", ["DGFP", "DS", "DPB", "CF"]),
    11: _etape("Secrétariat Gouv. (SGG)", "Conformité finale", ["DGFP", "DS", "DPB", "CF"], signature=True),
    12: _etape("Numéroteur", "Préparation numérotation", ["DGFP", "DS", "DPB", "CF"], signature=True),
}

WORKFLOWS_CONFIG = {
    TypeActe.RETRAITE_FONCTIONNAIRE: WORKFLOW_RETRAITE_FONCTIONNAIRE,
    TypeActe.AVANCEMENT_ECHELON: WORKFLOW_AVANCEMENT_ECHELON,
    TypeActe.AUTRE: WORKFLOW_AUTRE_ACTE,
}

# Nom officiel GIRAFE (ref_engine) correspondant à chaque TypeActe interne
# — purement informatif (affiché dans les réponses de l'API pour que
# GIRAFE reconnaisse facilement son propre référentiel), n'affecte aucune
# logique de détection.
REF_ENGINE_PAR_TYPE_ACTE = {
    TypeActe.AVANCEMENT_ECHELON: "APAE",
    TypeActe.AUTRE: "APAG",
    TypeActe.RETRAITE_FONCTIONNAIRE: "RET",
}

# ═══════════════════════════════════════════════════════════
#  PARAMÉTRAGE DYNAMIQUE DES VÉRIFICATIONS (interface admin Streamlit)
#
#  app/data/parametrage_verifications.json contient, pour chaque circuit
#  (APAE/APAG/RET) et chaque étape/profil, quelles vérifications sont
#  actives (en_tete, timbre, identite_agent, visa, delai_avancement),
#  quels tampons sont attendus, et si signature/numéro d'acte sont requis.
#
#  Ce fichier est relu à CHAQUE appel de valider_etape() (pas de cache) —
#  volontairement, pour qu'un changement fait dans l'interface admin
#  s'applique IMMÉDIATEMENT sur la prochaine vérification, sans avoir à
#  redémarrer l'API. Le fichier étant petit (quelques Ko), le coût de
#  cette relecture systématique est négligeable.
#
#  Si le fichier est absent ou invalide, on retombe sur les valeurs par
#  défaut codées en dur ci-dessus (WORKFLOWS_CONFIG) — l'API continue de
#  fonctionner normalement même sans configuration admin personnalisée.
# ═══════════════════════════════════════════════════════════

def _chemin_parametrage_verifications() -> str:
    from app.config import get_settings
    return os.path.join(get_settings().data_dir, "parametrage_verifications.json")


def charger_parametrage_verifications() -> dict:
    chemin = _chemin_parametrage_verifications()
    try:
        with open(chemin, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"⚠️ {chemin} introuvable/invalide ({e}) — utilisation des valeurs par défaut codées en dur.")
        return {}


def _config_etape_dynamique(circuit: str, numero_etape: int, config_defaut: dict) -> dict:
    """Fusionne la config par défaut (codée en dur) d'une étape avec la
    config admin (JSON) si elle existe pour cette étape précise — la
    config admin a toujours priorité quand elle est présente."""
    parametrage = charger_parametrage_verifications()
    etape_admin = parametrage.get(circuit, {}).get(str(numero_etape))
    if not etape_admin:
        # Pas de config admin pour cette étape : comportement par défaut
        # inchangé (toutes les vérifications actives, tampons codés en dur).
        return {
            "verifications": {
                "en_tete": True, "timbre": True, "identite_agent": True,
                "visa": True, "delai_avancement": True,
            },
            "tampons_requis": config_defaut.get("tampons_requis", []),
            "signature_requise": config_defaut.get("signature_requise", False),
            "numero_acte_requis": config_defaut.get("numero_acte_requis", False),
        }
    return {
        "verifications": etape_admin.get("verifications", {}),
        "tampons_requis": etape_admin.get("tampons_requis", config_defaut.get("tampons_requis", [])),
        "signature_requise": etape_admin.get("signature_requise", config_defaut.get("signature_requise", False)),
        "numero_acte_requis": etape_admin.get("numero_acte_requis", config_defaut.get("numero_acte_requis", False)),
    }

CODES_BLOQUANTS = {
    "ENTETE_NON_CONFORME",
    "SIGNATURE_MINISTRE_MANQUANTE", "NUMERO_ACTE_MANQUANT", "ERREUR_LECTURE",
    "DELAI_NON_CONFORME", "MATRICULE_NON_TROUVE_DANS_ACTE", "AGENT_INCONNU_BASE",
    "IDENTITE_NOM_INCORRECT", "IDENTITE_PRENOM_INCORRECT",
    "IDENTITE_DATE_NAISSANCE_INCORRECTE", "IDENTITE_CORPS_INCORRECT",
    "TIMBRE_INCORRECT",
}
CODES_IMPORTANTS = {
    "DATES_MANQUANTES", "DELAI_AVANCEMENT_A_VERIFIER", "DELAI_AVANCEMENT_INCORRECT",
}
CODES_INFORMATIFS = {"TEXTE_EXTRACTION_PARTIELLE", "LLM_VISAS_A_VERIFIER", "VISA_INCOHERENT", "DELAI_NON_VERIFIABLE"}


def determiner_criticite(code: str) -> NiveauCriticite:
    if code.startswith("TAMPON") and code.endswith("MANQUANT"):
        return NiveauCriticite.BLOQUANT
    if code in CODES_BLOQUANTS:
        return NiveauCriticite.BLOQUANT
    if code in CODES_IMPORTANTS:
        return NiveauCriticite.IMPORTANT
    if code in CODES_INFORMATIFS:
        return NiveauCriticite.INFORMATION
    return NiveauCriticite.IMPORTANT


class MoteurValidationGIRAFE:
    """Moteur de validation workflow complet."""

    def __init__(self):
        self.type_acte_detecte: Optional[TypeActe] = None
        self.workflow_actuel: Optional[dict] = None
        self.anomalies_textuelles: list = []
        self.checks_textuels: dict = {}
        self.resultats_abc_infos: dict = {}
        self.etapes_textuelles_faites: set = set()
        self.historique: list = []
        # Cache par empreinte de contenu — évite de ré-analyser un fichier
        # déjà vu, MAIS relance automatiquement l'analyse dès que le
        # contenu change réellement (ex: un tampon vient d'être ajouté
        # entre deux étapes).
        self.cache_vision: dict = {}

    def _analyser_visuel_avec_cache(self, pdf_bytes: bytes) -> dict:
        """Réutilise le résultat déjà obtenu si CE fichier exact a déjà été
        analysé. Dès que le contenu du PDF change (ex: un tampon vient
        d'être ajouté), l'empreinte change aussi → nouvelle analyse
        automatique, jamais de résultat périmé.

        IMPORTANT : un résultat en échec (clé "erreur" présente — panne de
        l'API vision, quota, timeout...) n'est JAMAIS mis en cache. Un tel
        résultat ne dit rien sur le contenu réel de l'acte (tous les
        tampons y sont à False par défaut) ; le mettre en cache figerait
        ce faux "aucun tampon détecté" pour toutes les étapes suivantes du
        même acte, jusqu'à la fin de la session. On préfère retenter une
        vraie analyse à chaque nouvel appel tant qu'aucun résultat valide
        n'a été obtenu."""
        empreinte = hashlib.sha256(pdf_bytes).hexdigest()
        if empreinte in self.cache_vision:
            logger.info(f"♻️  Vision réutilisée depuis le cache (empreinte {empreinte[:8]}...)")
            return self.cache_vision[empreinte]

        resultat = analyser_visuel_acte(pdf_bytes)
        if not resultat.get("erreur"):
            self.cache_vision[empreinte] = resultat
        else:
            logger.warning(f"Analyse visuelle en échec ({resultat['erreur']}) — résultat NON mis en cache, sera retenté au prochain appel.")
        return resultat

    def initialiser_workflow(self, acte_text: str) -> dict:
        type_str = detecter_type_acte(acte_text)
        self.type_acte_detecte = TypeActe(type_str)
        self.workflow_actuel = WORKFLOWS_CONFIG[self.type_acte_detecte]
        return {
            "type_acte_detecte": self.type_acte_detecte.value,
            "ref_engine": REF_ENGINE_PAR_TYPE_ACTE.get(self.type_acte_detecte),  # APAE / APAG / RET, pour repère GIRAFE
            "nb_etapes": len(self.workflow_actuel),
            "workflow_detail": {
                str(k): f"{v['nom']} — {v['role']}"
                for k, v in self.workflow_actuel.items()
            },
        }

    def valider_etape(
        self, acte_text: str, pdf_bytes: Optional[bytes],
        etape: int, acte_id: str, agents_externes: Optional[list] = None,
    ) -> dict:
        if self.workflow_actuel is None:
            self.initialiser_workflow(acte_text)

        if etape not in self.workflow_actuel:
            raise ValueError(f"Étape {etape} inexistante dans ce circuit")

        config = self.workflow_actuel[etape]
        profil = config["nom"]
        anomalies = []
        checks = {}

        # Config admin dynamique pour CETTE étape précise (relue à chaque
        # appel — voir _config_etape_dynamique ci-dessus) : quelles
        # vérifications sont actives, quels tampons sont attendus.
        circuit = REF_ENGINE_PAR_TYPE_ACTE.get(self.type_acte_detecte)
        cfg_dyn = _config_etape_dynamique(circuit, etape, config)
        verifs = cfg_dyn["verifications"]

        # ── Analyse textuelle ──
        # NB : cette section est scindée en deux parties bien distinctes :
        #
        #  1) En-tête / Timbre (point A / point C) : ne dépendent QUE du
        #     texte de l'acte, qui ne change jamais pour un acte_id donné
        #     → mis en cache une seule fois par acte via
        #     self.etapes_textuelles_faites, c'est du contenu figé.
        #
        #  2) Identité agent / Visa / Délai d'avancement : dépendent du
        #     JSON agent_info transmis par GIRAFE À CET APPEL PRÉCIS. Si
        #     GIRAFE corrige les informations de l'agent puis relance une
        #     vérification sur le même acte_id, il FAUT recalculer avec
        #     les nouvelles données — jamais réutiliser un résultat mis en
        #     cache lors d'un appel précédent. Cette partie est donc
        #     TOUJOURRS recalculée, à chaque appel, sans aucun cache.
        if config.get("analyse_textuelle"):
            if etape not in self.etapes_textuelles_faites:
                resultats_abc = verifier_points_abc(acte_text)

                anomalies_abc = []
                checks_abc = {}

                # Point A — actif uniquement si "en_tete" est coché côté admin
                if verifs.get("en_tete", True):
                    if not resultats_abc["point_A"]["conforme"]:
                        code = "ENTETE_NON_CONFORME"
                        anomalies_abc.append({
                            "code": code, "description": "En-tête non conforme",
                            "criticite": determiner_criticite(code).value,
                            "profil_concerne": profil, "etape": etape,
                            "recommandation": "Corriger l'en-tête officiel.",
                        })
                        checks_abc["En-tête officiel"] = "❌ NON CONFORME"
                    else:
                        checks_abc["En-tête officiel"] = "✅ CONFORME"

                # Point C — actif uniquement si "timbre" est coché côté admin
                if verifs.get("timbre", True):
                    if not resultats_abc["point_C"]["conforme"]:
                        code = "TIMBRE_INCORRECT"
                        anomalies_abc.append({
                            "code": code, "description": "Timbre incorrect",
                            "criticite": determiner_criticite(code).value,
                            "profil_concerne": profil, "etape": etape,
                            "recommandation": f"Attendu : '{SIGNATAIRE_OFFICIEL}'",
                        })
                        checks_abc["Timbre"] = "❌ NON CONFORME"
                    else:
                        checks_abc["Timbre"] = "✅ CONFORME"

                self.etapes_textuelles_faites.add(etape)
                # On conserve aussi les infos extraites de l'acte
                # (corps/statut/hiérarchie) pour ne pas relancer
                # verifier_points_abc à chaque appel — elles ne dépendent,
                # elles non plus, que du texte de l'acte (figé).
                self.resultats_abc_infos = resultats_abc["infos"]
                self.anomalies_textuelles = anomalies_abc
                self.checks_textuels = checks_abc

            anomalies.extend(self.anomalies_textuelles)
            checks.update(self.checks_textuels)
            infos_acte = self.resultats_abc_infos

            # Identité agent (matricule / nom / prénom / date de naissance
            # vs base des agents) — TOUJOURS recalculée avec les
            # agents_externes reçus à CET appel, jamais mise en cache.
            # Actif uniquement si "identite_agent" est coché côté admin.
            try:
                from app.services.agents_service import verifier_identite_agent, extraire_identite_agent, extraire_identite_par_date_naissance, verifier_delais_avancement, verifier_visa_coherent

                if verifs.get("identite_agent", True):
                    anomalies_id, checks_id = verifier_identite_agent(
                        acte_text, etape, profil, agents_externes,
                        corps_acte=infos_acte["corps"],
                    )
                    anomalies.extend(anomalies_id)
                    checks.update(checks_id)

                # Cohérence du visa (loi/décret cité) vs vrai statut du corps
                # — actif uniquement si "visa" est coché côté admin.
                if verifs.get("visa", True):
                    anomalies_visa, checks_visa = verifier_visa_coherent(acte_text, etape, profil)
                    anomalies.extend(anomalies_visa)
                    checks.update(checks_visa)

                # Délai d'avancement grade/échelon — actif uniquement si
                # "delai_avancement" est coché côté admin, ET UNIQUEMENT si
                # ce n'est PAS un acte de retraite (un départ en retraite ne
                # comporte jamais de calcul d'avancement de grade/échelon,
                # peu importe le circuit détecté).
                #
                # La détection se limite à la ligne "Objet :" (ou, à
                # défaut, au tout début de l'acte) — PAS à tout le corps du
                # texte, qui peut légitimement mentionner "retraite" sans
                # que l'acte en soit un lui-même (ex: référence standard à
                # "l'Institution de Prévoyance Retraite du Sénégal" dans un
                # acte de régularisation, ou une "retenue de pension de
                # retraite" dans le calcul de la rémunération).
                if verifs.get("delai_avancement", True):
                    texte_normalise_retraite = unicodedata.normalize(
                        "NFKD", acte_text.lower()
                    ).encode("ascii", "ignore").decode("ascii")
                    m_objet_retraite = re.search(r"objet\s*:?\s*(.{0,150})", texte_normalise_retraite)
                    zone_objet = m_objet_retraite.group(1) if m_objet_retraite else texte_normalise_retraite[:200]
                    est_acte_retraite = "retraite" in zone_objet

                    if not est_acte_retraite:
                        agents_acte = extraire_identite_agent(acte_text)
                        if not agents_acte:
                            # Aucun matricule dans l'acte (cas des actes
                            # d'ENGAGEMENT/NOMINATION/RÉGULARISATION) — repli sur
                            # la date de naissance, comme le fait déjà
                            # verifier_identite_agent pour l'identité.
                            agents_acte = extraire_identite_par_date_naissance(acte_text)
                        anomalies_delai, checks_delai = verifier_delais_avancement(
                            acte_text, agents_acte,
                            infos_acte["statut"],
                            infos_acte["hierarchie"],
                            infos_acte["corps"],
                            etape, profil,
                        )
                        anomalies.extend(anomalies_delai)
                        checks.update(checks_delai)
            except Exception as e:
                logger.warning(f"Vérification identité/délai indisponible : {e}")

        # ── Vérification visuelle ──
        tampons_detectes = []
        signature_detectee = False
        numero_detecte = False
        analyse_visuelle_indisponible = False
        detail_erreur_visuelle = ""

        # Tampons/signature/numéro attendus : viennent de la config admin
        # dynamique si elle existe pour cette étape, sinon des valeurs par
        # défaut codées en dur (voir _config_etape_dynamique).
        tampons_requis = cfg_dyn["tampons_requis"]
        signature_requise = cfg_dyn["signature_requise"]
        numero_requis = cfg_dyn["numero_acte_requis"]

        if (tampons_requis or signature_requise or numero_requis) and pdf_bytes:
            try:
                res_visuel = self._analyser_visuel_avec_cache(pdf_bytes)
                if res_visuel.get("erreur"):
                    # L'analyse a échoué (panne/quota/timeout de l'API
                    # vision) — on ne SAIT PAS si les tampons sont présents
                    # ou non. Il ne faut surtout pas traiter ce silence
                    # comme une absence réelle et rejeter l'acte sur cette
                    # base : on le signale distinctement, sans bloquer.
                    analyse_visuelle_indisponible = True
                    detail_erreur_visuelle = res_visuel["erreur"]
                    logger.warning(f"Analyse visuelle indisponible pour cette étape : {detail_erreur_visuelle}")
                else:
                    if res_visuel.get("tampon_DGFP"): tampons_detectes.append("DGFP")
                    if res_visuel.get("tampon_DIRSOLDE"): tampons_detectes.append("DS")
                    if res_visuel.get("tampon_DPB"): tampons_detectes.append("DPB")
                    if res_visuel.get("tampon_CF"): tampons_detectes.append("CF")
                    if res_visuel.get("tampon_DP"): tampons_detectes.append("DP")
                    signature_detectee = res_visuel.get("signature_cachet_ministre_bas", False)
                    numero_detecte = res_visuel.get("numero_acte_haut", False)
            except Exception as e:
                analyse_visuelle_indisponible = True
                detail_erreur_visuelle = str(e)
                logger.error(f"Erreur vision : {e}")

        # Vérification tampons
        for tampon in tampons_requis:
            if analyse_visuelle_indisponible:
                # Ni "présent" ni "absent" : l'analyse n'a simplement pas pu
                # se faire. On ne génère AUCUNE anomalie bloquante pour ce
                # tampon — l'acte ne doit pas être rejeté pour une panne
                # technique côté vision, il sera revérifié au prochain
                # appel (le résultat en échec n'étant pas mis en cache).
                checks[f"Tampon {tampon}"] = "⚠️ ANALYSE VISUELLE INDISPONIBLE — à revérifier"
            elif tampon in tampons_detectes:
                checks[f"Tampon {tampon}"] = "✅ PRÉSENT"
            else:
                checks[f"Tampon {tampon}"] = "❌ MANQUANT"
                code = f"TAMPON_{tampon}_MANQUANT"
                anomalies.append({
                    "code": code,
                    "description": f"Le tampon '{tampon}' est absent.",
                    "criticite": determiner_criticite(code).value,
                    "profil_concerne": profil, "etape": etape,
                    "recommandation": f"Vérifier que l'étape précédente a apposé le tampon '{tampon}'.",
                })

        if analyse_visuelle_indisponible and (tampons_requis or signature_requise or numero_requis):
            anomalies.append({
                "code": "ANALYSE_VISUELLE_INDISPONIBLE",
                "description": f"L'analyse visuelle (tampons/signature) n'a pas pu être réalisée pour le moment ({detail_erreur_visuelle}).",
                "criticite": NiveauCriticite.INFORMATION.value,
                "profil_concerne": profil, "etape": etape,
                "recommandation": "Relancer la validation dans quelques instants — aucune décision n'a été prise sur les tampons/la signature.",
            })

        # Vérification signature
        if signature_requise:
            if analyse_visuelle_indisponible:
                checks["Signature Ministre"] = "⚠️ ANALYSE VISUELLE INDISPONIBLE — à revérifier"
            elif signature_detectee:
                checks["Signature Ministre"] = "✅ PRÉSENTE"
            else:
                checks["Signature Ministre"] = "❌ MANQUANTE"
                code = "SIGNATURE_MINISTRE_MANQUANTE"
                anomalies.append({
                    "code": code,
                    "description": "La signature et le cachet du Ministre sont absents.",
                    "criticite": determiner_criticite(code).value,
                    "profil_concerne": profil, "etape": etape,
                    "recommandation": "Le Ministre doit avoir signé l'acte.",
                })

        # Statut final
        anomalies_bloquantes = [
            a for a in anomalies if a["criticite"] == NiveauCriticite.BLOQUANT.value
        ]
        anomalies_importantes = [
            a for a in anomalies if a["criticite"] == NiveauCriticite.IMPORTANT.value
        ]
        anomalies_information = [
            a for a in anomalies if a["criticite"] == NiveauCriticite.INFORMATION.value
        ]
        statut = StatutEtape.REJETE.value if anomalies_bloquantes else StatutEtape.VALIDE.value

        def _lister_points(liste_anomalies, avec_recommandation=False):
            return "\n".join(
                f"{i}. {a['description']}"
                + (f" → Action requise : {a['recommandation']}" if avec_recommandation and a.get("recommandation") else "")
                for i, a in enumerate(liste_anomalies, start=1)
            )

        # Détail complet de CHAQUE niveau réellement présent dans cet acte
        # (pas juste un compte) — Bloquant en premier, puis Important, puis
        # Information. Seul le niveau BLOQUANT affiche une recommandation
        # d'action : c'est le seul qui empêche réellement la validation de
        # l'acte, les autres niveaux sont indicatifs.
        blocs_niveaux = []
        if anomalies_bloquantes:
            blocs_niveaux.append(
                f"🔴 Bloquant ({len(anomalies_bloquantes)}) :\n{_lister_points(anomalies_bloquantes, avec_recommandation=True)}"
            )
        if anomalies_importantes:
            blocs_niveaux.append(
                f"🟠 Important ({len(anomalies_importantes)}) :\n{_lister_points(anomalies_importantes)}"
            )
        if anomalies_information:
            blocs_niveaux.append(
                f"🔵 Information ({len(anomalies_information)}) :\n{_lister_points(anomalies_information)}"
            )
        detail_niveaux = "\n\n".join(blocs_niveaux)

        if anomalies_bloquantes:
            liste_recommandations = [
                a["recommandation"] for a in anomalies_bloquantes if a.get("recommandation")
            ]
            message_verdict = (
                f"❌ Cet acte ne peut pas être validé à ce stade par le profil « {profil} ».\n\n"
                f"{detail_niveaux}\n\n"
                f"Une fois les anomalies bloquantes corrigées, l'acte pourra être resoumis pour validation."
            )
        else:
            liste_recommandations = []
            message_verdict = (
                f"✅ Cet acte a été examiné par le profil « {profil} » et satisfait à l'ensemble "
                f"des exigences réglementaires vérifiées à cette étape. Aucune anomalie bloquante "
                f"n'a été détectée."
                + (f"\n\n{detail_niveaux}" if detail_niveaux else "")
            )

        resultat = {
            "acte_id": acte_id,
            "etape": etape,
            "profil": profil,
            "statut": statut,
            "message_verdict": message_verdict,
            "recommandations": liste_recommandations,
            "anomalies": anomalies,
            "checks": checks,
            "tampons_detectes": tampons_detectes,
            "signature_detectee": signature_detectee,
            "numero_acte": "PRÉSENT" if numero_detecte else "MANQUANT",
            "timestamp": datetime.now().isoformat(),
        }

        self.historique.append(resultat)
        return resultat

    def verification_finale(self, pdf_bytes: bytes, acte_id: str) -> dict:
        if self.workflow_actuel is None:
            raise ValueError("Aucun workflow initialisé")

        nb_etapes = len(self.workflow_actuel)
        analyse_visuelle_finale_indisponible = False
        try:
            res_visuel = self._analyser_visuel_avec_cache(pdf_bytes)
            if res_visuel.get("erreur"):
                analyse_visuelle_finale_indisponible = True
                tampons_finaux, signature_finale, numero_final = [], False, False
            else:
                tampons_finaux = []
                if res_visuel.get("tampon_DGFP"): tampons_finaux.append("DGFP")
                if res_visuel.get("tampon_DIRSOLDE"): tampons_finaux.append("DS")
                if res_visuel.get("tampon_DPB"): tampons_finaux.append("DPB")
                if res_visuel.get("tampon_CF"): tampons_finaux.append("CF")
                if res_visuel.get("tampon_DP"): tampons_finaux.append("DP")
                signature_finale = res_visuel.get("signature_cachet_ministre_bas", False)
                numero_final = res_visuel.get("numero_acte_haut", False)
        except Exception:
            analyse_visuelle_finale_indisponible = True
            tampons_finaux, signature_finale, numero_final = [], False, False

        tous_tampons = sorted({
            t for cfg in self.workflow_actuel.values()
            for t in cfg.get("tampons_requis", [])
        })
        tampons_manquants = [t for t in tous_tampons if t not in tampons_finaux]

        toutes_anomalies = [a for r in self.historique for a in r["anomalies"]]
        nb_bloq = sum(1 for a in toutes_anomalies if a["criticite"] == NiveauCriticite.BLOQUANT.value)
        nb_imp = sum(1 for a in toutes_anomalies if a["criticite"] == NiveauCriticite.IMPORTANT.value)
        nb_info = sum(1 for a in toutes_anomalies if a["criticite"] == NiveauCriticite.INFORMATION.value)

        etapes_faites = sorted({r["etape"] for r in self.historique})
        etapes_manquantes = [n for n in range(1, nb_etapes + 1) if n not in etapes_faites]

        # Si l'analyse visuelle finale a échoué techniquement (panne API,
        # timeout...), on ne peut tirer AUCUNE conclusion sur les
        # tampons/la signature — on ne déclare donc pas l'acte non
        # conforme sur cette seule base, pour ne pas rejeter à tort un
        # acte qui a réellement tous ses tampons.
        conforme = (
            not analyse_visuelle_finale_indisponible
            and not tampons_manquants and signature_finale and numero_final
            and nb_bloq == 0 and not etapes_manquantes
        )

        return {
            "acte_id": acte_id,
            "type_acte": self.type_acte_detecte.value if self.type_acte_detecte else "inconnu",
            "ref_engine": REF_ENGINE_PAR_TYPE_ACTE.get(self.type_acte_detecte),  # APAE / APAG / RET
            "nb_etapes_circuit": nb_etapes,
            "etapes_effectuees": etapes_faites,
            "etapes_manquantes": etapes_manquantes,
            "tampons_attendus": tous_tampons,
            "tampons_detectes_final": tampons_finaux,
            "tampons_manquants_final": tampons_manquants,
            "signature_ministre_presente": signature_finale,
            "numero_acte_present": numero_final,
            "analyse_visuelle_indisponible": analyse_visuelle_finale_indisponible,
            "nb_anomalies_bloquantes": nb_bloq,
            "nb_anomalies_importantes": nb_imp,
            "nb_anomalies_informations": nb_info,
            "conforme_de_a_a_z": conforme,
            "timestamp": datetime.now().isoformat(),
        }


# ═══════════════════════════════════════════════════════════
#  GESTION MULTI-ACTES — un moteur isolé par acte_id, pour
#  permettre le traitement de plusieurs actes en parallèle
#  (indispensable pour un usage GIRAFE multi-agents).
# ═══════════════════════════════════════════════════════════

import time as _time

_DUREE_EXPIRATION_SECONDES = 2 * 60 * 60  # purge après 2h d'inactivité

# acte_id -> [MoteurValidationGIRAFE, timestamp_derniere_activite]
_moteurs_par_acte: dict = {}


def _purger_moteurs_expires():
    """Libère la mémoire des actes inactifs depuis trop longtemps —
    évite une fuite mémoire si /workflow/reset n'est jamais appelé."""
    maintenant = _time.time()
    expires = [
        aid for aid, (_, derniere_activite) in _moteurs_par_acte.items()
        if maintenant - derniere_activite > _DUREE_EXPIRATION_SECONDES
    ]
    for aid in expires:
        del _moteurs_par_acte[aid]
    if expires:
        logger.info(f" {len(expires)} acte(s) expiré(s) purgé(s) du cache (inactifs >2h)")


def get_moteur(acte_id: str) -> MoteurValidationGIRAFE:
    """Retourne le moteur isolé de CET acte précis. Deux actes différents
    (acte_id différents) ne partagent jamais leur état — ils peuvent être
    traités en parallèle sans risque de mélange."""
    _purger_moteurs_expires()
    if acte_id not in _moteurs_par_acte:
        _moteurs_par_acte[acte_id] = [MoteurValidationGIRAFE(), _time.time()]
    else:
        _moteurs_par_acte[acte_id][1] = _time.time()
    return _moteurs_par_acte[acte_id][0]


def reset_moteur(acte_id: str) -> MoteurValidationGIRAFE:
    """Réinitialise UNIQUEMENT le moteur de cet acte précis — n'affecte
    aucun autre acte en cours de traitement."""
    _moteurs_par_acte[acte_id] = [MoteurValidationGIRAFE(), _time.time()]
    return _moteurs_par_acte[acte_id][0]