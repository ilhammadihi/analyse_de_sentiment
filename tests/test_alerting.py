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


def test_telegram_retient_l_identifiant_du_message_envoye():
    """SANS LUI, UN MESSAGE PARTI À TORT EST DÉFINITIF.

    Quatre alertes fondées sur des avis mal attribués sont parties dans le
    groupe et n'ont pas pu être effacées : le code postait, lisait le statut
    HTTP, et jetait la réponse — donc l'identifiant, seule prise permettant de
    demander une suppression à l'API.
    """
    import reviews.alerting.notifiers as mod

    def _faux_post(url, **kw):
        return type("R", (), {
            "status_code": 200, "text": "",
            "json": lambda self=None: {"ok": True, "result": {"message_id": 4242}},
        })()

    original = mod.requests.post
    mod.requests.post = _faux_post
    try:
        n = notifiers.TelegramNotifier(_cfg_telegram())
        assert n.send(_alerte("negative_spike")) is True
        assert n.last_message_id == 4242
    finally:
        mod.requests.post = original


def test_un_identifiant_illisible_ne_fait_pas_echouer_l_envoi():
    """L'envoi a RÉUSSI : perdre l'identifiant coûte la possibilité de retirer
    ce message plus tard, jamais la notification elle-même."""
    import reviews.alerting.notifiers as mod

    def _sans_json(url, **kw):
        return type("R", (), {
            "status_code": 200, "text": "",
            "json": lambda self=None: (_ for _ in ()).throw(ValueError("pas du JSON")),
        })()

    original = mod.requests.post
    mod.requests.post = _sans_json
    try:
        n = notifiers.TelegramNotifier(_cfg_telegram())
        assert n.send(_alerte("negative_spike")) is True
        assert n.last_message_id is None
    finally:
        mod.requests.post = original


def test_telegram_garde_le_filtre_de_gravite():
    """Le filtre par type s'AJOUTE au seuil de gravité, il ne le remplace pas."""
    notifieur = notifiers.TelegramNotifier(_cfg_telegram(telegram_min_severity="error"))
    assert notifieur.send(_alerte("negative_spike", AlertSeverity.WARNING)) is False


# ---------------------------------------------------------------------------
# Extraits joints aux alertes de pic
# ---------------------------------------------------------------------------


class _StatsAvecVerbatims:
    """Dépôt minimal : rend des avis fixes et retient les périmètres demandés."""

    def __init__(self, reviews):
        self.reviews = reviews
        self.scopes = []

    def verbatims(self, f, polarity="negative", limit=20):
        self.scopes.append((f, polarity, limit))
        return {"reviews": self.reviews[:limit]}


def _manager_avec(stats, mouvements, alertes):
    from reviews.alerting.manager import AlertManager

    mgr = AlertManager.__new__(AlertManager)
    mgr.cfg = _cfg_telegram()
    mgr.stats_repo = stats
    mgr.alert_repo = None
    mgr.notifiers = []
    mgr._attacher_extraits(alertes, mouvements)
    return mgr


def test_les_extraits_sont_bornes_dates_et_tronques():
    """Deux avis au plus, chacun daté et tronqué.

    LA DATE N'EST PAS DÉCORATIVE : elle prouve que l'avis cité appartient bien
    à la fenêtre du pic. Sans elle, rien ne distingue une alerte fondée sur des
    avis d'hier d'une alerte illustrée par des avis de mars — et c'est le
    premier doute qu'un lecteur exprime.
    """
    from datetime import date as _date

    avis = [
        {"text": f"Avis numero {i} " + "x" * 400, "source": "Google Play",
         "occurred_at": _date(2026, 8, 8)}
        for i in range(6)
    ]
    stats = _StatsAvecVerbatims(avis)
    alerte = _alerte("negative_spike")
    alerte.company = "MTN Ghana"

    _manager_avec(stats, [{"label": "MTN Ghana", "key": 42}], [alerte])

    assert len(alerte.evidence) == 2
    assert all(len(e) < 260 for e in alerte.evidence), "extrait non tronqué"
    assert alerte.evidence[0].startswith("8 août ·"), "date absente ou mal placée"
    assert alerte.evidence[0].endswith("— Google Play")


def test_les_avis_sont_cherches_sur_la_fenetre_du_pic():
    """RÉGRESSION VISÉE : une alerte de pic sur sept jours illustrée par des
    avis plus anciens ferait douter du chiffre lui-même. Le périmètre transmis
    au dépôt doit porter EXACTEMENT la fenêtre configurée pour l'alerting."""
    stats = _StatsAvecVerbatims([])
    alerte = _alerte("negative_spike")
    alerte.company = "MTN Ghana"

    mgr_cfg = _cfg_telegram()
    _manager_avec(stats, [{"label": "MTN Ghana", "key": 42}], [alerte])

    scope, polarite, limite = stats.scopes[0]
    assert scope.days == mgr_cfg.spike_window_days
    assert polarite == "negative" and limite == 2


def test_une_date_illisible_ne_fait_pas_echouer_l_extrait():
    """Donnée abîmée : mieux vaut « ? » qu'une alerte perdue."""
    stats = _StatsAvecVerbatims([{"text": "Reseau coupe", "source": "X",
                                  "occurred_at": None}])
    alerte = _alerte("negative_spike")
    alerte.company = "MTN Ghana"
    _manager_avec(stats, [{"label": "MTN Ghana", "key": 42}], [alerte])
    assert alerte.evidence and alerte.evidence[0].startswith("? ·")


def test_les_extraits_sont_cherches_sur_LA_filiale_alertee():
    """RÉGRESSION VISÉE : la règle ne rend que le LIBELLÉ de la filiale, jamais
    son identifiant. Sans le rapprochement avec les lignes de mouvement, on
    interrogerait le périmètre global et on citerait les avis d'une AUTRE
    filiale sous le nom de celle qui alerte."""
    stats = _StatsAvecVerbatims([{"text": "Reseau coupe", "source": "App Store"}])
    alerte = _alerte("negative_spike")
    alerte.company = "MTN Ghana"

    _manager_avec(
        stats,
        [{"label": "Orange Mali", "key": 7}, {"label": "MTN Ghana", "key": 42}],
        [alerte],
    )

    scope = stats.scopes[0][0]
    assert scope.subsidiaries == (42,), "mauvaise filiale interrogée"


def test_une_filiale_introuvable_ne_declenche_aucune_requete():
    """Mieux vaut une alerte sans extrait qu'une alerte illustrée par les avis
    de quelqu'un d'autre."""
    stats = _StatsAvecVerbatims([{"text": "Peu importe", "source": "X"}])
    alerte = _alerte("negative_spike")
    alerte.company = "Filiale inconnue"

    _manager_avec(stats, [{"label": "MTN Ghana", "key": 42}], [alerte])

    assert stats.scopes == [] and alerte.evidence == []


def test_un_depot_en_echec_laisse_l_alerte_partir_sans_extrait():
    """Une notification sans extrait reste utile ; une collecte qui échoue à
    cause d'un extrait manquant ne l'est pas."""
    class _Casse:
        def verbatims(self, *a, **kw):
            raise RuntimeError("base indisponible")

    alerte = _alerte("negative_spike")
    alerte.company = "MTN Ghana"
    _manager_avec(_Casse(), [{"label": "MTN Ghana", "key": 42}], [alerte])
    assert alerte.evidence == []


def test_avis_et_evenements_sont_deux_sections_distinctes():
    """Un avis dit ce qu'un client RESSENT, un article dit ce qui s'est PASSÉ.

    Mêlés dans une même liste, une décision de régulateur se lirait comme une
    plainte d'abonné — et la portée de la presse (« ce pays » plutôt que
    « cette filiale ») disparaîtrait avec la distinction.
    """
    envoye = {}

    def _faux_post(url, **kw):
        envoye["text"] = kw["json"]["text"]
        return type("R", (), {"status_code": 200, "text": ""})()

    import reviews.alerting.notifiers as mod

    original = mod.requests.post
    mod.requests.post = _faux_post
    try:
        alerte = _alerte("negative_spike")
        alerte.evidence = ["Coupures repetees — Google Play"]
        alerte.events = ["2026-07-31 · inwi et Huawei etendent la couverture"]
        alerte.events_scope = "cette filiale"
        assert notifiers.TelegramNotifier(_cfg_telegram()).send(alerte) is True
    finally:
        mod.requests.post = original

    texte = envoye["text"]
    assert "Ce que disent les clients" in texte
    assert "Ce qui pourrait coïncider" in texte
    # La portée accompagne la section presse, pas la section avis.
    assert "presse : cette filiale" in texte
    # « pourrait coïncider » et jamais « à cause de » : un article contemporain
    # n'est pas une cause démontrée.
    assert "à cause de" not in texte


def test_telegram_rend_les_extraits_en_citation():
    """Le liseré de <blockquote> sépare la parole du client de la mesure ;
    sans lui, les deux se lisent comme un seul bloc de texte."""
    envoye = {}

    def _faux_post(url, **kw):
        envoye["text"] = kw["json"]["text"]
        return type("R", (), {"status_code": 200, "text": ""})()

    import reviews.alerting.notifiers as mod

    original = mod.requests.post
    mod.requests.post = _faux_post
    try:
        alerte = _alerte("negative_spike")
        alerte.evidence = ["Coupures repetees — Google Play", "Facture fausse — App Store"]
        notifieur = notifiers.TelegramNotifier(_cfg_telegram())
        assert notifieur.send(alerte) is True
    finally:
        mod.requests.post = original

    assert envoye["text"].count("<blockquote>") == 2
    assert "Ce que disent les clients" in envoye["text"]


# ---------------------------------------------------------------------------
# Borne de fraîcheur du fil d'alertes
# ---------------------------------------------------------------------------


class _CurseurEspion:
    """Curseur minimal : retient la requête et ses paramètres, ne rend rien."""

    def __init__(self):
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql, self.params = sql, list(params or [])

    def fetchall(self):
        return []


class _BaseEspionne:
    def __init__(self):
        self.curseur = _CurseurEspion()

    def cursor(self, dict_rows: bool = False):
        from contextlib import contextmanager

        @contextmanager
        def _ouvrir():
            yield self.curseur

        return _ouvrir()


def _lister(**kwargs):
    from reviews.storage.repository import AlertRepository

    db = _BaseEspionne()
    AlertRepository(db).list_recent(**kwargs)
    return db.curseur


def test_sans_borne_de_fraicheur_aucune_clause_de_date_n_est_posee():
    """L'absence de borne doit rester le comportement par défaut : d'autres
    appelants (supervision, export) veulent l'historique complet."""
    curseur = _lister()
    assert "a.created_at >=" not in curseur.sql


def test_la_borne_de_fraicheur_est_comptee_depuis_maintenant():
    """Elle borne l'ÂGE de l'alerte, pas la fenêtre d'analyse.

    RÉGRESSION VISÉE : sans elle, l'onglet Alertes empile indéfiniment — il
    affichait encore un pic du 27 juillet le 10 août, présenté comme actuel au
    milieu d'alertes du matin même. Un pic se calcule sur sept jours glissants ;
    passé ce délai, la période qu'il décrit ne recouvre plus aujourd'hui.
    """
    from datetime import timezone

    curseur = _lister(max_age_days=7)
    assert "a.created_at >= %s" in curseur.sql

    seuil = next(p for p in curseur.params if isinstance(p, datetime))
    attendu = datetime.now(timezone.utc) - timedelta(days=7)
    # Tolérance large : on vérifie la règle, pas l'instant d'exécution du test.
    assert abs((seuil - attendu).total_seconds()) < 60


def test_la_borne_de_fraicheur_s_ajoute_aux_autres_filtres():
    """Elle ne remplace ni la gravité ni la nature : les trois se cumulent,
    sinon afficher « critiques récentes » retomberait sur « toutes récentes »."""
    curseur = _lister(max_age_days=7, severity="error", kind="business")
    assert "a.created_at >= %s" in curseur.sql
    assert "a.severity = %s" in curseur.sql
    assert "a.type = ANY(%s)" in curseur.sql
