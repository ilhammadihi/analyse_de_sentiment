"""
Résumé de période et diagnostic de cause racine.

DEUX QUESTIONS, UNE SEULE MATIÈRE

    RÉSUMÉ      « Que s'est-il passé sur ce périmètre ? » Trois phrases qui
                remplacent la lecture de plusieurs milliers d'avis, en nommant
                les motifs dominants ET le pays où ils se concentrent.

    DIAGNOSTIC  « Pourquoi ? Et que fait-on ? » Une cause probable, ce qui
                l'appuie, ce qui pourrait l'infirmer, puis deux ou trois actions.

CAUSE ET RECOMMANDATION SONT PRODUITES PAR LE MÊME APPEL, DÉLIBÉRÉMENT
    Les séparer doublerait la consommation d'un quota déjà saturé — mesuré :
    200 appels par jour atteints les 4 et 5 août — mais surtout, deux appels
    indépendants peuvent se contredire. Une recommandation « vérifier les
    antennes » affichée sous un diagnostic « problème de facturation » ruinerait
    la confiance dans les deux. Un seul appel, deux champs dans la réponse : le
    dashboard reste libre de les afficher comme deux widgets.

CE QUI EMPÊCHE LE MODÈLE D'INVENTER UNE CAUSE
    Rien dans le prompt. Ce sont les SIGNAUX calculés en SQL
    (`storage/briefing_repository.py`) et les LECTURES calculées ici, en Python.

    Un modèle à qui l'on demande « quelle est la cause ? » trouvera toujours une
    réponse plausible : c'est ce qu'on lui a appris à faire. On ne lui laisse
    donc pas la question. Les concentrations tranchent « incident » contre
    « chronique », « local » contre « systémique », « réel » contre « artefact
    de collecte », et `_lectures()` en tire des contraintes IMPÉRATIVES que le
    prompt lui interdit de contourner. Le modèle rédige ; il ne conclut pas.

    C'est la même règle que `llm/insights.py` — les chiffres arrivent déjà
    calculés — poussée un cran plus loin parce que la question est plus ouverte,
    donc plus dangereuse.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from reviews.domain.aspects import label as aspect_label
from reviews.llm.cache import CachedText, InsightCache, scope_hash
from reviews.llm.client import LLMClient, LLMError, LLMUnavailable
from reviews.storage.briefing_repository import BriefingRepository
from reviews.storage.filters import StatsFilter
from reviews.storage.stats_repository import StatsRepository

logger = logging.getLogger(__name__)

DIGEST = "digest"
DIAGNOSIS = "diagnosis"
BRIEFING_KINDS = (DIGEST, DIAGNOSIS)

#: Version du prompt et du format de contexte. Entre dans l'empreinte de cache :
#: la faire évoluer périme les textes existants plutôt que d'afficher un ancien
#: texte sous un nouveau format.
#:
#:   v2 : les codes techniques d'aspect ne sont plus transmis au modèle. En v1,
#:        `signaux` portait le code brut à côté du libellé traduit, et des
#:        diagnostics sont partis en citant « le motif 'app_bugs' ». Les textes
#:        v1 sont donc invalidés : les réafficher continuerait de montrer un
#:        identifiant de base de données à un lecteur métier.
PROMPT_VERSION = 2

#: Fenêtre de rafraîchissement du cache, en heures.
#:
#: Un résumé « des dernières 24 h » dont l'empreinte ne dépend que de la journée
#: ne serait calculé qu'une fois par jour et vieillirait douze heures. À
#: l'inverse, une empreinte à la minute repaierait un appel à chaque affichage.
#:
#: 6 h EST LA CADENCE DE COLLECTE (`SCHEDULER_INTERVAL_MINUTES=360`) : entre deux
#: passages du planificateur, la matière du résumé n'a pas changé d'un avis.
#: Rafraîchir plus souvent, c'est payer pour recalculer une réponse identique.
REFRESH_HOURS = 6

#: Avis clients exigés sur la fenêtre pour qu'un résumé ou un diagnostic soit
#: produit. Même seuil que la fiabilité des taux ailleurs dans le projet : sous
#: 30 avis, un seul avis déplace une part de plusieurs points, et « la principale
#: plainte » désignerait en réalité deux personnes.
MIN_AVIS = 30

#: Part au-delà de laquelle un groupe est jugé DOMINANT.
SEUIL_DOMINANT = 60.0

#: Part en deçà de laquelle le phénomène est jugé DIFFUS — aucune cause unique.
SEUIL_DIFFUS = 30.0

#: Part d'une seule journée au-delà de laquelle on parle de PIC et non de fond.
SEUIL_PIC_JOURNALIER = 40.0

#: Motifs transmis, et extraits par motif. Au-delà, la queue de distribution ne
#: fait que consommer des jetons.
_TOP_PAIN_POINTS = 8
_VERBATIMS = 5


_SYSTEM_DIGEST = """Tu es analyste de la satisfaction client pour un groupe \
télécom présent dans plusieurs pays d'Afrique. Tu écris pour un décideur qui \
ouvre son tableau de bord et dispose de trente secondes.

On te fournit des chiffres DÉJÀ CALCULÉS : volumétrie de la période, et les \
principaux motifs d'insatisfaction croisés PAYS x MOTIF.

RÈGLES ABSOLUES :
- N'invente AUCUN chiffre et AUCUN pays. N'utilise que ceux fournis.
- Nomme les motifs AVEC leur pays. « Des problèmes de recharge » n'aide
  personne ; « des problèmes de recharge au Ghana » envoie quelqu'un travailler.
- Cite au maximum les quatre premiers motifs. Un résumé qui liste tout ne
  résume rien.
- La liste fournie est un EXTRAIT classé par volume, pas l'inventaire complet.
  N'écris jamais « exclusivement », « uniquement » ni « seul pays touché » : le
  champ "pays_concernes_au_total" dit combien de pays sont réellement
  concernés, et il est presque toujours supérieur au nombre de pays cités.
- Donne le volume sur lequel porte la période, une fois, en début de phrase.
- Pas de recommandation ici, pas de cause supposée : tu décris ce qui est
  observé, rien de plus.
- Pas de titre, pas de puces, pas de superlatif. Deux à trois phrases,
  70 mots maximum, en français.

Tu réponds UNIQUEMENT par un objet JSON de la forme :
{"synthese": "…", "fiabilite": "haute|moyenne|faible"}"""


_SYSTEM_DIAGNOSIS = """Tu es analyste de la satisfaction client pour un groupe \
télécom africain. Tu dois expliquer une insatisfaction et proposer quoi faire.

On te fournit des SIGNAUX déjà calculés : à quel point les plaintes sont \
concentrées sur un motif, un pays, une filiale, une source de collecte, une \
journée — et si le motif dominant existait déjà à la période précédente.

COMMENT LIRE LES SIGNAUX (tu n'as pas à les recalculer) :
- Motif concentré + une seule journée + motif absent avant = INCIDENT ponctuel.
- Motif concentré + étalé + déjà présent avant = PROBLÈME CHRONIQUE, pas une
  panne.
- Motif diffus = AUCUNE cause unique. Dis-le, ne choisis pas un motif au hasard.
- Une source de collecte très majoritaire = le phénomène peut n'être qu'un
  effet de cette plateforme, pas une dégradation du service.

RÈGLES ABSOLUES :
- Le champ "contraintes" contient des lectures DÉJÀ ÉTABLIES à partir des
  volumes réels. Tu DOIS les respecter et les refléter dans ta réponse. Elles
  priment sur ton intuition.
- N'invente AUCUN chiffre. N'utilise que ceux fournis.
- Ne descends JAMAIS sous le pays : tu ne disposes d'aucune donnée de ville, de
  quartier ou d'antenne. Ne nomme pas de zone géographique qui ne figure pas
  dans les données fournies.
- Distingue ce qui est CONSTATÉ de ce qui est SUPPOSÉ. Emploie le conditionnel
  pour une hypothèse.
- Les recommandations doivent viser le motif et l'entité dominants, être
  vérifiables, et tenir en une ligne chacune. Pas de conseil générique du type
  « améliorer le service client ». Si les signaux sont trop faibles pour agir,
  recommande d'abord d'instrumenter ou de vérifier.
- Deux à trois recommandations, jamais plus.

Tu réponds UNIQUEMENT par un objet JSON de la forme :
{"cause_probable": "…", "elements_a_verifier": "…", \
"recommandations": ["…", "…"], "fiabilite": "haute|moyenne|faible"}"""


class BriefingService:
    """Produit le résumé de période et le diagnostic de cause racine."""

    def __init__(
        self,
        db,
        briefing_repo: BriefingRepository,
        stats_repo: StatsRepository,
        client: LLMClient,
    ):
        self.db = db
        self.briefing = briefing_repo
        self.stats = stats_repo
        self.client = client
        self.cache = InsightCache(db)

    # ------------------------------------------------------------------ Public

    def digest(self, f: StatsFilter, use_cache: bool = True) -> dict:
        """Résumé de la période : ce qui remonte, et d'où.

        Ne lève jamais : une indisponibilité (pas de clé, quota épuisé) est une
        réponse légitime que le dashboard doit savoir afficher.
        """
        context = self._digest_context(f)
        if context["volumes"]["avis"] < MIN_AVIS:
            return _refus(
                f"Trop peu d'avis sur cette période "
                f"({context['volumes']['avis']}) pour en tirer un résumé.",
                payload=context,
            )
        return self._render(DIGEST, f, context, _SYSTEM_DIGEST, use_cache)

    def diagnose(self, f: StatsFilter, use_cache: bool = True) -> dict:
        """Cause probable, éléments à vérifier, et recommandations."""
        context = self._diagnosis_context(f)
        if context["volumes"]["avis"] < MIN_AVIS:
            return _refus(
                f"Trop peu d'avis sur cette période "
                f"({context['volumes']['avis']}) pour établir un diagnostic.",
                payload=context,
            )
        if not context["signaux"]["aspect"]["principal"]:
            return _refus(
                "Aucun motif d'insatisfaction analysé sur cette période : "
                "l'analyse sémantique n'est pas encore passée sur ce périmètre.",
                payload=context,
            )
        return self._render(DIAGNOSIS, f, context, _SYSTEM_DIAGNOSIS, use_cache)

    # ------------------------------------------------------------- Contexte

    def _digest_context(self, f: StatsFilter) -> dict:
        # On lit PLUS LARGE que ce qu'on transmet, uniquement pour savoir ce que
        # la troncature cache.
        #
        # RÉGRESSION OBSERVÉE : sur sept jours, les huit premiers motifs étaient
        # tous sud-africains alors que deux pays étaient concernés. Le modèle a
        # écrit « l'insatisfaction se concentre EXCLUSIVEMENT sur l'Afrique du
        # Sud » — une conclusion fausse, tirée honnêtement d'une liste qu'on lui
        # avait présentée comme complète. Une liste tronquée doit donc annoncer
        # qu'elle l'est, sinon elle ment par omission.
        larges = self.briefing.pain_points(f, limit=_TOP_PAIN_POINTS * 4)
        points = larges[:_TOP_PAIN_POINTS]
        pays_concernes = len({p["country"] for p in larges})

        return {
            "question": DIGEST,
            "periode": f.describe(),
            "volumes": self.briefing.volumes(f),
            # `aspect_label` traduit l'identifiant technique en libellé lisible :
            # le modèle doit écrire « problèmes de recharge », pas
            # « recharge_paiement ».
            "motifs_par_pays": [
                {
                    "pays": p["country"],
                    "motif": aspect_label(p["aspect"]),
                    "avis": p["avis"],
                    "nb_filiales": p["nb_filiales"],
                }
                for p in points
            ],
            "pays_concernes_au_total": pays_concernes,
            "liste_tronquee": len(larges) > len(points),
            "couverture_semantique": self.stats.semantic_coverage(f),
        }

    def _diagnosis_context(self, f: StatsFilter) -> dict:
        volumes = self.briefing.volumes(f)
        signaux = self.briefing.signals(f)
        dominant = signaux["aspect"]["principal"]

        # LE CODE TECHNIQUE NE DOIT PAS ATTEINDRE LE MODÈLE.
        #
        # `motif_dominant` était déjà traduit, mais `signaux` transportait le
        # code brut à côté — et c'est celui-là que le modèle reprenait :
        # « Le motif 'app_bugs' constitue un problème chronique » est parti
        # tel quel dans un briefing. Un identifiant de base de données n'a rien
        # à faire sous les yeux d'un responsable métier, et le lui montrer
        # laisse penser que le reste est tout aussi peu relu.
        #
        # Le code RESTE utilisé pour `verbatims_for_aspect`, qui interroge la
        # base : c'est la copie transmise au modèle qui est traduite.
        signaux_lisibles = dict(signaux)
        if dominant:
            signaux_lisibles["aspect"] = {
                **signaux["aspect"],
                "principal": aspect_label(dominant),
            }

        context: dict[str, Any] = {
            "question": DIAGNOSIS,
            "periode": f.describe(),
            "volumes": volumes,
            "signaux": signaux_lisibles,
            "motif_dominant": aspect_label(dominant) if dominant else None,
            "couverture_semantique": self.stats.semantic_coverage(f),
        }
        if dominant:
            context["extraits_du_motif_dominant"] = (
                self.briefing.verbatims_for_aspect(f, dominant, limit=_VERBATIMS)
            )
        context["contraintes"] = _lectures(signaux, volumes)
        return context

    # -------------------------------------------------------------- Rendu

    def _render(
        self,
        kind: str,
        f: StatsFilter,
        context: dict,
        system: str,
        use_cache: bool,
    ) -> dict:
        digest_hash = scope_hash(
            {
                "kind": kind,
                "scope": f.describe(),
                "prompt": PROMPT_VERSION,
                "bucket": _time_bucket(),
            }
        )

        if use_cache:
            cached = self.cache.read(kind, digest_hash)
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
                system=system,
                user=_render_context(context),
                # Le diagnostic rend quatre champs dont une liste : il lui faut
                # plus de place qu'à un résumé de trois phrases. Un plafond trop
                # court tronque la réponse, donc casse le JSON, donc perd
                # l'appel — la panne déjà rencontrée sur l'analyse sémantique.
                max_tokens=650 if kind == DIAGNOSIS else 350,
            )
        except LLMUnavailable as exc:
            return _refus(str(exc), payload=context)
        except LLMError as exc:
            logger.warning("%s en échec : %s", kind, exc)
            return _refus(f"Le service d'IA n'a pas répondu : {exc}", payload=context)

        entry = self._to_entry(kind, answer, context)
        if entry is None:
            return _refus(
                "Le modèle n'a pas produit de réponse exploitable.", payload=context
            )

        self.cache.write(kind, digest_hash, f.describe(), entry)
        return entry.as_dict()

    def _to_entry(
        self, kind: str, answer: Any, context: dict
    ) -> Optional[CachedText]:
        """Valide la réponse du modèle et la met en forme."""
        if kind == DIGEST:
            text = _first_text(answer, ("synthese", "synthèse", "summary", "text"))
            structure = None
        else:
            text = _first_text(answer, ("cause_probable", "cause", "synthese"))
            structure = _diagnosis_fields(answer)
        if not text:
            return None

        payload = dict(context)
        if structure:
            # La réponse structurée est conservée à côté du contexte : le
            # dashboard peut afficher cause et recommandations comme deux
            # widgets distincts sans re-découper une phrase.
            payload["_reponse"] = structure

        fiabilite = str(
            (answer or {}).get("fiabilite") or ""
        ).strip().lower() if isinstance(answer, dict) else ""

        return CachedText(
            text=text,
            kind=kind,
            cached=False,
            reliability=fiabilite if fiabilite in {"haute", "moyenne", "faible"} else None,
            payload=payload,
            model=self.client.cfg.effective_synthesis_model(),
        )


# ---------------------------------------------------------------------------
# Lectures imposées au modèle
# ---------------------------------------------------------------------------


def _lectures(signaux: dict, volumes: dict) -> list[str]:
    """Conclusions que les volumes IMPOSENT, calculées ici et non par le modèle.

    C'est le cœur du garde-fou. Chaque phrase produite ici est déterministe et
    vérifiable ; le prompt oblige le modèle à les respecter. Sans elles, il
    conclurait à une panne devant n'importe quelle concentration — c'est la
    réponse la plus « utile » en apparence, et la plus fausse.
    """
    lectures: list[str] = []

    aspect = signaux.get("aspect") or {}
    source = signaux.get("source") or {}
    temps = signaux.get("temporelle") or {}
    geo = signaux.get("geographique") or {}
    anterior = signaux.get("anteriorite") or {}

    part_aspect = aspect.get("part")
    if part_aspect is not None and part_aspect < SEUIL_DIFFUS:
        lectures.append(
            f"Le mécontentement est DIFFUS : le premier motif ne pèse que "
            f"{part_aspect} % des plaintes, réparties sur "
            f"{aspect.get('groupes')} motifs. Tu ne dois PAS désigner une cause "
            f"unique ; dis explicitement qu'aucun motif ne domine."
        )
    elif part_aspect is not None and part_aspect >= SEUIL_DOMINANT:
        lectures.append(
            f"Un motif DOMINE nettement ({part_aspect} % des plaintes) : "
            f"le diagnostic doit porter sur lui."
        )

    part_source = source.get("part")
    if part_source is not None and part_source >= SEUIL_DOMINANT:
        lectures.append(
            f"ATTENTION — {part_source} % des avis négatifs proviennent d'une "
            f"seule source de collecte ({source.get('principal')}). Tu DOIS "
            f"signaler que le phénomène peut refléter le biais de cette "
            f"plateforme plutôt qu'une dégradation réelle du service, et régler "
            f"la fiabilité sur \"moyenne\" au mieux."
        )

    part_jour = temps.get("part")
    nouveau = anterior.get("nouveau")
    if part_jour is not None and part_jour >= SEUIL_PIC_JOURNALIER and nouveau:
        lectures.append(
            f"Faisceau d'INCIDENT PONCTUEL : {part_jour} % des plaintes sont "
            f"tombées le {temps.get('principal')}, et ce motif était absent de "
            f"la période précédente. Une panne ou une opération technique est "
            f"une hypothèse recevable."
        )
    elif nouveau is False:
        lectures.append(
            f"Ce motif était DÉJÀ présent à la période précédente "
            f"({anterior.get('avis_periode_precedente')} avis). Il s'agit d'un "
            f"problème chronique, PAS d'un incident nouveau : ne parle ni de "
            f"panne soudaine ni de dégradation récente."
        )

    part_geo = geo.get("part")
    if part_geo is not None and geo.get("groupes", 0) > 1:
        if part_geo >= SEUIL_DOMINANT:
            lectures.append(
                f"Phénomène LOCALISÉ : {part_geo} % des plaintes viennent d'un "
                f"seul pays ({geo.get('principal')}). Une action ciblée y est "
                f"pertinente."
            )
        elif part_geo < SEUIL_DIFFUS:
            lectures.append(
                f"Phénomène RÉPARTI sur {geo.get('groupes')} pays (le premier ne "
                f"pèse que {part_geo} %). Une cause locale est peu probable ; "
                f"cherche plutôt une explication de groupe."
            )

    couverture = volumes.get("avis") or 0
    if couverture < 100:
        lectures.append(
            f"La période ne compte que {couverture} avis clients : reste prudent "
            f"et ne présente aucune part comme une tendance établie."
        )

    if not lectures:
        lectures.append(
            "Aucun signal ne se détache franchement : présente ton explication "
            "comme une piste, pas comme une conclusion."
        )
    return lectures


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------


def _time_bucket(now: Optional[datetime] = None) -> str:
    """Tranche de temps servant à périmer le cache.

    Rend une chaîne stable pendant `REFRESH_HOURS`, ce qui borne le coût : un
    périmètre donné n'est payé qu'une fois par tranche, quel que soit le nombre
    de lecteurs.
    """
    now = now or datetime.now(timezone.utc)
    return f"{now:%Y-%m-%d}T{(now.hour // REFRESH_HOURS) * REFRESH_HOURS:02d}"


def _render_context(context: dict) -> str:
    """Met le contexte en forme pour le modèle.

    En JSON indenté plutôt qu'en prose : les chiffres restent étiquetés, donc
    recopiables sans ambiguïté. Une mise en récit inviterait le modèle à
    reformuler des valeurs, c'est-à-dire à les altérer.
    """
    consigne = (
        "Résume ce qui remonte sur cette période."
        if context["question"] == DIGEST
        else (
            "Explique la cause probable de cette insatisfaction, puis propose "
            "des actions. Respecte impérativement le champ \"contraintes\"."
        )
    )
    return (
        f"{consigne}\n\nCONTEXTE CHIFFRÉ :\n"
        + json.dumps(context, ensure_ascii=False, indent=1, default=str)
    )


def _first_text(answer: Any, keys: tuple[str, ...]) -> Optional[str]:
    """Récupère un texte, que le modèle ait respecté le format ou non."""
    if isinstance(answer, str):
        return answer.strip() or None
    if isinstance(answer, dict):
        for key in keys:
            value = answer.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _diagnosis_fields(answer: Any) -> Optional[dict]:
    """Extrait les champs structurés d'un diagnostic, en tolérant l'à-peu-près.

    Les petits modèles rendent parfois `recommandations` sous forme de chaîne
    unique au lieu d'une liste. Refuser la réponse pour cette seule raison
    perdrait l'appel entier : on normalise.
    """
    if not isinstance(answer, dict):
        return None
    brut = answer.get("recommandations") or answer.get("recommendations")
    if isinstance(brut, str):
        recos = [brut.strip()] if brut.strip() else []
    elif isinstance(brut, list):
        recos = [str(x).strip() for x in brut if str(x).strip()]
    else:
        recos = []
    return {
        "cause_probable": _first_text(answer, ("cause_probable", "cause")),
        "elements_a_verifier": _first_text(
            answer, ("elements_a_verifier", "éléments_a_vérifier", "verifications")
        ),
        # Trois au maximum, comme l'exige le prompt : un modèle bavard ne doit
        # pas pouvoir imposer une liste de dix actions au dashboard.
        "recommandations": recos[:3],
    }


def _refus(reason: str, payload: Optional[dict] = None) -> dict:
    """Réponse d'indisponibilité, porteuse de sa raison et des chiffres connus."""
    return {
        "available": False,
        "reason": reason,
        "text": None,
        "payload": payload,
    }
