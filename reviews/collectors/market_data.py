"""
Collecteur d'indicateurs de marché — Banque Mondiale / UIT (dataset ITU_DH).

CE QU'IL APPORTE, ET POURQUOI ÇA MANQUAIT
    Jusqu'ici le corpus ne contenait que de l'OPINION : des avis, de la presse.
    Rien ne permettait de distinguer « la satisfaction baisse et le réseau est
    mauvais » de « la satisfaction baisse alors que le réseau est excellent ».
    Ces indicateurs apportent le fait mesurable qui manquait : abonnés, trafic
    data, couverture 2G/3G/4G/5G, utilisateurs Internet, investissement.

POURQUOI CETTE SOURCE PLUTÔT QU'UNE AUTRE
    Trois sources existent pour ces chiffres en Afrique, et elles s'excluent
    par leur coût d'intégration :

      - les RÉGULATEURS nationaux (NCC, ANRT, ICASA…) : mensuels et PAR
        OPÉRATEUR, donc la meilleure granularité — mais un site et un format
        par pays, soit 38 intégrations distinctes.
      - les RAPPORTS FINANCIERS des groupes : seuls à porter le revenu et
        l'ARPU — mais en PDF, l'extraction la plus fragile.
      - la BANQUE MONDIALE / UIT : gratuite, sans clé, en JSON, et couvrant
        ~200 économies d'un seul appel — donc les 38 pays du périmètre.

    Retenue pour commencer parce qu'elle est la seule à couvrir tout le
    périmètre immédiatement. Sa limite est assumée et doit être répétée à
    chaque usage : les données sont PAR PAYS et ANNUELLES. Aucun chiffre
    d'abonnés par opérateur ne sortira d'ici.

CE QUE CE MODULE N'EST PAS
    Ce n'est pas un `BaseCollector`. Les collecteurs du pipeline produisent des
    `Review` ; celui-ci produit des mesures qui n'ont ni auteur, ni note, ni
    sentiment. Le brancher sur le pipeline d'avis l'aurait fait entrer dans les
    agrégats de satisfaction — précisément ce que la séparation `source_kind`
    interdit à la presse. Il a donc son propre job et sa propre table.
"""

import logging
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

#: Point d'entrée public, sans authentification ni quota déclaré.
_BASE = "https://data360api.worldbank.org/data360/data"
_DATASET = "ITU_DH"

#: Indicateurs retenus, avec leur libellé lisible.
#:
#: VOLONTAIREMENT COURT. Le catalogue en compte des centaines ; en collecter
#: cent produirait un tableau que personne ne lit et 38 fois plus d'appels.
#: Ceux-ci répondent aux questions que le dashboard pose déjà — le marché
#: grandit-il, le réseau suit-il, les gens consomment-ils plus ?
INDICATEURS: dict[str, str] = {
    # --- Taille du marché
    "IT_CEL_SETS": "Abonnements mobiles",
    "IT_BB_MOB_PSB": "Abonnements haut débit mobile",
    "ACT_MOB_SB": "Haut débit mobile actif",
    "IT_MLT_MAIN": "Lignes fixes",
    "IT_NET_USER": "Utilisateurs d'Internet",
    "IT_HH_INT": "Ménages connectés à Internet",
    # --- Réseau
    "MOB_COV_2G": "Couverture 2G",
    "MOB_COV_3G": "Couverture 3G",
    "MOB_COV_4G": "Couverture 4G",
    "MOB_COV_5G": "Couverture 5G",
    "INT_BAND_PER_USR": "Bande passante internationale par utilisateur",
    # --- Usage
    "IT_BB_MOB_TRF": "Trafic data mobile",
    "IT_BB_FIX_TRF": "Trafic haut débit fixe",
    # --- Prix. LA SEULE APPROCHE DE LA MONÉTISATION DISPONIBLE GRATUITEMENT :
    # ce jeu de données ne contient AUCUN indicateur de revenu ni d'ARPU
    # (vérifié sur les 40 codes du catalogue). Le prix des paniers de
    # consommation en tient lieu, et il a l'avantage d'être plus frais que tout
    # le reste — renseigné jusqu'en 2025 quand les abonnés s'arrêtent à 2024.
    "PRI_DO_MOB": "Prix du panier data mobile",
    "PRI_HU_VD": "Prix du panier voix+data (usage élevé)",
    "PRI_FIX_BB_5G": "Prix du haut débit fixe",
    # --- Investissement. Conservé bien qu'il ne couvre que 4 pays sur 54 : la
    # collecte ne coûte rien et la source peut se remplir. Écarté de
    # l'AFFICHAGE par `INDICATEURS_AFFICHABLES`.
    "IT_INV_COMP": "Investissement dans les télécoms",
}

#: Indicateurs écartés APRÈS VÉRIFICATION, et pourquoi — pour qu'on ne perde
#: pas une seconde fois le temps de les tester :
#:
#:   IT_COMP_BB_MOB, IT_COMP_INT_SER, IT_COMP_FIB
#:       Semblent compter les opérateurs ; renvoient en réalité du TEXTE
#:       (« Full competition (year when competition was introduced) »). C'est un
#:       statut réglementaire, pas un dénombrement. Inexploitable dans une
#:       colonne numérique.
#:
#:   IT_BB_FIX_PSB
#:       Unité `GB_SB` comme `IT_BB_MOB_PSB`, mais valeurs d'un tout autre ordre
#:       — 4 480 pour le Maroc contre 15 à 193 pour le mobile. Le code d'unité
#:       ne désigne donc pas la même chose selon l'indicateur, et rien ne permet
#:       de trancher depuis l'API. Un chiffre qu'on ne sait pas nommer n'a rien
#:       à faire sur un écran.
_ECARTES_APRES_VERIFICATION = (
    "IT_COMP_BB_MOB", "IT_COMP_INT_SER", "IT_COMP_FIB", "IT_BB_FIX_PSB",
)

#: Indicateurs assez renseignés pour être MONTRÉS sur un écran.
#:
#: Mesuré sur l'année 2024 après collecte des 54 pays : `IT_INV_COMP` n'est
#: renseigné que pour QUATRE pays, contre 54 pour la couverture 4G. Une tuile
#: vide neuf fois sur dix fait douter de tout l'écran, y compris des chiffres
#: qui, eux, sont là. On l'écarte de l'affichage sans cesser de le collecter :
#: la donnée reste en base pour le jour où la source se remplira.
INDICATEURS_AFFICHABLES = tuple(
    c for c in INDICATEURS if c != "IT_INV_COMP"
)

#: Unités de la source, traduites pour l'affichage.
#:
#: SANS CETTE TABLE, LE DASHBOARD MENT PAR OMISSION. La source rend des codes
#: opaques — `PT_POP`, `SB_10P2_HB`, `XB_Y` — et un écran qui affiche
#: « Abonnements mobiles : 153,1 SB_10P2_HB » laisse le lecteur croire à un
#: nombre d'abonnés. Les deux lignes de `IT_CEL_SETS` (58 286 168 en `SB` et
#: 153,1 en `SB_10P2_HB`) ne se distinguent QUE par leur unité : c'est elle qui
#: porte le sens, elle doit donc être lisible.
#:
#: Les codes inconnus sont rendus tels quels par `unite_libelle` : afficher un
#: code brut est laid, mais moins grave que d'afficher une unité inventée.
#: `GB_SB` A ÉTÉ MAL TRADUIT UNE PREMIÈRE FOIS, et l'erreur est instructive :
#: le code évoque des gigaoctets, mais les valeurs observées sur les 54 pays
#: vont de 15,0 à 193,1 avec une moyenne de 59,1 — ce sont des ABONNEMENTS POUR
#: 100 HABITANTS, pas un volume de données. Un code d'unité ne se devine pas ;
#: il se vérifie contre la plage de valeurs réelles.
UNITES: dict[str, str] = {
    "SB": "abonnements",
    "SB_10P2_HB": "pour 100 habitants",
    "GB_SB": "pour 100 habitants",
    "PT_POP": "% de la population",
    "PT_HH": "% des ménages",
    "XB_Y": "Go par abonnement et par mois",
    "MBIT_S": "Mbit/s",
    "KBPS": "kbit/s par utilisateur",
    "USD": "USD par mois",
    "PPP": "USD PPA par mois",
}

#: Années conservées. Au-delà, on alourdit la base sans servir un dashboard qui
#: raisonne sur des tendances récentes.
ANNEE_PLANCHER = 2015

#: Secondes accordées à UN appel. La source répond en moins d'une seconde en
#: temps normal ; au-delà de vingt, c'est qu'elle est indisponible.
TIMEOUT = 20

#: Codes signalant « toutes catégories confondues » dans les axes de
#: ventilation de la source (`_T` = total, `_Z` = sans objet).
#:
#: NE GARDER QUE LES TOTAUX, ET C'EST UNE DÉCISION DE FOND.
#:
#: Mesuré : `IT_NET_USER` rend pour le Maroc 2012 HUIT observations sous la même
#: unité et la même année — le total (55,4 %), les femmes (45,8 %), les hommes
#: (65,4 %), puis des tranches d'âge et des découpages urbain/rural. Les
#: charger toutes ferait afficher « 65,4 % d'utilisateurs d'Internet au Maroc »
#: selon l'ordre d'arrivée : le chiffre des hommes, présenté comme le chiffre
#: du pays.
#:
#: Le dashboard raisonne au niveau national ; les ventilations
#: sociodémographiques répondraient à une autre question, qu'aucun écran ne
#: pose. On les écarte à la source plutôt que de les filtrer à la lecture —
#: une donnée qu'on ne sait pas montrer n'a pas à occuper la base.
_TOTAUX = {"_T", "_Z", "", None}

#: Axes sur lesquels on exige un total.
_AXES_VENTILATION = (
    "SEX", "AGE", "URBANISATION",
    "COMP_BREAKDOWN_1", "COMP_BREAKDOWN_2", "COMP_BREAKDOWN_3",
)


class MarketDataCollector:
    """Interroge la Banque Mondiale / UIT, indicateur par indicateur."""

    def __init__(self, timeout: int = TIMEOUT):
        self.timeout = timeout
        self.logger = logger

    def collect(
        self,
        pays: dict[str, int],
        indicateurs: Optional[list[str]] = None,
        annee_plancher: int = ANNEE_PLANCHER,
    ) -> tuple[list[dict], list[str]]:
        """Récupère les indicateurs pour les pays demandés.

        Args:
            pays: {ISO3: country_id}. L'appelant fournit la correspondance ;
                ce module ne connaît pas le modèle dimensionnel.
            indicateurs: sous-ensemble de `INDICATEURS`, ou tous par défaut.
            annee_plancher: années antérieures ignorées.

        Returns:
            (lignes prêtes pour `MarketRepository.upsert`, erreurs lisibles).

        UN APPEL PAR INDICATEUR ET PAR PAYS — assumé. L'API accepte de filtrer
        sur `REF_AREA`, et demander tous les pays d'un coup rendrait des
        réponses de plusieurs mégaoctets dont on jetterait l'essentiel. Neuf
        indicateurs sur 38 pays font 342 appels courts, exécutés une fois par
        mois : c'est le rythme d'une donnée annuelle, pas d'une collecte
        temps réel.
        """
        codes = indicateurs or list(INDICATEURS)
        lignes: list[dict] = []
        erreurs: list[str] = []

        for code in codes:
            for iso3, country_id in pays.items():
                try:
                    brut = self._fetch(code, iso3)
                except Exception as e:  # noqa: BLE001
                    # Un pays ou un indicateur qui échoue ne doit pas emporter
                    # les 341 autres : la collecte est partielle, jamais nulle.
                    erreurs.append(f"{code}/{iso3} : {e}")
                    continue

                for obs in brut:
                    ligne = self._normaliser(obs, country_id, annee_plancher)
                    if ligne is not None:
                        lignes.append(ligne)

        self.logger.info(
            "Marché : %d mesure(s) sur %d pays et %d indicateur(s), %d erreur(s)",
            len(lignes), len(pays), len(codes), len(erreurs),
        )
        return lignes, erreurs

    # ------------------------------------------------------------- Interne

    def _fetch(self, indicateur: str, iso3: str) -> list[dict]:
        reponse = requests.get(
            _BASE,
            params={
                "DATABASE_ID": _DATASET,
                "INDICATOR": indicateur,
                "REF_AREA": iso3,
            },
            timeout=self.timeout,
        )
        reponse.raise_for_status()
        charge = reponse.json()
        return charge.get("value") or []

    @staticmethod
    def _normaliser(obs: dict, country_id: int, annee_plancher: int) -> Optional[dict]:
        """Transforme une observation brute en ligne insérable, ou None.

        L'UNITÉ EST CONSERVÉE ET FAIT PARTIE DE LA CLÉ. Mesuré sur la source :
        `IT_CEL_SETS` rend pour le Maroc 2024 à la fois 58 286 168 en unité
        `SB` (abonnements) et 153,06 en `SB_10P2_HB` (pour 100 habitants). Les
        confondre afficherait « 153 abonnés au Maroc » selon l'ordre d'arrivée.
        """
        annee_brute = obs.get("TIME_PERIOD")
        valeur_brute = obs.get("OBS_VALUE")
        if annee_brute is None or valeur_brute in (None, ""):
            return None

        # Ventilations écartées : on ne conserve que « toutes catégories
        # confondues » sur chaque axe. Voir `_TOTAUX`.
        if any(obs.get(axe) not in _TOTAUX for axe in _AXES_VENTILATION):
            return None

        try:
            annee = int(str(annee_brute)[:4])
            valeur = float(valeur_brute)
        except (TypeError, ValueError):
            return None

        if annee < annee_plancher:
            return None

        return {
            "country_id": country_id,
            "indicator": obs.get("INDICATOR") or "",
            "unit": obs.get("UNIT_MEASURE") or "_Z",
            "year": annee,
            "value": valeur,
            "provider": "worldbank_itu",
        }


def libelle(indicateur: str) -> str:
    """Nom lisible d'un indicateur, ou son code s'il est inconnu."""
    return INDICATEURS.get(indicateur, indicateur)


def unite_libelle(unite: str) -> str:
    """Nom lisible d'une unité, ou son code s'il est inconnu."""
    return UNITES.get(unite, unite)
