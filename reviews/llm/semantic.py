"""
Analyse sémantique des avis : sentiment jugé sur la phrase, et aspects métier.

CE QUE CETTE COUCHE APPORTE PAR RAPPORT AU LEXIQUE
    Le lexique compte des mots. Il ne peut donc extraire que des mots, et c'est
    la cause directe de l'onglet « Motifs » actuel, dont les premiers motifs
    d'insatisfaction sont « can't », « bad », « doesn't », « its », « without ».

    Ici le modèle lit la phrase et la range dans une taxonomie FERMÉE
    (reviews/domain/aspects.py). « Le réseau tombe tous les soirs depuis la mise
    à jour » ne contient aucun terme de lexique et produit pourtant
    `coupures_pannes`. Le résultat est nommé, comptable, et comparable d'une
    filiale à l'autre.

TROIS PROPRIÉTÉS TENUES ICI, ET POURQUOI
    - INCRÉMENTAL. Seules les lignes dont `aspect_version` est nul ou périmé
      sont traitées. Sans cela, chaque exécution repaierait le corpus entier —
      inacceptable sur un quota gratuit.
    - PAR LOTS. Un appel porte `batch_size` avis. C'est le levier de coût
      principal : à 20 par appel, 3 300 avis clients tiennent en 165 appels,
      soit une seule journée de quota gratuit.
    - JAMAIS BLOQUANT. Un échec de lot est journalisé et laisse les lignes en
      attente pour la prochaine exécution. La collecte, le lexique et le
      dashboard n'en dépendent pas.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from reviews.domain.aspects import (
    ASPECT_VERSION,
    MAX_ASPECTS_PER_POLARITY,
    normalize,
    taxonomy_for_prompt,
)
from reviews.llm.client import LLMClient, LLMError, LLMUnavailable
from reviews.storage.db import Database

logger = logging.getLogger(__name__)

_VALID_SENTIMENTS = {"positive", "neutral", "negative"}

#: Jetons de sortie réservés PAR AVIS du lot.
#:
#: MESURÉ, PAS ESTIMÉ. Un premier backfill de 125 lots a perdu 5 lots (4 %) sur
#: « réponse illisible » : la sortie faisait 1 012 jetons en moyenne contre un
#: plafond global de 1 600, et les lots les plus riches en aspects le
#: dépassaient. Une réponse tronquée n'est pas un JSON valide — vingt avis
#: perdus et un appel de quota gaspillé à chaque fois.
#:
#: Le plafond doit donc être proportionnel au lot, pas constant : sinon monter
#: LLM_BATCH_SIZE réintroduit exactement la même panne, silencieusement.
#: 110 jetons couvrent le cas le plus verbeux (trois aspects de chaque côté),
#: contre ~50 en moyenne — la marge est volontairement large, les jetons non
#: produits n'étant pas facturés.
_TOKENS_PER_REVIEW = 110

#: Marge fixe pour l'enveloppe JSON (`{"resultats": [...]}`).
_TOKENS_OVERHEAD = 120


_SYSTEM = """Tu es un analyste de la satisfaction client pour des opérateurs \
télécoms africains. Tu lis des avis d'applications mobiles, d'agences et de \
boutiques, rédigés en français, en anglais ou en arabe, souvent mal \
orthographiés.

Pour chaque avis, tu produis trois informations :

1. sentiment : "negative", "neutral" ou "positive". Juge l'INTENTION de l'auteur,
   pas la présence de mots négatifs. « Je n'ai jamais eu le moindre problème »
   est POSITIF. Une question neutre ou un simple « ok » est NEUTRAL.
2. confiance : entre 0 et 1. Descends sous 0,5 si l'avis est trop court,
   ambigu, ou dans une langue que tu déchiffres mal.
3. les ASPECTS concernés, séparés selon qu'ils sont reprochés (aspects_negatifs)
   ou salués (aspects_positifs). Un même avis peut critiquer la facturation ET
   saluer la couverture réseau.

RÈGLES SUR LES ASPECTS — elles priment sur ton intuition :
- N'utilise QUE les identifiants de la liste fournie, à l'identique. N'invente
  jamais un aspect ; s'il n'y a rien de pertinent, utilise "autre".
- Trois aspects au maximum par polarité. Ne coche que ce qui est réellement
  évoqué : un aspect coché « au cas où » fausse les classements.
- N'infère pas un aspect du sentiment. Un avis « très mauvais service » sans
  précision donne "autre", pas "service_client".

Tu réponds UNIQUEMENT par un objet JSON, sans texte autour et sans balises de \
code."""


@dataclass
class AspectResult:
    """Verdict du modèle pour un avis."""

    review_id: str
    sentiment: Optional[str] = None
    confidence: Optional[float] = None
    neg_aspects: list[str] = field(default_factory=list)
    pos_aspects: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    """Bilan d'une exécution, destiné aux journaux et à la ligne de commande."""

    candidats: int = 0
    analyses: int = 0
    lots: int = 0
    lots_en_echec: int = 0
    arret: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "candidats": self.candidats,
            "analyses": self.analyses,
            "lots": self.lots,
            "lots_en_echec": self.lots_en_echec,
            "arret": self.arret,
        }


class SemanticAnalyzer:
    """Applique la taxonomie d'aspects aux avis pas encore analysés."""

    def __init__(self, db: Database, client: LLMClient):
        self.db = db
        self.client = client

    # --------------------------------------------------------------- Lecture

    def pending_count(self, source_kind: Optional[str] = "customer_review") -> int:
        """Avis restant à analyser sous la version courante de la taxonomie."""
        where, params = self._pending_where(source_kind)
        with self.db.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM reviews r {where}", params)
            return int(cur.fetchone()[0])

    def coverage(self, source_kind: Optional[str] = "customer_review") -> dict:
        """Part du corpus déjà passée par l'analyse sémantique.

        Affichée dans le dashboard : tant qu'elle n'est pas à 100 %, un même
        indicateur agrège deux classifieurs, et le lecteur doit le savoir.
        """
        clause, params = ("", [])
        if source_kind:
            clause = "WHERE s.kind = %s"
            params = [source_kind]
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE r.aspect_version = %s) AS analyses
                FROM reviews r
                LEFT JOIN dim_source s ON s.source_id = r.source_id
                {clause}
                """,
                [ASPECT_VERSION] + params,
            )
            row = dict(cur.fetchone())
        total = row["total"] or 0
        row["part"] = round(100.0 * row["analyses"] / total, 1) if total else None
        row["version"] = ASPECT_VERSION
        return row

    def _pending_where(self, source_kind: Optional[str]) -> tuple[str, list]:
        """Lignes à traiter : jamais analysées, ou analysées par une version périmée.

        `text` non vide est exigé : un avis Google Maps sans commentaire — quatre
        sur cinq — n'a rien à analyser, et l'envoyer au modèle brûlerait du quota
        pour obtenir « autre ».
        """
        clauses = [
            "(r.aspect_version IS NULL OR r.aspect_version < %s)",
            "r.text IS NOT NULL",
            "length(btrim(r.text)) >= 3",
        ]
        params: list[Any] = [ASPECT_VERSION]
        joined = ""
        if source_kind:
            joined = "LEFT JOIN dim_source s ON s.source_id = r.source_id"
            clauses.append("s.kind = %s")
            params.append(source_kind)
        return f"{joined} WHERE " + " AND ".join(clauses), params

    def _fetch_pending(self, limit: int, source_kind: Optional[str]) -> list[dict]:
        where, params = self._pending_where(source_kind)
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT r.review_id, r.title, LEFT(r.text, %s) AS text
                FROM reviews r
                {where}
                -- Les plus récents d'abord : si le quota s'épuise en cours de
                -- route, ce sont les avis dont le dashboard parle qui auront
                -- été traités, pas ceux de 2019.
                ORDER BY COALESCE(r.created_at, r.collected_at) DESC
                LIMIT %s
                """,
                [self.client.cfg.max_review_chars] + params + [limit],
            )
            return [dict(r) for r in cur.fetchall()]

    # --------------------------------------------------------------- Exécution

    def run(
        self,
        limit: int = 400,
        source_kind: Optional[str] = "customer_review",
    ) -> RunReport:
        """Analyse jusqu'à `limit` avis en attente.

        Ne lève pas sur un échec de lot : l'objectif est d'avancer autant que le
        quota le permet, puis de rendre la main proprement. Les lignes non
        traitées restent en attente pour la prochaine exécution.
        """
        report = RunReport()
        if not self.client.available:
            report.arret = self.client.unavailable_reason()
            logger.info("Analyse sémantique non lancée : %s", report.arret)
            return report

        pending = self._fetch_pending(limit, source_kind)
        report.candidats = len(pending)
        if not pending:
            return report

        for batch in _chunks(pending, self.client.cfg.batch_size):
            try:
                results = self.analyze_batch(batch)
            except LLMUnavailable as exc:
                # Budget ou quota : inutile d'enchaîner les lots suivants.
                report.arret = str(exc)
                logger.info("Analyse sémantique interrompue : %s", exc)
                break
            except LLMError as exc:
                report.lots_en_echec += 1
                logger.warning("Lot en échec, laissé en attente : %s", exc)
                continue

            self._persist(results)
            report.lots += 1
            report.analyses += len(results)

        logger.info("Analyse sémantique : %s", report.as_dict())
        return report

    def analyze_batch(self, reviews: list[dict]) -> list[AspectResult]:
        """Envoie un lot au modèle et renvoie un verdict par avis.

        Tout avis du lot reçoit un résultat, MÊME si le modèle l'a ignoré : il
        est alors renvoyé sans sentiment ni aspect, ce qui l'estampille quand
        même comme traité. Sans cela, un avis que le modèle refuse
        systématiquement de classer serait resoumis à chaque exécution et
        consommerait du quota indéfiniment.
        """
        payload = self._render_batch(reviews)
        data = self.client.complete_json(
            system=_SYSTEM,
            user=payload,
            # Dimensionné sur le lot réellement envoyé, jamais sur une constante
            # globale : c'est ce qui empêche une réponse tronquée, donc un JSON
            # illisible, donc la perte du lot entier.
            max_tokens=_TOKENS_OVERHEAD + _TOKENS_PER_REVIEW * len(reviews),
        )

        by_index = _index_results(data)
        results: list[AspectResult] = []
        for position, review in enumerate(reviews, start=1):
            raw = by_index.get(position)
            results.append(self._to_result(review["review_id"], raw))
        return results

    def _render_batch(self, reviews: list[dict]) -> str:
        """Compose le message utilisateur d'un lot.

        Les avis sont numérotés et référencés par leur NUMÉRO, pas par leur
        `review_id` : ces identifiants font une trentaine de caractères, et les
        faire écrire au modèle vingt fois par appel coûte des jetons pour rien —
        tout en lui offrant l'occasion de les recopier de travers.
        """
        lignes = []
        for i, review in enumerate(reviews, start=1):
            titre = (review.get("title") or "").strip()
            texte = (review.get("text") or "").strip()
            contenu = f"{titre}. {texte}" if titre else texte
            lignes.append(f"[{i}] {contenu}")

        return (
            "ASPECTS AUTORISÉS (identifiant : définition)\n"
            f"{taxonomy_for_prompt()}\n\n"
            f"AVIS À ANALYSER ({len(reviews)})\n"
            + "\n".join(lignes)
            + "\n\nRéponds avec cet objet JSON exactement, un élément par avis, "
            'dans l\'ordre :\n'
            '{"resultats": [{"i": 1, "sentiment": "negative", "confiance": 0.9, '
            '"aspects_negatifs": ["facturation_prix"], "aspects_positifs": []}]}'
        )

    def _to_result(self, review_id: str, raw: Optional[dict]) -> AspectResult:
        """Valide un verdict brut. Tout ce qui sort de la taxonomie est écarté."""
        if not isinstance(raw, dict):
            return AspectResult(review_id=review_id)

        sentiment = str(raw.get("sentiment") or "").strip().lower()
        if sentiment not in _VALID_SENTIMENTS:
            sentiment = None  # type: ignore[assignment]

        confidence = raw.get("confiance", raw.get("confidence"))
        try:
            confidence = min(1.0, max(0.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = None

        return AspectResult(
            review_id=review_id,
            sentiment=sentiment or None,
            confidence=confidence,
            neg_aspects=_clean_aspects(raw.get("aspects_negatifs")),
            pos_aspects=_clean_aspects(raw.get("aspects_positifs")),
        )

    # -------------------------------------------------------------- Écriture

    def _persist(self, results: list[AspectResult]) -> None:
        """Écrit les verdicts. Une transaction par lot, pas par avis."""
        if not results:
            return
        rows = [
            (
                r.sentiment,
                r.confidence,
                r.neg_aspects,
                r.pos_aspects,
                ASPECT_VERSION,
                r.review_id,
            )
            for r in results
        ]
        with self.db.cursor() as cur:
            cur.executemany(
                """
                UPDATE reviews
                   SET llm_sentiment   = %s,
                       llm_confidence  = %s,
                       neg_aspects     = %s,
                       pos_aspects     = %s,
                       aspect_version  = %s,
                       llm_analyzed_at = now()
                 WHERE review_id = %s
                """,
                rows,
            )


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------


def _chunks(items: list, size: int) -> Iterator[list]:
    for start in range(0, len(items), max(1, size)):
        yield items[start : start + size]


def _clean_aspects(raw: Any) -> list[str]:
    """Filtre une liste d'aspects contre la taxonomie, sans doublon ni surplus."""
    if not isinstance(raw, list):
        return []
    kept: dict[str, None] = {}  # dict : conserve l'ordre, dédoublonne
    for item in raw:
        key = normalize(item if isinstance(item, str) else None)
        if key:
            kept[key] = None
        elif item:
            logger.debug("Aspect hors taxonomie ignoré : %r", item)
    return list(kept)[:MAX_ASPECTS_PER_POLARITY]


def _index_results(data: Any) -> dict[int, dict]:
    """Indexe la réponse du modèle par numéro d'avis.

    Tolère les deux formes rencontrées en pratique : l'objet demandé
    (`{"resultats": [...]}`) et la liste nue, que les petits modèles renvoient
    régulièrement malgré la consigne. Refuser la seconde ferait perdre le lot
    entier pour une différence d'emballage.
    """
    items = data.get("resultats") if isinstance(data, dict) else data
    if isinstance(data, dict) and items is None:
        for value in data.values():
            if isinstance(value, list):
                items = value
                break
    if not isinstance(items, list):
        return {}

    indexed: dict[int, dict] = {}
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        try:
            key = int(item.get("i", position))
        except (TypeError, ValueError):
            key = position
        indexed[key] = item
    return indexed
