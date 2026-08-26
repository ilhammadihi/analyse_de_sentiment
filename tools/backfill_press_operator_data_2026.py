"""
Backfill PONCTUEL — abonnés récents par opérateur, sourcés presse/rapports.

POURQUOI CE N'EST PAS UN COLLECTEUR, ET NE LE DEVIENDRA PAS
    Les quatre régulateurs (`reviews/collectors/{ncc_nigeria,anrt_maroc,
    arcep_benin,nca_ghana}.py`) interrogent une page ou un fichier STABLE,
    republié à chaque période par la même source — un job planifié a donc un
    sens. Les lignes ci-dessous viennent d'articles de presse et de rapports
    financiers ponctuels (Afrique du Sud, Égypte, Kenya, Tanzanie, Algérie, Côte d'Ivoire, RDC) :
    chaque source est un instantané, pas un flux qu'on peut réinterroger. Un
    job planifié sur ce script réinsérerait indéfiniment les MÊMES chiffres
    en donnant l'illusion d'une donnée qui se rafraîchit — c'est le contraire
    de ce qui est recherché. Ce script s'exécute UNE FOIS, à la main.

D'OÙ VIENNENT CES CHIFFRES, ET POURQUOI ORANGE ÉGYPTE / DJEZZY / MOBILIS
N'Y SONT PAS
    Recherche du 24 août 2026, demandée explicitement pour compléter le
    Kenya/l'Afrique du Sud/l'Égypte/la Tanzanie au-delà des quatre
    régulateurs déjà intégrés, à condition que chaque chiffre soit RÉEL et
    TRACÉ — jamais estimé par un modèle. Trois opérateurs ont été écartés
    faute d'un chiffre à la hauteur de cette exigence :
      - Orange Égypte : le seul chiffre trouvé (~28 M) est une estimation de
        journaliste rétro-calculée depuis une part de marché supposée, pas
        un chiffre publié par Orange. Écarté.
      - Djezzy et Mobilis (Algérie) : chiffres d'origine régulateur (ARPCE)
        corroborés par la presse, mais sans page stable à citer comme
        `source_url`. Écarté par manque de traçabilité, pas par doute sur le
        chiffre — à réintégrer si une page ARPCE directe est trouvée.

    Ooredoo Algérie, lui, a un chiffre ET une source stable — conservé seul
    pour ce pays, d'où une couverture Algérie asymétrique (un seul opérateur
    sur trois) : c'est le reflet honnête de ce qui est vérifiable aujourd'hui,
    pas un choix de mise en scène.

    RECHERCHE ÉLARGIE DU 24 AOÛT 2026 (soir) — balayage systématique des 5
    grands groupes (Orange, MTN, Airtel, Vodacom/Vodafone, Moov Africa) sur
    les 43 filiales du portefeuille encore sans donnée. Écartés, ET POURQUOI :
      - MTN Guinée-Conakry, MTN Guinée-Bissau : L'OPÉRATEUR N'EXISTE PLUS.
        Vendus respectivement à l'État guinéen (déc. 2024) et à Telecel Group
        (août 2024). Le dernier chiffre MTN disponible est donc un chiffre
        d'une entité qui n'est plus MTN — ne PAS l'insérer sous ce code, même
        historique.
      - Orange Guinée : le seul chiffre 2025 est un CALCUL (part de marché
        76,2 % × marché total 12,8 M ARPT = ~9,75 M), pas un nombre publié —
        même défaut qu'Orange Égypte. Écarté.
      - Orange Guinée-Bissau, Airtel Seychelles : aucune donnée 2024-2026
        trouvée, seulement 2021 et 2015 respectivement — trop ancien pour ce
        backfill.
      - Airtel Madagascar, Orange Madagascar : chiffres trouvés mais sans
        citation ARTEC (régulateur) directe, méthodologie de comptage
        incertaine (« abonnés actifs mensuels » pour Airtel — pas la même
        chose qu'un parc total). Écartés par prudence, pas par certitude
        d'erreur.
      - Airtel Malawi : rapport annuel statutaire d'une société cotée
        (Malawi Stock Exchange) au chiffre plausible (8,8 M), mais la date
        exacte de clôture d'exercice n'a pas pu être confirmée sans lire le
        PDF (accès bloqué en 403) — écarté pour éviter d'inventer une date.
      - MTN RDC : **ALERTE MODÈLE DE DONNÉES, PAS UN MANQUE DE RECHERCHE.**
        MTN n'exploite PAS de réseau licencié en RDC — l'ARPTC accuse même
        MTN (13 février 2026) d'un débordement de signal illégal près de
        Goma/Rutshuru depuis le Rwanda voisin. `dim_subsidiary` contient
        pourtant une entrée « MTN RDC ». À vérifier avec l'encadrant : cette
        filiale est peut-être à corriger ou retirer du modèle dimensionnel —
        ce script n'y touche pas, il se contente de ne rien y insérer.
      - Orange Congo-Brazzaville, Orange Niger : **MÊME ALERTE.** Orange
        n'opère PAS au Congo-Brazzaville (les acteurs y sont MTN, Airtel et
        Congo Télécom). Au Niger, Orange a cédé sa filiale en nov. 2019,
        rebaptisée **Zamani Telecom** depuis déc. 2020 — près de 6 ans avant
        cette recherche. `dim_subsidiary` contient pourtant les deux entrées
        « Orange Congo » et « Orange Niger ». Même remarque que MTN RDC :
        à vérifier avec l'encadrant, pas corrigé ici.
      - Airtel Niger, Moov Africa Niger : seule donnée trouvée datée T1 2024
        (ARCEP Niger) — trop ancienne pour ce backfill, pas de 2025/2026
        disponible malgré une recherche dédiée.
      - Airtel Rwanda : seule une part de marché existe (36 % d'un total
        RURA de 13,29 M) — en tirer un nombre d'abonnés serait un calcul, pas
        un chiffre publié. Même défaut qu'Orange Guinée. Écarté.
      - MTN Cameroun : chiffre trouvé (12,77 M) mais non confronté au document
        source primaire (accès bloqué) — écarté par prudence, comme Airtel
        Malawi.

MÉTHODOLOGIES MÉLANGÉES, DONC TROIS CODES `metric` DISTINCTS
    Kenya (CA) et les compteurs Tanzanie Airtel/Halotel (TCRA) sont des
    comptages RÉGULATEUR (SIM actives), au même titre que NCC/ANRT/ARCEP/NCA
    — `abonnes_regulateur`. Les rapports D'ENTREPRISE (« clients » déclarés,
    méthodologie propre à chaque groupe) portent `abonnes_entreprise`. Un
    exemple mesuré qui justifie de ne JAMAIS les fusionner sous un même code :
    Vodacom Tanzanie déclare 27,7 M clients à fin mars 2026, quand la TCRA
    comptait 33,4 M cartes SIM actives pour le même opérateur trois mois plus
    tôt (déc. 2025) — un client peut détenir plusieurs SIM, les deux chiffres
    sont vrais et pourtant différents.

    Ajouté le 24 août 2026, une TROISIÈME méthodologie — `abonnes_analyste` —
    pour le Soudan du Sud et le Liberia (MTN, Orange) : chiffres d'un cabinet
    d'analyse reconnu (Omdia), relayés par la presse, PAS un régulateur ni un
    chiffre publié par l'opérateur. Marchés trop pauvres en donnée officielle
    pour exiger mieux ; Omdia reste une estimation méthodique, pas la
    supposition d'un journaliste — voir la distinction avec Orange Égypte
    ci-dessous, qui EST une telle supposition et reste écartée.

CE QUE « TELKOM » DÉSIGNE DEUX FOIS
    `dim_operator` porte un SEUL code `telkom`, réutilisé par deux filiales
    différentes (`dim_subsidiary` distingue par pays) : Telkom Afrique du Sud
    et Telkom Kenya, deux entreprises sans lien capitaire mais qui partagent
    le nom commercial. Résolu sans ambiguïté ici parce que la clé de
    résolution est (code opérateur, iso2), jamais le code seul.

USAGE
    python -m tools.backfill_press_operator_data_2026
"""

from reviews.storage.db import get_database
from reviews.storage.operator_market_repository import OperatorMarketRepository

ENTREPRISE = "abonnes_entreprise"
REGULATEUR = "abonnes_regulateur"
ANALYSTE = "abonnes_analyste"

LIGNES = [
    # --- Afrique du Sud — rapports d'entreprise ------------------------
    {"operator_code": "vodacom", "iso2": "ZA", "metric": ENTREPRISE,
     "period": "2025-03-31", "frequency": "annual", "value": 49_200_000,
     "source": "mybroadband",
     "source_url": "https://mybroadband.co.za/news/business-telecoms/595219-vodacom-bleeds-customers-in-south-africa-but-revenue-climbs.html"},
    {"operator_code": "mtn", "iso2": "ZA", "metric": ENTREPRISE,
     "period": "2025-06-30", "frequency": "annual", "value": 39_800_000,
     "source": "itweb",
     "source_url": "https://www.itweb.co.za/article/mtn-sa-delivers-resilient-performance-in-full-year-results/rW1xLv5nwX17Rk6m"},
    {"operator_code": "cell_c", "iso2": "ZA", "metric": ENTREPRISE,
     "period": "2026-05-31", "frequency": "annual", "value": 8_900_000,
     "source": "engineering_news",
     "source_url": "https://www.engineeringnews.co.za/article/cell-c-emerges-stronger-reduces-debt-as-it-delivers-its-maiden-results-post-listing-2026-08-21"},
    {"operator_code": "telkom", "iso2": "ZA", "metric": ENTREPRISE,
     "period": "2025-12-31", "frequency": "quarterly", "value": 25_300_000,
     "source": "techafricanews",
     "source_url": "https://techafricanews.com/2026/02/16/telkoms-data-revenue-hits-60-of-total-subscriber-base-surpasses-25-million/"},

    # --- Égypte — rapports d'entreprise (Orange Égypte écarté, voir ci-dessus)
    {"operator_code": "vodafone", "iso2": "EG", "metric": ENTREPRISE,
     "period": "2025-12-31", "frequency": "annual", "value": 53_000_000,
     "source": "techafricanews",
     "source_url": "https://techafricanews.com/2026/01/27/vodafone-egypt-achieves-historic-revenue-milestone-as-subscriber-base-hits-53-million/"},
    {"operator_code": "e_and", "iso2": "EG", "metric": ENTREPRISE,
     "period": "2025-09-30", "frequency": "quarterly", "value": 41_200_000,
     "source": "telecom_review",
     "source_url": "https://www.telecomreview.com/articles/reports-and-coverage/8531-e-group-s-q3-highlights-record-growth-and-global-ambitions"},
    {"operator_code": "we", "iso2": "EG", "metric": ENTREPRISE,
     "period": "2025-03-31", "frequency": "quarterly", "value": 14_300_000,
     "source": "connecting_africa",
     "source_url": "https://www.connectingafrica.com/investment/telecom-egypt-s-mobile-users-grow-44-"},

    # --- Kenya — comptage régulateur (CA), republié par la presse -------
    {"operator_code": "safaricom", "iso2": "KE", "metric": REGULATEUR,
     "period": "2026-03-31", "frequency": "quarterly", "value": 57_900_000,
     "source": "techweez",
     "source_url": "https://techweez.com/2026/06/22/kenya-mobile-subscriptions-q3-2026/"},
    {"operator_code": "airtel", "iso2": "KE", "metric": REGULATEUR,
     "period": "2026-03-31", "frequency": "quarterly", "value": 23_200_000,
     "source": "techweez",
     "source_url": "https://techweez.com/2026/06/22/kenya-mobile-subscriptions-q3-2026/"},
    {"operator_code": "telkom", "iso2": "KE", "metric": REGULATEUR,
     "period": "2026-03-31", "frequency": "quarterly", "value": 584_434,
     "source": "techcabal",
     "source_url": "https://techcabal.com/2026/04/04/telkom-drops-to-fifth-and-last/"},

    # --- Tanzanie — Vodacom en rapport d'entreprise, Airtel/Halotel en régulateur (TCRA)
    {"operator_code": "vodacom", "iso2": "TZ", "metric": ENTREPRISE,
     "period": "2026-03-31", "frequency": "annual", "value": 27_700_000,
     "source": "vodacom_tanzania",
     "source_url": "https://vodacom.co.tz/assets/uploads/Vodacom_Tanzania_Preliminary_Results_FY_2026_32838f48e2.pdf"},
    {"operator_code": "airtel", "iso2": "TZ", "metric": REGULATEUR,
     "period": "2025-12-31", "frequency": "quarterly", "value": 23_283_636,
     "source": "tanzania_invest",
     "source_url": "https://www.tanzaniainvest.com/telecoms/tcra-telecom-stats-q4-2025"},
    {"operator_code": "halotel", "iso2": "TZ", "metric": REGULATEUR,
     "period": "2025-12-31", "frequency": "quarterly", "value": 17_590_894,
     "source": "tanzania_invest",
     "source_url": "https://www.tanzaniainvest.com/telecoms/tcra-telecom-stats-q4-2025"},

    # --- Algérie — Ooredoo seulement, voir la note sur Djezzy/Mobilis ---
    {"operator_code": "ooredoo", "iso2": "DZ", "metric": ENTREPRISE,
     "period": "2026-06-30", "frequency": "annual", "value": 15_900_000,
     "source": "dzair_tube",
     "source_url": "https://www.dzair-tube.dz/en/ooredoo-algeria-expands-subscriber-base-to-15-9-million-as-first-half-revenue-climbs-14-5/"},

    # --- Côte d'Ivoire — comptage régulateur (ARTCI), les TROIS opérateurs
    # à la fois : contrairement à l'Algérie, la source republie un décompte
    # complet, ce qui évite une couverture asymétrique.
    {"operator_code": "orange", "iso2": "CI", "metric": REGULATEUR,
     "period": "2025-06-30", "frequency": "quarterly", "value": 30_865_164,
     "source": "koaci",
     "source_url": "https://www.koaci.com/index.php/article/2025/09/11/cote-divoire/societe/cote-divoire-telephonie-mobile-59942676-dabonnes-au-30-juin-2025-et-un-chiffre-daffaires-de-plus-de-257-milliards-ht_190072.html"},
    {"operator_code": "mtn", "iso2": "CI", "metric": REGULATEUR,
     "period": "2025-06-30", "frequency": "quarterly", "value": 17_116_804,
     "source": "koaci",
     "source_url": "https://www.koaci.com/index.php/article/2025/09/11/cote-divoire/societe/cote-divoire-telephonie-mobile-59942676-dabonnes-au-30-juin-2025-et-un-chiffre-daffaires-de-plus-de-257-milliards-ht_190072.html"},
    {"operator_code": "moov_africa", "iso2": "CI", "metric": REGULATEUR,
     "period": "2025-06-30", "frequency": "quarterly", "value": 11_960_708,
     "source": "koaci",
     "source_url": "https://www.koaci.com/index.php/article/2025/09/11/cote-divoire/societe/cote-divoire-telephonie-mobile-59942676-dabonnes-au-30-juin-2025-et-un-chiffre-daffaires-de-plus-de-257-milliards-ht_190072.html"},

    # --- RDC — comptage régulateur (ARPTC), chiffres ARRONDIS PAR LA SOURCE
    # ELLE-MÊME (« plus de 25 millions », pas un décompte exact comme les
    # autres lignes) : reportés tels quels, sans fausse précision inventée.
    {"operator_code": "vodacom", "iso2": "CD", "metric": REGULATEUR,
     "period": "2025-09-30", "frequency": "quarterly", "value": 25_000_000,
     "source": "finances_entreprises",
     "source_url": "https://finances-entreprises.com/rdc-les-abonnements-actifs-dans-la-telephonie-mobile-evalues-7328-millions-a-fin-septembre-2025/"},
    {"operator_code": "orange", "iso2": "CD", "metric": REGULATEUR,
     "period": "2025-09-30", "frequency": "quarterly", "value": 23_000_000,
     "source": "finances_entreprises",
     "source_url": "https://finances-entreprises.com/rdc-les-abonnements-actifs-dans-la-telephonie-mobile-evalues-7328-millions-a-fin-septembre-2025/"},
    {"operator_code": "airtel", "iso2": "CD", "metric": REGULATEUR,
     "period": "2025-09-30", "frequency": "quarterly", "value": 21_000_000,
     "source": "finances_entreprises",
     "source_url": "https://finances-entreprises.com/rdc-les-abonnements-actifs-dans-la-telephonie-mobile-evalues-7328-millions-a-fin-septembre-2025/"},
    {"operator_code": "africell", "iso2": "CD", "metric": REGULATEUR,
     "period": "2025-09-30", "frequency": "quarterly", "value": 3_000_000,
     "source": "finances_entreprises",
     "source_url": "https://finances-entreprises.com/rdc-les-abonnements-actifs-dans-la-telephonie-mobile-evalues-7328-millions-a-fin-septembre-2025/"},

    # --- Sénégal — Orange seul (ARTP via presse) : trouvé dans la MÊME
    # source que le total pays déjà utilisé pour la RDC/section historique,
    # mais laissé de côté par erreur lors de la première passe. Déplace le
    # Sénégal de la section pays vers la section opérateur (asymétrique,
    # Orange seul sur 3 — même traitement qu'Ooredoo Algérie) : voir
    # backfill_press_country_data_2026.py, la ligne Sénégal y a été retirée
    # pour ne pas doubler ce pays entre les deux sections.
    {"operator_code": "orange", "iso2": "SN", "metric": REGULATEUR,
     "period": "2025-12-31", "frequency": "quarterly", "value": 13_640_000,
     "source": "techafricanews",
     "source_url": "https://techafricanews.com/2026/03/17/senegals-mobile-connections-grow-4-4-in-2025-despite-q4-subscriber-loss/"},

    # --- Moov Africa — 6 filiales d'un seul coup, MEME SOURCE : les résultats
    # annuels 2025 du groupe Maroc Telecom (parent), publiés le 16 février
    # 2026, détaillent le parc par pays au 31 décembre 2025. Rapport
    # d'entreprise, pas régulateur — `abonnes_entreprise`. La Côte d'Ivoire
    # (13,403 M dans ce même rapport) N'EST PAS reprise ici : elle a déjà un
    # chiffre RÉGULATEUR (ARTCI, 11 960 708 à fin juin 2025) plus rigoureux —
    # un pays ne garde jamais les deux représentations.
    {"operator_code": "moov_africa", "iso2": "BF", "metric": ENTREPRISE,
     "period": "2025-12-31", "frequency": "annual", "value": 7_296_000,
     "source": "consonews",
     "source_url": "https://consonews.ma/64093.html"},
    {"operator_code": "moov_africa", "iso2": "GA", "metric": ENTREPRISE,
     "period": "2025-12-31", "frequency": "annual", "value": 1_609_000,
     "source": "consonews",
     "source_url": "https://consonews.ma/64093.html"},
    {"operator_code": "moov_africa", "iso2": "ML", "metric": ENTREPRISE,
     "period": "2025-12-31", "frequency": "annual", "value": 7_147_000,
     "source": "consonews",
     "source_url": "https://consonews.ma/64093.html"},
    {"operator_code": "moov_africa", "iso2": "NE", "metric": ENTREPRISE,
     "period": "2025-12-31", "frequency": "annual", "value": 4_547_000,
     "source": "consonews",
     "source_url": "https://consonews.ma/64093.html"},
    {"operator_code": "moov_africa", "iso2": "CF", "metric": ENTREPRISE,
     "period": "2025-12-31", "frequency": "annual", "value": 346_000,
     "source": "consonews",
     "source_url": "https://consonews.ma/64093.html"},
    {"operator_code": "moov_africa", "iso2": "TG", "metric": ENTREPRISE,
     "period": "2025-12-31", "frequency": "annual", "value": 4_011_000,
     "source": "consonews",
     "source_url": "https://consonews.ma/64093.html"},

    # --- Tchad — comptage régulateur (ARCEP Tchad), révélé en oct. 2025 mais
    # mesuré à fin 2024 : même profil de fraîcheur qu'ARCEP Bénin, déjà
    # accepté (source annuelle qui publie avec retard).
    {"operator_code": "airtel", "iso2": "TD", "metric": REGULATEUR,
     "period": "2024-12-31", "frequency": "annual", "value": 7_400_000,
     "source": "ecomatin",
     "source_url": "https://ecomatin.net/tchad-le-nombre-dabonnes-a-la-telephonie-mobile-frole-15-millions-arcep"},

    # --- Burkina Faso — comptage régulateur (ARCEP), fin 2024
    {"operator_code": "moov_africa", "iso2": "BF", "metric": REGULATEUR,
     "period": "2024-12-31", "frequency": "annual", "value": 12_023_000,
     "source": "horonyafinance",
     "source_url": "https://www.horonyafinance.com/burkina-telephonie-mobile-sur-un-total-de-27455-millions-dabonnes-orange-et-moov-africa-dominent-le-marche-avec-respectivement-12636-millions-et-12023-million/"},
    {"operator_code": "orange", "iso2": "BF", "metric": REGULATEUR,
     "period": "2024-12-31", "frequency": "annual", "value": 12_636_000,
     "source": "horonyafinance",
     "source_url": "https://www.horonyafinance.com/burkina-telephonie-mobile-sur-un-total-de-27455-millions-dabonnes-orange-et-moov-africa-dominent-le-marche-avec-respectivement-12636-millions-et-12023-million/"},

    # --- Lesotho, Mozambique — rapports d'entreprise Vodacom
    {"operator_code": "vodacom", "iso2": "LS", "metric": ENTREPRISE,
     "period": "2025-03-31", "frequency": "annual", "value": 1_600_000,
     "source": "vodacom_lesotho",
     "source_url": "https://www.vodacom.co.ls/assets/uploads/docs/vodacom-lesotho-esg-report-2025.pdf"},
    {"operator_code": "vodacom", "iso2": "MZ", "metric": ENTREPRISE,
     "period": "2025-03-31", "frequency": "annual", "value": 12_453_000,
     "source": "club_of_mozambique",
     "source_url": "https://clubofmozambique.com/news/vodacom-mozambique-surpasses-12-million-customers/"},

    # --- Soudan du Sud, Liberia — ANALYSTE (Omdia via presse), PAS un
    # régulateur ni un chiffre publié par l'opérateur lui-même : troisième
    # méthodologie, troisième code `metric` (`abonnes_analyste`). Marchés trop
    # pauvres en donnée officielle (Soudan du Sud, Liberia) pour exiger mieux,
    # mais Omdia reste un cabinet reconnu — pas une estimation de journaliste
    # rétro-calculée (voir le rejet d'Orange Égypte, qui LUI est une vraie
    # estimation de journaliste).
    {"operator_code": "mtn", "iso2": "SS", "metric": ANALYSTE,
     "period": "2025-09-30", "frequency": "quarterly", "value": 3_900_000,
     "source": "connecting_africa",
     "source_url": "https://www.connectingafrica.com/connectivity/mtn-south-sudan-expands-its-network-to-rural-abuyong"},
    {"operator_code": "mtn", "iso2": "LR", "metric": ANALYSTE,
     "period": "2026-06-30", "frequency": "quarterly", "value": 2_300_000,
     "source": "connecting_africa",
     "source_url": "https://www.connectingafrica.com/regulation/liberia-strips-starcell-of-operating-license"},
    {"operator_code": "orange", "iso2": "LR", "metric": ANALYSTE,
     "period": "2026-06-30", "frequency": "quarterly", "value": 3_200_000,
     "source": "connecting_africa",
     "source_url": "https://www.connectingafrica.com/regulation/liberia-strips-starcell-of-operating-license"},

    # --- Congo-Brazzaville — comptage régulateur (ARPCE) via presse
    {"operator_code": "airtel", "iso2": "CG", "metric": REGULATEUR,
     "period": "2026-04-30", "frequency": "annual", "value": 2_400_000,
     "source": "sikafinance",
     "source_url": "https://www.sikafinance.com/marches/congo-le-marche-de-la-telephonie-mobile-genere-6-4-milliards-fcfa-de-revenus-a-fin-avril-2026_63408"},
    {"operator_code": "mtn", "iso2": "CG", "metric": REGULATEUR,
     "period": "2026-04-30", "frequency": "annual", "value": 3_600_000,
     "source": "sikafinance",
     "source_url": "https://www.sikafinance.com/marches/congo-le-marche-de-la-telephonie-mobile-genere-6-4-milliards-fcfa-de-revenus-a-fin-avril-2026_63408"},

    # --- Rwanda — rapport d'entreprise (MTN Group)
    {"operator_code": "mtn", "iso2": "RW", "metric": ENTREPRISE,
     "period": "2025-09-30", "frequency": "quarterly", "value": 8_100_000,
     "source": "ktpress",
     "source_url": "https://www.connectingafrica.com/connectivity/mtn-grows-market-share-to-almost-65-in-rwanda"},

    # --- Zambie — Airtel en rapport d'entreprise, MTN en ANALYSTE (rapport
    # PwC relayé par la presse, pas un chiffre publié par MTN lui-même ni un
    # comptage ZICTA)
    {"operator_code": "airtel", "iso2": "ZM", "metric": ENTREPRISE,
     "period": "2025-09-30", "frequency": "quarterly", "value": 12_300_000,
     "source": "equityaxis",
     "source_url": "https://equityaxis.net/post/18894/2026/3/airtel-zambia-deploys-107-million-in-network-expansion-406-new-sites-added-targets-95-population-coverage"},
    {"operator_code": "mtn", "iso2": "ZM", "metric": ANALYSTE,
     "period": "2025-09-30", "frequency": "quarterly", "value": 6_700_000,
     "source": "equityaxis",
     "source_url": "https://equityaxis.net/post/18894/2026/3/airtel-zambia-deploys-107-million-in-network-expansion-406-new-sites-added-targets-95-population-coverage"},

    # --- Ouganda — rapports annuels d'entreprise (exercice clos 31 déc. 2025)
    {"operator_code": "airtel", "iso2": "UG", "metric": ENTREPRISE,
     "period": "2025-12-31", "frequency": "annual", "value": 19_200_000,
     "source": "pctechmag",
     "source_url": "https://pctechmag.com/2026/02/airtel-uganda-posts-strong-revenue-growth-in-fy2025-profit-rises-to-ugx-446-8-billion/"},
    {"operator_code": "mtn", "iso2": "UG", "metric": ENTREPRISE,
     "period": "2025-12-31", "frequency": "annual", "value": 24_200_000,
     "source": "pulse_ug",
     "source_url": "https://www.pulse.ug/story/2025-financial-results-comparing-mtn-and-airtel-uganda-revenue-profits-other-key-figures-2026031415214390137"},

    # --- Cameroun — Orange en rapport d'entreprise officiel (MTN Cameroun
    # écarté, voir la note plus haut : chiffre non confirmé contre le document
    # source)
    {"operator_code": "orange", "iso2": "CM", "metric": ENTREPRISE,
     "period": "2024-12-31", "frequency": "annual", "value": 13_050_000,
     "source": "digitalbusiness_africa",
     "source_url": "https://www.digitalbusiness.africa/orange-cameroun-franchit-la-barre-des-200-milliards-de-francs-cfa-de-revenus-au-1er-semestre-2025/"},

    # --- Gabon — comptage régulateur EXACT (ARCEP Gabon, PDF primaire),
    # meilleure source de tout ce backfill : chiffre précis à l'unité, date
    # exacte, document officiel directement lu.
    {"operator_code": "airtel", "iso2": "GA", "metric": REGULATEUR,
     "period": "2025-03-31", "frequency": "quarterly", "value": 1_554_352,
     "source": "arcep_gabon",
     "source_url": "https://www.arcep.ga/uploads/observatoires/mobile/Mobile%202025-1.pdf"},
    {"operator_code": "moov_africa", "iso2": "GA", "metric": REGULATEUR,
     "period": "2025-03-31", "frequency": "quarterly", "value": 1_625_538,
     "source": "arcep_gabon",
     "source_url": "https://www.arcep.ga/uploads/observatoires/mobile/Mobile%202025-1.pdf"},
]


def main() -> None:
    db = get_database()
    try:
        ecrites = OperatorMarketRepository(db).upsert(LIGNES)
        print(f"✓ {ecrites} mesure(s) enregistrée(s) sur {len(LIGNES)} préparée(s)")
    finally:
        db.close_all()


if __name__ == "__main__":
    main()
