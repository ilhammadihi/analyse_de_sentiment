"""
Module 5 — extraire une affirmation, chercher ses preuves, refuser de conclure.

LE PROBLÈME, TEL QU'IL SE POSE VRAIMENT
    Quarante clients écrivent « le réseau est coupé depuis hier ». La tentation
    est d'en conclure « panne réseau confirmée », de le transmettre à l'Agent 1,
    qui l'écrira dans un briefing, qui sera lu comme un fait établi.

    Or quarante avis concordants ne sont qu'UNE SEULE espèce de preuve. Ils
    peuvent décrire la même rumeur, le même fil viral, ou une panne de quartier
    prise pour une panne nationale. LE NOMBRE NE FAIT PAS LA CORROBORATION :
    c'est l'INDÉPENDANCE des espèces de preuves qui la fait.

CE MODULE NE RÉÉCRIT PAS LE MOTEUR DE PREUVES
    `PressRepository.evidence()` fait déjà exactement le travail difficile :
    chercher des articles datés sur le bon périmètre, élargir de la filiale au
    pays quand la filiale n'a pas de presse propre, exclure l'actualité des
    concurrents, écarter le positif, dédoublonner les reprises, et RENDRE le
    périmètre effectivement retenu pour qu'on ne présente jamais un article
    national comme parlant de la filiale.

    Tout cela est éprouvé et documenté. On l'appelle, on ne le refait pas.

CE QUE CE MODULE AJOUTE
    1. l'EXTRACTION de l'affirmation depuis les avis — sur les aspects, qui
       sont une taxonomie fermée, jamais sur des mots-clés libres ;
    2. le COMPTAGE DES ESPÈCES de preuves indépendantes ;
    3. la CONFIANCE, calculée et non demandée à un modèle.

POURQUOI LA CONFIANCE EST CALCULÉE
    Un modèle à qui l'on demande son degré de certitude rend un nombre corrélé
    à son aisance rédactionnelle, pas à la solidité du fait. La confiance
    découle donc ici du nombre d'espèces indépendantes et de leur nature —
    règle fixe, reproductible, et opposable.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

from reviews.domain.aspects import label as aspect_label
from reviews.storage.db import Database
from reviews.storage.press_repository import PressRepository

logger = logging.getLogger(__name__)

CONFIRMED = "CONFIRMED"
CORROBORATED = "CORROBORATED"
PLAUSIBLE = "PLAUSIBLE"
UNCONFIRMED = "UNCONFIRMED"

#: Avis portant le même aspect négatif pour qu'une affirmation soit extraite.
#:
#: 5 et non 2 : en dessous, on remonterait le grief individuel de deux clients
#: au rang d'affirmation à vérifier. L'objet du module est le fait COLLECTIF —
#: celui qui risque d'être relayé comme un événement.
MIN_AVIS_POUR_AFFIRMATION = 5

#: Part des avis négatifs de la période que l'aspect doit représenter.
#: Un aspect à 5 avis sur 800 n'est pas un phénomène, c'est un fond de bruit.
MIN_PART_AFFIRMATION = 0.15

#: Jours d'amorce avant la fenêtre pour la recherche de presse.
#:
#: UNE CAUSE PRÉCÈDE SON EFFET : un client mécontent d'une hausse annoncée le 3
#: écrit son avis le 10. `PressRepository` documente cette latence et laisse à
#: l'appelant le soin d'élargir — c'est ici que ça se fait.
AMORCE_JOURS = 10


@dataclass
class Affirmation:
    """Une affirmation extraite du corpus, et son degré de corroboration."""

    claim: str
    topic: str
    subsidiary_id: Optional[int]
    subsidiary: Optional[str]
    country: Optional[str]
    window_from: date
    window_to: date
    status: str = UNCONFIRMED
    confidence: float = 0.0
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def exploitable(self) -> bool:
        """L'Agent 1 ou 2 peut-il relayer cette affirmation ?

        C'est LA question que ce module existe pour trancher. `UNCONFIRMED`
        n'interdit pas de regarder le phénomène — il interdit de le PRÉSENTER
        comme un fait établi.
        """
        return self.status in (CONFIRMED, CORROBORATED)

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "topic": self.topic,
            "subsidiary_id": self.subsidiary_id,
            "subsidiary": self.subsidiary,
            "country": self.country,
            "window": [self.window_from.isoformat(), self.window_to.isoformat()],
            "status": self.status,
            "confidence": round(self.confidence, 2),
            "exploitable": self.exploitable,
            "evidence": self.evidence,
        }


def evaluer_corroboration(evidence: list[dict[str, Any]]) -> tuple[str, float]:
    """Traduit un faisceau de preuves en (statut, confiance). Fonction PURE.

    LES ESPÈCES, PAS LE VOLUME. Quarante avis restent une seule espèce ; un
    avis plus un article en font deux. C'est tout le raisonnement du module, et
    il tient dans le comptage de `source` distinctes.

    L'échelle :
        source officielle présente          -> CONFIRMED    (0,9)
        au moins deux espèces indépendantes -> CORROBORATED (0,75)
        une seule espèce, signal fort       -> PLAUSIBLE    (0,45)
        rien d'exploitable                  -> UNCONFIRMED  (0,2)
    """
    if not evidence:
        return UNCONFIRMED, 0.0

    especes = {e.get("source") for e in evidence if e.get("source")}

    # Une source officielle (opérateur, régulateur) tranche à elle seule : elle
    # ne rapporte pas un ressenti, elle constate. Aucune ne peut aujourd'hui
    # entrer dans le corpus — aucun collecteur ne les couvre — mais l'échelle
    # doit prévoir le cas, sans quoi ajouter cette source plus tard obligerait
    # à rouvrir la règle de confiance.
    if "official" in especes:
        return CONFIRMED, 0.9

    if len(especes) >= 2:
        # La confiance monte légèrement avec le nombre d'espèces, sans jamais
        # atteindre celle d'une source officielle : deux espèces concordantes
        # restent une coïncidence possible.
        return CORROBORATED, min(0.85, 0.75 + 0.05 * (len(especes) - 2))

    # Une seule espèce. Le volume ne fait pas la corroboration, mais il fait la
    # différence entre un signal et un bruit : on module la confiance sans
    # jamais franchir le seuil du corroboré.
    volume = max((e.get("count") or 0) for e in evidence)
    if volume >= MIN_AVIS_POUR_AFFIRMATION * 4:
        return PLAUSIBLE, 0.5
    return PLAUSIBLE if volume >= MIN_AVIS_POUR_AFFIRMATION else UNCONFIRMED, (
        0.45 if volume >= MIN_AVIS_POUR_AFFIRMATION else 0.2
    )


class VerificateurAffirmations:
    """Extrait les affirmations d'une période et cherche ce qui les corrobore."""

    def __init__(self, db: Database, press: Optional[PressRepository] = None):
        self.db = db
        # Injectable pour les tests : le moteur de preuves est réutilisé, pas
        # remplacé, et un test ne doit pas avoir besoin d'une base de presse.
        self.press = press or PressRepository(db)

    def analyser(
        self, *, subsidiary_id: int, jours: int = 14, aujourdhui: Optional[date] = None
    ) -> list[Affirmation]:
        """Affirmations d'une filiale sur la fenêtre, avec leur corroboration."""
        fin = aujourdhui or date.today()
        debut = fin - timedelta(days=jours)

        try:
            griefs = self._griefs_dominants(subsidiary_id, debut, fin)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Extraction des affirmations impossible pour la filiale %s",
                subsidiary_id, exc_info=True,
            )
            return []

        return [self._verifier(g, debut, fin) for g in griefs]

    # ---------------------------------------------------------- Extraction

    def _griefs_dominants(
        self, subsidiary_id: int, debut: date, fin: date
    ) -> list[dict[str, Any]]:
        """Aspects négatifs dominants de la période, avec leur poids.

        SUR LES ASPECTS, JAMAIS SUR DES MOTS-CLÉS. La taxonomie est fermée
        (`domain/aspects.py`), donc l'affirmation extraite est nommée,
        comparable d'une filiale à l'autre, et ne peut pas être inventée. Une
        extraction par mots-clés ramènerait « can't », « bad », « its » — le
        défaut exact que la couche sémantique a été écrite pour corriger.

        `aspect_scope <> 'app'` : un grief applicatif ne décrit pas un
        événement extérieur. Chercher dans la presse la cause d'un bug
        d'application est un contresens, et proposerait un article de
        régulateur comme explication d'un plantage au démarrage.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT t.aspect, COUNT(*) AS n,
                       COUNT(*)::float / NULLIF(SUM(COUNT(*)) OVER (), 0) AS part,
                       -- L'identifiant voyage avec le grief : `_verifier` en a
                       -- besoin pour interroger le moteur de preuves, et le
                       -- reprendre du paramètre d'appel obligerait à le passer
                       -- en double — donc à pouvoir les désynchroniser.
                       %s::int AS subsidiary_id,
                       MAX(sub.name) AS subsidiary, MAX(co.iso2) AS iso2
                FROM v_review_aspects t
                JOIN dim_subsidiary sub ON sub.subsidiary_id = t.subsidiary_id
                JOIN dim_country    co  ON co.country_id     = sub.country_id
                WHERE t.subsidiary_id = %s
                  AND t.polarity = 'negative'
                  AND t.source_kind = 'customer_review'
                  AND t.aspect_scope <> 'app'
                  AND t.aspect <> 'autre'
                  AND t.occurred_at >= %s AND t.occurred_at < %s
                GROUP BY t.aspect
                HAVING COUNT(*) >= %s
                ORDER BY COUNT(*) DESC
                LIMIT 3
                """,
                (subsidiary_id, subsidiary_id, debut, fin + timedelta(days=1),
                 MIN_AVIS_POUR_AFFIRMATION),
            )
            lignes = [dict(r) for r in cur.fetchall()]

        return [
            r for r in lignes
            if (r.get("part") or 0.0) >= MIN_PART_AFFIRMATION
        ]

    # -------------------------------------------------------- Vérification

    def _verifier(
        self, grief: dict[str, Any], debut: date, fin: date
    ) -> Affirmation:
        """Cherche les preuves indépendantes d'un grief et en tire un statut."""
        aspect = grief["aspect"]
        libelle = aspect_label(aspect)

        # ESPÈCE 1 — les avis clients. Toujours présente, par construction :
        # c'est d'eux que l'affirmation a été extraite. Elle ne suffit donc
        # JAMAIS à corroborer quoi que ce soit, et c'est le point de départ du
        # raisonnement, pas son aboutissement.
        evidence: list[dict[str, Any]] = [
            {
                "source": "customer_reviews",
                "count": grief["n"],
                "part": round(grief.get("part") or 0.0, 3),
                "type": "témoignages concordants",
                "date": fin.isoformat(),
            }
        ]

        # ESPÈCE 2 — la presse. Le moteur existant, avec son amorce amont.
        try:
            preuves = self.press.evidence(
                window=(debut - timedelta(days=AMORCE_JOURS), fin + timedelta(days=1)),
                level="subsidiary",
                value=str(grief.get("subsidiary_id") or ""),
            )
        except Exception:  # noqa: BLE001
            logger.warning("Preuves de presse indisponibles.", exc_info=True)
            preuves = {"articles": [], "perimetre": None, "elargi": False}

        for article in preuves.get("articles") or []:
            evidence.append(
                {
                    "source": "news",
                    "titre": article.get("titre"),
                    "media": article.get("media"),
                    "date": article.get("date"),
                    "type": "article de presse",
                    # LE PÉRIMÈTRE VOYAGE AVEC LA PREUVE. Un article national
                    # ne doit jamais être présenté comme parlant de la filiale —
                    # c'est la garantie que `PressRepository` rend explicitement
                    # et qu'il serait fautif de laisser tomber ici.
                    "perimetre": preuves.get("perimetre"),
                    "elargi": preuves.get("elargi", False),
                }
            )

        status, confiance = evaluer_corroboration(evidence)
        return Affirmation(
            claim=(
                f"{libelle} : {grief['n']} avis négatifs concordants sur la période"
            ),
            topic=aspect,
            subsidiary_id=grief.get("subsidiary_id"),
            subsidiary=grief.get("subsidiary"),
            country=grief.get("iso2"),
            window_from=debut,
            window_to=fin,
            status=status,
            confidence=confiance,
            evidence=evidence,
        )
