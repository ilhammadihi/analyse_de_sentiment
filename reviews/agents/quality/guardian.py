"""
Agent 3 — l'orchestrateur : arbitre, se souvient, alerte, et protège les autres.

L'ORDRE DES ÉTAPES EST LA GARANTIE, pas une commodité d'écriture

    couverture -> mapping -> DIAGNOSTIC -> contrôles -> score -> découverte
                                                            -> mémoire -> envoi

    Le mapping passe AVANT le diagnostic parce que le diagnostic en a besoin :
    une filiale à zéro avis dont un homonyme porte des avis orphelins n'est pas
    un trou de couverture, c'est un rattachement à corriger. Les traiter dans
    l'autre sens ferait chercher une source externe pour une donnée déjà en base.

    La découverte passe APRÈS le diagnostic, et n'est appelée QUE sur les cas
    déclarés enrichissables. C'est le garde-fou de la section 5 : on ne cherche
    ailleurs qu'une fois établi que les sources existantes ont été correctement
    exécutées et sont vides.

CE QU'IL REPREND À L'AGENT 1, SANS LE RÉÉCRIRE
    - `should_report()` — fonction pure, importée telle quelle. La règle « ne
      pas répéter, sauf aggravation » est la même, et en écrire une seconde
      version garantirait qu'elles divergent.
    - `AgentRepository` — le journal `agent_reports` a été conçu au pluriel
      exprès (migration 013). L'Agent 3 y signe sous son propre nom.
    - `TelegramNotifier.send_text()` — déjà utilisé par le briefing quotidien.

LA DISSYMÉTRIE DE LA MÉMOIRE EST INVERSÉE ICI, ET C'EST VOULU
    L'Agent 1 reparle quand le score MONTE (la situation empire). Ici le score
    est une note de qualité : il faut donc reparler quand il DESCEND. Reprendre
    la comparaison telle quelle aurait rendu l'agent muet précisément quand la
    qualité se dégrade — c'est-à-dire quand il sert à quelque chose.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from reviews.agents.quality.claims import VerificateurAffirmations
from reviews.agents.quality.couverture import MoniteurCouverture
from reviews.agents.quality.decouverte import DecouverteSources
from reviews.agents.quality.diagnostic import Cas, diagnostiquer
from reviews.agents.quality.mapping import DetecteurMapping, indices_par_filiale
from reviews.agents.quality.qualite import (
    ControlesFraicheur,
    ControlesQualite,
    completude_par_filiale,
)
from reviews.agents.quality.score import calculer_score
from reviews.alerting.notifiers import TelegramNotifier
from reviews.config import Settings
from reviews.storage.agent_repository import AgentRepository, should_report
from reviews.storage.db import Database
from reviews.storage.quality_repository import QualityRepository

logger = logging.getLogger(__name__)

#: Nom sous lequel l'agent signe dans `agent_reports`.
AGENT = "qualite"

#: Statuts qui méritent une notification. `ACCEPTABLE` en est exclu : une
#: filiale correcte mais perfectible n'appelle aucune action immédiate, et la
#: signaler noierait les deux statuts qui, eux, en appellent une.
STATUTS_A_SIGNALER = frozenset({"UNTRUSTED", "DEGRADED"})


@dataclass
class Passage:
    """Résultat d'un passage, pour la CLI, l'API et les tests."""

    filiales: int = 0
    diagnostics: dict[str, int] = field(default_factory=dict)
    constats: int = 0
    #: Constats instruits par le modèle lors de ce passage. Reste à 0 quand
    #: aucun modèle n'est configuré — l'agent est alors purement déterministe,
    #: ce qui est un mode de fonctionnement normal et non une panne.
    valides: int = 0
    indices_mapping: int = 0
    candidates: int = 0
    #: Nouvelles sources annoncées sur Telegram lors de ce passage — jamais
    #: plus d'une fois la même, voir `_notifier_nouvelles_sources`.
    sources_annoncees: int = 0
    affirmations: int = 0
    non_corrobores: int = 0
    signales: list[dict] = field(default_factory=list)
    tus: list[dict] = field(default_factory=list)
    envoye: bool = False
    raison_silence: Optional[str] = None
    erreurs: list[str] = field(default_factory=list)

    def resume(self) -> str:
        return (
            f"{self.filiales} filiale(s) · {self.constats} constat(s) · "
            f"{self.candidates} candidate(s) · {len(self.signales)} signalé(s) · "
            f"{len(self.tus)} tu(s)"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "filiales": self.filiales,
            "diagnostics": self.diagnostics,
            "constats": self.constats,
            "valides": self.valides,
            "indices_mapping": self.indices_mapping,
            "candidates": self.candidates,
            "sources_annoncees": self.sources_annoncees,
            "affirmations": self.affirmations,
            "non_corrobores": self.non_corrobores,
            "signales": self.signales,
            "tus": self.tus,
            "envoye": self.envoye,
            "raison_silence": self.raison_silence,
            "erreurs": self.erreurs,
        }


class QualityGuardian:
    """Passage complet du gardien de la qualité."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        notifier: Optional[TelegramNotifier] = None,
        decouverte: Optional[DecouverteSources] = None,
        validateur: Optional[Any] = None,
    ):
        self.db = db
        self.settings = settings
        self.cfg = settings.quality
        self.depot = QualityRepository(db)
        self.journal = AgentRepository(db)
        self.notifier = notifier
        self.validateur = validateur
        self.decouverte = decouverte or DecouverteSources(
            probe_enabled=self.cfg.probe_enabled,
            max_candidates=self.cfg.max_candidates,
            timeout=self.cfg.probe_timeout,
        )

    # ------------------------------------------------------------------ Public

    def run(self, dry_run: bool = False) -> Passage:
        """Un passage complet. Ne lève jamais : un agent muet vaut mieux qu'un crash.

        `dry_run` fait tout sauf écrire, appeler le modèle et envoyer. Il sert à
        voir ce que l'agent AURAIT dit — indispensable pour régler les seuils
        sans réveiller le groupe Telegram, exactement comme pour l'Agent 1.
        """
        passage = Passage()

        # --- 1. Couverture ---------------------------------------------------
        try:
            couvertures = MoniteurCouverture(self.db).analyser()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent qualité : couverture illisible")
            passage.raison_silence = f"couverture illisible : {exc}"
            return passage
        passage.filiales = len(couvertures)

        # --- 2. Mapping, AVANT le diagnostic qui s'en sert --------------------
        indices: dict[int, list[dict]] = {}
        try:
            tous = DetecteurMapping(self.db).analyser()
            indices = indices_par_filiale(tous)
            passage.indices_mapping = len(tous)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Agent qualité : détection de mapping en échec", exc_info=True)
            passage.erreurs.append(f"mapping : {exc}")

        # --- 3. Contrôles de qualité et de fraîcheur -------------------------
        constats: list[dict] = []
        try:
            constats += [
                c.as_dict()
                for c in ControlesQualite(
                    self.db,
                    min_text_chars=self.cfg.min_text_chars,
                    volume_spike_factor=self.cfg.volume_spike_factor,
                    volume_min_baseline=self.cfg.volume_min_baseline,
                ).analyser()
            ]
            cadences = self._cadences()
            constats += [
                c.as_dict()
                for c in ControlesFraicheur(
                    self.db, stale_factor=self.cfg.stale_factor
                ).analyser(cadences)
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Agent qualité : contrôles en échec", exc_info=True)
            passage.erreurs.append(f"contrôles : {exc}")
            cadences = self._cadences()

        # Les indices de mapping deviennent AUSSI des constats : ils doivent
        # apparaître dans la file d'instruction, pas seulement dans le
        # diagnostic d'une filiale. Un rattachement suspect sans filiale
        # identifiée — des avis orphelins, par exemple — n'appartient à aucun
        # diagnostic et serait sinon perdu.
        for liste in indices.values():
            for indice in liste:
                constats.append(_indice_en_constat(indice))

        if not dry_run and constats:
            passage.constats = self.depot.enregistrer_constats(constats)
        else:
            passage.constats = len(constats)

        # --- 3 bis. Validation sémantique, sur les seuls avis déjà signalés ---
        #
        # APRÈS les règles, jamais avant : le modèle n'instruit que ce qu'une
        # règle déterministe a déjà mis en doute. C'est ce qui tient le budget
        # (quelques dizaines d'avis par passage, pas 40 078) et ce qui garantit
        # qu'un modèle indisponible ne fait rien perdre — les constats restent
        # `FLAGGED` et repasseront.
        if not dry_run and self.validateur is not None:
            passage.valides = self._valider_semantiquement()

        # --- 4. Diagnostic, score, découverte --------------------------------
        completude = completude_par_filiale(self.db)
        ouverts = self.depot.constats_ouverts_par_filiale()
        poids = self.cfg.poids()

        scores: list[dict] = []
        candidates: list[dict] = []
        a_signaler: list[tuple[Any, Any]] = []

        for couverture in couvertures:
            diag = diagnostiquer(
                couverture,
                min_reviews=self.cfg.min_reviews,
                min_sources=self.cfg.min_sources,
                indices_mapping=indices.get(couverture.subsidiary_id),
            )
            passage.diagnostics[diag.cas.value] = (
                passage.diagnostics.get(diag.cas.value, 0) + 1
            )

            score = calculer_score(
                couverture,
                diag,
                poids=poids,
                min_reviews=self.cfg.min_reviews,
                min_sources=self.cfg.min_sources,
                stale_factor=self.cfg.stale_factor,
                cadences_minutes=cadences,
                stats_completude=completude.get(couverture.subsidiary_id),
                constats_ouverts=ouverts.get(couverture.subsidiary_id, 0),
                trusted_at=self.cfg.trusted_at,
                acceptable_at=self.cfg.acceptable_at,
                degraded_at=self.cfg.degraded_at,
            )
            scores.append(score.as_dict())

            # LA DÉCOUVERTE N'EST TENTÉE QUE SUR UN CAS ENRICHISSABLE.
            # C'est ici que le garde-fou de la section 5 est appliqué, et nulle
            # part ailleurs : `DecouverteSources` ne sait pas — et ne doit pas
            # savoir — s'il est légitime de chercher.
            if diag.enrichissable and not dry_run:
                try:
                    trouvees = self.decouverte.pour(couverture)
                    candidates += [c.as_dict() | {
                        "subsidiary_id": couverture.subsidiary_id,
                        "probe_at": c.probe_at,
                    } for c in trouvees]
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Découverte en échec pour %s", couverture.subsidiary,
                        exc_info=True,
                    )

            if score.statut in STATUTS_A_SIGNALER:
                a_signaler.append((couverture, (diag, score)))

        if not dry_run:
            try:
                self.depot.enregistrer_scores(scores)
            except Exception as exc:  # noqa: BLE001
                # Seule écriture dont l'échec compte : sans instantané, les
                # Agents 1 et 2 liraient un statut périmé en le croyant à jour.
                logger.exception("Agent qualité : instantanés non écrits")
                passage.erreurs.append(f"scores : {exc}")
            passage.candidates = self.depot.enregistrer_candidates(candidates)
            passage.sources_annoncees = self._notifier_nouvelles_sources()
        else:
            passage.candidates = len(candidates)

        # --- 5. Affirmations, sur les filiales qui ont de la matière ----------
        passage.affirmations, passage.non_corrobores = self._affirmations(
            couvertures, dry_run=dry_run
        )

        # --- 6. Mémoire, puis envoi ------------------------------------------
        self._arbitrer_et_envoyer(a_signaler, passage, dry_run=dry_run)
        return passage

    # ------------------------------------------------------- Nouvelles sources

    def _notifier_nouvelles_sources(self) -> int:
        """Annonce sur Telegram les sources VÉRIFIÉES qu'on n'a jamais signalées.

        MÉCANISME DISTINCT DE L'ALERTE DE SCORE (`_arbitrer_et_envoyer`), et
        volontairement. L'alerte de score parle d'une FILIALE qui va mal ; ce
        message-ci parle d'une SOURCE qui vient d'apparaître — deux faits de
        nature différente, qui n'ont pas à attendre l'un l'autre ni à se
        fondre dans un seul texte.

        COURT, ET SANS DÉTAIL TECHNIQUE. Le métier a été explicite : pas de
        code HTTP, pas de score, pas de vocabulaire de diagnostic. Le détail
        complet reste dans l'onglet Data Quality ; ce message n'est qu'un
        signal — « une source vient d'être trouvée » — suivi d'une proposition.

        UNE SEULE ANNONCE PAR SOURCE, POUR TOUJOURS. `notified_at` n'est jamais
        remis à zéro par `enregistrer_candidates` : ce n'est pas un
        refroidissement qui expire, c'est un fait qui ne se reproduit pas. Une
        source déjà portée à la connaissance de l'équipe n'a pas besoin d'un
        rappel — c'est exactement ce que « évite les alertes répétitives »
        demande.
        """
        if self.notifier is None:
            return 0
        try:
            # Même retenue que pour les filiales (`max_sujets`) : un lot de
            # dix découvertes d'un coup n'est pas plus lisible qu'un lot de
            # dix alertes, et pour la même raison — ce qu'on ne lit pas ne
            # protège personne.
            candidates = self.depot.candidates_a_notifier(limit=self.cfg.max_sujets)
        except Exception:  # noqa: BLE001
            logger.warning("Sources à annoncer illisibles.", exc_info=True)
            return 0
        if not candidates:
            return 0

        echapper = TelegramNotifier._echapper
        envoyees: list[int] = []
        for c in candidates:
            filiale = c.get("subsidiary") or "périmètre non ciblé"
            lignes = [
                f"🛡️ <b>Nouvelle source détectée — {echapper(filiale)}</b>",
                "",
                # LE NOM DE LA SOURCE RESTE AFFICHÉ, à la différence de
                # l'exemple donné par le métier : sans lui, la proposition
                # « ajouter cette source » ne dit pas LAQUELLE, et le message
                # devient inactionnable. C'est la seule ligne ajoutée au
                # gabarit demandé, et elle sert exactement le même but que le
                # reste — permettre d'agir sans ouvrir le dashboard.
                f"🔗 {echapper(c['source_name'])}",
            ]
            # Omis plutôt qu'un zéro inventé : voir `_lire_volume_avis`. Un
            # volume absent ne rend pas la source moins intéressante, il dit
            # seulement que la page ne l'affiche pas.
            if c.get("avis_estimes"):
                lignes.append(f"📌 {c['avis_estimes']} avis visibles")
            lignes.append("🌐 Source publique et gratuite")
            lignes += [
                "",
                "➡️ <b>Proposition :</b>",
                f"Ajouter cette source aux sources de données de "
                f"{echapper(filiale)}.",
            ]

            try:
                envoye = self.notifier.send_text("\n".join(lignes))
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Annonce de source non acheminée : %s", c["source_name"],
                    exc_info=True,
                )
                envoye = False

            # MARQUÉ SEULEMENT SI RÉELLEMENT ENVOYÉ. Un canal injoignable ne
            # doit pas faire perdre l'annonce pour de bon : elle reste en file
            # et sera retentée au passage suivant, comme n'importe quelle
            # alerte non acheminée ailleurs dans ce projet.
            if envoye:
                envoyees.append(c["candidate_id"])

        if envoyees:
            self.depot.marquer_notifiees(envoyees)
        return len(envoyees)

    # ------------------------------------------------------ Validation modèle

    def _valider_semantiquement(self) -> int:
        """Soumet au modèle les avis signalés et applique ses verdicts.

        NE LÈVE JAMAIS. Un modèle injoignable laisse les constats en `FLAGGED` :
        ils repasseront. Faire échouer le passage entier priverait l'agent de
        son score et de son diagnostic — c'est-à-dire de tout ce qu'il sait
        produire SANS modèle — pour une couche qui n'est qu'un supplément.
        """
        try:
            avis = self.depot.avis_a_valider(limit=40)
            if not avis:
                return 0
            verdicts, rapport = self.validateur.valider(avis)
        except Exception:  # noqa: BLE001
            logger.warning("Validation sémantique en échec", exc_info=True)
            return 0

        instruits = 0
        for verdict in verdicts:
            statut = verdict.statut()
            # Un verdict illisible produit `REVIEW_REQUIRED` et non un rejet :
            # on n'écarte jamais une donnée sur une réponse qu'on n'a pas su
            # lire. Voir `Verdict.statut`.
            if self.depot.statuer(verdict.flag_id, statut, detected_by="llm"):
                instruits += 1

        logger.info("Agent qualité : validation sémantique %s", rapport.as_dict())
        return instruits

    # ------------------------------------------------------------ Affirmations

    def _affirmations(self, couvertures, *, dry_run: bool) -> tuple[int, int]:
        """Vérifie les affirmations des filiales assez fournies pour en porter.

        BORNÉ AUX DIX FILIALES LES PLUS FOURNIES : chaque vérification interroge
        la presse, et le faire pour 135 filiales à chaque passage coûterait
        beaucoup pour un résultat dont l'essentiel serait vide — une filiale à
        douze avis ne produit pas d'affirmation collective.
        """
        verificateur = VerificateurAffirmations(self.db)
        fournies = sorted(
            (c for c in couvertures if c.avis_clients >= self.cfg.min_reviews),
            key=lambda c: -c.avis_clients,
        )[:10]

        toutes: list[dict] = []
        for couverture in fournies:
            try:
                toutes += [
                    a.as_dict()
                    for a in verificateur.analyser(
                        subsidiary_id=couverture.subsidiary_id
                    )
                ]
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Vérification d'affirmations en échec pour %s",
                    couverture.subsidiary, exc_info=True,
                )

        if toutes and not dry_run:
            self.depot.enregistrer_affirmations(toutes)
        non_corrobores = sum(1 for a in toutes if not a["exploitable"])
        return len(toutes), non_corrobores

    # ------------------------------------------------------- Mémoire et envoi

    def _arbitrer_et_envoyer(
        self, a_signaler: list, passage: Passage, *, dry_run: bool
    ) -> None:
        """Applique la retenue, la non-répétition, puis notifie."""
        if not a_signaler:
            passage.raison_silence = "aucune filiale sous le seuil de confiance"
            logger.info("Agent qualité : rien à signaler (%s)", passage.resume())
            return

        # Les pires d'abord, puis la retenue. Même règle que l'Agent 1 : trois
        # sujets au maximum, parce qu'un briefing de dix sujets n'est pas lu.
        a_signaler.sort(key=lambda x: x[1][1].global_score)
        selection = a_signaler[: self.cfg.max_sujets]

        a_dire: list[tuple[Any, Any, str]] = []
        for couverture, (diag, score) in selection:
            dernier = self.journal.last_report(
                AGENT, "subsidiary", str(couverture.subsidiary_id)
            )
            # LA COMPARAISON EST INVERSÉE (voir l'en-tête du module) : ici, une
            # aggravation est une BAISSE de score. On passe donc l'opposé du
            # score, de sorte que « pire qu'avant » redevienne « plus grand
            # qu'avant » et que `should_report` s'applique sans modification.
            parler, pourquoi = should_report(
                _negatif(dernier),
                -score.global_score * 100.0,
                cooldown_days=self.cfg.cooldown_jours,
                aggravation_points=self.cfg.aggravation_points,
            )
            if not parler:
                passage.tus.append(
                    {"entite": couverture.subsidiary, "raison": pourquoi}
                )
                continue
            texte = self._rediger(couverture, diag, score)
            a_dire.append((couverture, score, texte))
            passage.signales.append(
                {
                    "entite": couverture.subsidiary,
                    "raison": pourquoi,
                    "statut": score.statut,
                    "score": round(score.global_score * 100, 1),
                    "diagnostic": diag.cas.value,
                    "texte": texte,
                }
            )

        if not a_dire:
            passage.raison_silence = "tout était déjà signalé et rien n'a empiré"
            logger.info("Agent qualité : silence volontaire (%s)", passage.resume())
            return

        if dry_run:
            return

        passage.envoye = self._envoyer(a_dire)
        for couverture, score, texte in a_dire:
            self.journal.record(
                agent=AGENT,
                entity_level="subsidiary",
                entity_key=str(couverture.subsidiary_id),
                entity_label=couverture.subsidiary,
                # Stocké en NÉGATIF, cohérent avec la comparaison ci-dessus. Le
                # champ ne sert qu'à détecter une évolution ; il n'est jamais
                # montré à l'utilisateur (migration 013).
                score=-score.global_score * 100.0,
                text=texte,
                payload=score.as_dict(),
                delivered=passage.envoye,
            )

    def _rediger(self, couverture, diag, score) -> str:
        """Le texte d'un signalement. ENTIÈREMENT CALCULÉ, aucun modèle.

        Un modèle n'apporterait rien ici : les phrases utiles sont déjà écrites
        par le diagnostic (`raison`, `recommandation`) et par les composantes du
        score, chacune produite avec son chiffre. Les faire reformuler
        ajouterait un appel, une latence et un risque de contradiction entre la
        phrase et le nombre qu'elle commente.
        """
        lignes = [
            f"Score de qualité : {score.global_score * 100:.0f} % "
            f"({score.statut}).",
            diag.raison,
        ]

        faibles = sorted(
            (
                (nom, c)
                for nom, c in score.composantes.items()
                if c.valeur is not None and c.valeur < 0.5
            ),
            key=lambda x: x[1].valeur,
        )[:2]
        for nom, composante in faibles:
            lignes.append(f"· {_LIBELLES.get(nom, nom)} : {composante.explication}")

        if diag.recommandation:
            lignes.append(f"À faire : {diag.recommandation}")
        return "\n".join(lignes)

    def _envoyer(self, a_dire: list) -> bool:
        """Notification Telegram. Reprend `send_text`, déjà utilisé par l'Agent 1."""
        if self.notifier is None:
            logger.info("Agent qualité : aucun canal configuré, rien envoyé")
            return False

        echapper = TelegramNotifier._echapper
        lignes = ["🛡️ <b>Qualité des données</b>", ""]
        for i, (couverture, score, texte) in enumerate(a_dire, 1):
            emoji = "🔴" if score.statut == "UNTRUSTED" else "🟠"
            lignes.append(
                f"{emoji} <b>{i}. {echapper(couverture.subsidiary)}</b>"
            )
            lignes.append(echapper(texte))
            lignes.append("")

        lignes.append(
            f"<i>{len(a_dire)} filiale(s) sous le seuil de confiance — "
            "voir l'onglet Data Quality pour le détail et les preuves.</i>"
        )

        # L'ENVOI EST LA DERNIÈRE ÉTAPE, ET LA MOINS IMPORTANTE.
        #
        # À ce point, tout le travail utile est fait et écrit : diagnostic,
        # score, instantanés que liront les Agents 1 et 2. Laisser une panne de
        # canal remonter ferait échouer un passage abouti — et, pire, le
        # planificateur compterait un échec sur un job qui a parfaitement
        # travaillé. APScheduler désactive un job qui lève trop souvent : on
        # perdrait la surveillance à cause du messager.
        #
        # `TelegramNotifier.send_text` avale déjà ses propres exceptions et rend
        # False. Ce filet couvre le reste : un notifieur tiers, un attribut
        # inattendu, une injection de test.
        try:
            return self.notifier.send_text("\n".join(lignes))
        except Exception:  # noqa: BLE001
            logger.warning(
                "Agent qualité : alerte rédigée mais non acheminée.", exc_info=True
            )
            return False

    # ---------------------------------------------------------------- Interne

    def _cadences(self) -> dict[str, int]:
        """Cadence attendue de chaque source, en minutes, par code `dim_source`.

        Lue depuis la configuration du planificateur, qui est le point de vérité
        du rythme de chaque collecteur. La recopier ici ferait diverger la
        fraîcheur attendue de la fraîcheur réellement planifiée.
        """
        from reviews.agents.quality.couverture import SOURCE_VERS_COLLECTEUR

        return {
            code: self.settings.scraper_interval_minutes(collecteur)
            for code, collecteur in SOURCE_VERS_COLLECTEUR.items()
        }


#: Libellés lisibles des composantes, pour les notifications.
_LIBELLES = {
    "coverage": "Couverture",
    "freshness": "Fraîcheur",
    "completeness": "Complétude",
    "consistency": "Cohérence",
    "diversity": "Diversité des sources",
    "reliability": "Fiabilité de collecte",
}


def _indice_en_constat(indice: dict[str, Any]) -> dict[str, Any]:
    """Traduit un indice de mapping en constat persistable."""
    return {
        "kind": f"mapping_{indice.get('kind', 'suspect')}",
        "scope": "subsidiary" if indice.get("subsidiary_id") else "source",
        "subject_key": str(
            indice.get("subsidiary_id") or indice.get("company") or indice.get("target_id") or "?"
        ),
        "subsidiary_id": indice.get("subsidiary_id"),
        "source_code": indice.get("source"),
        "severity": "warning",
        "confidence": indice.get("confiance"),
        "reason": indice.get("raison") or "",
        "evidence": [indice],
    }


def _negatif(dernier: Optional[dict]) -> Optional[dict]:
    """Rend le dernier signalement tel quel : son score est DÉJÀ stocké en négatif.

    Cette fonction existe pour rendre l'intention visible à la lecture. Le
    stockage en négatif est un choix qui se comprend mal sans être nommé, et un
    `dernier` passé brut à `should_report` laisserait croire à un oubli.
    """
    return dernier


def build_quality_agent(
    db: Database, settings: Settings
) -> QualityGuardian:
    """Assemble l'agent avec ses dépendances réelles.

    Les deux dépendances externes sont OPTIONNELLES et le restent : sans modèle,
    l'agent tourne en règles déterministes — qui produisent déjà la couverture,
    le diagnostic, le mapping, les contrôles et le score. Sans Telegram, il
    journalise sans envoyer. Aucune des deux absences n'empêche le passage.

    C'est le MÊME constructeur pour la CLI, le planificateur et l'API, comme
    `build_campaign_agent` : c'est ce qui garantit qu'un diagnostic obtenu
    depuis le web applique exactement les mêmes seuils que depuis un terminal.
    """
    notifier = None
    alerting = settings.alerting
    if alerting.telegram_bot_token and alerting.telegram_chat_id:
        notifier = TelegramNotifier(alerting)
    else:
        logger.info("Agent qualité : Telegram non configuré, aucun envoi")

    validateur = None
    from reviews.config import quality_llm_config

    cfg_llm = quality_llm_config(settings)
    if cfg_llm is not None:
        from reviews.llm.client import LLMClient
        from reviews.llm.quality_validator import ValidateurQualite

        validateur = ValidateurQualite(LLMClient(cfg_llm, db=db))

    return QualityGuardian(
        db, settings, notifier=notifier, validateur=validateur
    )
