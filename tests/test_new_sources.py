"""
Tests des sources ajoutées en vague 1 : HelloPeter, GDELT, presse africaine,
et densification Google Maps. Aucun appel réseau.

Chaque test porte sur un piège VÉRIFIÉ en conditions réelles pendant la mise en
place, pas sur du parsing heureux : le 202 de HelloPeter, le refus de débit de
GDELT rendu en HTTP 200, la confusion FIPS/ISO, et le rattachement d'un article
à la mauvaise filiale d'un opérateur multi-pays.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from reviews.collectors.countries import COUNTRIES, country_names, fips_code
from reviews.collectors.gdelt import GDELTScraper, _RateLimited
from reviews.collectors.hellopeter import HelloPeterScraper
from reviews.collectors.press_feed import PressFeedScraper, _normalize
from reviews.collectors.targets import (
    googlemaps_locations, hellopeter_companies, gdelt_targets, press_matchers,
)
from reviews.domain.models import SourceEnum
from reviews.domain.sentiment import CUSTOMER_REVIEW, PRESS, domain_for_source


# ---------------------------------------------------------------------------
# Table pays : c'est là que se cachent les erreurs silencieuses
# ---------------------------------------------------------------------------

class TestCountries:
    def test_les_faux_amis_fips_ne_sont_pas_l_iso2(self):
        """FIPS != ISO 3166, et les confondre attribue au mauvais pays.

        Ces quatre couples sont ceux qui font vraiment mal : le code FIPS de
        l'un est l'ISO2 de l'autre. Si quelqu'un « simplifie » un jour la table
        en renvoyant l'ISO2, ce test tombe.
        """
        assert fips_code("ZA") == "SF"      # Afrique du Sud, PAS 'ZA'
        assert fips_code("ZM") == "ZA"      # 'ZA' désigne la Zambie chez GDELT
        assert fips_code("NG") == "NI"      # Nigeria
        assert fips_code("NE") == "NG"      # 'NG' désigne le Niger

    def test_aucun_pays_ne_partage_son_code_fips(self):
        """Deux pays avec le même FIPS = collecte fusionnée sans erreur."""
        codes = [fips for _, _, fips in COUNTRIES.values()]
        assert len(codes) == len(set(codes))

    def test_iso2_inconnu_ne_renvoie_pas_un_code_invente(self):
        """None force l'appelant à interroger sans filtre plutôt qu'à deviner."""
        assert fips_code("XX") is None
        assert fips_code("") is None

    def test_noms_francais_et_anglais_disponibles(self):
        noms = country_names("ZA")
        assert "Afrique du Sud" in noms and "South Africa" in noms

    def test_perimetre_de_operators_json_entierement_couvert(self):
        """Une filiale dont l'ISO2 manque ici est muette pour GDELT et la presse."""
        from reviews.collectors.targets import load_subsidiaries
        manquants = {
            s["iso2"].upper() for s in load_subsidiaries()
            if s.get("iso2") and s["iso2"].upper() not in COUNTRIES
        }
        assert not manquants, f"ISO2 absents de countries.py : {sorted(manquants)}"


# ---------------------------------------------------------------------------
# Domaine de sentiment : la presse ne doit pas être jugée au lexique des apps
# ---------------------------------------------------------------------------

class TestDomaineSentiment:
    def test_les_trois_sources_de_presse_utilisent_le_lexique_presse(self):
        for source in (SourceEnum.RSS_FEED, SourceEnum.GDELT, SourceEnum.PRESS_FEED):
            assert domain_for_source(source) == PRESS, source

    def test_hellopeter_est_de_la_voix_client(self):
        assert domain_for_source(SourceEnum.HELLOPETER) == CUSTOMER_REVIEW


# ---------------------------------------------------------------------------
# HelloPeter
# ---------------------------------------------------------------------------

class _FausseReponse:
    def __init__(self, status, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else ""
        self.ok = 200 <= status < 300

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class TestHelloPeter:
    LIGNE = {
        "id": 6522902,
        "created_at": "2026-08-04 15:51:48",
        "authorDisplayName": "Mamosa T",
        "review_title": "Poor service",
        "review_rating": 1,
        "review_content": "Returned the router because of poor network",
    }

    def test_init(self):
        assert HelloPeterScraper().name == "hellopeter"

    def test_reponse_202_acceptee(self, monkeypatch):
        """L'API répond 202 derrière Cloudflare, pas 200.

        C'est le piège qui a fait échouer la première vérification des slugs :
        un contrôle `status_code == 200` rejette 100 % des réponses valides.
        """
        scraper = HelloPeterScraper()
        monkeypatch.setattr(
            scraper.session, "get",
            lambda *a, **k: _FausseReponse(202, {"data": [self.LIGNE], "last_page": 1}),
        )
        payload = scraper._get_page("vodacom", 1)
        assert payload is not None and payload["data"]

    def test_404_est_une_absence_de_fiche_pas_une_panne(self, monkeypatch):
        scraper = HelloPeterScraper()
        monkeypatch.setattr(
            scraper.session, "get", lambda *a, **k: _FausseReponse(404)
        )
        assert scraper._get_page("inexistant", 1) is None

    def test_parsing_complet(self):
        scraper = HelloPeterScraper()
        created = scraper._parse_date(self.LIGNE["created_at"])
        review = scraper._to_review(self.LIGNE, "Vodacom South Africa", created)
        assert review.id == "hellopeter_6522902"
        assert review.company == "Vodacom South Africa"
        assert review.source == SourceEnum.HELLOPETER.value
        assert review.rating == 1
        assert review.author == "Mamosa T"
        assert review.created_at.tzinfo is not None

    def test_avis_sans_texte_ecarte(self):
        """Une note seule n'apporte rien à une analyse de sentiment textuelle."""
        scraper = HelloPeterScraper()
        assert scraper._to_review({**self.LIGNE, "review_content": "   "},
                                  "Vodacom South Africa", None) is None

    def test_note_hors_bornes_neutralisee_sans_perdre_l_avis(self):
        """Le modèle refuse une note hors 1-5 : la neutraliser sauve le texte."""
        scraper = HelloPeterScraper()
        review = scraper._to_review({**self.LIGNE, "review_rating": 9},
                                    "Vodacom South Africa", None)
        assert review is not None and review.rating is None

    def test_texte_tres_long_tronque_au_lieu_d_etre_rejete(self):
        scraper = HelloPeterScraper()
        review = scraper._to_review({**self.LIGNE, "review_content": "a" * 9000},
                                    "Vodacom South Africa", None)
        assert review is not None and len(review.text) <= 5000

    def test_date_illisible_ne_leve_pas(self):
        assert HelloPeterScraper()._parse_date("pas une date") is None
        assert HelloPeterScraper()._parse_date(None) is None

    def test_cibles_chargees_depuis_la_configuration(self):
        cibles = hellopeter_companies()
        assert cibles, "aucun slug HelloPeter configuré"
        # La plateforme est sud-africaine : mesuré, airtel-nigeria n'a qu'un
        # seul avis. Toute cible hors ZA serait une erreur de configuration.
        noms = {c["name"] for c in cibles}
        assert noms == {"Vodacom South Africa", "MTN South Africa",
                        "Telkom South Africa", "Cell C"}
        assert all(c["slug"] for c in cibles)


# ---------------------------------------------------------------------------
# GDELT
# ---------------------------------------------------------------------------

class TestGDELT:
    ARTICLE = {
        "url": "https://punchng.com/mtn-nigeria-clears-fx-debt/",
        "title": "MTN Nigeria Clears FX Debt",
        "seendate": "20260803T000000Z",
        "domain": "punchng.com",
        "language": "English",
        "sourcecountry": "Nigeria",
    }

    def test_init(self):
        assert GDELTScraper().name == "gdelt"

    def test_refus_de_debit_en_texte_brut_leve_au_lieu_de_rendre_zero_article(
        self, monkeypatch
    ):
        """Le refus arrive en HTTP 200 avec du TEXTE BRUT.

        Sans ce contrôle, le collecteur enregistrerait « 0 article » : une
        panne totale, silencieuse, et indiscernable d'une absence d'actualité.
        """
        scraper = GDELTScraper()
        monkeypatch.setattr(
            scraper.session, "get",
            lambda *a, **k: _FausseReponse(
                200, text="Please limit requests to one every 5 seconds"
            ),
        )
        with pytest.raises(_RateLimited):
            scraper._call_api('"MTN" sourcecountry:NI')

    def test_refus_de_debit_en_429_traite_comme_tel(self, monkeypatch):
        """RÉGRESSION — observé en collecte réelle.

        GDELT alterne entre les deux formes de refus. Le 429 tombait dans le
        `except Exception` générique : il était journalisé comme une panne de
        la filiale, sans jamais déclencher le ralentissement. Les 132 filiales
        s'enchaînaient alors en échec, pour un seul article récolté.
        """
        scraper = GDELTScraper()
        monkeypatch.setattr(
            scraper.session, "get", lambda *a, **k: _FausseReponse(429, text="")
        )
        with pytest.raises(_RateLimited):
            scraper._call_api('"MTN" sourcecountry:NI')

    def test_la_penalite_augmente_puis_retombe(self, monkeypatch):
        """Le délai s'apprend : il double au refus, se relâche au succès."""
        monkeypatch.setattr("reviews.collectors.gdelt.time.sleep", lambda _: None)
        scraper = GDELTScraper()
        base = scraper.cfg.min_interval_seconds

        assert scraper._delai_courant() == base
        scraper._penaliser()
        apres_un_refus = scraper._delai_courant()
        assert apres_un_refus > base
        scraper._penaliser()
        assert scraper._delai_courant() > apres_un_refus

        scraper._recompenser()
        assert scraper._delai_courant() < scraper.BACKOFF_MAX + base

    def test_la_penalite_est_plafonnee(self, monkeypatch):
        """Sans plafond, une longue fenêtre de bridage bloquerait le run."""
        monkeypatch.setattr("reviews.collectors.gdelt.time.sleep", lambda _: None)
        scraper = GDELTScraper()
        for _ in range(30):
            scraper._penaliser()
        assert scraper._penalite <= scraper.BACKOFF_MAX

    def test_requete_utilise_le_code_fips(self):
        scraper = GDELTScraper()
        requete = scraper._build_query(
            {"term": "Vodacom", "iso2": "ZA", "name": "Vodacom South Africa"}
        )
        assert requete == '"Vodacom" sourcecountry:SF'
        assert "sourcecountry:ZA" not in requete   # ZA = Zambie chez GDELT

    def test_pays_inconnu_donne_une_requete_sans_filtre(self):
        """Mieux vaut du bruit visible qu'une attribution fausse invisible."""
        scraper = GDELTScraper()
        requete = scraper._build_query({"term": "Orange", "iso2": "XX", "name": "X"})
        assert requete == '"Orange"'
        assert "sourcecountry" not in requete

    def test_parsing_article(self):
        scraper = GDELTScraper()
        created = scraper._parse_seendate(self.ARTICLE["seendate"])
        review = scraper._to_review(self.ARTICLE, "MTN Nigeria", created)
        assert review.company == "MTN Nigeria"
        assert review.source == SourceEnum.GDELT.value
        assert review.rating is None            # un article n'a pas de note
        assert "Nigeria" in review.text         # métadonnées conservées
        assert created == datetime(2026, 8, 3, tzinfo=timezone.utc)

    def test_article_sans_titre_ecarte(self):
        scraper = GDELTScraper()
        assert scraper._to_review({**self.ARTICLE, "title": " "}, "X", None) is None

    def test_toutes_les_filiales_ont_une_cible(self):
        cibles = gdelt_targets()
        assert len(cibles) > 100
        assert all(c["term"] and c["iso2"] and c["name"] for c in cibles)


# ---------------------------------------------------------------------------
# Presse africaine : le rattachement article → filiale
# ---------------------------------------------------------------------------

class _FausseEntree(dict):
    """feedparser expose ses champs en attributs ET en clés."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.__dict__.update(kw)


class TestPresseAfricaine:
    def test_init(self):
        scraper = PressFeedScraper()
        assert scraper.name == "press_feed"
        assert scraper._matchers, "aucune filiale reconnaissable"

    def test_normalisation_ignore_les_accents(self):
        assert _normalize("Sénégal") == _normalize("Senegal")
        assert _normalize("CÔTE D'IVOIRE") == _normalize("cote d'ivoire")

    def test_operateur_multipays_exige_un_marqueur_de_pays(self):
        """LE test central de cette source.

        « MTN » sans pays ne doit RIEN rattacher : sinon un article ghanéen
        atterrit sur les dix-sept filiales MTN à la fois, et le dashboard
        compare des filiales avec les mêmes articles partout.
        """
        scraper = PressFeedScraper()
        entree = _FausseEntree(
            title="MTN announces new data bundles", summary="", id="a1"
        )
        assert scraper._match_entries([("TechCabal", None, entree)]) == []

    def test_flux_national_sert_de_pays_quand_l_article_n_en_nomme_aucun(self):
        """Dans Nairametrics, qui ne couvre que le Nigeria, « MTN » = MTN Nigeria."""
        scraper = PressFeedScraper()
        entree = _FausseEntree(title="MTN posts higher quarterly revenue",
                               summary="", id="b1")
        noms = {r.company
                for r in scraper._match_entries([("Nairametrics", "NG", entree)])}
        assert noms == {"MTN Nigeria"}

    def test_le_texte_prime_sur_le_pays_d_edition_du_flux(self):
        """RÉGRESSION — bug observé en collecte réelle.

        « MTN Nigeria's growth engine stalled », publié par TechCentral (titre
        sud-africain), était rattaché à MTN Nigeria ET à MTN South Africa : une
        filiale se voyait imputer l'actualité d'une autre. Dès que l'article
        nomme un pays de l'opérateur, le pays d'édition du flux ne doit plus
        jouer.
        """
        scraper = PressFeedScraper()
        entree = _FausseEntree(
            title="MTN Nigeria's growth engine stalled in second quarter",
            summary="", id="b2",
        )
        noms = {r.company
                for r in scraper._match_entries([("TechCentral", "ZA", entree)])}
        assert noms == {"MTN Nigeria"}
        assert "MTN South Africa" not in noms

    def test_operateur_multipays_rattache_quand_le_pays_est_nomme(self):
        scraper = PressFeedScraper()
        entree = _FausseEntree(
            title="MTN Ghana announces new data bundles",
            summary="The operator said subscribers in Ghana would benefit.",
            id="a2",
        )
        noms = {r.company for r in scraper._match_entries([("TechCabal", None, entree)])}
        assert "MTN Ghana" in noms
        assert not any(n.startswith("MTN ") and "Ghana" not in n for n in noms)

    def test_operateur_monopays_reconnu_sans_marqueur(self):
        """Djezzy n'existe qu'en Algérie : exiger le pays perdrait l'article."""
        scraper = PressFeedScraper()
        entree = _FausseEntree(title="Djezzy lance une nouvelle offre",
                               summary="", id="a3")
        noms = {r.company for r in scraper._match_entries([("CIO Mag", None, entree)])}
        assert "Djezzy" in noms

    def test_pas_de_faux_positif_sur_un_mot_englobant(self):
        """Sans limite de mot, « Orange » se déclenche sur « oranges »."""
        scraper = PressFeedScraper()
        entree = _FausseEntree(
            title="Le marche des oranges au Senegal", summary="", id="a4"
        )
        noms = {r.company for r in scraper._match_entries([("Ecofin", None, entree)])}
        assert not any("Orange" in n for n in noms)

    def test_article_multi_filiales_emet_un_avis_par_filiale(self):
        """« Orange et MTN sanctionnés en Côte d'Ivoire » concerne les deux."""
        scraper = PressFeedScraper()
        entree = _FausseEntree(
            title="Orange et MTN sanctionnes en Cote d'Ivoire",
            summary="Le regulateur ivoirien a inflige des amendes.",
            id="a5",
        )
        noms = {r.company for r in scraper._match_entries([("JA", None, entree)])}
        assert len([n for n in noms if "Ivoire" in n or "Côte" in n]) >= 2

    def test_article_sans_operateur_connu_est_jete(self):
        scraper = PressFeedScraper()
        entree = _FausseEntree(title="La BAD finance un port", summary="", id="a6")
        assert scraper._match_entries([("Ecofin", None, entree)]) == []

    def test_resume_html_nettoye(self):
        entree = _FausseEntree(summary="<p>Texte <b>en gras</b></p>")
        assert PressFeedScraper._entry_summary(entree) == "Texte en gras"

    def test_matchers_couvrent_les_deux_regimes(self):
        matchers = press_matchers()
        mono = [m for m in matchers if not m["country_markers"]]
        multi = [m for m in matchers if m["country_markers"]]
        assert mono and multi, "les deux régimes doivent exister"


class TestNomsNonReconnaissables:
    """RÉGRESSION — un opérateur dont le nom est un mot courant.

    Découvert en intégrant Reddit, mais le défaut touchait DÉJÀ `rss_feed` et
    `press_feed`, qui s'appuient sur les mêmes `press_matchers()`.

    « WE » est l'opérateur historique égyptien et il est MONO-PAYS : la règle du
    marqueur de pays ne s'y appliquait donc pas, et `\\bwe\\b` mordait sur le
    pronom anglais dans n'importe quel texte. Mesuré avant correction :

        « We need better broadband in Lagos »  ->  ['WE Égypte']

    Un texte nigérian rattaché à une filiale égyptienne, sans aucun signe
    extérieur — exactement la faute que tout ce module existe pour empêcher.
    """

    def _filiales(self, texte, iso2=None):
        from reviews.domain.press_attribution import (
            compile_matchers, normalize, subsidiaries_named,
        )
        return subsidiaries_named(
            compile_matchers(press_matchers()), normalize(texte), iso2
        )

    def test_le_pronom_anglais_ne_rattache_plus_a_l_operateur_egyptien(self):
        assert self._filiales("We need better broadband in Lagos", "NG") == []

    def test_meme_dans_le_pays_de_l_operateur(self):
        """Le marqueur de pays ne sauve RIEN ici, et c'est le point clé.

        Dans un support égyptien, le pays fournit lui-même le marqueur : chaque
        « we » deviendrait un avis sur WE, alors que le texte parle peut-être
        de Vodafone. Le nom devait donc sortir de la reconnaissance, pas être
        soumis à une condition supplémentaire.
        """
        assert self._filiales(
            "We switched our data bundle, the network is awful", "EG"
        ) == []

    def test_le_ticker_bitcoin_ne_rattache_pas_l_operateur_botswanais(self):
        assert self._filiales("BTC bitcoin price is up, my data bundle too", "BW") == []

    def test_les_operateurs_normaux_restent_reconnus(self):
        """Le garde-fou doit rester CHIRURGICAL : il ne coupe que trois noms."""
        assert self._filiales("Vodafone Egypt raised its tariff", "EG") == [
            "Vodafone Égypte"
        ]
        assert self._filiales("Vodacom network is down", "ZA") == [
            "Vodacom South Africa"
        ]

    def test_la_liste_reste_courte_et_justifiee(self):
        """Chaque nom retiré prive une filiale de toute couverture presse et
        forum : la liste ne doit pas s'allonger par confort."""
        from reviews.collectors.targets import _NOMS_NON_RECONNAISSABLES
        assert _NOMS_NON_RECONNAISSABLES == {"we", "e&", "btc"}


# ---------------------------------------------------------------------------
# Densification Google Maps
# ---------------------------------------------------------------------------

class TestGoogleMapsMultiVilles:
    def test_sans_villes_le_comportement_est_inchange(self):
        """Le mode historique reste atteignable : une requête pays par filiale."""
        base = googlemaps_locations(cities_per_run=0)
        assert base
        assert len(base) == len({loc["query"] for loc in base})

    def test_les_villes_multiplient_les_fiches(self):
        base = googlemaps_locations(cities_per_run=0)
        dense = googlemaps_locations(cities_per_run=2)
        assert len(dense) > len(base) * 2

    def test_le_nom_de_filiale_est_preserve(self):
        """Le rattachement dimensionnel se fait sur `company` : le changer
        détacherait tous ces avis de leur filiale."""
        base = {loc["name"] for loc in googlemaps_locations(cities_per_run=0)}
        dense = {loc["name"] for loc in googlemaps_locations(cities_per_run=3)}
        assert base == dense

    def test_les_villes_tournent_d_un_jour_a_l_autre(self):
        """Sans rotation, seules les 2 premières villes seraient jamais visitées."""
        j1 = {loc["query"] for loc in
              googlemaps_locations(cities_per_run=2, today=date(2026, 1, 1))}
        j2 = {loc["query"] for loc in
              googlemaps_locations(cities_per_run=2, today=date(2026, 1, 2))}
        assert j1 != j2

    def test_meme_jour_memes_cibles(self):
        """Deux runs du même jour doivent viser pareil, sinon l'incrémental
        repart de zéro à chaque passage."""
        jour = date(2026, 3, 15)
        a = googlemaps_locations(cities_per_run=2, today=jour)
        b = googlemaps_locations(cities_per_run=2, today=jour)
        assert a == b


# ---------------------------------------------------------------------------
# Câblage : registre, configuration, dimensions
# ---------------------------------------------------------------------------

class TestCablage:
    def test_registre_et_configuration_declarent_les_memes_noms(self):
        """Une clé présente d'un seul côté = collecteur jamais lancé, ou
        « collecteur inconnu » à l'exécution."""
        from reviews.collectors import COLLECTORS
        from reviews.config import Settings

        settings = Settings()
        # Tous activés, pour comparer les ensembles de clés et non les états.
        for section in ("trustpilot", "playstore", "appstore", "googlemaps",
                        "rss_feed", "hellopeter", "gdelt", "press_feed",
                        "reddit"):
            getattr(settings, section).enabled = True
        assert set(settings.get_enabled_scrapers()) == set(COLLECTORS)

    def test_migration_007_sort_hellopeter_des_comparaisons(self):
        """HelloPeter ne doit pas entrer dans les agrégats de satisfaction.

        Mesuré : 97,7 % de négatifs contre 37 % sur Google Play. Mélangée, elle
        déplaçait la part de négatifs des filiales sud-africaines de +1,5 à
        +11,7 points selon la filiale — un décalage qui suit le rapport de
        volumes, donc qu'aucun coefficient fixe ne corrige.
        """
        from pathlib import Path
        sql = Path("migrations/007_source_comparable.sql").read_text(encoding="utf-8")
        assert "ADD COLUMN IF NOT EXISTS comparable" in sql
        assert "SET comparable = FALSE WHERE code = 'hellopeter'" in sql

    def test_les_mesures_de_satisfaction_excluent_les_sources_non_comparables(self):
        """Le garde-fou vit dans `_CLIENTS`, point de passage unique."""
        from reviews.storage.stats_repository import _CLIENTS, _MEASURES
        assert "source_comparable" in _CLIENTS
        assert "customer_review" in _CLIENTS
        # Toute mesure de satisfaction doit passer par ce fragment.
        for mesure in ("part_negatifs", "part_positifs", "note_moyenne",
                       "score_moyen", "avis_clients"):
            assert mesure in _MEASURES

    def test_le_volume_ecarte_reste_visible(self):
        """Sans cette mesure, 220 avis disparaîtraient sans explication.

        Une donnée retirée d'un calcul doit rester affichable, sinon l'écart
        entre `total` et `avis_clients + articles_presse` devient inexplicable
        pour qui lit le dashboard.
        """
        from reviews.storage.stats_repository import _MEASURES
        assert "avis_hors_comparaison" in _MEASURES

    def test_les_motifs_ne_filtrent_PAS_sur_comparable(self):
        """RÉGRESSION — le piège du « perdu des deux côtés ».

        Un troisième `kind` aurait sorti HelloPeter de la satisfaction ET des
        motifs. Or c'est là qu'elle vaut quelque chose : 886 caractères par avis
        contre 59 sur Google Play. Les vues de motifs et d'aspects doivent donc
        rester sans filtre `comparable`.
        """
        from pathlib import Path
        sql = Path("migrations/007_source_comparable.sql").read_text(encoding="utf-8")
        for vue in ("v_review_terms", "v_review_aspects"):
            debut = sql.index(f"CREATE VIEW {vue} AS")
            fin = sql.index(";", sql.index("FROM reviews r", debut))
            corps = sql[debut:fin]
            assert "comparable" not in corps, (
                f"{vue} filtre sur `comparable` : HelloPeter serait perdue "
                f"pour les motifs, ce qui est tout l'intérêt de la source"
            )

    def test_le_seuil_de_fiabilite_compte_comme_les_taux(self):
        """`min_subsidiary_reviews` doit compter le même dénominateur.

        Sinon une filiale franchit le seuil grâce à des avis qui n'entrent dans
        aucun de ses taux, et apparaît dans un classement avec un dénominateur
        bien plus petit qu'annoncé.
        """
        from pathlib import Path
        sql = Path("migrations/007_source_comparable.sql").read_text(encoding="utf-8")
        debut = sql.index("CREATE VIEW v_subsidiary_volume AS")
        corps = sql[debut:sql.index(";", debut)]
        assert "src.comparable" in corps

    def test_migration_006_declare_les_nouvelles_sources(self):
        """Une source absente de dim_source s'insère mais reste invisible."""
        from pathlib import Path
        sql = "\n".join(
            p.read_text(encoding="utf-8")
            for p in Path("migrations").glob("*.sql")
        )
        for source in SourceEnum:
            assert f"'{source.value}'" in sql, (
                f"{source.value} absent des migrations : ses avis seront "
                f"orphelins de dim_source, donc invisibles du dashboard"
            )


# ---------------------------------------------------------------------------
# Fiabilité, fraîcheur, composition — ce que le dashboard doit pouvoir dire
# ---------------------------------------------------------------------------

class TestMesuresDeConfiance:
    """Trois mesures ajoutées après constat sur la base réelle.

    Le classement des « pires opérateurs du continent » s'ouvrait sur
    « Econet Burundi — 100 % de négatifs », calculé sur UN avis de juillet 2024.
    Rien à l'écran ne permettait de s'en apercevoir.
    """

    def test_le_seuil_de_fiabilite_est_publie(self):
        """Le dashboard l'affiche pour expliquer le verdict : il doit exister."""
        from reviews.storage.stats_repository import RELIABILITY_MIN_REVIEWS
        assert RELIABILITY_MIN_REVIEWS >= 30, (
            "sous 30 avis, un seul avis fait bouger le taux de plus de 3 points"
        )

    def test_les_mesures_de_confiance_sont_dans_le_bloc_partage(self):
        """Dans `_MEASURES`, donc sur TOUS les écrans à la fois.

        Ajoutées endpoint par endpoint, elles finiraient par manquer là où on
        en a le plus besoin — le défaut que `_MEASURES` existe pour empêcher.
        """
        from reviews.storage.stats_repository import _MEASURES
        for mesure in ("fiable", "dernier_avis", "composition"):
            assert f"AS {mesure}" in _MEASURES, f"{mesure} absent de _MEASURES"

    def test_la_composition_couvre_toutes_les_sources(self):
        """Une source oubliée disparaîtrait de la composition sans erreur.

        Le lecteur croirait alors que le taux vient des seules sources listées,
        alors qu'il en agrège une de plus.
        """
        from reviews.storage.stats_repository import _COMPOSITION
        for source in SourceEnum:
            assert f"'{source.value}'" in _COMPOSITION, (
                f"{source.value} absent de la composition"
            )

    def test_la_composition_ne_compte_que_les_avis_comparables(self):
        """HelloPeter ne doit pas figurer dans une composition dont le taux
        l'exclut : la ligne et le pourcentage se contrediraient."""
        from reviews.storage.stats_repository import _COMPOSITION, _CLIENTS
        assert _CLIENTS in _COMPOSITION


class TestGdeltDistingueLesRefus:
    """GDELT refuse de DEUX façons, et les confondre coûtait cher.

    Les deux arrivent en HTTP 200 avec un corps en texte brut :
      * « Please limit requests… » — transitoire, étranger à la requête ;
      * « The specified phrase is too short. » — définitif, propre à CETTE
        requête (mesuré sur le terme « MTN », trois lettres).

    Tout corps non-JSON était lu comme un bridage. Le collecteur pénalisait
    donc le débit et, en mode unités, interrompait le passage entier pour un
    défaut ne concernant qu'une filiale — en écrasant au passage le seul
    message qui disait la vraie cause.
    """

    def _scraper_avec_reponse(self, monkeypatch, corps):
        from reviews.collectors import gdelt as mod

        s = mod.GDELTScraper()

        class _Reponse:
            status_code = 200
            text = corps

            def raise_for_status(self):
                pass

        monkeypatch.setattr(s.session, "get", lambda url, timeout=None: _Reponse())
        return s, mod

    def test_le_bridage_reste_un_bridage(self, monkeypatch):
        s, mod = self._scraper_avec_reponse(
            monkeypatch, "Please limit requests to one every 5 seconds"
        )
        with pytest.raises(mod._RateLimited):
            s._call_api("peu importe")

    def test_une_requete_invalide_nest_pas_un_bridage(self, monkeypatch):
        s, mod = self._scraper_avec_reponse(
            monkeypatch, "The specified phrase is too short."
        )
        with pytest.raises(mod._RequeteRefusee) as exc:
            s._call_api('"MTN"')
        # Le message de l'API doit survivre : c'est lui qui dit quoi corriger.
        assert "too short" in str(exc.value)

    def test_un_refus_de_requete_ne_devient_pas_un_backoff(self, monkeypatch):
        """En mode unités, seul le bridage doit rendre la main.

        Un refus définitif doit faire échouer SA seule unité, qui portera le
        message dans `collection_jobs.error_message`.
        """
        from reviews.collectors.base import CollectorBackoff

        s, mod = self._scraper_avec_reponse(
            monkeypatch, "The specified phrase is too short."
        )
        job = SimpleNamespace(company="MTN Nigeria", query="MTN", country="NG",
                              cursor={}, label="MTN Nigeria")
        with pytest.raises(mod._RequeteRefusee):
            s.collect_unit(job, save_cursor=lambda c: None)
