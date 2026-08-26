-- ===========================================================================
-- 014 — market_indicators : le contexte marché, à côté de la satisfaction
-- ===========================================================================
--
-- POURQUOI UNE TABLE À PART, ET SURTOUT PAS DANS `reviews`
--   Un indicateur de marché n'est pas un avis. Il n'a ni auteur, ni note, ni
--   sentiment, ni texte ; il porte un pays, une année et une valeur. L'insérer
--   dans `reviews` obligerait à laisser vides quinze colonnes et, surtout, il
--   entrerait dans tous les agrégats de satisfaction — la règle `source_kind`
--   existe précisément pour empêcher la presse d'y entrer, on ne va pas la
--   contourner par une autre porte.
--
-- CE QUE CETTE TABLE PERMET, ET QUE LES AVIS NE PERMETTAIENT PAS
--   Répondre à « la satisfaction baisse-t-elle PARCE QUE le réseau est
--   mauvais ? ». Jusqu'ici le corpus ne contenait que l'opinion ; il ne
--   contenait aucun fait mesurable sur le réseau lui-même. Un recul de
--   satisfaction dans un pays couvert à 99,8 % en 4G ne raconte pas la même
--   histoire que le même recul dans un pays couvert à 60 %.
--
-- LA MAILLE, ET SA LIMITE — À CONNAÎTRE AVANT DE PROMETTRE
--   La source retenue (Banque Mondiale / UIT, dataset ITU_DH) est gratuite,
--   sans clé, et couvre ~200 économies : elle porte donc les 38 pays du
--   périmètre d'un seul appel. Mais elle est PAR PAYS et ANNUELLE, jamais par
--   opérateur. « Orange Maroc a gagné 5 % d'abonnés » n'est PAS soutenable par
--   cette table ; « le Maroc compte 58,3 M d'abonnements mobiles en 2024 » l'est.
--   La granularité opérateur demande les régulateurs nationaux, un par pays.
--
-- POURQUOI L'UNITÉ FAIT PARTIE DE LA CLÉ
--   Mesuré sur la source : `IT_CEL_SETS` rend DEUX lignes pour le Maroc 2024 —
--   58 286 168 en unité `SB` (abonnements) et 153,06 en `SB_10P2_HB` (pour 100
--   habitants). Sans l'unité dans la clé, la seconde écrase la première selon
--   l'ordre d'arrivée, et l'écran afficherait « 153 abonnés au Maroc ».

CREATE TABLE IF NOT EXISTS market_indicators (
    country_id    INTEGER      NOT NULL REFERENCES dim_country(country_id),

    --: Code de l'indicateur chez le fournisseur (IT_CEL_SETS, MOB_COV_4G…).
    --: Conservé tel quel plutôt que traduit : c'est lui qui permet de
    --: retrouver la donnée à sa source, et un libellé traduit se périme.
    indicator     VARCHAR(40)  NOT NULL,

    --: Unité au sens du fournisseur. Fait partie de la clé — voir ci-dessus.
    unit          VARCHAR(20)  NOT NULL,

    --: Année d'observation. La source est annuelle ; un SMALLINT suffit et
    --: interdit d'y ranger par erreur une date complète.
    year          SMALLINT     NOT NULL,

    value         DOUBLE PRECISION NOT NULL,

    --: Fournisseur, pour qu'une donnée reste rattachable à son origine le jour
    --: où une seconde source (régulateur national) alimentera la même table.
    provider      VARCHAR(40)  NOT NULL DEFAULT 'worldbank_itu',

    collected_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (country_id, indicator, unit, year, provider)
);

--: Lecture typique : tous les indicateurs d'un pays, du plus récent au plus
--: ancien — c'est la requête du dashboard et celle de l'agent.
CREATE INDEX IF NOT EXISTS idx_market_country_year
    ON market_indicators (country_id, year DESC);

--: Lecture transverse : un indicateur donné sur tous les pays, pour les
--: classements et les comparaisons.
CREATE INDEX IF NOT EXISTS idx_market_indicator_year
    ON market_indicators (indicator, year DESC);
