"""Tests des règles d'alerting (pures : aucune BD, aucun envoi réseau).

Les tests de routage Telegram remplacent `requests.post` : rien ne part sur le
réseau, et surtout rien n'arrive dans le groupe de l'équipe pendant un `pytest`.
"""

from datetime import datetime, timedelta

from reviews.alerting import notifiers, rules
from reviews.config import AlertingConfig
from reviews.domain.models import (
    Alert, AlertSeverity, Review, ScraperResult, PipelineRun, SourceEnum,
)


def _run_with(reviews, **run_kwargs):
    sr = ScraperResult(scraper_name="trustpilot", reviews=reviews,
                       inserted_count=len(reviews), started_at=datetime.utcnow(),
                       ended_at=datetime.utcnow(), status="success")
    run = PipelineRun(run_id="r", started_at=datetime.utcnow(),
                      ended_at=datetime.utcnow(), status="success", **run_kwargs)
    run.scraper_results["trustpilot"] = sr
    return run


def _neg(i):
    return Review(id=str(i), company="Moov Benin", source=SourceEnum.TRUSTPILOT,
                  text="arnaque panne", sentiment="negative")


def _mouvement(**kw):
    """Ligne de variation, au format renvoyé par StatsRepository.movers()."""
    base = {
        "label": "Orange Mali",
        "operator": "Orange",
        "country": "Mali",
        "avis_clients": 40,
        "avis_clients_avant": 35,
        "part_negatifs": 30.0,
        "part_negatifs_avant": 10.0,
        "delta_negatifs": 20.0,
    }
    return {**base, **kw}


# ---------------------------------------------------------------------------
# Alertes TECHNIQUES — déduites du run seul
# ---------------------------------------------------------------------------


def test_zero_reviews_alert():
    run = PipelineRun(run_id="r", started_at=datetime.utcnow(),
                      ended_at=datetime.utcnow(), status="success", total_reviews=0)
    types = {a.type for a in rules.evaluate(run, AlertingConfig())}
    assert "zero_reviews" in types


def test_zero_reviews_muet_sur_un_run_mono_source():
    """Un run d'une seule source sans nouveauté n'est pas une panne.

    Depuis qu'un job planifie chaque collecteur séparément, c'est le cas
    ORDINAIRE : la plupart des sources n'ont rien de neuf à la plupart des
    passages. Laisser `zero_reviews` se déclencher produisait une alerte de
    gravité « error » par source et par cycle, doublonnant `scraper_zero` en
    moins précis — celui-ci nomme la source.
    """
    run = _run_with([])                     # une source, zéro insertion
    types = {a.type for a in rules.evaluate(run, AlertingConfig())}
    assert "zero_reviews" not in types
    assert "scraper_zero" in types          # le signal utile reste, lui


def test_zero_reviews_conserve_sur_un_run_multi_sources():
    """Sur un run qui couvre plusieurs sources, le silence général se dit."""
    run = _run_with([])
    run.scraper_results["gdelt"] = ScraperResult(
        scraper_name="gdelt", started_at=datetime.utcnow(),
        ended_at=datetime.utcnow(), status="success",
    )
    types = {a.type for a in rules.evaluate(run, AlertingConfig())}
    assert "zero_reviews" in types


def test_high_duplicates_rate_is_bounded_by_100_percent():
    """Le taux se rapporte au volume TÉLÉCHARGÉ, pas aux seuls nouveaux.

    Le cas ci-dessous est réel : 12 nouveaux pour 417 doublons. Rapporté aux
    nouveaux, le « taux » valait 3475 % — affiché juste sous la carte des
    passages qui annonçait 97,2 % pour le même run. Les deux écrans doivent
    partager le dénominateur de `taux_doublons` (stats_repository.py).
    """
    run = PipelineRun(run_id="r", started_at=datetime.utcnow(),
                      ended_at=datetime.utcnow(), status="success",
                      total_reviews=12, total_duplicates=417)
    alerte = next(a for a in rules.evaluate(run, AlertingConfig())
                  if a.type == "high_duplicates")
    assert "97.2 %" in alerte.message
    assert "3475" not in alerte.message


def test_high_duplicates_silent_below_half_of_downloads():
    """Autant de nouveaux que de doublons n'est pas un gaspillage : 50 % est le
    seuil, et il se lit sur le volume téléchargé."""
    run = PipelineRun(run_id="r", started_at=datetime.utcnow(),
                      ended_at=datetime.utcnow(), status="success",
                      total_reviews=100, total_duplicates=100)
    types = {a.type for a in rules.evaluate(run, AlertingConfig())}
    assert "high_duplicates" not in types


def test_disabled_alerting_returns_nothing():
    run = _run_with([_neg(i) for i in range(12)], total_reviews=12)
    assert rules.evaluate(run, AlertingConfig(enabled=False)) == []


def test_run_evaluation_no_longer_emits_business_alerts():
    """Un pic ne peut PAS se déduire du contenu d'un run.

    Un run collecte des avis publiés sur des décennies — la base en contient de
    1970 à 2026 dans un même run — donc la part de négatifs de son lot ne décrit
    aucune période. L'ancienne règle en tirait pourtant une alerte « pic », d'où
    une seule alerte métier sur 216. `evaluate()` ne produit désormais que des
    alertes techniques ; le métier passe par `negative_spike_alerts`.
    """
    run = _run_with([_neg(i) for i in range(50)], total_reviews=50)
    types = {a.type for a in rules.evaluate(run, AlertingConfig())}
    assert "negative_spike" not in types


# ---------------------------------------------------------------------------
# Alertes MÉTIER — un pic est une VARIATION sur une fenêtre courte
# ---------------------------------------------------------------------------


def test_spike_detected_on_a_rise():
    """Passer de 10 % à 30 % de négatifs en une semaine est un pic."""
    alerts = rules.negative_spike_alerts([_mouvement()], AlertingConfig())
    assert len(alerts) == 1
    assert alerts[0].type == "negative_spike"
    assert alerts[0].company == "Orange Mali"
    assert "10 % → 30 %" in alerts[0].message
    assert "Mali" in alerts[0].message


def test_no_spike_when_the_rise_is_small():
    """Une hausse sous le seuil est une fluctuation, pas un pic.

    C'est ce qui empêche le fil métier de se remplir de bruit : sans seuil de
    variation, la moindre oscillation hebdomadaire produirait une alerte.
    """
    petit = _mouvement(part_negatifs=14.0, part_negatifs_avant=10.0, delta_negatifs=4.0)
    assert rules.negative_spike_alerts([petit], AlertingConfig()) == []


def test_high_level_alerts_even_without_a_rise():
    """Une filiale déjà très dégradée mais STABLE doit tout de même alerter.

    Une règle de variation seule la laisserait passer indéfiniment : elle ne
    monte plus, elle est simplement mauvaise depuis toujours.
    """
    stable = _mouvement(part_negatifs=62.0, part_negatifs_avant=61.0, delta_negatifs=1.0)
    alerts = rules.negative_spike_alerts([stable], AlertingConfig())
    assert len(alerts) == 1
    assert "62 %" in alerts[0].message


def test_volume_guard_rejects_unreliable_rates():
    """Un taux calculé sur trop peu d'avis n'est pas un signal.

    Sans ce garde-fou, une filiale passant de 1 à 2 avis négatifs afficherait
    +100 points et dominerait toutes les alertes.
    """
    maigre = _mouvement(avis_clients=3, part_negatifs=67.0,
                        part_negatifs_avant=0.0, delta_negatifs=67.0)
    assert rules.negative_spike_alerts([maigre], AlertingConfig()) == []


def test_missing_rate_is_ignored():
    """Aucun avis client sur la fenêtre : rien à conclure, pas d'alerte."""
    vide = _mouvement(part_negatifs=None)
    assert rules.negative_spike_alerts([vide], AlertingConfig()) == []


def test_severity_scales_with_the_magnitude():
    """Tout ne peut pas arriver au même niveau de gravité.

    Sans cette nuance, l'échelle de gravité ne sert plus à rien et le filtre
    « Erreurs » du dashboard ne trie plus rien.
    """
    modere = _mouvement(part_negatifs=28.0, part_negatifs_avant=10.0, delta_negatifs=18.0)
    brutal = _mouvement(part_negatifs=45.0, part_negatifs_avant=5.0, delta_negatifs=40.0)

    assert rules.negative_spike_alerts([modere], AlertingConfig())[0].severity.value == "warning"
    assert rules.negative_spike_alerts([brutal], AlertingConfig())[0].severity.value == "error"


def test_thresholds_are_configurable():
    """Les seuils doivent pouvoir se régler sans toucher au code."""
    petit = _mouvement(part_negatifs=16.0, part_negatifs_avant=10.0, delta_negatifs=6.0)
    assert rules.negative_spike_alerts([petit], AlertingConfig()) == []
    assert rules.negative_spike_alerts(
        [petit], AlertingConfig(spike_delta_points=5.0)
    )


# ---------------------------------------------------------------------------
# « Trop lent » se mesure contre la cadence de la source
# ---------------------------------------------------------------------------


def _run_de(duree_heures: float, budget_heures: float | None):
    debut = datetime(2026, 8, 9, 0, 0, 0)
    return PipelineRun(
        run_id="r", started_at=debut,
        ended_at=debut + timedelta(hours=duree_heures),
        status="success", total_reviews=5,
        budget_seconds=budget_heures * 3600 if budget_heures else None,
    )


def test_slow_run_muet_quand_la_source_reste_dans_sa_cadence():
    """Google Maps : ~10 h de collecte pour une cadence de 24 h.

    Le seuil absolu d'une heure le déclarait en panne à chaque passage, alors
    qu'il tient largement son rythme. Une alerte qui sonne à tous les coups
    n'est plus lue.
    """
    types = {a.type for a in rules.evaluate(_run_de(10, 24), AlertingConfig())}
    assert "slow_run" not in types


def test_slow_run_signale_la_source_qui_depasse_sa_cadence():
    """Une source à 6 h de cadence qui en prend 7 ne rattrapera jamais.

    C'est exactement la pathologie qui a rendu l'alerting muet : une collecte
    plus longue que sa propre fenêtre de planification. C'est ce que la règle
    doit voir, et le seuil fixe ne le voyait pas — il voyait « plus d'une
    heure », ce qui n'a de sens pour aucune source en particulier.
    """
    types = {a.type for a in rules.evaluate(_run_de(7, 6), AlertingConfig())}
    assert "slow_run" in types


def test_slow_run_retombe_sur_une_heure_sans_budget():
    """Exécution manuelle : pas de cadence connue, seuil historique conservé."""
    assert "slow_run" in {a.type for a in rules.evaluate(_run_de(2, None), AlertingConfig())}
    assert "slow_run" not in {a.type for a in rules.evaluate(_run_de(0.5, None), AlertingConfig())}


# ---------------------------------------------------------------------------
# Routage Telegram : la gravité ne suffit pas à trier
# ---------------------------------------------------------------------------


def _cfg_telegram(**kw):
    return AlertingConfig(telegram_bot_token="jeton", telegram_chat_id="-1", **kw)


def _alerte(type_: str, severite=AlertSeverity.WARNING):
    return Alert(type=type_, severity=severite, title="t", message="m")


def test_telegram_ne_pousse_pas_les_alertes_de_collecte_courantes():
    """`scraper_zero` est un « warning » et les trois quarts de la base.

    Filtré sur la seule gravité, le groupe recevait des dizaines de « zéro
    nouvel avis » par jour et le pic de mécontentement passait au milieu sans
    être vu. Aucune information n'est perdue : l'alerte reste persistée et
    visible dans le dashboard.

    Aucun réseau n'est touché ici : le filtre rejette avant l'appel HTTP.
    """
    notifieur = notifiers.TelegramNotifier(_cfg_telegram())
    assert notifieur.send(_alerte("scraper_zero")) is False
    assert notifieur.send(_alerte("high_duplicates")) is False
    assert notifieur.send(_alerte("collect_errors")) is False


def test_telegram_pousse_le_metier_et_les_pannes_franches(monkeypatch):
    envoyes = []

    class _Reponse:
        status_code = 200
        text = ""

    def _post(url, **kwargs):
        envoyes.append(kwargs.get("json", {}))
        return _Reponse()

    monkeypatch.setattr(notifiers.requests, "post", _post)
    notifieur = notifiers.TelegramNotifier(_cfg_telegram())

    assert notifieur.send(_alerte("negative_spike")) is True
    assert notifieur.send(_alerte("run_failed", AlertSeverity.ERROR)) is True
    assert notifieur.send(_alerte("scraper_failed", AlertSeverity.ERROR)) is True
    assert len(envoyes) == 3


def test_telegram_filtre_configurable(monkeypatch):
    """La liste se règle sans toucher au code, et `*` la désactive."""
    monkeypatch.setattr(notifiers.requests, "post",
                        lambda url, **kw: type("R", (), {"status_code": 200, "text": ""})())

    choisi = notifiers.TelegramNotifier(_cfg_telegram(telegram_alert_types="scraper_zero"))
    assert choisi.send(_alerte("scraper_zero")) is True
    assert choisi.send(_alerte("negative_spike")) is False

    tout = notifiers.TelegramNotifier(_cfg_telegram(telegram_alert_types="*"))
    assert tout.send(_alerte("scraper_zero")) is True


def test_telegram_garde_le_filtre_de_gravite():
    """Le filtre par type s'AJOUTE au seuil de gravité, il ne le remplace pas."""
    notifieur = notifiers.TelegramNotifier(_cfg_telegram(telegram_min_severity="error"))
    assert notifieur.send(_alerte("negative_spike", AlertSeverity.WARNING)) is False
