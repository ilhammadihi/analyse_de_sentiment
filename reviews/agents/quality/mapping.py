"""
Module 2 — soupçons de mauvais rattachement.

CE MODULE NE DÉCOUVRE RIEN DE NEUF : IL EXÉCUTE CE QUI ÉTAIT DÉJÀ ÉCRIT
    Les migrations 008 et 009 se terminent toutes deux par un contrôle en
    commentaire, présenté comme « devant rester vide » :

        SELECT DISTINCT op.name, r.target_name
        FROM reviews r ... WHERE position(lower(op.name) in lower(r.target_name)) = 0;

    C'est exactement la règle de détection de mapping demandée. Elle a été
    écrite parce que le cas s'est produit : « Agence Vodacom Johannesburg »
    remonte « Cellucity - Bedfordview » en premier résultat, un revendeur tiers
    dont les avis étaient enregistrés comme des avis Vodacom.

    Un contrôle que personne n'exécute ne protège de rien. Ce module le promeut
    de commentaire en règle exécutée à chaque passage, journalisée et datée.

LES QUATRE SOUPÇONS, ET POURQUOI CEUX-LÀ
    1. SOUS-CIBLE ÉTRANGÈRE — le nom de la fiche ou de l'application ne
       contient pas celui de l'opérateur. Le cas Cellucity.
    2. AVIS ORPHELINS — des avis en base dont le `company` ne correspond à
       aucune filiale. Ils disparaissent de TOUTES les vues du dashboard sans
       la moindre erreur : c'est le mode de panne le plus silencieux du modèle
       dimensionnel, et `dim_source` le documente déjà comme tel.
    3. ALIAS PROCHE — une filiale à zéro avis dont le nom ressemble fortement
       à un `company` porteur d'avis. C'est le cas « notre base dit 0, la
       source semble en avoir beaucoup » de l'énoncé.
    4. FICHE PARTAGÉE — une même sous-cible rattachée à deux filiales.

RIEN N'EST JAMAIS CORRIGÉ ICI
    L'énoncé l'exige, et c'est de toute façon la seule position tenable : un
    alias ajouté automatiquement déplacerait des avis d'une filiale à l'autre,
    donc changerait des taux publiés, sur la foi d'une ressemblance de chaînes.
    Le module PROPOSE, avec ses preuves. La décision reste humaine.
"""

import logging
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Optional

from reviews.storage.db import Database

logger = logging.getLogger(__name__)

#: Ressemblance à partir de laquelle deux noms sont jugés « proches ».
#:
#: 0,82 et non 0,70 : mesuré sur le périmètre, un seuil bas rapproche « MTN
#: Bénin » de « MTN Congo » (0,78) — deux filiales bien distinctes du même
#: opérateur. Or c'est précisément la faute que ce module existe pour détecter,
#: il serait absurde qu'il la commette. À 0,82, seules les variantes
#: orthographiques du même nom passent.
SEUIL_RESSEMBLANCE = 0.82

#: Avis minimum sur un `company` orphelin pour qu'il vaille un signalement. En
#: dessous, c'est du bruit de collecte et non un défaut de rattachement.
MIN_AVIS_ORPHELINS = 3


@dataclass
class IndiceMapping:
    """Un soupçon de mauvais rattachement, avec ce qui le fonde."""

    kind: str
    subsidiary_id: Optional[int]
    subsidiary: Optional[str]
    raison: str
    preuve: dict[str, Any]
    confiance: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "indice_mapping",
            "kind": self.kind,
            "subsidiary_id": self.subsidiary_id,
            "subsidiary": self.subsidiary,
            "raison": self.raison,
            "confiance": round(self.confiance, 2),
            **self.preuve,
        }


class DetecteurMapping:
    """Exécute les quatre contrôles de rattachement."""

    def __init__(self, db: Database):
        self.db = db

    def analyser(self) -> list[IndiceMapping]:
        """Tous les soupçons, du plus sûr au moins sûr.

        Ne lève jamais : un contrôle illisible ne doit pas faire tomber le
        passage entier de l'agent. Chaque contrôle est isolé — un schéma
        partiellement migré fait perdre UN contrôle, pas les quatre.
        """
        indices: list[IndiceMapping] = []
        for nom, controle in (
            ("sous-cible étrangère", self._sous_cibles_etrangeres),
            ("avis orphelins", self._avis_orphelins),
            ("alias proche", self._alias_proches),
            ("fiche partagée", self._cibles_partagees),
        ):
            try:
                indices.extend(controle())
            except Exception:  # noqa: BLE001
                logger.warning("Contrôle de mapping « %s » illisible.", nom, exc_info=True)
        return sorted(indices, key=lambda i: -i.confiance)

    # ------------------------------------------------------------ Contrôle 1

    def _sous_cibles_etrangeres(self) -> list[IndiceMapping]:
        """Le contrôle des migrations 008 et 009, enfin exécuté.

        `position(... in ...) = 0` teste l'absence de sous-chaîne. Comparé en
        minuscules des deux côtés, sur les sous-cibles seules (`target_name`
        non nul) — la presse et les flux n'ont pas de sous-cible et le test n'a
        aucun sens pour eux.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT sub.subsidiary_id, sub.name AS subsidiary,
                       op.name AS operateur, r.source, r.target_name,
                       COUNT(*) AS avis
                FROM reviews r
                JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
                JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
                WHERE r.target_name IS NOT NULL
                  AND position(lower(op.name) in lower(r.target_name)) = 0
                GROUP BY sub.subsidiary_id, sub.name, op.name, r.source, r.target_name
                HAVING COUNT(*) >= %s
                ORDER BY COUNT(*) DESC
                LIMIT 50
                """,
                (MIN_AVIS_ORPHELINS,),
            )
            lignes = cur.fetchall()

        return [
            IndiceMapping(
                kind="sous_cible_etrangere",
                subsidiary_id=r["subsidiary_id"],
                subsidiary=r["subsidiary"],
                raison=(
                    f"« {r['target_name']} » ne porte pas le nom de l'opérateur "
                    f"{r['operateur']}, et lui apporte pourtant {r['avis']} avis."
                ),
                preuve={
                    "source": r["source"],
                    "target_name": r["target_name"],
                    "operateur": r["operateur"],
                    "avis": r["avis"],
                },
                # Forte : c'est une règle exacte sur une chaîne, pas une
                # ressemblance. Non maximale : une enseigne peut légitimement
                # exploiter une marque commerciale distincte (« Ayoba » pour
                # MTN), et le module ne peut pas le savoir.
                confiance=0.8,
            )
            for r in lignes
        ]

    # ------------------------------------------------------------ Contrôle 2

    def _avis_orphelins(self) -> list[IndiceMapping]:
        """Avis en base rattachés à aucune filiale.

        LE MODE DE PANNE LE PLUS SILENCIEUX DU MODÈLE. `dim_source` le
        documente : un avis inséré sans clé dimensionnelle « disparaît de toutes
        les vues du dashboard, sans erreur pour le signaler ». Il est collecté,
        stocké, facturé en temps de scraping — et invisible.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT r.company, r.source, COUNT(*) AS avis,
                       MAX(r.collected_at) AS derniere
                FROM reviews r
                WHERE r.subsidiary_id IS NULL
                GROUP BY r.company, r.source
                HAVING COUNT(*) >= %s
                ORDER BY COUNT(*) DESC
                LIMIT 30
                """,
                (MIN_AVIS_ORPHELINS,),
            )
            lignes = cur.fetchall()

        return [
            IndiceMapping(
                kind="avis_orphelins",
                subsidiary_id=None,
                subsidiary=None,
                raison=(
                    f"{r['avis']} avis collectés sous « {r['company']} » "
                    f"({r['source']}) ne sont rattachés à aucune filiale : ils "
                    "sont invisibles dans tout le dashboard."
                ),
                preuve={
                    "company": r["company"],
                    "source": r["source"],
                    "avis": r["avis"],
                    "date": r["derniere"].isoformat() if r["derniere"] else None,
                },
                # Certitude : la clé est nulle, ce n'est pas une interprétation.
                confiance=0.95,
            )
            for r in lignes
        ]

    # ------------------------------------------------------------ Contrôle 3

    def _alias_proches(self) -> list[IndiceMapping]:
        """« Notre base dit 0, la source semble en avoir beaucoup ».

        Rapproche les filiales SANS avis des `company` orphelins QUI EN ONT.
        C'est le scénario exact décrit par l'énoncé, et le seul contrôle du
        module qui repose sur une ressemblance — d'où un seuil élevé et une
        confiance plafonnée.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT sub.subsidiary_id, sub.name
                FROM dim_subsidiary sub
                LEFT JOIN v_subsidiary_volume v
                       ON v.subsidiary_id = sub.subsidiary_id
                WHERE sub.active AND COALESCE(v.avis_clients, 0) = 0
                """
            )
            vides = [(r["subsidiary_id"], r["name"]) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT r.company, COUNT(*) AS avis
                FROM reviews r
                WHERE r.subsidiary_id IS NULL AND r.company IS NOT NULL
                GROUP BY r.company
                HAVING COUNT(*) >= %s
                """,
                (MIN_AVIS_ORPHELINS,),
            )
            candidats = [(r["company"], r["avis"]) for r in cur.fetchall()]

        indices: list[IndiceMapping] = []
        for sid, nom in vides:
            for company, avis in candidats:
                ratio = SequenceMatcher(None, nom.lower(), company.lower()).ratio()
                if ratio < SEUIL_RESSEMBLANCE:
                    continue
                indices.append(
                    IndiceMapping(
                        kind="alias_manquant",
                        subsidiary_id=sid,
                        subsidiary=nom,
                        raison=(
                            f"« {nom} » n'a aucun avis, alors que {avis} avis sont "
                            f"collectés sous « {company} » sans rattachement "
                            f"(ressemblance {ratio:.0%})."
                        ),
                        preuve={
                            "company": company,
                            "avis": avis,
                            "ressemblance": round(ratio, 3),
                            "correction_proposee": (
                                f"Ajouter « {company} » aux alias de « {nom} » "
                                "dans dim_subsidiary, APRÈS vérification manuelle."
                            ),
                        },
                        # Plafonnée : une ressemblance de chaînes est un indice,
                        # jamais une preuve d'identité. Même prudence que
                        # `verify_operator_coverage.py`, dont les rapprochements
                        # sont explicitement des propositions.
                        confiance=min(0.7, ratio),
                    )
                )
        return indices

    # ------------------------------------------------------------ Contrôle 4

    def _cibles_partagees(self) -> list[IndiceMapping]:
        """Une même sous-cible rattachée à deux filiales.

        Une fiche Google ou une application appartient à UNE filiale. Deux
        rattachements signifient qu'au moins l'un des deux est faux — et le
        commentaire de la migration 008 le documente déjà : « Agence MTN Lagos »
        et « Agence MTN Nigeria » renvoient la même fiche.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT r.target_id, r.source,
                       COUNT(DISTINCT r.subsidiary_id) AS filiales,
                       string_agg(DISTINCT sub.name, ' / ') AS noms,
                       COUNT(*) AS avis
                FROM reviews r
                JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
                WHERE r.target_id IS NOT NULL
                GROUP BY r.target_id, r.source
                HAVING COUNT(DISTINCT r.subsidiary_id) > 1
                ORDER BY COUNT(*) DESC
                LIMIT 20
                """
            )
            lignes = cur.fetchall()

        return [
            IndiceMapping(
                kind="cible_partagee",
                subsidiary_id=None,
                subsidiary=r["noms"],
                raison=(
                    f"La sous-cible {r['target_id']} ({r['source']}) est rattachée "
                    f"à {r['filiales']} filiales à la fois : {r['noms']}. "
                    "Au moins un rattachement est faux."
                ),
                preuve={
                    "target_id": r["target_id"],
                    "source": r["source"],
                    "filiales": r["filiales"],
                    "avis": r["avis"],
                },
                confiance=0.9,
            )
            for r in lignes
        ]


def indices_par_filiale(
    indices: list[IndiceMapping],
) -> dict[int, list[dict[str, Any]]]:
    """Regroupe les indices par filiale, au format attendu par `diagnostiquer`."""
    out: dict[int, list[dict[str, Any]]] = {}
    for indice in indices:
        if indice.subsidiary_id is None:
            continue
        out.setdefault(indice.subsidiary_id, []).append(indice.as_dict())
    return out
