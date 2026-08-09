"""
Scraper Google Maps.
Collecte les avis des agences Moov Africa.
"""

import logging
import time
import re
import unicodedata
from typing import Optional
from datetime import datetime, timedelta, timezone
import sys

from playwright.sync_api import sync_playwright, Page, Browser
from reviews.collectors.base import BaseCollector
from reviews.collectors.targets import googlemaps_locations
from reviews.domain.models import Review, SourceEnum
from reviews.config import get_settings

settings = get_settings()
from reviews.processing.resilience import RetryConfig

logger = logging.getLogger(__name__)


class GoogleMapsScraper(BaseCollector):
    """Scraper pour Google Maps."""

    # Playwright (API sync) est lié au thread OS qui le démarre : pas de
    # timeout par thread worker (voir scheduler.execute_with_retry).
    USES_THREAD_TIMEOUT = False

    # OBLIGATOIRE : sans user-agent explicite, Chromium annonce "HeadlessChrome"
    # et Google sert une fiche lieu dégradée SANS l'onglet Avis (seuls
    # "Présentation" et "À propos" existent) => wait_for expirait à chaque run.
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    # Agences à scraper : chargées depuis config/operators.json (targets.py).
    # La requête est du texte libre (pas d'identifiant à vérifier), mais chaque
    # fiche demande une session navigateur complète : ce collecteur est le plus
    # lent, prévoir le budget de timeout en conséquence à grande échelle.

    # Mapping durées relatives → jours
    TIME_UNITS = {
        "year": 365, "month": 30, "week": 7, "day": 1,
        "hour": 1/24, "minute": 1/1440, "second": 0
    }
    
    def __init__(self):
        retry_config = RetryConfig(
            max_attempts=settings.scraping.retry_max_attempts,
            backoff_factor=settings.scraping.retry_backoff_factor,
            timeout=settings.scraping.request_timeout,
        )
        super().__init__("googlemaps", retry_config)
        self.browser: Optional[Browser] = None
        #: Session partagée entre unités (mode `collection_jobs`). Ouverte par
        #: `open_session`, fermée par `close_session` — jamais par unité : le
        #: démarrage de Playwright coûte deux à trois secondes, soit une
        #: vingtaine de minutes gaspillées sur 405 unités.
        self._playwright = None
        self._context = None
        #: Agences déjà visitées pendant CE run. Une même fiche remonte sur
        #: plusieurs requêtes — mesuré, « Agence MTN Lagos » et « Agence MTN
        #: Nigeria » renvoient le même établissement en tête — et la visiter
        #: deux fois coûterait une minute pour zéro avis nouveau.
        self._seen_places: set[str] = set()

    def collect(self) -> list[Review]:
        """Collecte les avis depuis Google Maps pour toutes les agences."""
        all_reviews = []
        playwright = None
        self._seen_places.clear()

        try:
            playwright = sync_playwright().start()
            self.browser = playwright.chromium.launch(
                # Pas de channel="chrome" : cela exige Google Chrome de marque,
                # absent des images Docker. On utilise le Chromium fourni par
                # Playwright, présent partout (local et conteneur).
                headless=settings.googlemaps.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ]
            )
            
            context = self.browser.new_context(
                locale="en-US",
                timezone_id="Africa/Casablanca",
                viewport={"width": 1400, "height": 1000},
                user_agent=self.USER_AGENT,
            )
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            
            locations = googlemaps_locations(
                cities_per_run=settings.googlemaps.cities_per_run
            )
            self.logger.info(
                "%d recherche(s) : %d filiale(s) × (1 pays + %d ville(s)), "
                "jusqu'à %d agence(s) retenue(s) par recherche",
                len(locations),
                len({loc["name"] for loc in locations}),
                settings.googlemaps.cities_per_run,
                settings.googlemaps.places_per_query,
            )
            failures = 0
            for location in locations:
                try:
                    self.logger.info(f"Recherche : {location['query']}")
                    reviews = self._scrape_location(context, location)
                    all_reviews.extend(reviews)
                    time.sleep(5)  # Pause entre les recherches
                except Exception as e:
                    failures += 1
                    self.logger.error(f"Erreur scraping {location['name']} : {e}")
                    continue

            context.close()
            self.logger.info(
                "%d agence(s) distincte(s) visitée(s), %d avis",
                len(self._seen_places), len(all_reviews),
            )

            # Si AUCUNE agence n'a pu être scrapée, c'est une panne du scraper
            # (UI Google modifiée, blocage anti-bot…), pas une absence d'avis :
            # on lève pour que le retry se déclenche et que le run soit "failed"
            # au lieu de "success avec 0 avis" (échec silencieux).
            if locations and failures == len(locations):
                raise RuntimeError(
                    f"Les {failures} agences ont échoué : Google Maps inaccessible "
                    f"ou structure de page modifiée"
                )

            return all_reviews

        except Exception as e:
            self.logger.error(f"Erreur Playwright : {e}")
            raise
        finally:
            # Toujours tout fermer, sinon le driver Playwright (et sa boucle
            # asyncio interne) reste actif dans ce thread et empêche tout
            # nouveau sync_playwright().start() ultérieur dans le même thread
            # (erreur "Playwright Sync API inside the asyncio loop").
            if self.browser:
                try:
                    self.browser.close()
                except Exception:
                    pass
            if playwright:
                try:
                    playwright.stop()
                except Exception:
                    pass
    
    # ------------------------------------------------------------------
    # MODE UNITÉS — une recherche = un job reprenable (collection_jobs)
    #
    # `collect()` ci-dessus reste utilisable (exécution manuelle, tests), mais
    # le pipeline emprunte ce chemin : il persiste après CHAQUE recherche.
    # Auparavant, les 405 recherches ne produisaient une écriture qu'à la toute
    # fin — une interruption perdait dix heures de travail intégralement, ce
    # qui s'est produit tous les jours du 4 au 9 août 2026.
    # ------------------------------------------------------------------

    SUPPORTS_UNITS = True

    def plan_units(self) -> list[dict]:
        """Une unité par recherche : filiale × lieu."""
        locations = googlemaps_locations(
            cities_per_run=settings.googlemaps.cities_per_run
        )
        return [
            {
                # La filiale ET la requête : deux filiales d'un même opérateur
                # peuvent produire une requête pays identique, et l'unité doit
                # rester attachée à la bonne.
                "job_key": f"{loc['name']}|{loc['query']}",
                "company": loc["name"],
                "operator": loc.get("operator"),
                "country": loc.get("country"),
                "location": loc.get("city") or loc.get("country"),
                "query": loc["query"],
            }
            for loc in locations
        ]

    def open_session(self) -> None:
        """Démarre le navigateur UNE fois pour toutes les unités du passage."""
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(
            headless=settings.googlemaps.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        self._context = self.browser.new_context(
            locale="en-US",
            timezone_id="Africa/Casablanca",
            viewport={"width": 1400, "height": 1000},
            user_agent=self.USER_AGENT,
        )
        self._context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        self._seen_places.clear()

    def close_session(self) -> None:
        """Referme tout, même après une erreur.

        Sans cela, le pilote Playwright et sa boucle asyncio restent actifs dans
        ce thread et interdisent tout `sync_playwright().start()` ultérieur —
        la deuxième unité échouerait sur « Playwright Sync API inside the
        asyncio loop ».
        """
        for fermeture in (
            lambda: self._context.close() if self._context else None,
            lambda: self.browser.close() if self.browser else None,
            lambda: self._playwright.stop() if self._playwright else None,
        ):
            try:
                fermeture()
            except Exception:  # noqa: BLE001
                pass
        self._context = self.browser = self._playwright = None

    def collect_unit(self, job, save_cursor) -> list[Review]:
        """Collecte UNE recherche, en reprenant les fiches déjà faites.

        Le curseur porte les `place_id` déjà traités. Une recherche ouvre
        jusqu'à cinq fiches à ~20 s chacune : interrompue à la quatrième, elle
        reprend à la quatrième et non à la première.
        """
        location = {
            "query": job.query,
            "name": job.company,
            "operator": job.operator or "",
        }
        deja_faites = set(job.cursor.get("places_done") or [])

        page = self._context.new_page()
        try:
            places = self._search_places(page, location)
        finally:
            page.close()

        if not places:
            return []

        collectes: list[Review] = []
        for place in places:
            if place["place_id"] in deja_faites:
                self.logger.debug("Fiche déjà traitée, ignorée : %s", place["name"])
                continue
            try:
                collectes.extend(self._scrape_place(self._context, place, location))
            except Exception as e:  # noqa: BLE001
                # Une fiche en échec ne condamne pas les autres de la même
                # recherche : on la note comme faite pour ne pas boucler dessus
                # au prochain passage, l'agence reviendra au cycle suivant.
                self.logger.warning("Erreur sur l'agence %s : %s", place["name"], e)

            deja_faites.add(place["place_id"])
            # Enregistré APRÈS chaque fiche, pas à la fin : c'est tout l'objet
            # du curseur, et l'interruption arrive avant la fin.
            save_cursor({"places_done": sorted(deja_faites)})
            time.sleep(2)

        return collectes

    def _scrape_location(self, context, location: dict) -> list[Review]:
        """Scrape les avis de TOUTES les agences retenues pour une requête.

        Une recherche Google Maps renvoie une LISTE d'établissements — mesuré :
        10 à 11 pour « Agence MTN Lagos » ou « Agence Vodacom Johannesburg ».
        Le collecteur n'en ouvrait qu'un seul (`.first`) et jetait les autres,
        alors qu'ils étaient déjà chargés dans la page : les récupérer ne coûte
        aucune recherche supplémentaire, seulement l'ouverture des fiches.
        """
        page = context.new_page()
        try:
            places = self._search_places(page, location)
            if not places:
                return []

            collected: list[Review] = []
            for place in places:
                try:
                    collected.extend(self._scrape_place(context, place, location))
                except Exception as e:  # noqa: BLE001
                    self.logger.warning(
                        "Erreur sur l'agence %s : %s", place["name"], e
                    )
                    continue
                time.sleep(2)
            return collected
        finally:
            page.close()

    def _scrape_place(self, context, place: dict, location: dict) -> list[Review]:
        """Avis d'UNE agence identifiée."""
        page = context.new_page()
        try:
            page.goto(place["url"], wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)
            self._handle_consent(page)

            # Onglet Avis introuvable = panne (voir USER_AGENT) : on propage
            if not self._open_reviews_tab(page):
                raise RuntimeError(
                    f"Onglet Avis introuvable pour {place['name']}"
                )

            time.sleep(3)

            # Trier du plus récent au plus ancien AVANT d'extraire.
            #
            # C'est la correction qui donne sa valeur à cette source. Google
            # trie par défaut par « pertinence », ce qui remonte les avis
            # anciens et populaires : les avis Google Maps collectés jusqu'ici
            # avaient 1 390 jours d'âge moyen — près de quatre ans — et 5,5 %
            # seulement dataient de moins de 90 jours, contre 59 % côté Play
            # Store. Un dashboard « temps réel » alimenté par des avis de 2019
            # décrit un opérateur qui n'existe plus.
            #
            # Le tri échoue sans être bloquant : on collecte alors comme avant,
            # en le signalant, plutôt que de perdre la fiche entière.
            self._sort_by_newest(page)

            # Scroller pour charger les avis
            reviews_list = self._scroll_and_extract(page)

            # Parser les avis bruts, en leur attachant l'agence d'origine
            parsed = self._parse_reviews(reviews_list, location["name"], place)
            self.logger.info(
                "%s — %s : %d avis", location["name"], place["name"], len(parsed)
            )
            return parsed

        finally:
            page.close()

    # -----------------------------------------------------------------------
    # Recherche : de UNE fiche à TOUTES les agences de l'enseigne
    # -----------------------------------------------------------------------

    #: Extrait l'identifiant Google d'une URL de fiche (`!1s0x…:0x…`).
    _PLACE_ID_RE = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", re.IGNORECASE)

    @staticmethod
    def _normalize(text: str) -> str:
        """Minuscules sans accents, pour comparer « Sénégal » et « Senegal »."""
        decomposed = unicodedata.normalize("NFKD", text or "")
        return "".join(
            c for c in decomposed if not unicodedata.combining(c)
        ).lower()

    def _search_places(self, page: Page, location: dict) -> list[dict]:
        """Agences de l'enseigne renvoyées par une recherche.

        DEUX FILTRES, et le second est le plus important.

        1. Le PLAFOND `places_per_query` borne le coût : chaque fiche retenue
           est une page à ouvrir et à faire défiler, sur le collecteur déjà le
           plus lent du projet.

        2. Le NOM doit contenir celui de l'opérateur. Google classe par
           pertinence commerciale, pas par appartenance à l'enseigne : mesuré,
           « Agence Vodacom Johannesburg » remonte « Cellucity - Bedfordview »
           en PREMIER résultat — un revendeur tiers, dont les avis étaient
           jusqu'ici enregistrés comme des avis Vodacom. Le collecteur prenait
           ce premier résultat sans le vérifier, et rien en base ne permettait
           de s'en apercevoir.
        """
        query = location["query"]
        try:
            # hl=en force l'UI en anglais : `locale` seul est ignoré par Google
            # (il suit la géoloc IP et renvoyait "Avis"/"il y a 3 mois" en FR,
            # que _relative_to_iso — parseur anglais — ne sait pas lire).
            url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}?hl=en"
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)
            self._handle_consent(page)
            time.sleep(1)

            # Une recherche à résultat unique redirige directement sur la fiche,
            # sans passer par une liste : on récupère alors ce seul lieu.
            if "/place/" in page.url:
                nom = (page.title() or query).split(" - ")[0].strip()
                brut = [{"name": nom, "url": page.url}]
            else:
                brut = page.locator("a.hfpxzc").evaluate_all(
                    "els => els.map(e => ({"
                    "  name: e.getAttribute('aria-label') || '',"
                    "  url:  e.href"
                    "}))"
                )
        except Exception as e:  # noqa: BLE001
            self.logger.error("Erreur recherche « %s » : %s", query, e)
            return []

        if not brut:
            self.logger.warning("Aucun lieu trouvé pour : %s", query)
            return []

        operateur = self._normalize(location.get("operator") or "")
        retenus, ecartes = [], []
        for item in brut:
            nom = (item.get("name") or "").strip()
            url = item.get("url") or ""
            if not nom or not url:
                continue
            if operateur and operateur not in self._normalize(nom):
                ecartes.append(nom)
                continue
            place_id = self._place_id_from_url(url)
            # Une même agence remonte sur plusieurs requêtes (la requête pays et
            # la requête ville tombent souvent sur la même fiche) : on ne la
            # visite qu'une fois par run.
            if place_id in self._seen_places:
                continue
            self._seen_places.add(place_id)
            retenus.append({"name": nom, "url": url, "place_id": place_id})
            if len(retenus) >= settings.googlemaps.places_per_query:
                break

        if ecartes:
            self.logger.info(
                "%s : %d lieu(x) écarté(s), nom sans « %s » (%s)",
                query, len(ecartes), location.get("operator"),
                ", ".join(ecartes[:3]),
            )
        self.logger.info(
            "%s : %d agence(s) retenue(s) sur %d résultat(s)",
            query, len(retenus), len(brut),
        )
        return retenus

    def _place_id_from_url(self, url: str) -> str:
        """Identifiant stable d'une fiche.

        L'identifiant hexadécimal de l'URL est l'identité réelle du lieu et
        survit aux changements de nom. À défaut — Google ne le met pas toujours
        dans le lien de la liste — on retombe sur le segment `/place/<nom>/`,
        moins stable mais suffisant pour dédupliquer à l'intérieur d'un run.
        """
        found = self._PLACE_ID_RE.search(url)
        if found:
            return found.group(1)
        segment = url.split("/place/")[-1].split("/")[0] if "/place/" in url else url
        return segment[:255]

    def _open_place(self, page: Page, query: str) -> bool:
        """Ouvre un lieu sur Google Maps."""
        try:
            self.logger.debug(f"Recherche : {query}")
            
            # hl=en force l'UI en anglais : `locale` seul est ignoré par Google
            # (il suit la géoloc IP et renvoyait "Avis"/"il y a 3 mois" en FR,
            # que _relative_to_iso — parseur anglais — ne sait pas lire).
            url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}?hl=en"
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)

            # Accepter le bandeau de consentement cookies s'il est présent
            # (sans ça, l'UI de la fiche lieu ne se charge jamais et l'onglet
            # Avis reste introuvable)
            self._handle_consent(page)
            time.sleep(1)

            # Chercher un résultat de recherche
            try:
                first_result = page.locator('a.hfpxzc').first
                if first_result.count() > 0:
                    first_result.click()
                    page.wait_for_url("**/place/**", timeout=10000)
            except:
                pass
            
            # Vérifier qu'on est sur une page de lieu
            if "/place/" not in page.url:
                self.logger.warning(f"Aucun lieu trouvé pour : {query}")
                return False
            
            return True
            
        except Exception as e:
            self.logger.error(f"Erreur ouverture lieu : {e}")
            return False

    def _handle_consent(self, page: Page):
        """Accepte le bandeau de consentement cookies Google si présent."""
        try:
            buttons = page.query_selector_all("button")
            for btn in buttons:
                try:
                    text = (btn.inner_text() or "").strip().lower()
                    aria_label = (btn.get_attribute("aria-label") or "").lower()
                    if any(
                        keyword in text or keyword in aria_label
                        for keyword in ("accept all", "tout accepter", "j'accepte", "i agree")
                    ):
                        btn.click()
                        return
                except Exception:
                    continue
        except Exception as e:
            self.logger.debug(f"Erreur gestion consentement : {e}")

    def _open_reviews_tab(self, page: Page) -> bool:
        """Ouvre l'onglet Reviews."""
        try:
            self.logger.debug("Ouverture onglet Reviews")
            
            # Chercher le bouton Reviews
            # `.first` : plusieurs [role=tab] correspondent parfois, et wait_for
            # est strict (il lèverait "strict mode violation" sur un multi-match).
            review_tab = page.locator('button[role="tab"]').filter(
                has_text=re.compile(r"Reviews|Avis", re.IGNORECASE)
            ).first

            # 20s suffisent : l'onglet apparaît en ~2s quand la page est saine.
            # 60s ne faisaient que retarder de 4 min/run un échec systématique.
            review_tab.wait_for(state="visible", timeout=20000)
            review_tab.click()

            # data-review-id est un attribut stable, contrairement aux classes
            # obfusquées (div.jftiEf) que Google renomme à chaque refonte.
            page.wait_for_selector('div[data-review-id]', timeout=30000)

            return True
            
        except Exception as e:
            self.logger.error(f"Erreur ouverture Reviews : {e}")
            return False
    
    def _sort_by_newest(self, page: Page) -> bool:
        """Bascule le tri des avis sur « Newest ».

        POURQUOI C'EST DÉTERMINANT
            Google classe les avis par pertinence par défaut, et la pertinence
            favorise les avis anciens et très votés. Comme on ne récupère qu'une
            partie des avis d'une fiche, l'ordre décide entièrement de CE QU'ON
            COLLECTE : trié par pertinence, un échantillon de dix avis peut
            n'en contenir aucun de l'année en cours.

        L'interface est forcée en anglais (`hl=en` dans l'URL), donc les
        libellés « Sort » et « Newest » sont stables. On accepte tout de même
        les variantes françaises : si Google ignore `hl` pour une géolocalisation
        donnée, le tri continue de fonctionner.

        Returns:
            True si le tri a été appliqué. False sans lever d'exception : mieux
            vaut des avis mal triés que pas d'avis du tout.
        """
        try:
            # Le bouton de tri porte un libellé qui varie (« Sort »,
            # « Most relevant »…) : on le cible par son rôle et son texte
            # plutôt que par une classe obfusquée.
            bouton = page.locator(
                'button[aria-label*="Sort"], button[data-value="Sort"]'
            ).first
            if bouton.count() == 0:
                bouton = page.locator("button").filter(
                    has_text=re.compile(r"^(Sort|Trier|Most relevant|Plus pertinents)", re.I)
                ).first
            bouton.wait_for(state="visible", timeout=8000)
            bouton.click()

            # Le menu s'ouvre en `menuitemradio` : on choisit l'entrée
            # « Newest » / « Les plus récents ».
            option = page.locator('[role="menuitemradio"], [role="menuitem"]').filter(
                has_text=re.compile(r"Newest|plus récents|plus recents", re.I)
            ).first
            option.wait_for(state="visible", timeout=8000)
            option.click()

            # La liste se recharge : attendre que les avis soient re-rendus,
            # sinon le défilement compterait ceux de l'ordre précédent.
            page.wait_for_selector("div[data-review-id]", timeout=15000)
            time.sleep(2)
            self.logger.debug("Tri par date appliqué")
            return True

        except Exception as e:  # noqa: BLE001
            self.logger.warning(
                "Tri par date impossible (%s) : les avis seront collectés par "
                "pertinence, donc plus anciens.", type(e).__name__,
            )
            return False

    def _scroll_and_extract(self, page: Page) -> list[dict]:
        """Scrolle pour charger les avis et les extrait."""
        reviews_list = []
        prev_count = 0
        stable_count = 0
        
        for i in range(400):
            try:
                # Scroller.
                #
                # Le conteneur défilable est trouvé DYNAMIQUEMENT, en remontant
                # depuis un avis jusqu'au premier ancêtre réellement défilable.
                # La version précédente ciblait `div.m6QErb.DxyBCb.kA9KIf.dS8AEf`,
                # une classe obfusquée que Google renomme à chaque refonte ; son
                # repli `div[role="main"]` désigne tout le panneau latéral, dont
                # le scrollTop ne fait pas défiler la LISTE d'avis imbriquée.
                # Résultat : le défilement ne se déclenchait plus, le compteur
                # restait à la première page et le collecteur s'arrêtait à
                # 10 avis — sur 21 filiales, exactement 10 avis avaient été
                # collectés, tous par Google Maps.
                #
                # On pousse en plus le DERNIER avis dans la vue : la liste est
                # virtualisée, et c'est ce qui déclenche le chargement du lot
                # suivant même quand le conteneur défile mal.
                probe = page.evaluate("""
                    () => {
                        const nodes = document.querySelectorAll('div[data-review-id]');
                        if (!nodes.length) return { count: 0, scroller: false };

                        let el = nodes[0].parentElement, scroller = null;
                        while (el && el !== document.body) {
                            const style = getComputedStyle(el);
                            if (/(auto|scroll)/.test(style.overflowY)
                                && el.scrollHeight > el.clientHeight + 50) {
                                scroller = el;
                                break;
                            }
                            el = el.parentElement;
                        }
                        if (scroller) scroller.scrollTop = scroller.scrollHeight;
                        nodes[nodes.length - 1].scrollIntoView(false);

                        // Compter les avis DISTINCTS : Google rend chaque avis
                        // dans deux panneaux (Présentation + Avis), le comptage
                        // brut doublait et coupait le scroll à mi-parcours.
                        const ids = new Set();
                        nodes.forEach(r => ids.add(r.getAttribute('data-review-id')));
                        return { count: ids.size, scroller: Boolean(scroller) };
                    }
                """)
                count = probe["count"]

                if i == 0 and not probe["scroller"]:
                    # Signalé une seule fois, au premier tour : si Google change
                    # encore sa structure, ce message est ce qui permettra de
                    # comprendre pourquoi les volumes replafonnent.
                    self.logger.warning(
                        "Aucun conteneur défilable trouvé autour des avis — "
                        "seule la première page sera collectée."
                    )

                time.sleep(1.3)
                self.logger.debug(f"Scroll {i+1} : {count} avis")
                
                if count >= settings.googlemaps.max_reviews:
                    break
                
                if count == prev_count:
                    stable_count += 1
                    if stable_count >= 4:
                        self.logger.debug("Fin du scraping (stable)")
                        break
                else:
                    stable_count = 0
                
                prev_count = count
                
            except Exception as e:
                self.logger.debug(f"Erreur scroll {i+1} : {e}")
                break
        
        # Développer les textes tronqués
        try:
            page.evaluate("""
                () => {
                    let n = 0;
                    const buttons = document.querySelectorAll(
                        'button[aria-label="See more"], button.w8nwRe'
                    );
                    buttons.forEach(b => {
                        if (b.innerText.toLowerCase().includes('more')) {
                            b.click();
                            n++;
                        }
                    });
                    return n;
                }
            """)
            time.sleep(1)
        except:
            pass
        
        # Extraire les avis
        try:
            raw = page.evaluate("""
                () => {
                    const out = [];
                    const seen = new Set();
                    document.querySelectorAll('div[data-review-id]').forEach(r => {
                        const id = r.getAttribute('data-review-id');
                        // Même avis présent dans plusieurs panneaux : on ne le
                        // garde qu'une fois (sinon doublons en base).
                        if (!id || seen.has(id)) return;
                        seen.add(id);
                        const starEl = r.querySelector('span[role="img"]');
                        const textEl = r.querySelector('span.wiI7pd');
                        const dateEl = r.querySelector('span.rsqaWe');
                        const authorEl = r.querySelector('div.d4r55');

                        out.push({
                            id: id,
                            author: authorEl ? authorEl.innerText : null,
                            rating_aria: starEl ? starEl.getAttribute('aria-label') : null,
                            date_rel: dateEl ? dateEl.innerText : null,
                            text: textEl ? textEl.innerText : null
                        });
                    });
                    return out;
                }
            """)
            
            self.logger.info(f"{len(raw)} avis bruts extraits")
            return raw
            
        except Exception as e:
            self.logger.error(f"Erreur extraction : {e}")
            return []
    
    def _parse_reviews(self, raw_reviews: list, location_name: str,
                       place: dict) -> list[Review]:
        """Parse les avis bruts en objets Review.

        Les avis SANS TEXTE (note seule, sans commentaire) sont écartés : le
        moteur de sentiment travaille sur du texte, et une note isolée n'a
        aucun motif à livrer. C'est fréquent sur Google Maps, où déposer cinq
        étoiles sans écrire un mot ne coûte qu'un geste.

        Ce rejet est désormais COMPTÉ et journalisé. Silencieux, il donnait
        l'impression d'une panne : une fiche annonçant dix avis n'en produisait
        que deux, sans que rien n'explique l'écart.
        """
        parsed = []
        sans_texte = 0
        now = datetime.now(timezone.utc)

        for rv in raw_reviews[:settings.googlemaps.max_reviews]:
            try:
                rating = self._parse_rating(rv.get("rating_aria"))
                published = self._relative_to_iso(rv.get("date_rel"), now)

                if not rv.get("text"):
                    sans_texte += 1
                    continue

                review = Review(
                    id=rv.get("id"),
                    # `company` reste la FILIALE, jamais l'agence : c'est lui
                    # qui porte le rattachement dimensionnel (dim_subsidiary.
                    # aliases). Mettre le nom de l'agence ici détacherait tous
                    # ces avis de leur filiale.
                    company=location_name,
                    source=SourceEnum.GOOGLE_MAPS,
                    title=None,  # Google Maps n'a pas de titre
                    text=rv.get("text"),
                    rating=rating,
                    author=rv.get("author"),
                    created_at=published,
                    target_id=place.get("place_id"),
                    target_name=(place.get("name") or None),
                )
                parsed.append(review)
            except Exception as e:
                self.logger.warning(f"Erreur parsing avis : {e}")
                continue

        if sans_texte:
            self.logger.info(
                "%s : %d avis retenus, %d écartés faute de commentaire "
                "(note seule, inexploitable pour l'analyse de sentiment)",
                location_name, len(parsed), sans_texte,
            )

        return parsed
    
    def _parse_rating(self, aria: Optional[str]) -> Optional[int]:
        """Parse la note depuis aria-label."""
        if not aria:
            return None
        match = re.search(r"(\d+(?:\.\d+)?)", aria)
        if match:
            return int(round(float(match.group(1))))
        return None
    
    def _relative_to_iso(
        self,
        rel: Optional[str],
        now: datetime
    ) -> Optional[str]:
        """Convertit une date relative en ISO."""
        if not rel:
            return None
        
        r = rel.lower().strip()
        
        # Cas spéciaux
        if "yesterday" in r:
            return (now - timedelta(days=1)).isoformat()
        if "today" in r or "just now" in r:
            return now.isoformat()
        
        # Parsrer "X <unit> ago"
        match = re.search(
            r"(\d+|a|an)\s+(year|month|week|day|hour|minute|second)",
            r
        )
        if not match:
            return None
        
        n = 1 if match.group(1) in ("a", "an") else int(match.group(1))
        unit = match.group(2)
        
        delta_days = n * self.TIME_UNITS.get(unit, 0)
        return (now - timedelta(days=delta_days)).isoformat()