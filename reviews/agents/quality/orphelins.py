"""
Réattribution des avis orphelins — proposer, tracer, n'appliquer que le sûr.

CE QUE L'ANALYSE A ÉTABLI AVANT D'ÉCRIRE UNE LIGNE (17 août 2026)
    1 215 avis sans `subsidiary_id`. On s'attendait à un problème
    d'identification ; c'en est un de REJEU :

        1 202  correspondent EXACTEMENT à un alias déclaré
        1 043  correspondent en plus au nom de la filiale
           13  ne correspondent à rien : tous « Orange Senegal », soit
               « Orange Sénégal » privé de son accent

    Et la fuite est colmatée : 24 256 avis collectés depuis le 5 août, zéro
    orphelin. L'arriéré est FIGÉ et BORNÉ.

CONSÉQUENCE SUR LA CONCEPTION, ET ELLE EST IMPORTANTE
    La chaîne demandée — règles, puis modèle si nécessaire — est implémentée
    intégralement, mais « si nécessaire » ne se déclenche PAS aujourd'hui :
    aucun orphelin n'atteint l'étage du modèle. C'est le bon résultat, pas une
    fonctionnalité manquante. Payer du quota pour redécouvrir une égalité de
    chaînes ajouterait un risque d'erreur là où il n'y en a aucun.

    L'étage du modèle reste écrit et testé parce que le cas se présentera :
    une source nouvelle, un opérateur renommé, un libellé composite.

L'ORDRE DES RÈGLES VA DU PLUS SÛR AU PLUS PERMISSIF
    Chacune ne s'exécute que si les précédentes n'ont rien donné, et la
    méthode retenue est inscrite dans la ligne. Une correspondance obtenue
    après repli d'accents n'a pas la valeur d'une égalité stricte, et le
    dashboard doit pouvoir les distinguer.

RIEN N'EST APPLIQUÉ SANS DEMANDE EXPLICITE
    `analyser()` ne fait que proposer. `appliquer()` écrit, ligne à ligne, en
    conservant l'état antérieur — le retour arrière est une requête, pas une
    restauration de sauvegarde.
"""

import logging
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from reviews.storage.db import Database

logger = logging.getLogger(__name__)

AUTO_SAFE = "AUTO_SAFE"
HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
UNRESOLVED = "UNRESOLVED"

#: Statuts applicables sans intervention humaine.
#:
#: `HIGH_CONFIDENCE` en est EXCLU par défaut, et c'est le garde-fou du module :
#: une correspondance obtenue après repli d'accents est très probablement juste,
#: mais elle repose sur une normalisation que personne n'a validée. On la
#: propose, on ne l'applique pas de sa propre initiative.
APPLICABLES_D_OFFICE = frozenset({AUTO_SAFE})


def normaliser(valeur: Optional[str]) -> str:
    """Replie casse, accents et espaces multiples.

    MÊME NORMALISATION QUE `GoogleMapsScraper._normalize`, et délibérément :
    le projet a déjà tranché comment comparer deux noms d'entité, et en écrire
    une seconde version garantirait qu'elles divergent le jour où l'une des
    deux gagne une règle.

    C'est cette fonction qui rattrape les 13 « Orange Senegal » à
    « Orange Sénégal ».
    """
    if not valeur:
        return ""
    sans_accent = "".join(
        c for c in unicodedata.normalize("NFD", valeur)
        if unicodedata.category(c) != "Mn"
    )
    return " ".join(sans_accent.lower().split())


@dataclass
class Proposition:
    """Une réattribution proposée, avec ce qui la fonde et ce qu'elle écarte."""

    review_id: str
    company: Optional[str]
    source_code: Optional[str]
    previous_subsidiary_id: Optional[int] = None
    proposed_subsidiary_id: Optional[int] = None
    proposed_subsidiary: Optional[str] = None
    method: str = "aucune"
    confidence: float = 0.0
    status: str = UNRESOLVED
    reason: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def applicable(self) -> bool:
        return (
            self.status in APPLICABLES_D_OFFICE
            and self.proposed_subsidiary_id is not None
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "company": self.company,
            "source_code": self.source_code,
            "previous_subsidiary_id": self.previous_subsidiary_id,
            "proposed_subsidiary_id": self.proposed_subsidiary_id,
            "proposed_subsidiary": self.proposed_subsidiary,
            "method": self.method,
            "confidence": round(self.confidence, 3),
            "status": self.status,
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass
class RapportOrphelins:
    """Bilan d'un passage, pour la CLI, l'API et les tests."""

    orphelins: int = 0
    auto_safe: int = 0
    haute_confiance: int = 0
    a_revoir: int = 0
    non_resolus: int = 0
    appliques: int = 0
    par_methode: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "orphelins": self.orphelins,
            "AUTO_SAFE": self.auto_safe,
            "HIGH_CONFIDENCE": self.haute_confiance,
            "REVIEW_REQUIRED": self.a_revoir,
            "UNRESOLVED": self.non_resolus,
            "appliques": self.appliques,
            "par_methode": self.par_methode,
        }

    def resume(self) -> str:
        return (
            f"{self.orphelins} orphelin(s) · {self.auto_safe} sûr(s) · "
            f"{self.haute_confiance} probable(s) · {self.a_revoir} à revoir · "
            f"{self.non_resolus} non résolu(s) · {self.appliques} appliqué(s)"
        )


class ResolveurOrphelins:
    """Propose une filiale pour chaque avis orphelin, et applique le sûr."""

    def __init__(self, db: Database, validateur: Optional[Any] = None):
        self.db = db
        # Étage modèle, optionnel. Absent = les cas ambigus restent en
        # REVIEW_REQUIRED, ce qui est le bon comportement : mieux vaut une file
        # d'instruction qu'une attribution devinée.
        self.validateur = validateur

    # ------------------------------------------------------------------ Public

    def analyser(self, limit: int = 5000) -> tuple[list[Proposition], RapportOrphelins]:
        """Établit une proposition par avis orphelin. N'écrit RIEN.

        Les deux référentiels — orphelins et filiales — sont chargés en UNE
        requête chacun, puis rapprochés en mémoire. Le faire en SQL par avis
        coûterait 1 215 allers-retours pour un travail que deux dictionnaires
        font en une passe.
        """
        rapport = RapportOrphelins()
        orphelins = self._orphelins(limit)
        rapport.orphelins = len(orphelins)
        if not orphelins:
            return [], rapport

        index = self._index_filiales()
        propositions = [self._resoudre(o, index) for o in orphelins]

        for p in propositions:
            rapport.par_methode[p.method] = rapport.par_methode.get(p.method, 0) + 1
            if p.status == AUTO_SAFE:
                rapport.auto_safe += 1
            elif p.status == HIGH_CONFIDENCE:
                rapport.haute_confiance += 1
            elif p.status == REVIEW_REQUIRED:
                rapport.a_revoir += 1
            else:
                rapport.non_resolus += 1

        return propositions, rapport

    def appliquer(
        self, propositions: list[Proposition], *, inclure_haute_confiance: bool = False
    ) -> int:
        """Écrit dans `reviews` les seules propositions autorisées.

        `inclure_haute_confiance` est un ACTE EXPLICITE de l'exploitant. Par
        défaut, seules les correspondances strictement déterministes passent :
        une normalisation d'accents est très probablement juste, mais elle
        repose sur une règle que personne n'a validée, et déplacer des avis
        d'une filiale à l'autre change des taux publiés.
        """
        autorises = set(APPLICABLES_D_OFFICE)
        if inclure_haute_confiance:
            autorises.add(HIGH_CONFIDENCE)

        a_ecrire = [
            p for p in propositions
            if p.status in autorises and p.proposed_subsidiary_id is not None
        ]
        if not a_ecrire:
            return 0

        with self.db.cursor() as cur:
            cur.executemany(
                """
                UPDATE reviews SET subsidiary_id = %s
                 WHERE review_id = %s AND subsidiary_id IS NULL
                """,
                [(p.proposed_subsidiary_id, p.review_id) for p in a_ecrire],
            )
            # `subsidiary_id IS NULL` dans le WHERE : garde-fou contre une
            # ré-application concurrente. Si un autre passage a déjà rattaché
            # l'avis, on ne l'écrase pas — on ne peut pas savoir laquelle des
            # deux attributions est la bonne, et écraser en silence serait la
            # pire des réponses.
            cur.executemany(
                """
                UPDATE orphan_resolutions
                   SET applied_at = now(), updated_at = now()
                 WHERE review_id = %s
                """,
                [(p.review_id,) for p in a_ecrire],
            )
        logger.info("Orphelins : %d avis rattachés", len(a_ecrire))
        return len(a_ecrire)

    # ------------------------------------------------------------- Résolution

    def _resoudre(self, orphelin: dict, index: "_IndexFiliales") -> Proposition:
        """Applique les règles, du plus sûr au plus permissif."""
        p = Proposition(
            review_id=orphelin["review_id"],
            company=orphelin.get("company"),
            source_code=orphelin.get("source_code"),
            previous_subsidiary_id=None,
        )
        company = (orphelin.get("company") or "").strip()
        if not company:
            p.reason = "L'avis ne porte aucun libellé d'entité : rien à rapprocher."
            return p

        # --- 1. Égalité STRICTE avec un alias déclaré ------------------------
        #
        # Le prédicat exact de l'insertion (`repository.py`). Une correspondance
        # ici signifie que l'avis AURAIT dû être rattaché à la collecte : c'est
        # un rejeu, pas une décision. D'où AUTO_SAFE.
        cibles = index.par_alias_exact.get(company)
        if cibles and len(cibles) == 1:
            return self._retenir(
                p, cibles[0], "alias_exact", 1.0, AUTO_SAFE,
                f"« {company} » est un alias déclaré de « {cibles[0][1]} ». "
                "Le rattachement aurait dû se faire à la collecte : cet avis "
                "précède la déclaration de cet alias.",
                {"type": "regle", "fait": "égalité stricte avec un alias déclaré"},
            )

        # --- 2. Égalité avec le NOM de la filiale ----------------------------
        cibles = index.par_nom_exact.get(company)
        if cibles and len(cibles) == 1:
            return self._retenir(
                p, cibles[0], "nom_exact", 0.98, AUTO_SAFE,
                f"« {company} » est exactement le nom de la filiale.",
                {"type": "regle", "fait": "égalité stricte avec dim_subsidiary.name"},
            )

        # --- 3. Égalité APRÈS normalisation ----------------------------------
        #
        # Rattrape les 13 « Orange Senegal ». HIGH_CONFIDENCE et non AUTO_SAFE :
        # la correspondance est très probable, mais elle passe par une règle de
        # repli qu'un humain doit avoir validée une fois.
        cle = normaliser(company)
        cibles = index.par_normalise.get(cle)
        if cibles and len(cibles) == 1:
            return self._retenir(
                p, cibles[0], "alias_normalise", 0.9, HIGH_CONFIDENCE,
                f"« {company} » correspond à « {cibles[0][1]} » après repli de "
                "la casse et des accents.",
                {"type": "regle", "fait": "égalité après normalisation",
                 "cle_normalisee": cle},
            )

        # --- 4. Plusieurs candidates : on ne tranche pas ---------------------
        #
        # AVANT l'étage du modèle, et c'est délibéré. Une ambiguïté entre deux
        # filiales déclarées est un défaut de NOTRE configuration — deux alias
        # qui se recouvrent — et un modèle n'a aucune information pour la
        # lever. Il produirait une réponse plausible et invérifiable.
        toutes = (
            index.par_alias_exact.get(company)
            or index.par_nom_exact.get(company)
            or index.par_normalise.get(cle)
            or []
        )
        if len(toutes) > 1:
            p.status = REVIEW_REQUIRED
            p.method = "ambigu"
            p.confidence = 0.4
            p.reason = (
                f"« {company} » correspond à {len(toutes)} filiales à la fois : "
                + ", ".join(nom for _, nom in toutes[:4])
                + ". Deux alias se recouvrent — c'est la configuration qu'il "
                "faut corriger, pas l'avis."
            )
            p.evidence = [
                {"type": "candidate", "subsidiary_id": sid, "subsidiary": nom}
                for sid, nom in toutes[:6]
            ]
            return p

        # --- 5. Étage du modèle ----------------------------------------------
        #
        # NON ATTEINT SUR LE CORPUS ACTUEL, et c'est le bon résultat. Écrit
        # pour le cas qui se présentera : une source nouvelle, un opérateur
        # renommé, un libellé composite qu'aucune règle ne reconnaît.
        if self.validateur is not None:
            verdict = self._demander_au_modele(orphelin, index)
            if verdict is not None:
                return verdict

        p.status = UNRESOLVED
        p.method = "aucune"
        p.reason = (
            f"Aucune filiale déclarée ne correspond à « {company} », même après "
            "normalisation."
        )
        return p

    def _demander_au_modele(
        self, orphelin: dict, index: "_IndexFiliales"
    ) -> Optional[Proposition]:
        """Soumet le cas au modèle. Rend None si rien d'exploitable.

        LE MODÈLE NE CHOISIT PAS DANS LE VIDE : on lui présente une liste
        FERMÉE de candidates, celles dont le nom partage un mot avec le
        libellé. Lui demander « à quelle filiale appartient cet avis ? » sans
        liste produirait un nom inventé — la faute que tout ce module existe
        pour empêcher.

        JAMAIS AUTO_SAFE, quelle que soit la confiance annoncée. Un verdict de
        modèle est une proposition à instruire, jamais un rejeu de règle.
        """
        company = (orphelin.get("company") or "").strip()
        candidates = index.proches(company, limite=6)
        if not candidates:
            return None

        try:
            verdicts, _ = self.validateur.valider(
                [
                    {
                        "flag_id": 0,
                        "review_id": orphelin["review_id"],
                        "kind": "rattachement_inconnu",
                        "title": None,
                        "text": (orphelin.get("text") or "")[:400],
                        "operator": company,
                        "country": ", ".join(nom for _, nom in candidates),
                        "source": orphelin.get("source_code"),
                    }
                ]
            )
        except Exception:  # noqa: BLE001
            logger.warning("Étage modèle indisponible pour un orphelin", exc_info=True)
            return None

        if not verdicts or not verdicts[0].valide:
            return None

        # Le verdict ne DÉSIGNE pas une filiale — le validateur n'est pas fait
        # pour cela. Il dit si l'avis est exploitable. On s'en sert donc pour
        # ORIENTER vers l'instruction humaine, avec la meilleure candidate en
        # tête, jamais pour trancher. C'est la limite honnête de cet étage tant
        # qu'aucun cas réel n'a permis de le spécialiser.
        v = verdicts[0]
        sid, nom = candidates[0]
        return Proposition(
            review_id=orphelin["review_id"],
            company=company,
            source_code=orphelin.get("source_code"),
            proposed_subsidiary_id=sid,
            proposed_subsidiary=nom,
            method="llm",
            confidence=min(0.6, v.confidence or 0.0),
            status=REVIEW_REQUIRED,
            reason=(
                f"Aucune règle ne reconnaît « {company} ». Le modèle juge l'avis "
                f"{'exploitable' if v.relevant else 'non exploitable'} "
                f"({v.reason[:120]}). Candidate la plus proche : « {nom} ». "
                "À valider par un humain."
            ),
            evidence=[
                {"type": "verdict_modele", **v.as_dict()},
                *[
                    {"type": "candidate", "subsidiary_id": s, "subsidiary": n}
                    for s, n in candidates
                ],
            ],
        )

    @staticmethod
    def _retenir(
        p: Proposition, cible: tuple[int, str], methode: str,
        confiance: float, statut: str, raison: str, preuve: dict,
    ) -> Proposition:
        p.proposed_subsidiary_id, p.proposed_subsidiary = cible
        p.method, p.confidence, p.status, p.reason = methode, confiance, statut, raison
        p.evidence = [{**preuve, "source": "dim_subsidiary",
                       "subsidiary_id": cible[0], "subsidiary": cible[1]}]
        return p

    # -------------------------------------------------------------- Chargement

    def _orphelins(self, limit: int) -> list[dict]:
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT r.review_id, r.company, s.code AS source_code,
                       LEFT(COALESCE(r.title, '') || ' ' || COALESCE(r.text, ''), 400)
                           AS text
                FROM reviews r
                LEFT JOIN dim_source s ON s.source_id = r.source_id
                WHERE r.subsidiary_id IS NULL
                ORDER BY r.collected_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def _index_filiales(self) -> "_IndexFiliales":
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                "SELECT subsidiary_id, name, aliases FROM dim_subsidiary "
                "WHERE active"
            )
            return _IndexFiliales([dict(r) for r in cur.fetchall()])


class _IndexFiliales:
    """Trois index de rapprochement, construits une fois par passage.

    Les valeurs sont des LISTES et non des entrées uniques : c'est ce qui
    permet de détecter qu'un libellé correspond à deux filiales, donc de
    refuser de trancher plutôt que d'en élire une au hasard.
    """

    def __init__(self, filiales: list[dict]):
        self.par_alias_exact: dict[str, list[tuple[int, str]]] = {}
        self.par_nom_exact: dict[str, list[tuple[int, str]]] = {}
        self.par_normalise: dict[str, list[tuple[int, str]]] = {}
        self._tous: list[tuple[int, str]] = []

        for f in filiales:
            cible = (f["subsidiary_id"], f["name"])
            self._tous.append(cible)
            self.par_nom_exact.setdefault(f["name"], []).append(cible)
            self.par_normalise.setdefault(normaliser(f["name"]), []).append(cible)
            for alias in f.get("aliases") or []:
                self.par_alias_exact.setdefault(alias, []).append(cible)
                cle = normaliser(alias)
                # Un alias qui se normalise comme le nom ne doit pas créer un
                # doublon dans l'index : il ferait croire à une ambiguïté entre
                # une filiale et elle-même, et bloquerait sa propre résolution.
                if cible not in self.par_normalise.setdefault(cle, []):
                    self.par_normalise[cle].append(cible)

    def proches(self, libelle: str, limite: int = 6) -> list[tuple[int, str]]:
        """Filiales partageant au moins un mot significatif avec le libellé.

        Sert à composer la liste FERMÉE présentée au modèle. Les mots de moins
        de trois lettres sont écartés : « de », « du », « la » rapprocheraient
        n'importe quoi de n'importe quoi.
        """
        mots = {m for m in normaliser(libelle).split() if len(m) >= 3}
        if not mots:
            return []
        scores = [
            (len(mots & {m for m in normaliser(nom).split() if len(m) >= 3}), sid, nom)
            for sid, nom in self._tous
        ]
        retenus = sorted((s for s in scores if s[0] > 0), reverse=True)
        return [(sid, nom) for _, sid, nom in retenus[:limite]]
