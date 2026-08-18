"""
Module 4 — validation sémantique des avis douteux.

CE QU'IL FAIT, ET SUR QUOI SEULEMENT
    Il ne relit PAS le corpus. Il n'est appelé que sur les avis qu'une règle
    déterministe a déjà marqués douteux — quelques dizaines par passage, pas
    40 078. C'est la section 18 de l'énoncé appliquée littéralement : « le LLM
    ne doit pas recevoir toute la base », et c'est aussi la seule façon de
    tenir un budget de 60 appels par jour.

    L'ordre est le même que partout dans ce projet : les règles trient, le
    modèle instruit. Jamais l'inverse.

CALQUÉ SUR `SemanticAnalyzer`, ET DÉLIBÉRÉMENT
    Ce patron est éprouvé : lots, plafond de jetons proportionnel au lot,
    numérotation par position plutôt que par identifiant, réponse reparsée
    défensivement, avis non classé estampillé quand même. Chacune de ces
    décisions corrige une panne mesurée — dont 4 % de lots perdus par troncature
    lors du premier backfill. Réinventer un second patron ici, c'est se garantir
    de repayer les mêmes.

CE QUI CHANGE PAR RAPPORT À L'ANALYSE SÉMANTIQUE
    La sortie n'est pas une taxonomie mais un VERDICT, et un verdict invalide
    n'est jamais un rejet : il devient `REVIEW_REQUIRED`. Un modèle
    indisponible ou incohérent ne doit pas pouvoir faire écarter des données —
    ce serait la suppression silencieuse que l'énoncé interdit, obtenue par
    accident plutôt que par décision.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from reviews.llm.client import LLMClient, LLMError, LLMUnavailable

logger = logging.getLogger(__name__)

#: Jetons de sortie réservés PAR AVIS.
#:
#: 170 et non 110 comme l'analyse sémantique : le verdict porte huit champs
#: dont une `raison` en texte libre, là où un résultat d'aspects tient en trois
#: listes courtes. Proportionnel au lot, jamais constant — sinon augmenter
#: `LLM3_BATCH_SIZE` réintroduit la troncature silencieusement.
_TOKENS_PAR_AVIS = 170
_TOKENS_ENVELOPPE = 150

#: Provision pour les JETONS DE RAISONNEMENT, mesurée le 17 août 2026.
#:
#: POURQUOI CETTE CONSTANTE EXISTE, ET CE QU'ELLE ÉVITE
#:     `gpt-oss:20b` — le modèle retenu sur Ollama Cloud — est un modèle à
#:     raisonnement : il renvoie DEUX champs, `reasoning` et `content`, et les
#:     deux se paient sur `completion_tokens`. Vérifié sur un appel trivial
#:     (« réponds {"ok": true} ») : 71 jetons produits, dont 59 de raisonnement
#:     pour 12 de réponse utile.
#:
#:     Conséquence directe, observée : le même appel avec `max_tokens=32`
#:     rend `content` VIDE. Le budget est intégralement parti dans le
#:     raisonnement, la réponse n'a jamais commencé, et `LLMClient._parse` lève
#:     alors « Le modèle a renvoyé une réponse vide » — c'est-à-dire un lot
#:     entier perdu et un appel de quota gaspillé.
#:
#:     C'est exactement la panne que `_TOKENS_PAR_AVIS` avait été dimensionné
#:     pour éviter côté Gemini (4 % de lots perdus sur le premier backfill
#:     sémantique). Elle revient par une autre porte dès qu'on change de famille
#:     de modèle, et un budget calé sur la seule sortie utile la rouvre.
#:
#: FORFAITAIRE ET NON PROPORTIONNEL AU LOT : le raisonnement porte sur la
#: CONSIGNE, qui ne change pas avec le nombre d'avis. Le rendre proportionnel
#: gonflerait le plafond sans raison sur les gros lots.
#:
#: Sans effet sur un modèle sans raisonnement : les jetons non produits ne sont
#: pas facturés, la constante ne coûte donc rien à Gemini.
_TOKENS_RAISONNEMENT = 400

#: Sujets autorisés. LISTE FERMÉE, comme la taxonomie d'aspects : un champ
#: libre produirait autant de libellés que d'appels et ne serait pas comptable.
SUJETS = ("network", "billing", "app", "customer_service", "coverage", "other")

_SYSTEM = """Tu es un contrôleur qualité de données pour une plateforme qui \
analyse la satisfaction des clients d'opérateurs télécoms africains.

On te soumet des avis SUSPECTS, déjà signalés par une règle automatique. Ton \
rôle est de trancher, avis par avis, s'ils sont exploitables pour une analyse \
de satisfaction.

Pour chaque avis, tu réponds :
- relevant       : l'avis parle-t-il réellement d'un service télécom ?
- operator_match : parle-t-il de l'opérateur indiqué ?
- subsidiary_match : rien ne contredit-il le pays/la filiale indiqués ?
- spam           : publicité, arnaque, texte automatique, lien commercial ?
- duplicate      : reformulation évidente d'un autre avis du même lot ?
- topic          : un seul parmi network, billing, app, customer_service, \
coverage, other.
- confidence     : entre 0 et 1. Descends sous 0,5 si l'avis est trop court, \
ambigu, ou dans une langue que tu déchiffres mal.
- reason         : une phrase courte, en français, justifiant ton verdict.

RÈGLES QUI PRIMENT SUR TON INTUITION :
- Dans le doute, réponds relevant=true et confidence basse. Il vaut mieux \
garder un avis douteux que rejeter un avis valable : les données ne sont \
jamais supprimées, seulement marquées.
- Un avis très négatif n'est PAS du spam. La colère n'est pas de la publicité.
- Un avis court mais sincère (« réseau nul ») est pertinent : il porte un \
sentiment. Seuls les textes SANS contenu (« ok », « ... ») ne le sont pas.
- subsidiary_match=false demande un indice EXPLICITE dans le texte (une autre \
ville, un autre pays, un autre opérateur nommé). L'absence d'indice n'est pas \
une contradiction : réponds true.

Tu réponds UNIQUEMENT par un objet JSON, sans texte autour ni balises de code."""


@dataclass
class Verdict:
    """Ce que le modèle a jugé d'un avis signalé."""

    flag_id: int
    review_id: str
    relevant: Optional[bool] = None
    operator_match: Optional[bool] = None
    subsidiary_match: Optional[bool] = None
    spam: Optional[bool] = None
    duplicate: Optional[bool] = None
    topic: Optional[str] = None
    confidence: Optional[float] = None
    reason: str = ""

    @property
    def valide(self) -> bool:
        """Le modèle a-t-il réellement rendu un verdict exploitable ?

        On exige `relevant` ET une confiance : une réponse partielle est le
        symptôme d'une troncature ou d'une hallucination de format, et la
        traiter comme un verdict ferait marquer des avis sur du vide.
        """
        return self.relevant is not None and self.confidence is not None

    def statut(self, seuil_confiance: float = 0.6) -> str:
        """Traduit le verdict en statut de constat.

        `REVIEW_REQUIRED` EST LE REPLI, et il l'est dans les deux cas où l'on
        ne sait pas : réponse inexploitable, ou confiance trop basse. Rejeter
        sur une réponse incertaine reviendrait à écarter des données sur un
        doute — exactement ce que la section 12 de l'énoncé refuse.
        """
        if not self.valide:
            return "REVIEW_REQUIRED"
        if (self.confidence or 0.0) < seuil_confiance:
            return "REVIEW_REQUIRED"
        mauvais = (
            not self.relevant
            or bool(self.spam)
            or bool(self.duplicate)
            or self.operator_match is False
        )
        return "REJECTED" if mauvais else "ACCEPTED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "relevant": self.relevant,
            "operator_match": self.operator_match,
            "subsidiary_match": self.subsidiary_match,
            "spam": self.spam,
            "duplicate": self.duplicate,
            "topic": self.topic,
            "confidence": self.confidence,
            "reason": self.reason,
        }


@dataclass
class RapportValidation:
    """Bilan d'une exécution, pour les journaux et la ligne de commande."""

    candidats: int = 0
    valides: int = 0
    acceptes: int = 0
    rejetes: int = 0
    a_revoir: int = 0
    lots: int = 0
    lots_en_echec: int = 0
    arret: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidats": self.candidats, "valides": self.valides,
            "acceptes": self.acceptes, "rejetes": self.rejetes,
            "a_revoir": self.a_revoir, "lots": self.lots,
            "lots_en_echec": self.lots_en_echec, "arret": self.arret,
        }


class ValidateurQualite:
    """Soumet au modèle les avis signalés, et traduit ses verdicts en statuts."""

    def __init__(
        self, client: LLMClient, *, seuil_confiance: float = 0.6
    ):
        self.client = client
        self.seuil_confiance = seuil_confiance

    def valider(self, avis: list[dict[str, Any]]) -> tuple[list[Verdict], RapportValidation]:
        """Instruit une liste d'avis signalés. Ne lève jamais.

        Un échec de lot laisse ses avis en `FLAGGED` : ils repasseront. C'est
        la même politique que l'analyse sémantique — avancer autant que le
        quota le permet, puis rendre la main proprement.
        """
        rapport = RapportValidation(candidats=len(avis))
        verdicts: list[Verdict] = []

        if not avis:
            return verdicts, rapport
        if not self.client.available:
            rapport.arret = self.client.unavailable_reason()
            logger.info("Validation qualité non lancée : %s", rapport.arret)
            return verdicts, rapport

        for lot in _lots(avis, max(1, self.client.cfg.batch_size)):
            try:
                verdicts.extend(self._valider_lot(lot))
                rapport.lots += 1
            except LLMUnavailable as exc:
                rapport.arret = str(exc)
                logger.info("Validation qualité interrompue : %s", exc)
                break
            except LLMError as exc:
                rapport.lots_en_echec += 1
                logger.warning("Lot de validation en échec, laissé en attente : %s", exc)
                continue

        for v in verdicts:
            statut = v.statut(self.seuil_confiance)
            rapport.valides += 1 if v.valide else 0
            if statut == "ACCEPTED":
                rapport.acceptes += 1
            elif statut == "REJECTED":
                rapport.rejetes += 1
            else:
                rapport.a_revoir += 1

        logger.info("Validation qualité : %s", rapport.as_dict())
        return verdicts, rapport

    # -------------------------------------------------------------- Interne

    def _valider_lot(self, lot: list[dict[str, Any]]) -> list[Verdict]:
        data = self.client.complete_json(
            system=_SYSTEM,
            user=self._composer(lot),
            max_tokens=(
                _TOKENS_ENVELOPPE
                + _TOKENS_RAISONNEMENT
                + _TOKENS_PAR_AVIS * len(lot)
            ),
        )
        par_position = _indexer(data)
        return [
            self._vers_verdict(avis, par_position.get(i))
            for i, avis in enumerate(lot, start=1)
        ]

    def _composer(self, lot: list[dict[str, Any]]) -> str:
        """Compose le message d'un lot.

        CONTEXTE MINIMAL ET CIBLÉ (section 18) : l'avis, et les seules
        métadonnées nécessaires au verdict — l'opérateur et le pays attendus,
        sans quoi « parle-t-il du bon opérateur ? » n'a pas de sens. Rien
        d'autre. Envoyer les statistiques de la filiale ferait payer des jetons
        pour une information que la question ne mobilise pas.

        Référencés par NUMÉRO et non par `review_id` : ces identifiants font une
        trentaine de caractères et les faire recopier au modèle coûte des jetons
        tout en lui offrant l'occasion de les écrire de travers.
        """
        lignes = []
        for i, avis in enumerate(lot, start=1):
            titre = (avis.get("title") or "").strip()
            texte = (avis.get("text") or "").strip()
            contenu = f"{titre}. {texte}" if titre else texte
            lignes.append(
                f"[{i}] opérateur attendu : {avis.get('operator') or '?'} "
                f"({avis.get('country') or '?'}) · source : {avis.get('source') or '?'} "
                f"· signalé pour : {avis.get('kind') or '?'}\n"
                f"     {contenu}"
            )

        return (
            f"AVIS À CONTRÔLER ({len(lot)})\n"
            + "\n".join(lignes)
            + "\n\nRéponds avec cet objet JSON exactement, un élément par avis, "
            "dans l'ordre :\n"
            '{"resultats": [{"i": 1, "relevant": true, "operator_match": true, '
            '"subsidiary_match": true, "spam": false, "duplicate": false, '
            '"topic": "network", "confidence": 0.9, "reason": "..."}]}'
        )

    def _vers_verdict(
        self, avis: dict[str, Any], brut: Optional[dict]
    ) -> Verdict:
        """Valide un verdict brut. Tout ce qui sort du contrat est neutralisé."""
        verdict = Verdict(
            flag_id=avis["flag_id"], review_id=avis["review_id"]
        )
        if not isinstance(brut, dict):
            return verdict

        verdict.relevant = _bool(brut.get("relevant"))
        verdict.operator_match = _bool(brut.get("operator_match"))
        verdict.subsidiary_match = _bool(brut.get("subsidiary_match"))
        verdict.spam = _bool(brut.get("spam"))
        verdict.duplicate = _bool(brut.get("duplicate"))

        sujet = str(brut.get("topic") or "").strip().lower()
        verdict.topic = sujet if sujet in SUJETS else "other"

        try:
            verdict.confidence = min(1.0, max(0.0, float(brut.get("confidence"))))
        except (TypeError, ValueError):
            verdict.confidence = None

        verdict.reason = str(brut.get("reason") or "").strip()[:300]
        return verdict


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------


def _lots(items: list, taille: int) -> Iterator[list]:
    for debut in range(0, len(items), max(1, taille)):
        yield items[debut : debut + taille]


def _bool(valeur: Any) -> Optional[bool]:
    """Booléen tolérant. None quand le modèle n'a rien dit d'exploitable.

    Les petits modèles rendent régulièrement « true »/« oui » en chaîne malgré
    la consigne. Refuser ces formes ferait perdre le verdict — donc l'appel —
    pour une différence d'emballage.
    """
    if isinstance(valeur, bool):
        return valeur
    if isinstance(valeur, str):
        v = valeur.strip().lower()
        if v in ("true", "oui", "yes", "1"):
            return True
        if v in ("false", "non", "no", "0"):
            return False
    if isinstance(valeur, (int, float)) and valeur in (0, 1):
        return bool(valeur)
    return None


def _indexer(data: Any) -> dict[int, dict]:
    """Indexe la réponse par numéro d'avis.

    Tolère l'objet demandé et la liste nue, que les petits modèles renvoient
    malgré la consigne — même défense que `semantic._index_results`, pour la
    même raison : refuser la seconde forme perdrait le lot entier.
    """
    items = data.get("resultats") if isinstance(data, dict) else data
    if isinstance(data, dict) and items is None:
        for valeur in data.values():
            if isinstance(valeur, list):
                items = valeur
                break
    if not isinstance(items, list):
        return {}

    indexe: dict[int, dict] = {}
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        try:
            cle = int(item.get("i", position))
        except (TypeError, ValueError):
            cle = position
        indexe[cle] = item
    return indexe
