"""Tests de parsing des collecteurs (sans appel réseau)."""

from reviews.collectors.playstore import PlayStoreScraper
from reviews.collectors.appstore import AppStoreScraper
from reviews.collectors.google_maps import GoogleMapsScraper
from reviews.collectors.trustpilot import TrustpilotScraper


class TestPlayStore:
    def test_init(self):
        s = PlayStoreScraper()
        assert s.name == "playstore"
        assert s.retry_config is not None

    def test_parse_reviews(self):
        raw = [{"reviewId": "test-1", "content": "Bon app", "score": 5,
                "userName": "User1", "at": "2024-01-15T10:30:00Z"}]
        parsed = PlayStoreScraper()._parse_reviews(raw, {"name": "Test App"})
        assert len(parsed) == 1
        assert parsed[0].id == "test-1"
        assert parsed[0].text == "Bon app"
        assert parsed[0].rating == 5


class TestAppStore:
    ENTRY_XML = """<entry xmlns="http://www.w3.org/2005/Atom"
                          xmlns:im="http://itunes.apple.com/rss">
        <id>14325567095</id>
        <title>Application correcte</title>
        <content type="text">Le réseau est stable depuis la mise à jour</content>
        <im:rating>4</im:rating>
        <updated>2026-04-30T08:57:22-07:00</updated>
        <author><name>Awa</name></author>
    </entry>"""

    def test_init(self):
        s = AppStoreScraper()
        assert s.name == "appstore"

    def test_cibles_chargees_depuis_la_configuration(self):
        """Les apps ne sont plus codées en dur : elles viennent du JSON.

        L'INVARIANT PORTE SUR LE COUPLE (app_id, boutique), PAS SUR L'app_id.

        Ce test exigeait auparavant un app_id unique par filiale. C'était une
        précaution raisonnable mais fausse, et elle interdisait de suivre treize
        filiales Airtel : l'opérateur ne publie qu'une app, « My Airtel Africa »
        (1462268018), pour toute l'Afrique.

        La mesure a tranché (tools/verify_identifiers.py). Les flux d'avis de
        cette app ont été comparés sur quatre boutiques nationales : 49 avis en
        Tanzanie, 49 en Zambie, 15 à Madagascar, 2 aux Seychelles, et ZÉRO avis
        en commun entre deux boutiques quelconques. L'App Store sert donc des
        avis distincts par pays, et partager un app_id est légitime — à
        condition que la BOUTIQUE diffère, puisque c'est elle qui détermine de
        quel marché viennent les avis.

        Un couple (app_id, store_country) dupliqué, lui, resterait une faute :
        il attribuerait deux fois les mêmes avis.
        """
        from reviews.collectors.targets import appstore_apps

        apps = appstore_apps()
        assert apps, "aucune app App Store déclarée en configuration"
        assert all({"app_id", "name", "country"} <= set(a) for a in apps)

        couples = [(a["app_id"], a["country"]) for a in apps]
        doublons = {c for c in couples if couples.count(c) > 1}
        assert not doublons, (
            f"couple (app_id, boutique) dupliqué : {doublons} — les mêmes avis "
            f"seraient collectés deux fois"
        )

    def test_package_playstore_exclusif_a_une_filiale(self):
        """Un package Play ne doit JAMAIS être partagé entre deux filiales.

        La règle est l'inverse de celle de l'App Store, et pour une raison
        mesurée : interrogé avec `country=tz`, `zm`, `ng`, `ke` puis `ug`, le
        package `com.airtel.africa.selfcare` renvoie exactement LES MÊMES vingt
        avis (intersection complète). Le paramètre de pays ne filtre pas les
        avis sur Google Play.

        Partager un package entre filiales dupliquerait donc les mêmes avis sur
        autant de pays, gonflant les volumes et attribuant à chaque pays le
        sentiment de tous les autres — exactement ce que le dashboard sert à
        distinguer.
        """
        from reviews.collectors.targets import playstore_apps

        apps = playstore_apps()
        assert apps, "aucune app Play Store déclarée en configuration"

        ids = [a["package_id"] for a in apps]
        doublons = {i for i in ids if ids.count(i) > 1}
        assert not doublons, (
            f"package_id partagé entre plusieurs filiales : {doublons} — Play "
            f"ne segmente pas les avis par pays, ils seraient dupliqués"
        )

    def test_parse_xml_entry_accepte_un_avis_complet(self):
        """Régression : un Element ElementTree sans enfant est *falsy*.

        Le test `if not review_id or not content` rejetait donc tous les avis,
        pourtant complets (vérifié : 49/49 rejetés sur un flux réel).
        """
        import xml.etree.ElementTree as ET
        entry = ET.fromstring(self.ENTRY_XML)

        out = AppStoreScraper()._parse_xml_entry(entry)

        assert out is not None, "l'avis ne doit pas être rejeté"
        assert out["id"] == "14325567095"
        assert out["rating"] == 4
        assert out["author"] == "Awa"

    def test_parse_xml_entry_date_avec_decalage_negatif(self):
        """Apple date en '-07:00' : l'ancien parsing retombait sur utcnow()."""
        import xml.etree.ElementTree as ET
        entry = ET.fromstring(self.ENTRY_XML)

        out = AppStoreScraper()._parse_xml_entry(entry)

        assert out["created_at"].year == 2026
        assert out["created_at"].month == 4
        assert out["created_at"].day == 30


class TestGoogleMaps:
    def test_parse_rating(self):
        s = GoogleMapsScraper()
        assert s._parse_rating("Rated 4 stars out of five stars") == 4
        assert s._parse_rating(None) is None

    def test_relative_to_iso_anglais(self):
        """Les dates doivent être en anglais : d'où le hl=en dans l'URL."""
        from datetime import datetime, timezone
        s = GoogleMapsScraper()
        now = datetime(2026, 7, 21, tzinfo=timezone.utc)

        assert s._relative_to_iso("5 months ago", now).startswith("2026-02")
        assert s._relative_to_iso("a year ago", now).startswith("2025-07")
        assert s._relative_to_iso("yesterday", now).startswith("2026-07-20")
        # Une UI en français ne serait pas comprise : c'est ce que hl=en évite.
        assert s._relative_to_iso("il y a 5 mois", now) is None

    def test_parse_reviews_ignore_avis_sans_texte(self):
        s = GoogleMapsScraper()
        raw = [
            {"id": "g1", "rating_aria": "5 stars", "date_rel": "2 months ago",
             "author": "Alice", "text": "Bon accueil"},
            {"id": "g2", "rating_aria": "1 star", "date_rel": "1 day ago",
             "author": "Bob", "text": None},          # sans texte -> ignoré
        ]
        place = {"place_id": "0x1:0x2", "name": "Vodacom Shop Rosebank"}
        parsed = s._parse_reviews(raw, "Agence Test", place)
        assert [r.id for r in parsed] == ["g1"]
        assert parsed[0].rating == 5
        assert parsed[0].author == "Alice"
        # L'agence d'origine accompagne désormais chaque avis : sans elle, on ne
        # peut ni mesurer la couverture réelle ni détecter une attribution à la
        # mauvaise enseigne.
        assert parsed[0].target_id == "0x1:0x2"
        assert parsed[0].target_name == "Vodacom Shop Rosebank"
        # `company` reste la FILIALE : c'est lui qui porte le rattachement
        # dimensionnel, le remplacer par le nom de l'agence détacherait l'avis.
        assert parsed[0].company == "Agence Test"


class TestRSSFeed:
    def test_collect_dedoublonne_entre_mots_cles(self, monkeypatch):
        """Un même article remonte sur plusieurs mots-clés : une seule copie."""
        from reviews.collectors.rss_feed import RSSFeedScraper
        from reviews.domain.models import Review, SourceEnum

        s = RSSFeedScraper()
        s.OPERATORS = ["Moov Africa Benin"]
        s.KEYWORDS = ["panne", "reseau", "service"]

        def faux_fetch(operator, keyword):
            # Le même article renvoyé par les 3 mots-clés
            return [Review(id="article-1", company=operator,
                           source=SourceEnum.RSS_FEED, title="Panne",
                           text="Panne réseau signalée")]

        monkeypatch.setattr(s, "_fetch_articles", faux_fetch)

        collectes = s.collect()
        assert len(collectes) == 1
        assert collectes[0].id == "article-1"

    def test_collect_ignore_un_flux_en_erreur(self, monkeypatch):
        """Un flux qui échoue ne doit pas faire tomber toute la collecte."""
        from reviews.collectors.rss_feed import RSSFeedScraper
        from reviews.domain.models import Review, SourceEnum

        s = RSSFeedScraper()
        s.OPERATORS = ["Moov Africa Benin"]
        s.KEYWORDS = ["panne", "reseau"]

        def faux_fetch(operator, keyword):
            if keyword == "panne":
                raise RuntimeError("flux indisponible")
            return [Review(id="ok-1", company=operator,
                           source=SourceEnum.RSS_FEED, title="T", text="Texte")]

        monkeypatch.setattr(s, "_fetch_articles", faux_fetch)

        collectes = s.collect()
        assert [r.id for r in collectes] == ["ok-1"]


class TestTrustpilot:
    def test_normalize_review_champs_imbriques(self):
        """L'auteur, la date et la vérification sont imbriqués dans le JSON."""
        brut = {
            "id": "tp-1",
            "title": "Très bon service",
            "text": "Rien à redire",
            "rating": 5,
            "likes": 3,
            "consumer": {"displayName": "Angela Lin"},
            "dates": {"publishedDate": "2026-07-14T09:30:20.000Z"},
            "labels": {"verification": {"isVerified": True}},
        }
        out = TrustpilotScraper._normalize_review(brut)

        assert out["author"] == "Angela Lin"          # et non consumer brut
        assert out["created_at"] == "2026-07-14T09:30:20.000Z"
        assert out["verified"] is True

    def test_normalize_review_champs_absents(self):
        """Un avis sans consumer/dates/labels ne doit pas lever."""
        out = TrustpilotScraper._normalize_review({"id": "tp-2", "text": "ok"})
        assert out["author"] is None
        assert out["created_at"] is None
        assert out["verified"] is None

    def test_extract_ignore_reponse_de_redirection(self):
        """Une réponse 308 ne contient aucun avis : elle ne doit rien produire."""
        s = TrustpilotScraper()
        data = {"pageProps": {"__N_REDIRECT": "/review/x", "__N_REDIRECT_STATUS": 308}}
        assert s._extract_reviews_from_api_response(data) == []

    def test_parse_reviews_dedoublonne_et_garde_verified(self):
        s = TrustpilotScraper()
        raw = [
            {"id": "a", "text": "Super", "rating": 5, "author": "X",
             "created_at": None, "likes": 0, "verified": False},
            {"id": "a", "text": "Super", "rating": 5, "author": "X",
             "created_at": None, "likes": 0, "verified": False},   # doublon
        ]
        parsed = s._parse_reviews(raw, "Moov Test")
        assert len(parsed) == 1
        assert parsed[0].verified is False   # et non True en dur


class TestBudgetDeTempsDesBoutiques:
    """Le budget d'un collecteur doit suivre la TAILLE de son périmètre.

    Panne réelle du 6 au 9 août 2026 : `playstore` et `appstore` prenaient leur
    budget sur `scraping.request_timeout` (180 s), dimensionné pour UNE requête,
    alors que `collect()` parcourt tous les paquets à la suite. Tant que le
    périmètre tenait en une centaine d'apps la confusion passait — 133 s
    mesurées le 4 août. À 262 apps, la seule pause de politesse d'une seconde
    entre apps totalise 262 s : le budget était épuisé AVANT le premier appel
    réseau. Résultat : trois tentatives, trois « Opération expirée », 543 s et
    zéro avis à chaque passage, pendant cinq jours.

    Ces tests échouent dès que le périmètre repasse devant le budget — avant la
    production, cette fois.
    """

    #: Coût mesuré par app, pause de politesse comprise : 1,20 s de travail +
    #: 1 s d'attente côté Play Store, 1,66 s + 1 s côté App Store.
    COUT_PAR_APP = 2.0

    def test_le_budget_playstore_couvre_le_perimetre(self):
        from reviews.collectors.targets import playstore_apps
        from reviews.config import Settings

        budget = Settings().playstore.collector_timeout
        assert budget >= self.COUT_PAR_APP * len(playstore_apps())

    def test_le_budget_appstore_couvre_le_perimetre(self):
        from reviews.collectors.targets import appstore_apps
        from reviews.config import Settings

        budget = Settings().appstore.collector_timeout
        assert budget >= self.COUT_PAR_APP * len(appstore_apps())

    def test_le_budget_nest_pas_celui_dune_requete_unitaire(self):
        """La confusion des deux notions est la cause racine, pas le chiffre."""
        from reviews.config import Settings

        s = Settings()
        assert PlayStoreScraper().retry_config.timeout == s.playstore.collector_timeout
        assert AppStoreScraper().retry_config.timeout == s.appstore.collector_timeout
        assert PlayStoreScraper().retry_config.timeout != s.scraping.request_timeout
