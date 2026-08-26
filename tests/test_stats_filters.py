"""
Tests du contrat de filtre du dashboard — purs, aucune BD.

Ce module concentre les règles qui, si elles cassent, produisent des chiffres
faux SANS provoquer d'erreur : une fenêtre mal calculée, une comparaison contre
une durée inégale, une valeur interpolée dans le SQL au lieu d'être liée. Ce
sont précisément les défauts qu'aucun test d'intégration ne rattrape, parce que
l'API répond 200 dans tous ces cas.
"""

from datetime import date, timedelta

import pytest

from reviews.storage.filters import (
    ALERTS,
    ALLOWED_GRANULARITIES,
    BOTH_SIDES,
    CUSTOMER,
    DATA_FLOOR,
    ENRICHED,
    PRESS,
    TERMS,
    StatsFilter,
    pick_granularity,
    resolve_level,
    safe_granularity,
)

TODAY = date(2026, 7, 29)


# ---------------------------------------------------------------------------
# Fenêtre temporelle
# ---------------------------------------------------------------------------


def test_window_is_half_open_and_includes_today():
    """La borne de fin est exclusive et vaut demain.

    Une borne inclusive à aujourd'hui exclurait les avis arrivés après
    00:00:00 — soit exactement ceux qu'un dashboard temps réel surveille.
    """
    start, end = StatsFilter(days=30, date_to=TODAY).resolved_window()
    assert end == TODAY + timedelta(days=1)
    assert (end - start).days == 30


def test_date_from_takes_precedence_over_days():
    f = StatsFilter(days=7, date_from=date(2026, 1, 1), date_to=TODAY)
    start, _ = f.resolved_window()
    assert start == date(2026, 1, 1)


def test_unbounded_window_starts_at_data_floor():
    """Sans période, la fenêtre part du plancher, pas de l'époque Unix."""
    start, _ = StatsFilter(date_to=TODAY).resolved_window()
    assert start == DATA_FLOOR


def test_window_is_clamped_to_data_floor():
    """Une demande antérieure au plancher est ramenée au plancher.

    C'est ce qui écarte les dates mal parsées des flux RSS (la base en contient
    une à 1970-11-22) : sans ce garde-fou, elles étirent l'axe de toutes les
    courbes sur cinquante ans et rendent illisibles les douze derniers mois.
    """
    start, _ = StatsFilter(date_from=date(1970, 1, 1), date_to=TODAY).resolved_window()
    assert start == DATA_FLOOR


def test_previous_window_has_equal_length_and_is_adjacent():
    """La période de comparaison doit avoir EXACTEMENT la même durée.

    Comparer 30 jours à 90 jours produirait un écart dû à la seule durée, et une
    variation affichée en « pts » serait alors un artefact.
    """
    f = StatsFilter(days=30, date_to=TODAY)
    start, end = f.resolved_window()
    prev_start, prev_end = f.previous_window()

    assert (end - start).days == (prev_end - prev_start).days
    assert prev_end == start  # contiguës, sans trou ni chevauchement


def test_has_time_bound_distinguishes_comparable_periods():
    assert StatsFilter(days=30).has_time_bound() is True
    assert StatsFilter(date_from=date(2026, 1, 1)).has_time_bound() is True
    # Tout l'historique : il n'existe pas de « période précédente », et une
    # variation calculée contre une fenêtre vide serait un faux chiffre.
    assert StatsFilter().has_time_bound() is False
    assert StatsFilter(countries=("SN",)).has_time_bound() is False


# ---------------------------------------------------------------------------
# Construction du WHERE
# ---------------------------------------------------------------------------


def test_values_are_bound_never_interpolated():
    """Aucune valeur de filtre ne doit apparaître dans le texte du SQL.

    C'est la garantie anti-injection du module : seuls des noms de colonnes
    écrits en dur sont interpolés, tout le reste passe en paramètre lié.
    """
    f = StatsFilter(
        days=30,
        date_to=TODAY,
        countries=("SN", "CI"),
        operators=(7, 4),
        subsidiaries=(12,),
        regions=("Afrique de l'Ouest",),
        source_kind=CUSTOMER,
        sources=("google_play",),
    )
    sql, params = f.where()

    for forbidden in ("SN", "CI", "google_play", "Afrique", "2026"):
        assert forbidden not in sql, f"{forbidden!r} interpolé dans le SQL"

    assert sql.count("%s") == len(params)
    assert ["SN", "CI"] in params
    assert [7, 4] in params


def test_where_omits_unset_axes():
    """Un axe non renseigné ne doit produire aucun prédicat.

    `about` fait exception et c'est délibéré : son défaut n'est pas « aucun
    filtre » mais `operator` (migration 019). Il est donc neutralisé ici pour
    que le test continue de porter sur ce qu'il vise — les axes laissés vides.
    """
    sql, params = StatsFilter(days=7, date_to=TODAY, about=BOTH_SIDES).where()
    assert sql.count("%s") == 2  # les deux bornes de date, rien d'autre
    assert len(params) == 2
    for column in ("iso2", "operator_id", "subsidiary_id", "source_kind", "about"):
        assert column not in sql


def test_include_time_false_keeps_organisation_axes_and_floor():
    """Sans borne de période, les filtres d'organisation ET le plancher restent.

    C'est le mode de la tuile « collecté sur 24 h » : elle porte sa propre
    fenêtre mais doit parler du même périmètre que le reste de l'écran.
    """
    f = StatsFilter(days=365, date_to=TODAY, countries=("SN",), about=BOTH_SIDES)
    sql, params = f.where(include_time=False)
    assert params[0] == DATA_FLOOR
    assert "iso2" in sql
    assert sql.count("%s") == 2  # plancher + pays


def test_source_kind_override_wins_over_filter():
    """Les calculs de satisfaction doivent pouvoir forcer les avis clients.

    Sinon un utilisateur filtrant sur la presse verrait une note moyenne
    calculée sur des articles, qui n'ont pas d'étoiles.
    """
    f = StatsFilter(days=30, date_to=TODAY, source_kind=PRESS)
    _, params = f.where(source_kind=CUSTOMER)
    assert CUSTOMER in params
    assert PRESS not in params


def test_previous_window_where_differs_only_by_dates():
    f = StatsFilter(days=30, date_to=TODAY, countries=("SN",))
    current_sql, current_params = f.where()
    prev_sql, prev_params = f.where(window=f.previous_window())

    assert current_sql == prev_sql  # même forme, donc même plan de requête
    assert current_params[:2] != prev_params[:2]
    assert current_params[2:] == prev_params[2:]


def test_terms_columns_cover_the_same_axes_as_enriched():
    """Les deux vues doivent exposer les mêmes axes.

    C'est l'invariant qui permet au constructeur de WHERE de s'appliquer à
    v_review_terms sans aucun cas particulier. Si une vue prend du retard sur
    l'autre, l'onglet Motifs cesse silencieusement d'honorer un filtre.
    """
    assert set(vars(ENRICHED)) == set(vars(TERMS))
    for axis, expression in vars(TERMS).items():
        assert expression, f"axe {axis} non défini pour v_review_terms"


def test_describe_reports_the_applied_scope():
    f = StatsFilter(days=30, date_to=TODAY, countries=("SN",))
    d = f.describe()
    assert d["from"] == (TODAY - timedelta(days=29)).isoformat()
    assert d["to"] == TODAY.isoformat()  # borne rendue inclusive pour l'affichage
    assert d["days"] == 30
    assert d["countries"] == ["SN"]
    assert d["comparable"] is True


# ---------------------------------------------------------------------------
# Granularité
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "days,expected",
    [(1, "day"), (30, "day"), (45, "day"), (46, "week"), (400, "week"), (401, "month")],
)
def test_granularity_follows_duration(days, expected):
    """Un pas journalier sur douze mois produit 365 points illisibles."""
    assert pick_granularity(days) == expected


def test_safe_granularity_rejects_anything_off_whitelist():
    """La granularité est interpolée dans date_trunc : la liste blanche est la
    seule protection possible, date_trunc n'acceptant pas de paramètre lié."""
    assert safe_granularity("month", 10) == "month"
    assert safe_granularity("DROP TABLE reviews", 10) == "day"
    assert safe_granularity(None, 500) == "month"
    assert safe_granularity("", 10) in ALLOWED_GRANULARITIES


# ---------------------------------------------------------------------------
# Niveaux d'agrégation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", ["country", "operator", "subsidiary", "region", "source"])
def test_known_levels_resolve(level):
    name, resolved = resolve_level(level)
    assert name == level
    assert resolved.key and resolved.label


def test_unknown_level_is_refused_not_interpolated():
    """Le niveau vient de l'URL et sert à composer un GROUP BY : il doit être
    refusé explicitement, jamais atteindre le SQL."""
    with pytest.raises(ValueError) as excinfo:
        resolve_level("reviews; DROP TABLE reviews")
    assert "inconnu" in str(excinfo.value)


def test_level_defaults_to_subsidiary():
    assert resolve_level(None)[0] == "subsidiary"


# ---------------------------------------------------------------------------
# Périmètre du fil d'alertes
# ---------------------------------------------------------------------------


def test_for_alerts_drops_source_predicates():
    """Une alerte n'a pas de type de source : les prédicats doivent disparaître.

    Les conserver produirait un `WHERE NULL = 'press'`, toujours faux : le fil
    d'alertes se viderait dès qu'on touche au sélecteur de source, sans qu'aucun
    message n'explique l'écran vide.
    """
    f = StatsFilter(days=30, date_to=TODAY, source_kind=PRESS, sources=("rss_feed",))
    alerts = f.for_alerts()

    assert alerts.source_kind is None
    assert alerts.sources == ()
    # Les autres axes sont intégralement conservés : c'est tout l'intérêt.
    assert alerts.days == f.days
    assert alerts.date_to == f.date_to

    sql, params = alerts.where(cols=ALERTS)
    assert "source" not in sql
    assert PRESS not in params


def test_for_alerts_keeps_organisation_axes():
    """Le fil d'alertes doit suivre le pays et l'opérateur affichés à côté."""
    f = StatsFilter(days=30, date_to=TODAY, countries=("ML",), operators=(7,))
    sql, params = f.for_alerts().where(cols=ALERTS)

    assert "co.iso2" in sql
    assert "sub.operator_id" in sql
    assert ["ML"] in params
    assert [7] in params


def test_alerts_columns_cover_the_same_axes():
    """Même invariant que pour v_review_terms : aucun axe ne doit manquer.

    Un axe absent ici serait un filtre silencieusement ignoré par le fil
    d'alertes, donc un écran qui affiche des chiffres sur un pays et des
    alertes sur un autre.
    """
    assert set(vars(ALERTS)) == set(vars(ENRICHED))


# ---------------------------------------------------------------------------
# Seuil de fiabilité
# ---------------------------------------------------------------------------


def test_threshold_absent_when_zero():
    """Sans seuil, aucune sous-requête ne doit alourdir la clause."""
    sql, _ = StatsFilter(days=30, date_to=TODAY).where()
    assert "v_subsidiary_volume" not in sql


def test_threshold_filters_on_total_volume_not_the_window():
    """Le seuil porte sur le volume TOTAL de la filiale, hors fenêtre.

    Borné à la période affichée, il ferait entrer et sortir des filiales à
    chaque changement de période : la composition d'un classement dépendrait du
    zoom. C'est la garantie que la sous-requête n'hérite d'aucune borne de date.
    """
    sql, params = StatsFilter(
        days=7, date_to=TODAY, min_subsidiary_reviews=10
    ).where()

    assert "v_subsidiary_volume" in sql
    assert "avis_clients >= %s" in sql
    assert 10 in params
    # La sous-requête ne doit porter aucun prédicat de date : elle compte tout
    # l'historique de la filiale.
    subquery = sql[sql.index("v_subsidiary_volume"):]
    assert "created_at" not in subquery and "occurred_at" not in subquery


def test_threshold_applies_to_every_view():
    """Le seuil doit s'appliquer aussi à la vue des motifs.

    Sinon l'onglet Motifs agrégerait des termes issus de filiales que tous les
    autres écrans ont exclues, et ses totaux cesseraient de se recouper avec
    ceux de la vue d'ensemble.
    """
    for cols in (ENRICHED, TERMS):
        sql, params = StatsFilter(
            days=30, date_to=TODAY, min_subsidiary_reviews=10
        ).where(cols=cols)
        assert "v_subsidiary_volume" in sql
        assert cols.subsidiary_id in sql


def test_threshold_is_dropped_for_alerts():
    """Une alerte n'a pas de volume : le seuil n'a pas de sens pour elle.

    Le conserver ferait disparaître les alertes des filiales sous le seuil —
    or ce sont précisément celles dont on veut être averti quand elles se
    mettent à générer du mécontentement.
    """
    f = StatsFilter(days=30, date_to=TODAY, min_subsidiary_reviews=10)
    sql, _ = f.for_alerts().where(cols=ALERTS)
    assert "v_subsidiary_volume" not in sql


def test_describe_reports_the_threshold():
    """Un chiffre lu sans son seuil est un chiffre mal interprété."""
    d = StatsFilter(days=30, date_to=TODAY, min_subsidiary_reviews=10).describe()
    assert d["min_subsidiary_reviews"] == 10
