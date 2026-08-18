"""
Module 6 — contrôles de qualité sur le corpus, et Module 7 — fraîcheur des sources.

CE QUI EST DÉJÀ TENU AILLEURS, ET QU'ON NE REFAIT PAS
    - Les doublons EXACTS : `reviews.checksum` est UNIQUE depuis la migration
      001. Un doublon strict ne peut pas entrer en base. Ce module ne cherche
      donc que les doublons SÉMANTIQUES — même texte à la ponctuation près,
      même avis republié sous deux sous-cibles.
    - Les dates aberrantes : `DATA_FLOOR` (2005) les écarte de tout agrégat
      borné. On les COMPTE ici, pour dire combien de lignes sont concernées,
      sans réappliquer le filtre.
    - La pertinence d'un article : `press_relevance.est_pertinent` est déjà
      écrit, éprouvé et multilingue. Réutilisé tel quel.

LA RÈGLE QUI GOUVERNE TOUT LE MODULE
    Rien n'est jamais supprimé. Chaque contrôle produit un CONSTAT — un
    marquage daté, motivé, réversible. L'énoncé l'exige, et le projet en a déjà
    payé le prix : les quatre alertes envoyées le 13 août sur des avis mal
    attribués n'ont pas pu être retirées du groupe Telegram. Ce qui est effacé
    ne se rattrape pas.

POURQUOI LES CONTRÔLES SONT BORNÉS EN VOLUME
    Chaque requête est plafonnée. Le corpus fait 40 078 avis et grossira ; un
    contrôle qui rendrait dix mille constats à chaque passage saturerait la
    table et l'écran, et personne n'en instruirait un seul. Un agent qui
    signale tout ne signale rien — c'est la même retenue que les trois sujets
    maximum de l'Agent 1.
"""

import logging
from dataclasses import dataclass
from typing import Any, Optional

from reviews.storage.db import Database

logger = logging.getLogger(__name__)

#: Constats rendus au maximum par contrôle et par passage.
MAX_PAR_CONTROLE = 50


@dataclass
class Constat:
    """Un problème de qualité repéré par une règle déterministe."""

    kind: str
    scope: str
    subject_key: str
    reason: str
    subsidiary_id: Optional[int] = None
    source_code: Optional[str] = None
    severity: str = "warning"
    confidence: Optional[float] = None
    evidence: Optional[list[dict[str, Any]]] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "scope": self.scope,
            "subject_key": self.subject_key,
            "reason": self.reason,
            "subsidiary_id": self.subsidiary_id,
            "source_code": self.source_code,
            "severity": self.severity,
            "confidence": self.confidence,
            "evidence": self.evidence or [],
        }


class ControlesQualite:
    """Les contrôles déterministes sur le corpus d'avis."""

    def __init__(
        self,
        db: Database,
        *,
        min_text_chars: int = 30,
        volume_spike_factor: float = 10.0,
        volume_min_baseline: int = 20,
    ):
        self.db = db
        self.min_text_chars = min_text_chars
        self.volume_spike_factor = volume_spike_factor
        self.volume_min_baseline = volume_min_baseline

    def analyser(self) -> list[Constat]:
        """Tous les constats. Chaque contrôle est isolé des autres."""
        constats: list[Constat] = []
        for nom, controle in (
            ("doublons sémantiques", self._doublons_semantiques),
            ("textes insuffisants", self._textes_insuffisants),
            ("dates aberrantes", self._dates_aberrantes),
            ("volume anormal", self._volume_anormal),
        ):
            try:
                constats.extend(controle())
            except Exception:  # noqa: BLE001
                logger.warning("Contrôle « %s » illisible.", nom, exc_info=True)
        return constats

    # ------------------------------------------------------------- Doublons

    def _doublons_semantiques(self) -> list[Constat]:
        """Même texte, même filiale, identifiants différents.

        Le checksum unique attrape déjà l'identique strict. Ce qui passe au
        travers : le même avis republié sous deux sous-cibles (deux fiches
        Google du même magasin), ou repris par deux médias. On normalise donc
        sur le texte seul — minuscules, espaces réduits — plutôt que sur le
        checksum, qui inclut la source et la sous-cible.

        On garde la ligne la plus ancienne comme référence et on ne signale que
        les SUIVANTES : signaler les deux ferait douter de l'originale.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                WITH normalises AS (
                    SELECT review_id, subsidiary_id, source_id,
                           lower(regexp_replace(btrim(text), '\\s+', ' ', 'g')) AS cle,
                           COALESCE(created_at, collected_at) AS quand
                    FROM reviews
                    WHERE text IS NOT NULL AND length(btrim(text)) >= %s
                ),
                groupes AS (
                    SELECT cle, subsidiary_id, COUNT(*) AS n
                    FROM normalises
                    GROUP BY cle, subsidiary_id
                    HAVING COUNT(*) > 1
                ),
                classe AS (
                    SELECT n.review_id, n.subsidiary_id, g.n,
                           left(n.cle, 120) AS extrait,
                           row_number() OVER (
                               PARTITION BY n.cle, n.subsidiary_id
                               ORDER BY n.quand, n.review_id
                           ) AS rang
                    FROM normalises n
                    JOIN groupes g
                      ON g.cle = n.cle
                     AND g.subsidiary_id IS NOT DISTINCT FROM n.subsidiary_id
                )
                SELECT review_id, subsidiary_id, n, extrait
                FROM classe WHERE rang > 1
                ORDER BY n DESC
                LIMIT %s
                """,
                (self.min_text_chars, MAX_PAR_CONTROLE),
            )
            lignes = cur.fetchall()

        return [
            Constat(
                kind="doublon_semantique",
                scope="review",
                subject_key=r["review_id"],
                subsidiary_id=r["subsidiary_id"],
                reason=(
                    f"Texte identique à {r['n'] - 1} autre(s) avis de la même "
                    f"filiale : « {r['extrait']}… »"
                ),
                # Déterministe : c'est une égalité de chaînes normalisées.
                confidence=1.0,
                evidence=[
                    {
                        "type": "regle",
                        "source": "base de données",
                        "fait": "texte normalisé identique",
                        "occurrences": r["n"],
                    }
                ],
            )
            for r in lignes
        ]

    # ------------------------------------------------------- Texte utilisable

    def _textes_insuffisants(self) -> list[Constat]:
        """Avis dont le texte ne porte pas d'information analysable.

        AGRÉGÉ PAR FILIALE ET PAR SOURCE, jamais un constat par avis. Quatre
        avis Google Maps sur cinq n'ont pas de commentaire : un constat par
        ligne produirait des dizaines de milliers d'entrées pour un fait déjà
        connu et documenté. Ce qui mérite un signalement, c'est la PART.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT r.subsidiary_id, s.code AS source,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (
                           WHERE r.text IS NULL
                              OR length(btrim(r.text)) < %s
                       ) AS courts
                FROM reviews r
                JOIN dim_source s ON s.source_id = r.source_id
                WHERE s.kind = 'customer_review' AND r.subsidiary_id IS NOT NULL
                GROUP BY r.subsidiary_id, s.code
                HAVING COUNT(*) >= 20
                   AND COUNT(*) FILTER (
                           WHERE r.text IS NULL
                              OR length(btrim(r.text)) < %s
                       )::float / COUNT(*) > 0.9
                ORDER BY COUNT(*) DESC
                LIMIT %s
                """,
                (self.min_text_chars, self.min_text_chars, MAX_PAR_CONTROLE),
            )
            lignes = cur.fetchall()

        return [
            Constat(
                kind="texte_insuffisant",
                scope="subsidiary",
                subject_key=str(r["subsidiary_id"]),
                subsidiary_id=r["subsidiary_id"],
                source_code=r["source"],
                severity="info",
                reason=(
                    f"{r['courts']}/{r['total']} avis {r['source']} font moins de "
                    f"{self.min_text_chars} caractères : le volume est là, la "
                    "matière d'analyse non."
                ),
                confidence=1.0,
                evidence=[
                    {
                        "type": "regle",
                        "source": r["source"],
                        "fait": "part d'avis sans texte exploitable",
                        "valeur": round(r["courts"] / r["total"], 3),
                    }
                ],
            )
            for r in lignes
        ]

    # ------------------------------------------------------ Cohérence du temps

    def _dates_aberrantes(self) -> list[Constat]:
        """Avis datés avant l'existence des plateformes, ou dans le futur.

        `DATA_FLOOR` les écarte déjà des agrégats bornés. Le constat sert à
        savoir COMBIEN de lignes sont concernées et par quelle source : une
        date mal parsée est un défaut de collecteur, pas une fatalité, et sans
        décompte personne ne saura jamais qu'il faut le corriger.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT s.code AS source, COUNT(*) AS n,
                       MIN(r.created_at) AS plus_ancienne,
                       MAX(r.created_at) AS plus_recente
                FROM reviews r
                JOIN dim_source s ON s.source_id = r.source_id
                WHERE r.created_at IS NOT NULL
                  AND (r.created_at < DATE '2005-01-01'
                       OR r.created_at > now() + INTERVAL '2 days')
                GROUP BY s.code
                ORDER BY COUNT(*) DESC
                LIMIT %s
                """,
                (MAX_PAR_CONTROLE,),
            )
            lignes = cur.fetchall()

        return [
            Constat(
                kind="incoherence_temporelle",
                scope="source",
                subject_key=r["source"],
                source_code=r["source"],
                reason=(
                    f"{r['n']} avis {r['source']} portent une date impossible "
                    f"(de {r['plus_ancienne']:%Y-%m-%d} à {r['plus_recente']:%Y-%m-%d}) : "
                    "date mal interprétée à la collecte."
                ),
                confidence=1.0,
                evidence=[
                    {
                        "type": "regle",
                        "source": r["source"],
                        "fait": "date hors du plancher de données (2005)",
                        "valeur": r["n"],
                    }
                ],
            )
            for r in lignes
        ]

    # ------------------------------------------------------------ Volume

    def _volume_anormal(self) -> list[Constat]:
        """Un jour de collecte hors de proportion avec les précédents.

        COMPARÉ À LA MÉDIANE et non à la moyenne : la collecte est en dents de
        scie par construction — un passage Google Maps insère des centaines
        d'avis d'un coup, le suivant zéro. Une moyenne serait tirée par ces
        pics et ne déclencherait jamais ; la médiane décrit la journée typique.

        Le plancher `volume_min_baseline` évite de crier au loup quand on passe
        de 1 à 12 avis.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                WITH par_jour AS (
                    SELECT s.code AS source,
                           date_trunc('day', r.collected_at)::date AS jour,
                           COUNT(*) AS n
                    FROM reviews r
                    JOIN dim_source s ON s.source_id = r.source_id
                    WHERE r.collected_at >= now() - INTERVAL '30 days'
                    GROUP BY 1, 2
                ),
                reference AS (
                    SELECT source,
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY n) AS mediane
                    FROM par_jour
                    WHERE jour < CURRENT_DATE
                    GROUP BY source
                )
                SELECT p.source, p.jour, p.n, r.mediane
                FROM par_jour p
                JOIN reference r ON r.source = p.source
                WHERE r.mediane >= %s
                  AND p.n > r.mediane * %s
                ORDER BY p.jour DESC
                LIMIT %s
                """,
                (self.volume_min_baseline, self.volume_spike_factor, MAX_PAR_CONTROLE),
            )
            lignes = cur.fetchall()

        return [
            Constat(
                kind="volume_anormal",
                scope="source",
                subject_key=f"{r['source']}:{r['jour']}",
                source_code=r["source"],
                reason=(
                    f"{r['n']} avis {r['source']} collectés le "
                    f"{r['jour']:%d/%m/%Y}, contre une médiane de "
                    f"{float(r['mediane']):.0f} par jour."
                ),
                confidence=1.0,
                evidence=[
                    {
                        "type": "regle",
                        "source": r["source"],
                        "fait": "volume journalier hors norme",
                        "valeur": r["n"],
                        "mediane": float(r["mediane"]),
                        "date": r["jour"].isoformat(),
                    }
                ],
            )
            for r in lignes
        ]


class ControlesFraicheur:
    """Module 7 — sources qui ont cessé de produire.

    À LA MAILLE DE LA SOURCE, là où `score.fraicheur_score` travaille à la
    maille de la filiale. Les deux sont nécessaires et ne disent pas la même
    chose : une source globalement muette est une panne à corriger une fois ;
    une filiale en retard sur une source active est un problème de couverture
    qui lui est propre.
    """

    def __init__(self, db: Database, *, stale_factor: float = 3.0):
        self.db = db
        self.stale_factor = stale_factor

    def analyser(self, cadences_minutes: dict[str, int]) -> list[Constat]:
        """Sources dont le dernier avis remonte à plus de N fois leur cadence."""
        try:
            with self.db.cursor(dict_rows=True) as cur:
                cur.execute(
                    """
                    SELECT s.code AS source, s.kind,
                           COUNT(r.review_id) AS avis,
                           MAX(r.collected_at) AS derniere,
                           EXTRACT(EPOCH FROM (now() - MAX(r.collected_at))) / 3600
                               AS heures
                    FROM dim_source s
                    LEFT JOIN reviews r ON r.source_id = s.source_id
                    GROUP BY s.code, s.kind
                    """
                )
                lignes = cur.fetchall()
        except Exception:  # noqa: BLE001
            logger.warning("Contrôle de fraîcheur illisible.", exc_info=True)
            return []

        constats: list[Constat] = []
        for r in lignes:
            # Une source sans le moindre avis n'est pas « périmée » : elle n'a
            # jamais rien produit. C'est un sujet de couverture, traité par le
            # diagnostic, et le confondre ici enverrait une alerte de panne pour
            # une source simplement sans cible — la faute Trustpilot exactement.
            if not r["avis"] or r["derniere"] is None:
                continue

            cadence_h = max(1.0, cadences_minutes.get(r["source"], 360) / 60.0)
            limite = cadence_h * self.stale_factor
            heures = float(r["heures"] or 0.0)
            if heures <= limite:
                continue

            constats.append(
                Constat(
                    kind="source_muette",
                    scope="source",
                    subject_key=r["source"],
                    source_code=r["source"],
                    severity="error" if heures > limite * 2 else "warning",
                    reason=(
                        f"Aucun avis {r['source']} depuis {heures:.0f} h, alors que "
                        f"la cadence déclarée est de {cadence_h:.0f} h "
                        f"(seuil : {limite:.0f} h)."
                    ),
                    confidence=1.0,
                    evidence=[
                        {
                            "type": "mesure",
                            "source": r["source"],
                            "fait": "heures depuis le dernier avis",
                            "valeur": round(heures, 1),
                            "cadence_attendue_h": cadence_h,
                            "date": r["derniere"].isoformat(),
                        }
                    ],
                )
            )
        return constats


def completude_par_filiale(db: Database) -> dict[int, dict[str, Any]]:
    """Statistiques de complétude, par filiale — alimente `completude_score`.

    Une seule requête pour tout le périmètre, même raison que le moniteur de
    couverture : 135 requêtes à chaque passage feraient de l'agent le problème
    qu'il surveille.
    """
    try:
        with db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT r.subsidiary_id,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (
                           WHERE r.text IS NOT NULL AND length(btrim(r.text)) >= 30
                       ) AS avec_texte,
                       COUNT(*) FILTER (WHERE r.created_at IS NOT NULL) AS avec_date
                FROM reviews r
                JOIN dim_source s ON s.source_id = r.source_id
                WHERE s.kind = 'customer_review' AND r.subsidiary_id IS NOT NULL
                GROUP BY r.subsidiary_id
                """
            )
            return {r["subsidiary_id"]: dict(r) for r in cur.fetchall()}
    except Exception:  # noqa: BLE001
        logger.warning("Statistiques de complétude illisibles.", exc_info=True)
        return {}
