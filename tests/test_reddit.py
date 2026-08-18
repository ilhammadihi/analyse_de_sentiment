"""Tests du collecteur Reddit. Aucun appel réseau.

Chaque test porte sur un piège VÉRIFIÉ pendant la mise en place le 7 août 2026,
pas sur du parsing heureux :

  * le refus de débit, qui arrive tantôt en 429 tantôt déguisé en HTTP 200 ;
  * le pied de page « submitted by /u/x [link] [comments] » que Reddit ajoute à
    chaque message et qui polluerait l'analyse de sentiment ;
  * le rattachement d'un fil à la mauvaise filiale d'un opérateur multi-pays ;
  * le forum généraliste, où « orange » est un fruit.
"""

from datetime import datetime, timezone

import pytest

from reviews.collectors.reddit import RedditScraper, _RateLimited
from reviews.collectors.targets import reddit_targets
from reviews.domain.models import SourceEnum
from reviews.domain.sentiment import CUSTOMER_REVIEW, domain_for_source


# ---------------------------------------------------------------------------
# Doublures
# ---------------------------------------------------------------------------

class _FausseReponse:
    """Réponse HTTP minimale, avec en-têtes (le débit se lit dedans)."""

    def __init__(self, status, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FausseEntree(dict):
    """feedparser expose ses champs en attributs ET en clés."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.__dict__.update(kw)


def _contenu(message_html: str) -> list[dict]:
    """Reproduit la forme réelle d'un `<content>` Reddit, pied de page inclus."""
    return [{"value": (
        f'<!-- SC_OFF --><div class="md">{message_html}</div><!-- SC_ON -->'
        ' &#32; submitted by &#32; '
        '<a href="https://www.reddit.com/user/abonne"> /u/abonne </a> '
        '<br/> <span><a href="https://redd.it/x">[link]</a></span> '
        '<span><a href="https://redd.it/x">[comments]</a></span>'
    )}]


ZA = {"subreddit": "southafrica", "iso2": "ZA", "operators": [], "query": ""}
NG = {"subreddit": "Nigeria", "iso2": "NG", "operators": [], "query": ""}
KE = {"subreddit": "Kenya", "iso2": "KE", "operators": [], "query": ""}
SN = {"subreddit": "Senegal", "iso2": "SN", "operators": [], "query": ""}
FLUX_VIDE = b'<?xml version="1.0" encoding="UTF-8"?><feed></feed>'


# ---------------------------------------------------------------------------
# Câblage de base
# ---------------------------------------------------------------------------

class TestInitialisation:
    def test_init(self):
        scraper = RedditScraper()
        assert scraper.name == "reddit"
        assert scraper.retry_config is not None
        assert scraper._matchers, "aucune filiale reconnaissable"

    def test_reddit_est_de_la_voix_client_pas_de_la_presse(self):
        """Un fil de forum a le registre d'un avis, pas celui d'un article.

        Le classer « presse » lui appliquerait le lexique où « la fibre ARRIVE »
        est une bonne nouvelle — contresens garanti sur « my data is gone ».
        """
        assert domain_for_source(SourceEnum.REDDIT) == CUSTOMER_REVIEW


# ---------------------------------------------------------------------------
# Débit : la contrainte qui commande toute la conception
# ---------------------------------------------------------------------------

class TestDebit:
    def test_429_leve_au_lieu_de_rendre_zero_fil(self, monkeypatch):
        scraper = RedditScraper()
        monkeypatch.setattr(
            scraper.session, "get",
            lambda *a, **k: _FausseReponse(429, b"", {"x-ratelimit-reset": "37"}),
        )
        with pytest.raises(_RateLimited) as refus:
            scraper._call_feed(ZA)
        assert refus.value.reset_seconds == 37.0

    def test_refus_deguise_en_200_leve_aussi(self, monkeypatch):
        """LE piège de cette source, et il est silencieux.

        Reddit sert parfois une page HTML « you are doing that too much » avec
        un code 200. Un client qui se fie au code HTTP y lit « 0 fil » et croit
        à un pays sans discussion, alors qu'il n'a rien pu interroger.
        """
        scraper = RedditScraper()
        monkeypatch.setattr(
            scraper.session, "get",
            lambda *a, **k: _FausseReponse(
                200, b"<!doctype html><html><body>you are doing that too much"
            ),
        )
        with pytest.raises(_RateLimited):
            scraper._call_feed(ZA)

    def test_un_flux_atom_valide_passe(self, monkeypatch):
        """Le contrôle ci-dessus ne doit pas rejeter les réponses correctes."""
        scraper = RedditScraper()
        monkeypatch.setattr(
            scraper.session, "get", lambda *a, **k: _FausseReponse(200, FLUX_VIDE)
        )
        assert scraper._call_feed(ZA) == FLUX_VIDE

    def test_la_penalite_suit_le_delai_annonce_par_reddit(self, monkeypatch):
        """Reddit DIT combien de temps il reste : on l'attend, sans deviner.

        C'est la différence avec GDELT, qui n'annonce rien et impose de doubler
        à l'aveugle.
        """
        monkeypatch.setattr("reviews.collectors.reddit.time.sleep", lambda _: None)
        scraper = RedditScraper()
        scraper._penaliser(45.0)
        assert scraper._penalite == 45.0

    def test_sans_delai_annonce_la_penalite_double(self, monkeypatch):
        monkeypatch.setattr("reviews.collectors.reddit.time.sleep", lambda _: None)
        scraper = RedditScraper()
        scraper._penaliser(None)
        premiere = scraper._penalite
        scraper._penaliser(None)
        assert scraper._penalite > premiere

    def test_la_penalite_est_plafonnee(self, monkeypatch):
        """Sans plafond, une longue fenêtre de bridage bloquerait le run."""
        monkeypatch.setattr("reviews.collectors.reddit.time.sleep", lambda _: None)
        scraper = RedditScraper()
        for _ in range(30):
            scraper._penaliser(None)
        assert scraper._penalite <= scraper.BACKOFF_MAX
        scraper._penaliser(99_999.0)
        assert scraper._penalite <= scraper.BACKOFF_MAX

    def test_la_penalite_retombe_apres_un_succes(self, monkeypatch):
        monkeypatch.setattr("reviews.collectors.reddit.time.sleep", lambda _: None)
        scraper = RedditScraper()
        scraper._penaliser(60.0)
        scraper._recompenser()
        assert scraper._penalite < 60.0

    def test_un_succes_direct_relache_aussi_la_penalite(self, monkeypatch):
        """RÉGRESSION — sinon la pénalité est DÉFINITIVE.

        Ne récompenser que le chemin de rattrapage laissait un unique refus en
        début de run ralentir tous les subreddits suivants, y compris ceux qui
        passaient du premier coup : le run n'avait plus aucun moyen de revenir
        à sa cadence nominale.
        """
        monkeypatch.setattr("reviews.collectors.reddit.time.sleep", lambda _: None)
        scraper = RedditScraper()
        scraper._penaliser(60.0)
        avant = scraper._penalite
        monkeypatch.setattr(scraper, "_fetch_target", lambda target: [])
        scraper._fetch_avec_repli(ZA)
        assert scraper._penalite < avant

    def test_tout_rejete_leve_au_lieu_de_signaler_un_succes_vide(self, monkeypatch):
        """Un run « réussi avec 0 avis » ne déclenche aucune alerte.

        Si TOUS les subreddits sont refusés, c'est le limiteur qui est mal réglé
        ou Reddit qui a durci sa politique : une panne, pas un continent
        silencieux.
        """
        monkeypatch.setattr("reviews.collectors.reddit.time.sleep", lambda _: None)
        scraper = RedditScraper()
        monkeypatch.setattr(
            scraper.cfg, "subreddits", "southafrica|ZA,Nigeria|NG"
        )
        monkeypatch.setattr(
            scraper, "_fetch_avec_repli",
            lambda target: (_ for _ in ()).throw(_RateLimited("429")),
        )
        with pytest.raises(RuntimeError, match="débit"):
            scraper.collect()


# ---------------------------------------------------------------------------
# Extraction : ce que Reddit ajoute au message
# ---------------------------------------------------------------------------

class TestExtraction:
    def test_le_pied_de_page_reddit_est_retire(self):
        """« submitted by /u/x [link] [comments] » suit CHAQUE message.

        Repris tel quel, il entre dans l'analyse de sentiment : « link » et
        « comments » finiraient parmi les termes déclenchés de tous les avis
        Reddit du corpus, et l'onglet Motifs deviendrait illisible.
        """
        entree = _FausseEntree(
            content=_contenu("<p>Mon forfait data a disparu</p>")
        )
        corps = RedditScraper._body(entree)
        assert corps == "Mon forfait data a disparu"
        assert "submitted by" not in corps
        assert "[link]" not in corps and "[comments]" not in corps

    def test_le_repli_fonctionne_si_reddit_change_son_enveloppe(self):
        """Sans repli, la panne serait TOTALE et SILENCIEUSE.

        Si `div.md` disparaît, s'en tenir à lui viderait le corps de tous les
        avis Reddit : plus que des titres, et aucune erreur pour le dire.
        """
        entree = _FausseEntree(content=[{"value": (
            '<div class="autre-enveloppe"><p>Mon forfait data a disparu</p></div>'
            ' submitted by <a href="#">/u/abonne</a> '
            '<span><a href="#">[link]</a></span>'
        )}])
        corps = RedditScraper._body(entree)
        assert "Mon forfait data a disparu" in corps
        assert "submitted by" not in corps
        assert "[link]" not in corps

    def test_fil_de_simple_lien_donne_un_corps_vide(self):
        """Sans `div.md`, il n'y a pas de message : le titre porte seul le sens."""
        entree = _FausseEntree(content=[{"value": (
            'submitted by <a href="#">/u/abonne</a> '
            '<span><a href="#">[link]</a></span>'
        )}])
        assert RedditScraper._body(entree) == ""

    def test_contenu_absent_ne_leve_pas(self):
        assert RedditScraper._body(_FausseEntree()) == ""

    def test_auteur_sans_prefixe(self):
        assert RedditScraper._author(_FausseEntree(author="/u/abonne")) == "abonne"
        assert RedditScraper._author(_FausseEntree(author="u/abonne")) == "abonne"

    def test_compte_supprime_n_est_pas_un_auteur(self):
        """Sinon « [deleted] » apparaîtrait comme un contributeur à part entière."""
        assert RedditScraper._author(_FausseEntree(author="[deleted]")) is None
        assert RedditScraper._author(_FausseEntree(author="")) is None
        assert RedditScraper._author(_FausseEntree()) is None

    def test_date_lue_depuis_le_struct_time(self):
        entree = _FausseEntree(
            published_parsed=(2026, 8, 5, 14, 30, 0, 0, 0, 0)
        )
        assert RedditScraper._published(entree) == datetime(
            2026, 8, 5, 14, 30, tzinfo=timezone.utc
        )

    def test_date_absente_ne_leve_pas(self):
        assert RedditScraper._published(_FausseEntree()) is None


# ---------------------------------------------------------------------------
# Rattachement fil → filiale : là où se cachent les erreurs invisibles
# ---------------------------------------------------------------------------

class TestRattachement:
    def test_le_subreddit_pays_leve_l_ambiguite_multi_pays(self):
        """Dans r/Nigeria, « MTN » désigne forcément MTN Nigeria.

        C'est tout l'intérêt d'interroger des subreddits PAYS : sans ce
        marqueur, le fil serait rattaché aux dix-sept filiales MTN du périmètre
        à la fois.
        """
        scraper = RedditScraper()
        entree = _FausseEntree(
            title="MTN network down again since this morning",
            content=_contenu("<p>No data at all, anyone else?</p>"),
            id="t3_aaa",
        )
        reviews, pertinent = scraper._to_reviews(entree, NG)
        assert pertinent
        assert {r.company for r in reviews} == {"MTN Nigeria"}

    def test_le_texte_prime_sur_le_pays_du_subreddit(self):
        """Un fil de r/southafrica qui parle de MTN Nigeria concerne MTN Nigeria.

        Même règle que celle imposée à `press_feed` par un cas réel : le pays du
        support ne sert que de REPLI, sinon une filiale se voit imputer
        l'actualité d'une autre.
        """
        scraper = RedditScraper()
        entree = _FausseEntree(
            title="MTN Nigeria data prices went up again",
            content=_contenu("<p>The network there is a mess</p>"),
            id="t3_bbb",
        )
        noms = {r.company for r in scraper._to_reviews(entree, ZA)[0]}
        assert noms == {"MTN Nigeria"}
        assert "MTN South Africa" not in noms

    def test_fil_multi_operateurs_emet_un_avis_par_filiale(self):
        """« Airtel et Safaricom sont tous les deux en panne » concerne les deux."""
        scraper = RedditScraper()
        entree = _FausseEntree(
            title="Both Airtel and Safaricom network down in Nairobi",
            content=_contenu("<p>No data since morning</p>"),
            id="t3_ccc",
        )
        noms = {r.company for r in scraper._to_reviews(entree, KE)[0]}
        assert len(noms) >= 2
        assert any("Airtel" in n for n in noms)
        assert any("Safaricom" in n for n in noms)

    def test_le_vocabulaire_telecom_est_exige(self, monkeypatch):
        """Un forum généraliste parle d'autre chose, et « orange » est un fruit.

        Le fil est posté dans r/Senegal, où Orange EST un opérateur du
        périmètre : sans le contrôle de vocabulaire, il serait donc rattaché à
        Orange Sénégal. La seconde moitié du test le vérifie en désactivant le
        contrôle — sans quoi ce test passerait pour de mauvaises raisons.
        """
        scraper = RedditScraper()
        entree = _FausseEntree(
            title="Ou trouver du bon jus d'orange a Dakar ?",
            content=_contenu("<p>Je cherche des recommandations</p>"),
            id="t3_ddd",
        )
        reviews, pertinent = scraper._to_reviews(entree, SN)
        assert reviews == []
        assert not pertinent

        # Sans le garde-fou, ce fruit devient un avis client sur Orange Sénégal.
        monkeypatch.setattr(scraper.cfg, "require_telecom_terms", False)
        sans_controle = {r.company for r in scraper._to_reviews(entree, SN)[0]}
        assert "Orange Sénégal" in sans_controle, (
            "le test ne prouverait rien si le fil n'était pas rattachable"
        )

    def test_un_fil_telecom_du_meme_operateur_passe(self):
        """Le contre-exemple du test précédent : même opérateur, vrai sujet.

        Le filtre doit séparer les deux populations, pas rejeter Orange partout.
        """
        scraper = RedditScraper()
        entree = _FausseEntree(
            title="Orange a encore coupe mon forfait internet",
            content=_contenu("<p>Aucune connexion depuis ce matin</p>"),
            id="t3_ddd2",
        )
        noms = {r.company for r in scraper._to_reviews(entree, SN)[0]}
        assert noms == {"Orange Sénégal"}

    def test_fil_telecom_sans_operateur_connu_est_jete(self):
        scraper = RedditScraper()
        entree = _FausseEntree(
            title="Which fiber provider should I pick for my flat?",
            content=_contenu("<p>Looking at broadband options</p>"),
            id="t3_eee",
        )
        assert scraper._to_reviews(entree, ZA)[0] == []

    def test_fil_sans_titre_ecarte(self):
        scraper = RedditScraper()
        assert scraper._to_reviews(_FausseEntree(title="  ", id="t3_fff"), ZA)[0] == []

    def test_l_avis_produit_est_conforme(self):
        scraper = RedditScraper()
        entree = _FausseEntree(
            title="Vodacom data bundle disappeared overnight",
            content=_contenu("<p>Lost 5GB with no explanation</p>"),
            id="t3_ggg",
            author="/u/abonne",
            published_parsed=(2026, 8, 5, 9, 0, 0, 0, 0, 0),
        )
        reviews = scraper._to_reviews(entree, ZA)[0]
        assert len(reviews) == 1
        avis = reviews[0]
        assert avis.id == "reddit_t3_ggg"
        assert avis.company == "Vodacom South Africa"
        assert avis.source == SourceEnum.REDDIT.value
        assert avis.rating is None            # un fil de forum n'a pas de note
        assert avis.author == "abonne"
        assert "r/southafrica" in avis.text   # provenance conservée
        assert "Lost 5GB" in avis.text        # le corps est bien repris
        assert avis.created_at.tzinfo is not None

    def test_le_repere_incremental_ecarte_un_fil_deja_connu(self):
        """L'arrêt incrémental doit porter sur (filiale, source), comme ailleurs."""
        scraper = RedditScraper()
        scraper.since = {
            ("Vodacom South Africa", "reddit", None):
                datetime(2026, 8, 20, tzinfo=timezone.utc)
        }
        entree = _FausseEntree(
            title="Vodacom network outage in Durban",
            content=_contenu("<p>No data for hours</p>"),
            id="t3_hhh",
            published_parsed=(2026, 8, 5, 9, 0, 0, 0, 0, 0),   # antérieur
        )
        assert scraper._to_reviews(entree, ZA)[0] == []


# ---------------------------------------------------------------------------
# Cibles : une requête par PAYS, pas par filiale
# ---------------------------------------------------------------------------

class TestCibles:
    def test_la_requete_disjoint_les_operateurs_du_pays(self):
        cibles = reddit_targets([("southafrica", "ZA")])
        assert len(cibles) == 1
        requete = cibles[0]["query"]
        for operateur in ("Vodacom", "MTN", "Telkom"):
            assert operateur in requete
        assert " OR " in requete

    def test_les_noms_composes_sont_entre_guillemets(self):
        """Sans guillemets, Reddit découpe « Cell C » et « C » ramène tout."""
        requete = reddit_targets([("southafrica", "ZA")])[0]["query"]
        assert '"Cell C"' in requete

    def test_les_noms_ingerables_sortent_aussi_de_la_requete(self):
        """Chercher « WE » dans r/Egypt ramène tout l'anglais du subreddit.

        Le rattachement les écarte déjà (voir TestNomsNonReconnaissables), mais
        les laisser dans la requête dépenserait une minute de run — le coût
        d'un appel Reddit — pour des résultats qu'aucune règle ne pourra
        ensuite attribuer.
        """
        requete = reddit_targets([("Egypt", "EG")])[0]["query"]
        assert "Vodafone" in requete and "Orange" in requete
        assert "WE" not in requete and "e&" not in requete

    def test_pays_sans_filiale_declaree_est_ignore(self):
        """Une requête sans filtre ramènerait TOUT le subreddit : une minute
        de run dépensée pour du bruit intégral."""
        assert reddit_targets([("france", "FR")]) == []
        assert reddit_targets([("quelquechose", None)]) == []

    def test_une_cible_par_subreddit_et_non_par_filiale(self):
        """C'est ce qui ramène le run de 2 h 15 à ~20 min."""
        cibles = reddit_targets([("southafrica", "ZA"), ("Nigeria", "NG")])
        assert len(cibles) == 2

    def test_le_prefixe_r_est_tolere_en_configuration(self):
        from reviews.config import RedditConfig
        cfg = RedditConfig(REDDIT_SUBREDDITS="r/southafrica|ZA, /r/Nigeria|NG")
        assert cfg.subreddits_list() == [("southafrica", "ZA"), ("Nigeria", "NG")]

    def test_configuration_vide_ne_leve_pas(self):
        from reviews.config import RedditConfig
        assert RedditConfig(REDDIT_SUBREDDITS="").subreddits_list() == []


# ---------------------------------------------------------------------------
# Câblage : registre, configuration, dimensions
# ---------------------------------------------------------------------------

class TestCablage:
    def test_reddit_est_dans_le_registre_et_la_configuration(self):
        """Une clé présente d'un seul côté = collecteur jamais lancé, ou
        « collecteur inconnu » à l'exécution."""
        from reviews.collectors import COLLECTORS
        from reviews.config import Settings

        assert "reddit" in COLLECTORS
        settings = Settings()
        settings.reddit.enabled = True
        assert "reddit" in settings.get_enabled_scrapers()

    def test_la_migration_declare_reddit_hors_comparaison(self):
        """Sans cette ligne, Reddit entrerait dans tous les taux du dashboard.

        Deux raisons indépendantes l'interdisent : la couverture très inégale
        d'un pays à l'autre, et le biais de recrutement d'un forum. Voir
        l'en-tête de la migration.
        """
        from pathlib import Path
        sql = Path("migrations/011_reddit.sql").read_text(encoding="utf-8")
        assert "'reddit'" in sql
        assert "'customer_review'" in sql
        assert "SET comparable = FALSE WHERE code = 'reddit'" in sql

    def test_la_cadence_reddit_tient_compte_du_debit(self):
        """À une requête par minute, repasser toutes les six heures n'a pas de
        sens : le collecteur passerait son temps à attendre."""
        from reviews.config import get_settings
        assert get_settings().scraper_interval_minutes("reddit") >= 720

    def test_le_budget_couvre_la_liste_de_subreddits(self):
        """RÉGRESSION À PRÉVENIR — l'échec serait silencieux et TOUJOURS le même.

        Si le budget ne couvre pas len(subreddits) x min_interval, le run est
        coupé en plein milieu. L'ordre des cibles étant stable, ce sont
        systématiquement les mêmes derniers pays qui ne seraient jamais
        collectés — sans qu'aucune erreur ne le signale.
        """
        from reviews.config import get_settings
        cfg = get_settings().reddit
        besoin = len(cfg.subreddits_list()) * cfg.min_interval_seconds
        assert cfg.collector_timeout >= besoin, (
            f"budget {cfg.collector_timeout}s insuffisant pour "
            f"{len(cfg.subreddits_list())} subreddits à {cfg.min_interval_seconds}s"
        )
