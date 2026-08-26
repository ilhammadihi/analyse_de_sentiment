"""
Backfill PONCTUEL — abonnés mobile 2025-2026 par PAYS, sourcés presse/régulateur.

POURQUOI CE N'EST PAS UN COLLECTEUR, ET POURQUOI CE N'EST PAS PAR OPÉRATEUR
    Même principe que `backfill_press_operator_data_2026.py` : chaque ligne
    vient d'un article de presse ponctuel citant un régulateur national, pas
    d'une page qu'on peut réinterroger — ce script s'exécute UNE FOIS, à la
    main, jamais planifié.

    À LA MAILLE PAYS et non opérateur, parce que la recherche du 24 août 2026
    n'a trouvé, pour l'Ouganda, qu'un TOTAL national — pas de répartition par
    opérateur assez fraîche et sourcée (MTN/Airtel datent de mi-2024
    seulement). Publier un total nu vaut mieux qu'inventer une répartition.

    LE SÉNÉGAL A ÉTÉ RETIRÉ D'ICI LE 24 AOÛT 2026 (retrouvé après coup dans la
    même source déjà citée pour son total : Orange y a un chiffre exact,
    13,64 M, ARTP T4 2025). Il vit maintenant dans
    `backfill_press_operator_data_2026.py`, asymétrique (Orange seul sur 3,
    même traitement qu'Ooredoo Algérie) — voir la note ci-dessous sur pourquoi
    un pays ne garde jamais les deux représentations à la fois.

MÊME INDICATEUR QUE LA BANQUE MONDIALE/UIT, `provider` DISTINCT
    Ces lignes réutilisent `IT_CEL_SETS`/`SB` — LE MÊME COUPLE indicateur/
    unité que `market_data.py` — avec `provider='press'` plutôt qu'un code
    inventé : c'est la même GRANDEUR (abonnements mobiles totaux, pays), donc
    la même clé de lecture pour `MarketRepository.latest()` / `latest_by_country()`
    (qui prennent l'année la plus récente TOUS PROVIDERS confondus). Un
    provider différent, lui, est indispensable : sans lui, un 2025 `press`
    remplacerait silencieusement un 2024 `worldbank_itu` sans que l'écran
    puisse dire d'où vient le chiffre affiché — voir la migration 026 et son
    usage dans `TabMarche.tsx` (badge « presse »).

CE QUI EST ÉCARTÉ, ET POURQUOI
    Les pays déjà couverts au niveau OPÉRATEUR (Nigeria, Maroc, Bénin, Ghana,
    Afrique du Sud, Égypte, Kenya, Tanzanie, Algérie, Côte d'Ivoire, RDC) ne
    sont PAS repris ici, même quand un total national existe par ailleurs
    (l'Afrique du Sud a un total ICASA, p. ex.) : afficher un total pays ET
    une somme d'opérateurs pour le même pays réintroduirait exactement le
    problème « deux chiffres pour le même fait » qu'on a retiré du dashboard
    le 24 août 2026 — voir TabMarche.tsx. Un pays n'a qu'UNE représentation
    ici : la plus fine disponible, jamais les deux.

USAGE
    python -m tools.backfill_press_country_data_2026
"""

from reviews.storage.db import get_database
from reviews.storage.market_repository import MarketRepository

INDICATOR = "IT_CEL_SETS"
UNIT = "SB"
PROVIDER = "press"

# (iso3, year, value, source, source_url)
LIGNES = [
    ("UGA", 2026, 61_600_000, "chimpreports",
     "https://chimpreports.com/ugandas-mobile-subscriptions-soar-to-61-6-million-ucc/"),
]


def main() -> None:
    db = get_database()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT iso3, country_id FROM dim_country WHERE iso3 IS NOT NULL")
            pays = {r[0]: r[1] for r in cur.fetchall()}

        lignes = []
        introuvables = []
        for iso3, year, value, source, source_url in LIGNES:
            country_id = pays.get(iso3)
            if country_id is None:
                introuvables.append(iso3)
                continue
            lignes.append({
                "country_id": country_id, "indicator": INDICATOR, "unit": UNIT,
                "year": year, "value": value, "provider": PROVIDER,
                "source_url": source_url,
            })

        ecrites = MarketRepository(db).upsert(lignes)
        print(f"✓ {ecrites} mesure(s) enregistrée(s) sur {len(LIGNES)} préparée(s)")
        if introuvables:
            print(f"⚠ pays introuvable(s) dans dim_country : {', '.join(introuvables)}")
    finally:
        db.close_all()


if __name__ == "__main__":
    main()
