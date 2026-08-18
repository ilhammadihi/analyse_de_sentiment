"""
Synthèses en langage naturel : transformer un écart de KPI en explication lisible.

CE QUE FAIT — ET NE FAIT PAS — CETTE COUCHE
    Elle ne découvre rien. Les chiffres sont calculés par SQL, les motifs
    viennent de la base, les verbatims aussi. Le modèle reçoit tout cela déjà
    mesuré et n'a qu'une tâche : l'écrire en deux ou trois phrases qu'un
    décideur lit sans avoir à interpréter un tableau.

    C'est une contrainte de conception, pas une prudence de façade. Un modèle à
    qui l'on demande de « regarder les données » invente des chiffres
    plausibles ; un modèle à qui l'on fournit « 45,3 % contre 51,6 %
    auparavant » ne peut que les recopier. Le prompt le lui interdit
    explicitement, et `payload` conserve en base ce qui lui a été transmis —
    de sorte qu'une phrase affichée reste rapprochable de son entrée six
    semaines plus tard.

POURQUOI UN CACHE, ET POURQUOI SUR UNE EMPREINTE DE PÉRIMÈTRE
    Le fournisseur est gratuit, donc contingenté. Deux lecteurs qui ouvrent le
    même écart entre deux filiales sur la même période posent la même question :
    elle est payée une fois. L'empreinte porte le périmètre COMPLET (filtres,
    fenêtre, entités, version du prompt) — deux périmètres différents ne
    partagent donc jamais une phrase, ce qui serait la pire des erreurs ici :
    un texte juste, affiché sous des chiffres qui ne sont pas les siens.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from reviews.domain.aspects import label as aspect_label
from reviews.llm.client import LLMClient, LLMError, LLMUnavailable
from reviews.storage.db import Database
from reviews.storage.filters import CUSTOMER, StatsFilter
from reviews.storage.press_repository import PressRepository
from reviews.storage.stats_repository import StatsRepository

logger = logging.getLogger(__name__)

#: Version du prompt et du format de contexte.
#:
#: Entre dans l'empreinte de cache : la faire évoluer périme les synthèses
#: existantes plutôt que d'afficher un ancien texte sous un nouveau format.
#:
#:   v2 : la période antérieure n'est plus transmise sur une COMPARAISON. En v1
#:        elle l'était, et le modèle refusait de comparer deux filiales de 262 et
#:        257 avis au motif que leurs fenêtres précédentes étaient trop maigres —
#:        un contresens causé par une donnée hors sujet. Les synthèses de
#:        comparaison produites en v1 sont donc invalidées, pas réaffichées.
#:   v3 : des articles de presse datés accompagnent désormais chaque entité, et
#:        le prompt autorise une cause EXTERNE à condition de citer l'un d'eux.
#:        Les synthèses v2 ont été écrites sous l'interdiction inverse : les
#:        réafficher sous un écran qui promet une explication externe ferait
#:        passer pour une limite du corpus ce qui n'est qu'un texte périmé.
PROMPT_VERSION = 3

#: Types de question traités.
SPIKE = "spike"
COMPARISON = "comparison"
KINDS = (SPIKE, COMPARISON)

#: Motifs transmis au modèle, par entité. Au-delà, la queue de distribution
#: n'apporte que du bruit et fait payer des jetons.
_TOP_MOTIFS = 6

#: Avis d'exemple transmis. Trois par entité comparée, six sur un pic : assez
#: pour que le modèle nomme un problème concret, assez peu pour qu'il ne se
#: mette pas à raconter des cas particuliers comme s'ils étaient la tendance.
_SAMPLE_SPIKE = 6
_SAMPLE_COMPARISON = 3

#: Caractères par verbatim transmis.
_VERBATIM_CHARS = 260

#: Avis clients exigés sur CHACUNE des deux fenêtres pour qu'une variation soit
#: présentée comme un fait.
#:
#: CE SEUIL N'EST PAS COSMÉTIQUE — il corrige un piège mesuré sur les données
#: réelles. Vodacom South Africa affiche « −75,2 points » de part de négatifs
#: sur 90 jours : la fenêtre précédente contient UN avis, négatif, donc 100 %,
#: contre 24,8 % sur 254 avis aujourd'hui. La soustraction est exacte et
#: l'affirmation qu'elle suggère est fausse.
#:
#: On ne délègue pas ce jugement au modèle. Un seuil évalué en Python est
#: déterministe ; une consigne de prudence dans un prompt est une espérance.
#: Le chiffre reste transmis — le masquer serait une autre forme de mensonge —
#: mais il part accompagné d'un avertissement que le prompt oblige à reprendre.
_MIN_VOLUME_FOR_DELTA = 30

#: Jours ajoutés AVANT la fenêtre analysée pour la recherche de presse.
#:
#: Une cause précède son effet, et le délai n'est pas nul : l'abonné qui subit
#: une hausse tarifaire annoncée le 3 écrit son avis quand il reçoit sa facture.
#: Chercher l'article dans la seule fenêtre d'analyse écarterait mécaniquement
#: celui qui l'explique. Quatorze jours couvrent un cycle de facturation sans
#: remonter si loin que n'importe quel événement du trimestre devienne candidat.
_AMORCE_PRESSE_JOURS = 14


_SYSTEM = """Tu es analyste de la satisfaction client pour un groupe télécom \
présent dans plusieurs pays d'Afrique. Tu écris pour un décideur pressé.

On te fournit des chiffres DÉJÀ CALCULÉS et des extraits d'avis réels. Ta seule \
tâche est d'en faire deux à trois phrases en français.

RÈGLES ABSOLUES :
- N'invente AUCUN chiffre. N'utilise que ceux fournis, avec la même valeur et la
  même unité. Si une donnée manque, ne la mentionne pas.
- Distingue ce que tu constates de ce que tu supposes. Si les motifs
  n'expliquent pas l'écart, dis-le franchement plutôt que de broder.

CAUSE EXTÉRIEURE — CE QUE TU AS LE DROIT DE DIRE :
- Chaque entité peut porter un champ "faits_externes" : des articles de presse
  DATÉS, collectés sur son périmètre. Tu peux proposer une cause extérieure
  (hausse tarifaire, panne, décision du régulateur, rachat) À LA SEULE CONDITION
  de t'appuyer sur l'un de ces articles, en reprenant sa date.
- Il t'est INTERDIT d'avancer une cause extérieure qui ne figure pas dans cette
  liste, même si elle te paraît évidente ou probable pour ce pays, cet opérateur
  ou ce secteur. Tes connaissances générales ne sont pas une preuve ici.
- Si "faits_externes" est vide, ou si aucun article ne se rapporte au mouvement
  constaté, tu DOIS l'écrire — « aucun événement extérieur documenté sur la
  période » — et t'en tenir aux motifs internes. C'est une réponse valable, pas
  un échec.
- Le champ "perimetre_presse" indique à quelle maille les articles ont été
  trouvés. S'il ne désigne pas l'entité elle-même mais son pays ou son
  opérateur, dis-le : « un article national du 14 juillet », jamais « un article
  sur cette filiale ».
- Un article contemporain n'est pas une cause démontrée. Écris « pourrait tenir
  à », « coïncide avec » — jamais « à cause de ».
- Si une entité porte un champ "avertissement", tu DOIS en reprendre le contenu
  dans ta réponse et régler "fiabilite" sur "faible". Cet avertissement a été
  calculé sur les volumes réels : il prime sur ta propre lecture des chiffres.
- Pas de recommandation, pas de plan d'action, pas de titre, pas de puces :
  du texte courant, deux à trois phrases, 60 mots maximum.
- N'emploie pas de superlatif ni de formule d'accroche. Le ton est factuel.

Tu réponds UNIQUEMENT par un objet JSON de la forme :
{"synthese": "…", "fiabilite": "haute|moyenne|faible"}"""


@dataclass
class Insight:
    """Une synthèse, telle qu'elle est renvoyée au dashboard."""

    text: str
    kind: str
    cached: bool
    reliability: Optional[str] = None
    payload: Optional[dict] = None
    model: Optional[str] = None
    created_at: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "available": True,
            "text": self.text,
            "kind": self.kind,
            "cached": self.cached,
            "reliability": self.reliability,
            "payload": self.payload,
            "model": self.model,
            "created_at": self.created_at,
        }


class InsightService:
    """Construit le contexte chiffré d'une question, puis le fait rédiger."""

    def __init__(self, db: Database, repo: StatsRepository, client: LLMClient):
        self.db = db
        self.repo = repo
        self.client = client
        # Construit ici plutôt qu'injecté : la presse est un détail interne de
        # la façon dont cette couche fabrique son contexte, pas une dépendance
        # que l'API doive connaître et câbler.
        self.press = PressRepository(db)

    # ------------------------------------------------------------------ Public

    def explain(
        self,
        kind: str,
        f: StatsFilter,
        level: str = "subsidiary",
        entities: Optional[list[str]] = None,
        use_cache: bool = True,
    ) -> dict:
        """Explique un pic ou un écart. Renvoie toujours un dict, jamais une erreur.

        La méthode ne lève pas : une indisponibilité (pas de clé, quota épuisé)
        est une réponse légitime que le dashboard doit savoir afficher, pas une
        panne. Elle se distingue par `available: false` et porte sa raison en
        français.
        """
        if kind not in KINDS:
            return _refus(f"Type de synthèse inconnu : {kind}.")

        entities = [e for e in (entities or []) if str(e).strip()]
        if kind == COMPARISON and len(entities) < 2:
            return _refus("La comparaison demande au moins deux entités.")

        try:
            context = self._build_context(kind, f, level, entities)
        except ValueError as exc:
            return _refus(str(exc))

        if not context.get("entites"):
            return _refus("Aucune donnée sur ce périmètre : rien à expliquer.")

        scope_hash = self._hash(kind, f, level, entities)

        if use_cache:
            cached = self._read_cache(kind, scope_hash)
            if cached:
                return cached.as_dict()

        # Le contexte est construit AVANT de vérifier la disponibilité : même
        # sans modèle, le dashboard reçoit les chiffres et peut les afficher
        # bruts. Une fonctionnalité dégradée reste utilisable ; une page vide,
        # non.
        reason = self.client.unavailable_reason()
        if reason:
            return _refus(reason, payload=context)

        try:
            answer = self.client.complete_json(
                system=_SYSTEM,
                user=_render_context(context),
                max_tokens=400,
            )
        except LLMUnavailable as exc:
            return _refus(str(exc), payload=context)
        except LLMError as exc:
            logger.warning("Synthèse LLM en échec : %s", exc)
            return _refus(f"Le service d'IA n'a pas répondu : {exc}", payload=context)

        text = _extract_text(answer)
        if not text:
            return _refus("Le modèle n'a pas produit de synthèse exploitable.", payload=context)

        reliability = str(answer.get("fiabilite") or "").strip().lower() or None
        insight = Insight(
            text=text,
            kind=kind,
            cached=False,
            reliability=reliability if reliability in {"haute", "moyenne", "faible"} else None,
            payload=context,
            model=self.client.cfg.effective_synthesis_model(),
        )
        self._write_cache(kind, scope_hash, f, insight)
        return insight.as_dict()

    # ------------------------------------------------------------ Contexte

    def _build_context(
        self, kind: str, f: StatsFilter, level: str, entities: list[str]
    ) -> dict:
        """Rassemble les chiffres, les motifs et les extraits transmis au modèle."""
        context: dict[str, Any] = {
            "question": kind,
            "periode": f.describe(),
            "niveau": level,
            "entites": [],
        }

        # Un pic sans entité désignée porte sur tout le périmètre filtré.
        targets: list[Optional[str]] = list(entities) if entities else [None]
        sample = _SAMPLE_SPIKE if kind == SPIKE else _SAMPLE_COMPARISON

        for value in targets:
            scoped = _scope_to(f, level, value) if value is not None else f
            overview = self.repo.overview(scoped)
            current = overview["current"]
            previous = overview["previous"]

            entry: dict[str, Any] = {
                "nom": self._label(level, value) if value is not None else "Périmètre filtré",
                "avis_clients": current["avis_clients"],
                "part_negatifs": _num(current["part_negatifs"]),
                "part_positifs": _num(current["part_positifs"]),
                "note_moyenne": _num(current["note_moyenne"]),
            }

            # La période antérieure n'est transmise QUE pour un pic.
            #
            # Une comparaison porte sur l'écart entre entités À LA MÊME DATE :
            # la fenêtre précédente n'y entre pas. L'envoyer quand même a produit
            # un vrai contresens en test — le modèle a refusé de comparer MTN
            # Ghana (52,3 % de négatifs sur 262 avis) à Vodacom South Africa
            # (23,3 % sur 257), au motif que « les volumes de la période
            # précédente sont trop faibles ». Les deux volumes courants étaient
            # pourtant excellents ; c'est la donnée hors sujet qui l'a égaré.
            if kind == SPIKE and previous:
                entry["periode_precedente"] = {
                    "avis_clients": previous["avis_clients"],
                    "part_negatifs": _num(previous["part_negatifs"]),
                    "part_positifs": _num(previous["part_positifs"]),
                }
                # La variation est calculée ICI, en points, et transmise toute
                # faite. Laisser le modèle soustraire deux pourcentages est le
                # moyen le plus sûr d'obtenir un troisième chiffre inventé.
                entry["variation_negatifs_points"] = _delta(
                    current["part_negatifs"], previous["part_negatifs"]
                )
                entry["variation_positifs_points"] = _delta(
                    current["part_positifs"], previous["part_positifs"]
                )

                # Fiabilité de la variation, tranchée ICI et non par le modèle.
                avant, apres = previous["avis_clients"], current["avis_clients"]
                if min(avant, apres) < _MIN_VOLUME_FOR_DELTA:
                    entry["variation_fiable"] = False
                    entry["avertissement"] = (
                        f"VARIATION NON FIABLE : elle compare {avant} avis sur la "
                        f"période précédente à {apres} sur la période courante. "
                        "En dessous de "
                        f"{_MIN_VOLUME_FOR_DELTA} avis, un ou deux avis suffisent "
                        "à déplacer le taux de dizaines de points. Tu dois dire "
                        "que le volume est trop faible pour conclure, et ne pas "
                        "présenter cette variation comme un fait établi."
                    )
                else:
                    entry["variation_fiable"] = True

            # Sur une COMPARAISON, la fragilité ne se juge pas sur l'historique
            # mais sur le volume COURANT de l'entité : c'est lui qui porte le
            # taux qu'on met en regard d'un autre.
            elif kind == COMPARISON and current["avis_clients"] < _MIN_VOLUME_FOR_DELTA:
                entry["avertissement"] = (
                    f"ÉCART FRAGILE POUR CETTE ENTITÉ : son taux repose sur "
                    f"{current['avis_clients']} avis seulement. Signale que la "
                    "comparaison la concernant est peu solide, sans remettre en "
                    "cause les entités mieux dotées."
                )

            entry["motifs_negatifs"] = self._motifs(scoped, "negative")
            entry["motifs_positifs"] = self._motifs(scoped, "positive")
            entry["avis_negatifs_exemples"] = self._verbatims(scoped, sample)

            preuves = self._faits_externes(scoped, level, value)
            entry["faits_externes"] = preuves["articles"]
            entry["perimetre_presse"] = preuves["perimetre"]
            context["entites"].append(entry)

        # Couverture de l'analyse sémantique : conditionne la confiance à
        # accorder aux motifs. En dessous de la moitié du périmètre, ils sont
        # calculés sur un échantillon et le modèle doit le savoir.
        context["couverture_semantique"] = self.repo.semantic_coverage(f)
        return context

    def _faits_externes(
        self, f: StatsFilter, level: str, value: Optional[str]
    ) -> dict[str, Any]:
        """Articles de presse datés du périmètre, candidats à une cause externe.

        La fenêtre est celle de l'analyse, ÉLARGIE VERS L'AMONT seulement. Vers
        l'aval elle ne l'est pas : un article publié après la fin de la période
        observée ne peut pas expliquer des avis déjà écrits, et l'y inclure
        offrirait au modèle une coïncidence chronologiquement impossible.
        """
        start, end = f.resolved_window()
        fenetre = (start - timedelta(days=_AMORCE_PRESSE_JOURS), end)
        return self.press.evidence(window=fenetre, level=level, value=value)

    def _motifs(self, f: StatsFilter, polarity: str) -> list[dict]:
        """Motifs du périmètre, aspects métier d'abord.

        Repli sur les termes du lexique si l'analyse sémantique n'est pas encore
        passée : mieux vaut « can't, bad, useless » que rien, à condition que la
        clé `source` prévienne le modèle de ce qu'il lit. C'est exactement la
        raison d'être de cette clé.
        """
        for dimension in ("aspects", "terms"):
            data = self.repo.themes(
                f, polarity=polarity, limit=_TOP_MOTIFS, dimension=dimension
            )
            rows = data.get("terms") or []
            if rows:
                return [
                    {
                        "motif": aspect_label(r["term"]) if dimension == "aspects" else r["term"],
                        "avis": r["avis"],
                        "source": "aspect métier" if dimension == "aspects" else "mot du lexique",
                    }
                    for r in rows
                ]
        return []

    def _verbatims(self, f: StatsFilter, limit: int) -> list[str]:
        data = self.repo.verbatims(f, polarity="negative", limit=limit)
        extraits = []
        for review in data.get("reviews", []):
            texte = (review.get("text") or "").strip().replace("\n", " ")
            if texte:
                extraits.append(texte[:_VERBATIM_CHARS])
        return extraits

    def _label(self, level: str, value: str) -> str:
        """Nom lisible d'une entité, pour que la synthèse la désigne correctement."""
        queries = {
            "subsidiary": ("SELECT name FROM dim_subsidiary WHERE subsidiary_id = %s", int),
            "operator": ("SELECT name FROM dim_operator WHERE operator_id = %s", int),
            "country": ("SELECT name FROM dim_country WHERE iso2 = %s", str),
        }
        if level == "region":
            return str(value)
        entry = queries.get(level)
        if entry is None:
            return str(value)
        sql, cast = entry
        try:
            casted = cast(value)
        except (TypeError, ValueError):
            return str(value)
        with self.db.cursor() as cur:
            cur.execute(sql, (casted,))
            row = cur.fetchone()
        return row[0] if row else str(value)

    # --------------------------------------------------------------- Cache

    def _hash(
        self, kind: str, f: StatsFilter, level: str, entities: list[str]
    ) -> str:
        """Empreinte stable d'une question.

        `sort_keys` et le tri des entités rendent l'empreinte indépendante de
        l'ordre dans lequel l'URL énumère les filtres : sans cela, le même écran
        rouvert avec deux filiales sélectionnées dans l'autre sens repaierait un
        appel pour obtenir la phrase déjà en cache.
        """
        material = {
            "kind": kind,
            "level": level,
            "entities": sorted(str(e) for e in entities),
            "scope": f.describe(),
            "prompt": PROMPT_VERSION,
        }
        blob = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _read_cache(self, kind: str, scope_hash: str) -> Optional[Insight]:
        try:
            with self.db.cursor(dict_rows=True) as cur:
                cur.execute(
                    "SELECT text, payload, model, created_at FROM llm_insights "
                    "WHERE kind = %s AND scope_hash = %s",
                    (kind, scope_hash),
                )
                row = cur.fetchone()
        except Exception:  # noqa: BLE001
            logger.warning("Cache des synthèses illisible.", exc_info=True)
            return None
        if not row:
            return None
        payload = row["payload"] or {}
        return Insight(
            text=row["text"],
            kind=kind,
            cached=True,
            reliability=payload.get("_fiabilite"),
            payload=payload,
            model=row["model"],
            created_at=row["created_at"].isoformat() if row["created_at"] else None,
        )

    def _write_cache(
        self, kind: str, scope_hash: str, f: StatsFilter, insight: Insight
    ) -> None:
        payload = dict(insight.payload or {})
        payload["_fiabilite"] = insight.reliability
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO llm_insights
                        (kind, scope_hash, scope, payload, text, model)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    -- Une question déjà répondue par un appel concurrent ne
                    -- doit pas faire échouer celui-ci : les deux textes sont
                    -- équivalents, le premier arrivé fait foi.
                    ON CONFLICT (kind, scope_hash) DO NOTHING
                    """,
                    (
                        kind,
                        scope_hash,
                        json.dumps(f.describe(), default=str),
                        json.dumps(payload, ensure_ascii=False, default=str),
                        insight.text,
                        insight.model,
                    ),
                )
        except Exception:  # noqa: BLE001
            # Un cache non écrit coûte un appel de plus, pas une panne.
            logger.warning("Synthèse non mise en cache.", exc_info=True)


# ---------------------------------------------------------------------------
# Rendu du contexte et utilitaires
# ---------------------------------------------------------------------------


def _render_context(context: dict) -> str:
    """Met le contexte en forme pour le modèle.

    En JSON indenté plutôt qu'en prose : les chiffres restent étiquetés, donc
    recopiables sans ambiguïté. Une mise en récit du contexte inviterait le
    modèle à reformuler des valeurs, c'est-à-dire à les altérer.
    """
    consigne = (
        "Explique l'ÉVOLUTION de cette entité entre la période précédente et la "
        "période courante."
        if context["question"] == SPIKE
        else "Explique l'ÉCART entre ces entités sur la même période."
    )
    consigne += (
        " Cherche d'abord si un article de « faits_externes » coïncide avec ce "
        "mouvement ; à défaut, dis qu'aucun événement extérieur n'est documenté "
        "et explique par les motifs internes."
    )
    return (
        f"{consigne}\n\nCONTEXTE CHIFFRÉ :\n"
        + json.dumps(context, ensure_ascii=False, indent=1, default=str)
    )


def _scope_to(f: StatsFilter, level: str, value: str) -> StatsFilter:
    """Resserre un périmètre sur une entité.

    Les identifiants attendus sont ceux du CONTRAT DE FILTRE, pas ceux des
    classements : un pays se désigne par son code ISO alpha-2, comme dans
    `?country=SN`, et non par son `country_id`. Utiliser ici la clé des
    classements produirait un filtre qui ne correspond à rien et une réponse
    vide sans explication.
    """
    from dataclasses import replace

    if level == "subsidiary":
        return replace(f, subsidiaries=(int(value),))
    if level == "operator":
        return replace(f, operators=(int(value),))
    if level == "country":
        return replace(f, countries=(str(value),))
    if level == "region":
        return replace(f, regions=(str(value),))
    raise ValueError(
        f"Niveau « {level} » non pris en charge par les synthèses. "
        "Valeurs acceptées : subsidiary, operator, country, region."
    )


def _num(value: Any) -> Optional[float]:
    """Convertit un Decimal PostgreSQL en float sérialisable, ou None."""
    return None if value is None else float(value)


def _delta(current: Any, previous: Any) -> Optional[float]:
    if current is None or previous is None:
        return None
    return round(float(current) - float(previous), 1)


def _extract_text(answer: Any) -> Optional[str]:
    """Récupère la synthèse, que le modèle ait respecté le format ou non."""
    if isinstance(answer, str):
        return answer.strip() or None
    if isinstance(answer, dict):
        for key in ("synthese", "synthèse", "summary", "text"):
            value = answer.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _refus(reason: str, payload: Optional[dict] = None) -> dict:
    """Réponse d'indisponibilité, porteuse de sa raison et des chiffres déjà connus."""
    return {"available": False, "reason": reason, "payload": payload}
