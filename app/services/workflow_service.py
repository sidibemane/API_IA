"""Service Workflow : MoteurValidationGIRAFE complet."""

import hashlib
import logging
from datetime import datetime
from typing import Optional

from app.models import TypeActe, NiveauCriticite, StatutEtape
from app.services.regles_service import (
    extraire_infos_acte, verifier_points_abc, detecter_type_acte,
    calcul_delai_annees, get_delai_reglementaire, SIGNATAIRE_OFFICIEL,
)
from app.services.vision_service import analyser_visuel_acte
from app.services.llm_service import verification_semantique

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
#  WORKFLOWS (3 circuits)
# ═══════════════════════════════════════════════════════════

def _etape(nom, role, tampons=None, signature=False, numero=False):
    return {
        "nom": nom, "role": role, "type_action": ["valider"],
        "analyse_textuelle": True,
        "tampons_requis": tampons or [],
        "signature_requise": signature,
        "numero_acte_requis": numero,
    }

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

CODES_BLOQUANTS = {
    "ENTETE_NON_CONFORME", "TYPE_ACTE_INCORRECT", "TYPE_ACTE_NON_DETECTE",
    "SIGNATURE_MINISTRE_MANQUANTE", "NUMERO_ACTE_MANQUANT", "ERREUR_LECTURE",
    "DELAI_NON_CONFORME", "MATRICULE_NON_TROUVE_DANS_ACTE", "AGENT_INCONNU_BASE",
    "IDENTITE_NOM_INCORRECT", "IDENTITE_PRENOM_INCORRECT",
    "IDENTITE_DATE_NAISSANCE_INCORRECTE", "DELAI_AVANCEMENT_INCORRECT",
    "SIGNATAIRE_INCORRECT",
}
CODES_IMPORTANTS = {
    "DATES_MANQUANTES", "LLM_INCOHERENCE_OBJET",
    "LLM_CONTRADICTION_INTERNE", "DELAI_NON_VERIFIABLE", "DELAI_AVANCEMENT_A_VERIFIER",
}
CODES_INFORMATIFS = {"TEXTE_EXTRACTION_PARTIELLE", "LLM_VISAS_A_VERIFIER"}


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
        self.etapes_textuelles_faites: set = set()
        self.historique: list = []
        # Cache par empreinte de contenu — évite de re-solliciter Gemini/HF
        # pour un fichier ou un texte déjà analysé, MAIS relance
        # automatiquement l'analyse dès que le contenu change réellement
        # (ex: un tampon vient d'être ajouté entre deux étapes).
        self.cache_vision: dict = {}
        self.cache_semantique: dict = {}

    def _analyser_visuel_avec_cache(self, pdf_bytes: bytes) -> dict:
        """Réutilise le résultat déjà obtenu si CE fichier exact a déjà été
        analysé. Dès que le contenu du PDF change (ex: un tampon vient
        d'être ajouté), l'empreinte change aussi → nouvelle analyse
        automatique, jamais de résultat périmé."""
        empreinte = hashlib.sha256(pdf_bytes).hexdigest()
        if empreinte in self.cache_vision:
            logger.info(f"♻️  Vision réutilisée depuis le cache (empreinte {empreinte[:8]}...)")
            return self.cache_vision[empreinte]

        resultat = analyser_visuel_acte(pdf_bytes)
        self.cache_vision[empreinte] = resultat
        return resultat

    def _verification_semantique_avec_cache(self, acte_text: str) -> dict:
        """Même principe pour la vérification sémantique (LLM) : réutilise
        le résultat si CE texte exact a déjà été analysé."""
        empreinte = hashlib.sha256(acte_text.encode("utf-8")).hexdigest()
        if empreinte in self.cache_semantique:
            logger.info(f"♻️  Analyse sémantique réutilisée depuis le cache (empreinte {empreinte[:8]}...)")
            return self.cache_semantique[empreinte]

        resultat = verification_semantique(acte_text)
        self.cache_semantique[empreinte] = resultat
        return resultat

    def initialiser_workflow(self, acte_text: str) -> dict:
        type_str = detecter_type_acte(acte_text)
        self.type_acte_detecte = TypeActe(type_str)
        self.workflow_actuel = WORKFLOWS_CONFIG[self.type_acte_detecte]
        return {
            "type_acte_detecte": self.type_acte_detecte.value,
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

        # ── Analyse textuelle ──
        if config.get("analyse_textuelle") and etape not in self.etapes_textuelles_faites:
            resultats_abc = verifier_points_abc(acte_text)

            # Point A
            if not resultats_abc["point_A"]["conforme"]:
                code = "ENTETE_NON_CONFORME"
                anomalies.append({
                    "code": code, "description": "En-tête non conforme [RÈGLE J-02]",
                    "criticite": determiner_criticite(code).value,
                    "profil_concerne": profil, "etape": etape,
                    "recommandation": "Corriger l'en-tête officiel.",
                })
                checks["En-tête officiel"] = "❌ NON CONFORME"
            else:
                checks["En-tête officiel"] = "✅ CONFORME"

            # Point B
            if not resultats_abc["point_B"]["conforme"]:
                code = "TYPE_ACTE_INCORRECT"
                anomalies.append({
                    "code": code,
                    "description": f"Type incorrect. Constaté: {resultats_abc['point_B']['constate']}, Attendu: {resultats_abc['point_B']['attendu']} [RÈGLE J-03]",
                    "criticite": determiner_criticite(code).value,
                    "profil_concerne": profil, "etape": etape,
                    "recommandation": "Vérifier la nature de l'acte.",
                })
                checks["Type d'acte"] = f"❌ Non conforme (attendu {resultats_abc['point_B']['attendu']})"
            else:
                checks["Type d'acte"] = f"✅ {resultats_abc['point_B']['constate']}"

            # Point C
            if not resultats_abc["point_C"]["conforme"]:
                code = "SIGNATAIRE_INCORRECT"
                anomalies.append({
                    "code": code, "description": "Signataire incorrect [RÈGLE J-02]",
                    "criticite": determiner_criticite(code).value,
                    "profil_concerne": profil, "etape": etape,
                    "recommandation": f"Attendu : '{SIGNATAIRE_OFFICIEL}'",
                })
                checks["Signataire"] = "❌ NON CONFORME"
            else:
                checks["Signataire"] = "✅ CONFORME"

            # Vérification sémantique LLM
            try:
                sem = self._verification_semantique_avec_cache(acte_text)
                if sem.get("coherence_objet", "").upper().startswith("NON"):
                    code = "LLM_INCOHERENCE_OBJET"
                    anomalies.append({
                        "code": code,
                        "description": "Qwen 14B signale une incohérence entre l'objet et le corps de l'acte.",
                        "criticite": determiner_criticite(code).value,
                        "profil_concerne": "Global", "etape": 0,
                        "recommandation": "Faire relire l'acte par un agent.",
                    })
            except Exception as e:
                logger.warning(f"Vérification sémantique indisponible : {e}")

            # Identité agent (matricule / nom / prénom / date de naissance vs base des agents)
            try:
                from app.services.agents_service import verifier_identite_agent, extraire_identite_agent, verifier_delais_avancement
                anomalies_id, checks_id = verifier_identite_agent(acte_text, etape, profil, agents_externes)
                anomalies.extend(anomalies_id)
                checks.update(checks_id)

                # Délai d'avancement grade/échelon (utilise les infos déjà extraites ci-dessus)
                agents_acte = extraire_identite_agent(acte_text)
                anomalies_delai, checks_delai = verifier_delais_avancement(
                    acte_text, agents_acte,
                    resultats_abc["infos"]["statut"],
                    resultats_abc["infos"]["hierarchie"],
                    resultats_abc["infos"]["corps"],
                    etape, profil,
                )
                anomalies.extend(anomalies_delai)
                checks.update(checks_delai)
            except Exception as e:
                logger.warning(f"Vérification identité/délai indisponible : {e}")

            self.etapes_textuelles_faites.add(etape)
            self.anomalies_textuelles = anomalies
            self.checks_textuels = checks

        elif config.get("analyse_textuelle"):
            anomalies.extend(self.anomalies_textuelles)
            checks.update(self.checks_textuels)

        # ── Vérification visuelle ──
        tampons_detectes = []
        signature_detectee = False
        numero_detecte = False

        tampons_requis = config.get("tampons_requis", [])
        signature_requise = config.get("signature_requise", False)
        numero_requis = config.get("numero_acte_requis", False)

        if (tampons_requis or signature_requise or numero_requis) and pdf_bytes:
            try:
                res_visuel = self._analyser_visuel_avec_cache(pdf_bytes)
                if res_visuel.get("tampon_DGFP"): tampons_detectes.append("DGFP")
                if res_visuel.get("tampon_DIRSOLDE"): tampons_detectes.append("DS")
                if res_visuel.get("tampon_DPB"): tampons_detectes.append("DPB")
                if res_visuel.get("tampon_CF"): tampons_detectes.append("CF")
                if res_visuel.get("tampon_DP"): tampons_detectes.append("DP")
                signature_detectee = res_visuel.get("signature_cachet_ministre_bas", False)
                numero_detecte = res_visuel.get("numero_acte_haut", False)
            except Exception as e:
                logger.error(f"Erreur vision : {e}")

        # Vérification tampons
        for tampon in tampons_requis:
            if tampon in tampons_detectes:
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

        # Vérification signature
        if signature_requise:
            if signature_detectee:
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
        statut = StatutEtape.REJETE.value if anomalies_bloquantes else StatutEtape.VALIDE.value

        if anomalies_bloquantes:
            liste_motifs = "; ".join(f"{a['code']} — {a['description']}" for a in anomalies_bloquantes)
            liste_recommandations = [
                a["recommandation"] for a in anomalies_bloquantes if a.get("recommandation")
            ]
            message_verdict = (
                f"❌ Cet acte NE PEUT PAS être validé par le profil « {profil} » "
                f"en raison de {len(anomalies_bloquantes)} anomalie(s) bloquante(s) : {liste_motifs}."
            )
        else:
            liste_recommandations = []
            message_verdict = f"✅ Cet acte est validé par le profil « {profil} », aucune anomalie bloquante."

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
        try:
            res_visuel = self._analyser_visuel_avec_cache(pdf_bytes)
            tampons_finaux = []
            if res_visuel.get("tampon_DGFP"): tampons_finaux.append("DGFP")
            if res_visuel.get("tampon_DIRSOLDE"): tampons_finaux.append("DS")
            if res_visuel.get("tampon_DPB"): tampons_finaux.append("DPB")
            if res_visuel.get("tampon_CF"): tampons_finaux.append("CF")
            if res_visuel.get("tampon_DP"): tampons_finaux.append("DP")
            signature_finale = res_visuel.get("signature_cachet_ministre_bas", False)
            numero_final = res_visuel.get("numero_acte_haut", False)
        except:
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

        conforme = (
            not tampons_manquants and signature_finale and numero_final
            and nb_bloq == 0 and not etapes_manquantes
        )

        return {
            "acte_id": acte_id,
            "type_acte": self.type_acte_detecte.value if self.type_acte_detecte else "inconnu",
            "nb_etapes_circuit": nb_etapes,
            "etapes_effectuees": etapes_faites,
            "etapes_manquantes": etapes_manquantes,
            "tampons_attendus": tous_tampons,
            "tampons_detectes_final": tampons_finaux,
            "tampons_manquants_final": tampons_manquants,
            "signature_ministre_presente": signature_finale,
            "numero_acte_present": numero_final,
            "nb_anomalies_bloquantes": nb_bloq,
            "nb_anomalies_importantes": nb_imp,
            "nb_anomalies_informations": nb_info,
            "conforme_de_a_a_z": conforme,
            "timestamp": datetime.now().isoformat(),
        }


# Instance globale
_moteur_global = MoteurValidationGIRAFE()


def get_moteur() -> MoteurValidationGIRAFE:
    return _moteur_global


def reset_moteur():
    global _moteur_global
    _moteur_global = MoteurValidationGIRAFE()
    return _moteur_global