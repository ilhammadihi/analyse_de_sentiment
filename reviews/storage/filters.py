"""
Contrat de filtre commun à tous les agrégats du dashboard.

POURQUOI CE MODULE EXISTE
    Le dashboard doit pouvoir croiser quatre axes (période, pays, opérateur,
    filiale) sur une dizaine d'endpoints. Écrire le WHERE à la main dans chaque
    requête garantissait deux dérives, déjà constatées avant ce module :
      - des dénominateurs incohérents d'un écran à l'autre (la presse comptée
        ici, exclue là), donc des chiffres qui se contredisent ;
      - un filtre ajouté sur un écran et oublié sur les autres.

    Un seul objet porte donc le périmètre, et un seul constructeur produit le
    WHERE. Ajouter un axe de filtrage = un champ ici, et tous les endpoints en
    bénéficient.

INVARIANT À TENIR
    Toute vue interrogeable par un filtre doit exposer les mêmes colonnes
    d'axes (iso2, region, operator_id, subsidiary_id, source_kind, source_code,
    about) et une date d'occurrence. C'est vrai de v_reviews_enriched
    (migration 002), de v_review_terms (migration 004) et de v_review_aspects
    (migration 005) ; leur correspondance de colonnes est déclarée plus bas dans
    ENRICHED, TERMS et ASPECTS.

    Un axe manquant sur une vue ne lève AUCUNE erreur : il produit un filtre
    silencieusement ignoré sur cet écran, donc deux onglets qui affichent des
    chiffres contradictoires sans que rien ne le signale. C'est pour cette
    raison que `FilterColumns` n'a aucun champ optionnel — ajouter un axe casse
    la construction de toute correspondance qui l'aurait oublié.
"""

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Plancher de date
# ---------------------------------------------------------------------------

# Aucune des sources collectées ne peut produire un avis antérieur à 2005
# (l'App Store ouvre en 2008, Google Play en 2008, Trustpilot en 2007). Les
# quelques lignes datées avant cette borne — la base en contient, dont une à
# 1970-11-22 — sont des dates mal parsées côté flux RSS, pas des données.
#
# Elles ne sont pas nombreuses mais elles sont nuisibles : elles étirent l'axe
# temporel de toutes les courbes sur cinquante ans, ce qui rend illisibles les
# variations des douze derniers mois. On les écarte donc de tout agrégat borné
# dans le temps, systématiquement et au même endroit.
DATA_FLOOR = date(2005, 1, 1)


# ---------------------------------------------------------------------------
# Correspondance de colonnes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilterColumns:
    """Où trouver chaque axe de filtrage dans une vue donnée.

    Les valeurs sont des FRAGMENTS SQL, jamais des données utilisateur : elles
    sont écrites ici en dur et interpolées dans la requête. Les valeurs
    filtrées, elles, passent exclusivement par des paramètres liés.
    """

    occurred_at: str
    iso2: str
    region: str
    operator_id: str
    subsidiary_id: str
    source_kind: str
    source_code: str
    about: str
    about_source: str


#: Colonnes de `v_reviews_enriched`, aliasée `v` (un avis par ligne).
ENRICHED = FilterColumns(
    # La vue expose created_at (date de publication à la source) et collected_at
    # (date de collecte). On raisonne sur la publication, en se rabattant sur la
    # collecte quand la source ne date pas ses contenus.
    occurred_at="COALESCE(v.created_at, v.collected_at)",
    iso2="v.iso2",
    region="v.region",
    operator_id="v.operator_id",
    subsidiary_id="v.subsidiary_id",
    source_kind="v.source_kind",
    source_code="v.source_code",
    about="v.about",
    about_source="v.about_source",
)

#: Colonnes de `v_review_terms`, aliasée `t` (un terme déclenché par ligne).
TERMS = FilterColumns(
    occurred_at="t.occurred_at",
    iso2="t.iso2",
    region="t.region",
    operator_id="t.operator_id",
    subsidiary_id="t.subsidiary_id",
    source_kind="t.source_kind",
    source_code="t.source_code",
    about="t.about",
    about_source="t.about_source",
)

#: Colonnes de `v_review_aspects`, aliasée `t` (un aspect métier par ligne).
#:
#: IDENTIQUES à TERMS, et ce n'est pas un oubli de factorisation : la vue de la
#: migration 005 a été écrite pour exposer exactement les mêmes noms de colonnes
#: que celle de la 004. C'est ce qui permet à une seule méthode de repository de
#: servir les deux dimensions — motifs lexicaux et aspects métier — sans une
#: branche de code par dimension.
#:
#: La constante existe malgré tout, plutôt qu'un simple alias `ASPECTS = TERMS` :
#: le jour où l'une des deux vues gagne un axe, c'est ici que la divergence
#: devra s'écrire, et un alias la rendrait invisible.
ASPECTS = FilterColumns(
    occurred_at="t.occurred_at",
    iso2="t.iso2",
    region="t.region",
    operator_id="t.operator_id",
    subsidiary_id="t.subsidiary_id",
    source_kind="t.source_kind",
    source_code="t.source_code",
    about="t.about",
    about_source="t.about_source",
)

#: Colonnes de la table `alerts`, aliasée `a` et jointe aux dimensions.
#:
#: Le fil d'alertes doit obéir au même périmètre que le reste du dashboard :
#: consulter le Mali et lire une alerte sur la Zambie contredit la promesse
#: d'un périmètre unique, et fait douter de tous les autres chiffres de l'écran.
#:
#: `source_kind` et `about` n'existent pas pour une alerte — elle porte un code
#: de source (`alerts.source`) mais ni la notion d'avis client contre presse, ni
#: celle d'objet de l'avis. Ces prédicats doivent donc être neutralisés avant
#: d'appeler `where()` avec cette correspondance : voir `StatsFilter.for_alerts()`.
ALERTS = FilterColumns(
    occurred_at="a.created_at",
    iso2="co.iso2",
    region="co.region",
    operator_id="sub.operator_id",
    subsidiary_id="sub.subsidiary_id",
    source_kind="NULL",  # inexploitable, neutralisé par for_alerts()
    source_code="a.source",
    about="NULL",        # idem
    about_source="NULL", # idem
)


# ---------------------------------------------------------------------------
# Périmètre
# ---------------------------------------------------------------------------

#: Sépare les avis de clients de la couverture presse. La satisfaction (note,
#: sentiment) ne se calcule que sur `customer_review` : la presse est neutre à
#: 90 % et deux fois plus volumineuse, elle divise par deux tout taux qu'on
#: calculerait sur le total.
CUSTOMER = "customer_review"
PRESS = "press"


# ---------------------------------------------------------------------------
# Objet de l'avis
# ---------------------------------------------------------------------------

#: Sépare ce qui est dit de l'OPÉRATEUR de ce qui est dit de son APPLICATION.
#:
#: LE PROBLÈME, MESURÉ (migration 019) : les boutiques d'applications pèsent
#: 83 % des avis clients, et leurs trois premiers motifs négatifs sont
#: app_bugs (3 598 avis), app_connexion (2 381) et app_ergonomie (1 715). Ce
#: sont des jugements sur un LOGICIEL. Mélangés au reste, ils faisaient monter
#: la part de négatifs d'une filiale à chaque mise à jour ratée, déclenchaient
#: un « pic de mécontentement », et l'Agent 1 en cherchait ensuite la cause du
#: côté du réseau ou de la facturation — qui n'avaient pas bougé.
#:
#: LES DEUX CÔTÉS SE CHEVAUCHENT, ET C'EST VOULU. 2 006 avis nomment les deux
#: griefs à la fois (`about = 'both'`) et affichent 93,0 % de négatifs : ce sont
#: les avis les plus argumentés du corpus. Ils comptent des deux côtés, parce
#: qu'ils contiennent réellement les deux plaintes. D'où des prédicats écrits en
#: EXCLUSION (`<> 'app'`) et non en égalité : `= 'operator'` perdrait ces 2 006
#: avis de chaque côté.
OPERATOR = "operator"
APP = "app"

#: Les deux côtés confondus — le comportement d'avant la séparation. Reste
#: accessible, mais n'est plus le défaut : c'est précisément ce mélange qui
#: produisait de fausses alertes.
BOTH_SIDES = "all"

#: Liste blanche. La valeur vient de l'URL ; sans validation, une faute de
#: frappe (`about=operatuer`) ne lèverait rien et rendrait un écran vide.
ALLOWED_ABOUT = frozenset({OPERATOR, APP, BOTH_SIDES})


@dataclass(frozen=True)
class StatsFilter:
    """Périmètre d'interrogation : quand, où, chez qui, sur quelles sources."""

    # --- Temps ---
    #: Fenêtre glissante en jours, comptée depuis `date_to`. Ignorée si
    #: `date_from` est fourni. `None` et sans `date_from` = tout l'historique.
    days: Optional[int] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None

    # --- Espace / organisation ---
    countries: tuple[str, ...] = ()      # codes ISO 3166-1 alpha-2
    regions: tuple[str, ...] = ()
    operators: tuple[int, ...] = ()      # dim_operator.operator_id
    subsidiaries: tuple[int, ...] = ()   # dim_subsidiary.subsidiary_id

    # --- Sources ---
    source_kind: Optional[str] = None    # CUSTOMER | PRESS | None (les deux)
    sources: tuple[str, ...] = ()        # codes de dim_source

    # --- Objet de l'avis ---
    #: De quoi l'avis parle : du service de la filiale, ou de son application.
    #:
    #: LE DÉFAUT EST `OPERATOR`, ET C'EST TOUTE LA CORRECTION. Un tableau de
    #: bord qui compare des filiales compare des SERVICES ; tant que ce défaut
    #: valait « les deux », 20 107 notes d'application entraient dans le taux de
    #: mécontentement de filiales dont le réseau et la facturation n'étaient pas
    #: en cause.
    #:
    #: Il est porté ICI plutôt que dans `_MEASURES` pour que `about=app` reste
    #: une question POSABLE : la satisfaction applicative se calcule alors avec
    #: exactement les mêmes mesures, sur l'autre moitié du corpus. Inscrit dans
    #: les mesures, il se serait cumulé au filtre et n'aurait laissé passer que
    #: les 2 006 avis mixtes — un chiffre faux, sans erreur pour le signaler.
    #:
    #: `BOTH_SIDES` restitue le comportement antérieur pour qui veut comparer.
    about: str = OPERATOR

    #: Exiger que l'avis ne parle QUE du côté demandé (`about = 'operator'`),
    #: au lieu de l'inclure dès qu'il en parle (`about <> 'app'`).
    #:
    #: LES DEUX MODES NE SERVENT PAS LA MÊME CHOSE, et les confondre produit
    #: deux fautes opposées :
    #:
    #:   - UN TAUX doit être INCLUSIF. Un avis qui dénonce une recharge perdue
    #:     ET un bug de connexion est un client mécontent du service ; l'exclure
    #:     du dénominateur ferait disparaître 2 006 avis — les plus argumentés
    #:     du corpus, 93 % de négatifs — et sous-estimerait le mécontentement.
    #:
    #:   - UN EXTRAIT doit être EXCLUSIF. Mesuré sur les trois pics du 16 août,
    #:     les avis cités en preuve étaient tous des avis mixtes dont le texte
    #:     parle surtout de l'application : « App is typically good... however
    #:     the app is currently not starting up at all » illustrait un « pic de
    #:     mécontentement » censé porter sur le service. Le taux était juste, la
    #:     citation le démentait — et c'est la citation que le lecteur retient.
    #:
    #: D'où un drapeau séparé plutôt qu'une quatrième valeur d'`about` : ce
    #: n'est pas un autre périmètre, c'est la même question posée pour illustrer
    #: plutôt que pour compter.
    #:
    #: EXIGE DEUX PROPRIÉTÉS, ET NON UNE (migration 021) : l'avis ne parle que
    #: du côté demandé, ET on le sait de son TEXTE (`about_source = 'aspects'`).
    #: Sans la seconde, un avis classé sur le seul défaut de sa source restait
    #: citable — mesuré : deux purs avis d'application remontés par Google Maps,
    #: dont « In any case, this app is great! », pouvaient illustrer un pic de
    #: mécontentement du service. Deux lignes sur dix mille, mais une citation
    #: est ce que le lecteur retient, et une seule suffit à ruiner l'alerte.
    about_strict: bool = False

    # --- Fiabilité ---
    #: Nombre minimal d'avis clients, TOUS TEMPS CONFONDUS, pour qu'une filiale
    #: entre dans les écrans d'analyse.
    #:
    #: Répond à un besoin explicite : ne pas encombrer les classements de
    #: filiales à deux ou trois avis, dont le taux de négatifs n'est pas
    #: comparable à celui d'une filiale à quatre cents avis.
    #:
    #: Le décompte est volontairement HORS FENÊTRE. Borné à la période
    #: affichée, il ferait entrer et sortir des filiales à chaque changement de
    #: période : la composition d'un classement dépendrait du zoom, ce qu'aucun
    #: lecteur ne peut anticiper. Le volume total est une propriété de la
    #: filiale, pas de la vue.
    #:
    #: 0 = aucun seuil (toutes les filiales, y compris celles sans aucun avis).
    min_subsidiary_reviews: int = 0

    # ------------------------------------------------------------------ Temps

    def resolved_window(self) -> tuple[date, date]:
        """Fenêtre effective, en intervalle semi-ouvert [début, fin[.

        Semi-ouvert et non fermé : une borne de fin inclusive sur des
        `timestamptz` exclurait silencieusement les avis de la journée en cours
        arrivés après 00:00:00 — soit précisément ceux que surveille un
        dashboard temps réel.
        """
        end = (self.date_to or date.today()) + timedelta(days=1)
        if self.date_from:
            start = self.date_from
        elif self.days:
            start = end - timedelta(days=self.days)
        else:
            start = DATA_FLOOR
        return max(start, DATA_FLOOR), end

    def previous_window(self) -> tuple[date, date]:
        """Fenêtre de même durée précédant immédiatement la fenêtre courante.

        C'est la référence des variations affichées sur les tuiles et de
        l'onglet Comparer : « ‑3,2 pts » n'a de sens que contre une durée égale.
        Comparer 30 jours à 90 jours produirait un écart dû à la seule durée.
        """
        start, end = self.resolved_window()
        length = (end - start).days
        return start - timedelta(days=length), start

    def for_alerts(self) -> "StatsFilter":
        """Même périmètre, débarrassé des axes qui n'ont pas de sens pour une alerte.

        Une alerte n'est ni un avis client ni un article : elle n'a ni type de
        source, ni objet. Conserver ces prédicats produirait un
        `WHERE NULL = 'press'`, toujours faux, et le fil d'alertes se viderait
        silencieusement dès qu'on touche au sélecteur de source — un écran vide
        sans cause visible.
        """
        return replace(
            self,
            source_kind=None,
            sources=(),
            about=BOTH_SIDES,
            min_subsidiary_reviews=0,
        )

    def has_time_bound(self) -> bool:
        """Vrai si l'utilisateur a réellement borné la période.

        Sert à ne pas afficher une variation contre une période précédente
        inexistante : sur « tout l'historique », la fenêtre antérieure démarre
        avant le plancher de données et ne contient rien.
        """
        return bool(self.days or self.date_from)

    # -------------------------------------------------------------- WHERE SQL

    def where(
        self,
        cols: FilterColumns = ENRICHED,
        window: Optional[tuple[date, date]] = None,
        source_kind: Optional[str] = None,
        include_time: bool = True,
    ) -> tuple[str, list[Any]]:
        """Construit la clause WHERE et ses paramètres liés.

        Args:
            cols: correspondance de colonnes de la vue interrogée.
            window: fenêtre à appliquer ; par défaut la fenêtre courante.
                Passer `previous_window()` produit la clause de comparaison.
            source_kind: force le type de source, en ignorant celui du filtre.
                Utilisé par les requêtes qui doivent isoler les avis clients
                quoi qu'ait demandé l'utilisateur (calculs de satisfaction).
            include_time: mettre à False pour ne garder que les axes
                organisationnels. Nécessaire aux indicateurs qui portent leur
                propre fenêtre — « collecté sur 24 h » doit rester sur 24 h même
                quand l'utilisateur regarde douze mois, sinon la tuile mesure
                autre chose que ce qu'annonce son libellé.

        Returns:
            (fragment SQL commençant par « WHERE », liste des paramètres).

        Les valeurs ne sont JAMAIS interpolées : chaque prédicat utilise un
        paramètre lié (`%s`), y compris les listes, passées en `= ANY(%s)`.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if include_time:
            start, end = window or self.resolved_window()
            clauses += [f"{cols.occurred_at} >= %s", f"{cols.occurred_at} < %s"]
            params += [start, end]
        else:
            # Le plancher reste appliqué : les dates aberrantes ne doivent
            # apparaître dans aucun agrégat, borné ou non.
            clauses.append(f"{cols.occurred_at} >= %s")
            params.append(DATA_FLOOR)

        # `= ANY(%s)` plutôt qu'un IN construit dynamiquement : un seul
        # paramètre lié quel que soit le nombre de valeurs, donc une requête au
        # texte stable — que PostgreSQL peut mettre en cache de plan, et qui
        # ferme la porte à toute injection par la longueur de la liste.
        if self.countries:
            clauses.append(f"{cols.iso2} = ANY(%s)")
            params.append(list(self.countries))
        if self.regions:
            clauses.append(f"{cols.region} = ANY(%s)")
            params.append(list(self.regions))
        if self.operators:
            clauses.append(f"{cols.operator_id} = ANY(%s)")
            params.append(list(self.operators))
        if self.subsidiaries:
            clauses.append(f"{cols.subsidiary_id} = ANY(%s)")
            params.append(list(self.subsidiaries))

        kind = source_kind or self.source_kind
        if kind:
            clauses.append(f"{cols.source_kind} = %s")
            params.append(kind)
        if self.sources:
            clauses.append(f"{cols.source_code} = ANY(%s)")
            params.append(list(self.sources))

        # Objet de l'avis. Par défaut écrit en EXCLUSION de l'autre côté, jamais
        # en égalité au côté demandé : les 2 006 avis qui nomment les deux
        # griefs (`about = 'both'`) doivent rester COMPTÉS des deux côtés,
        # puisqu'ils contiennent réellement les deux plaintes.
        #
        # `about_strict` renverse ce choix pour les usages qui CITENT au lieu de
        # compter : un avis mixte est un mauvais exemple, son texte parlant le
        # plus souvent de l'application.
        if self.about in (OPERATOR, APP):
            if self.about_strict:
                clauses.append(f"{cols.about} = %s")
                params.append(self.about)
                # ET on doit le SAVOIR, pas le présumer. Un avis sans aspect
                # exploitable est classé sur le défaut de sa source : c'est une
                # présomption, et c'est par là que « In any case, this app is
                # great! » (Google Maps, aucun aspect) pouvait se retrouver
                # cité sous un pic de mécontentement du service.
                #
                # Exigence réservée à la CITATION. Appliquée aux taux, elle
                # retirerait les 1 872 notes d'agences Google Maps — le socle du
                # signal pour les 130 filiales que cette source couvre seule —
                # pour rattraper deux lignes.
                clauses.append(f"{cols.about_source} = %s")
                params.append("aspects")
            else:
                clauses.append(f"{cols.about} <> %s")
                params.append(APP if self.about == OPERATOR else OPERATOR)

        # Seuil de fiabilité. Sous-requête sur `v_subsidiary_volume` (migration
        # 004) plutôt qu'un HAVING : le seuil doit s'appliquer AVANT
        # l'agrégation, y compris aux écrans qui ne groupent pas par filiale —
        # la vue d'ensemble et les motifs doivent exclure les mêmes filiales que
        # les classements, sans quoi les totaux ne se recoupent plus d'un écran
        # à l'autre.
        if self.min_subsidiary_reviews > 0:
            # Le seuil compte du CÔTÉ qu'on regarde. Comparer des applications
            # tout en exigeant 30 avis de SERVICE écarterait des filiales dont
            # l'application est abondamment notée — et le classement affiché ne
            # correspondrait plus au seuil annoncé. C'est le même invariant que
            # la migration 007 a posé pour `comparable` : le décompte de
            # fiabilité doit compter exactement comme comptent les taux.
            colonne = "avis_app" if self.about == APP else "avis_clients"
            clauses.append(
                f"{cols.subsidiary_id} IN ("
                f"SELECT subsidiary_id FROM v_subsidiary_volume "
                f"WHERE {colonne} >= %s)"
            )
            params.append(self.min_subsidiary_reviews)

        return "WHERE " + " AND ".join(clauses), params

    def aspect_scope_clause(
        self, column: str = "t.aspect_scope"
    ) -> tuple[str, list[Any]]:
        """Prédicat écartant les ASPECTS qui contredisent le côté demandé.

        POURQUOI CE N'EST PAS LE MÊME FILTRE QUE `about`. `about` porte sur
        l'AVIS, ce prédicat sur l'ASPECT. Un avis mixte reste dans le périmètre
        opérateur — il contient bien une plainte de service — mais ses aspects
        applicatifs, eux, n'ont rien à y faire.

        Sans ce prédicat, le classement des motifs de service affichait « Bugs
        de l'application » en quatrième position avec 1 030 avis, et l'Agent 1
        écrivait « Les plaintes portent surtout sur : … bugs de l'application »
        sous un titre annonçant une dégradation du service. Le chiffre était
        exact et la phrase trompeuse.

        Ne s'applique qu'à `v_review_aspects`, seule vue à porter le côté de
        l'aspect lui-même : renvoie une chaîne vide partout ailleurs.
        """
        if self.about == OPERATOR:
            return f" AND {column} <> %s", [APP]
        if self.about == APP:
            return f" AND {column} <> %s", [OPERATOR]
        return "", []

    # ------------------------------------------------------------- Description

    def describe(self) -> dict[str, Any]:
        """Périmètre appliqué, renvoyé avec chaque réponse.

        Le dashboard l'affiche sous les chiffres. Ce n'est pas décoratif : un
        taux lu sans son périmètre est un taux mal interprété, et une capture
        d'écran de soutenance sans périmètre est indéfendable.
        """
        start, end = self.resolved_window()
        return {
            "from": start.isoformat(),
            "to": (end - timedelta(days=1)).isoformat(),
            "days": (end - start).days,
            "countries": list(self.countries),
            "regions": list(self.regions),
            "operators": list(self.operators),
            "subsidiaries": list(self.subsidiaries),
            "source_kind": self.source_kind,
            "sources": list(self.sources),
            # Renvoyé avec chaque réponse, au même titre que la période : un
            # taux de mécontentement ne veut pas dire la même chose selon qu'il
            # porte sur le service ou sur l'application, et rien d'autre à
            # l'écran ne le dirait.
            "about": self.about,
            "min_subsidiary_reviews": self.min_subsidiary_reviews,
            "comparable": self.has_time_bound(),
        }


# ---------------------------------------------------------------------------
# Granularité temporelle
# ---------------------------------------------------------------------------


def pick_granularity(days: int) -> str:
    """Choisit le pas de temps d'une courbe selon la durée demandée.

    Un pas journalier sur douze mois produit 365 points sur ~600 px : le tracé
    devient du bruit et masque la tendance qu'on cherche justement à lire. On
    agrège donc, et le dashboard indique le pas retenu.

    Renvoie une unité acceptée par `date_trunc`.
    """
    if days <= 45:
        return "day"
    if days <= 400:
        return "week"
    return "month"


#: Unités autorisées dans un `date_trunc`. Le nom d'unité est interpolé dans le
#: SQL (date_trunc n'accepte pas de paramètre lié pour son premier argument) :
#: il doit donc obligatoirement être validé contre cette liste blanche.
ALLOWED_GRANULARITIES = frozenset({"day", "week", "month"})


def safe_granularity(value: Optional[str], days: int) -> str:
    """Valide une granularité demandée, ou la choisit automatiquement."""
    if value in ALLOWED_GRANULARITIES:
        return value
    return pick_granularity(days)


# ---------------------------------------------------------------------------
# Niveaux d'agrégation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Level:
    """Un niveau d'agrégation : comment regrouper et comment nommer le groupe."""

    key: str          # expression SQL de l'identifiant du groupe
    label: str        # expression SQL du libellé lisible
    extra: tuple[str, ...] = field(default_factory=tuple)  # colonnes de contexte


#: Niveaux disponibles pour les endpoints groupés (courbes, classements,
#: variations). Liste blanche : le nom du niveau vient de l'URL et sert à
#: composer du SQL, il ne doit jamais être utilisé tel quel.
LEVELS: dict[str, Level] = {
    "country": Level(key="v.country_id", label="v.country", extra=("v.iso2", "v.region")),
    "operator": Level(key="v.operator_id", label="v.operator", extra=("v.parent_group",)),
    "subsidiary": Level(
        key="v.subsidiary_id",
        label="v.subsidiary",
        extra=("v.operator", "v.country", "v.iso2"),
    ),
    "region": Level(key="v.region", label="v.region"),
    "source": Level(key="v.source_code", label="v.source", extra=("v.source_kind",)),
}


def resolve_level(name: Optional[str], default: str = "subsidiary") -> tuple[str, Level]:
    """Résout un nom de niveau venu de l'URL, ou lève une erreur explicite."""
    chosen = name or default
    level = LEVELS.get(chosen)
    if level is None:
        raise ValueError(
            f"Niveau « {chosen} » inconnu. Valeurs acceptées : "
            + ", ".join(sorted(LEVELS))
        )
    return chosen, level
