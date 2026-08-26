-- ===========================================================================
-- 025 — operator_market_indicators : le contexte marché, PAR OPÉRATEUR
-- ===========================================================================
--
-- POURQUOI UNE TABLE À PART DE `market_indicators`
--   `market_indicators` (014) est clé sur `country_id` : elle ne peut pas
--   porter "MTN Nigeria a gagné 2 % d'abonnés" sans mentir sur la maille. Cette
--   table est clé sur `subsidiary_id` (le croisement opérateur × pays déjà
--   modélisé par `dim_subsidiary`), et ajoute deux colonnes que la source pays
--   n'avait pas besoin de porter :
--
--     - `period` est une DATE, pas une année : les régulateurs nationaux
--       publient mensuellement ou trimestriellement, jamais annuellement.
--     - `frequency` accompagne chaque mesure. Voir `market_indicators` (014) et la
--       recherche du 23 août 2026 : NCC Nigeria est mensuel, ANRT Maroc mêle
--       mensuel et trimestriel selon le jeu de données, CA Kenya/ARTP
--       Sénégal/ARCEP Bénin sont trimestriels. Forcer une cadence unique
--       aurait fait mentir le dashboard par arrondi ("dernière donnée : mai
--       2026" pour un chiffre en réalité trimestriel).
--
-- POURQUOI `subsidiary_id` ET NON (operator_id, country_id) SÉPARÉMENT
--   `dim_subsidiary` porte déjà l'unicité (operator_id, country_id) — voir
--   002. La réutiliser évite une deuxième source de vérité sur "quel
--   opérateur existe dans quel pays", et permet de rejoindre directement
--   `v_stats_by_subsidiary` pour croiser satisfaction et donnée réelle.
--
-- POURQUOI `source` ET `source_url` SONT DEUX COLONNES DISTINCTES DE `metric`
--   `source` identifie le RÉGULATEUR ('ncc_nigeria', puis 'anrt_maroc',
--   'ca_kenya'...) et fait partie de la clé : deux régulateurs pourraient un
--   jour publier le même indicateur pour la même filiale (rare, mais pas
--   impossible aux frontières). `source_url` n'est PAS dans la clé — c'est une
--   traçabilité, pas une dimension — et permet à l'écran de citer la page
--   d'origine plutôt qu'un nom de fournisseur opaque.
CREATE TABLE IF NOT EXISTS operator_market_indicators (
    subsidiary_id INTEGER      NOT NULL REFERENCES dim_subsidiary(subsidiary_id),

    --: Code interne, court et stable. 'abonnes_gsm' pour commencer — voir
    --: `ncc_nigeria.py`. Volontairement distinct des codes `market_indicators`
    --: (IT_CEL_SETS…) : ce ne sont pas les mêmes fournisseurs, pas la même clé
    --: de vérité, les confondre romprait la distinction pays/opérateur.
    metric        VARCHAR(40)  NOT NULL,

    --: Premier jour de la période couverte (mois ou trimestre). Une DATE et
    --: non une année : voir la note en tête de fichier.
    period        DATE         NOT NULL,

    --: 'monthly' | 'quarterly' | 'annual'. Porté par la ligne, jamais déduit
    --: du fournisseur à l'affichage : deux jeux de données d'un même
    --: régulateur (ANRT) peuvent avoir des cadences différentes.
    frequency     VARCHAR(10)  NOT NULL,

    value         DOUBLE PRECISION NOT NULL,

    --: Régulateur ou fournisseur. Fait partie de la clé — voir ci-dessus.
    source        VARCHAR(40)  NOT NULL,

    --: Page d'origine, pour que l'écran puisse citer sa source précisément.
    source_url    TEXT,

    collected_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),

    PRIMARY KEY (subsidiary_id, metric, period, source),
    CONSTRAINT chk_operator_market_frequency
        CHECK (frequency IN ('monthly', 'quarterly', 'annual'))
);

--: Lecture typique : tout l'historique d'une filiale, du plus récent au plus
--: ancien — la série qu'affiche une fiche opérateur.
CREATE INDEX IF NOT EXISTS idx_operator_market_subsidiary
    ON operator_market_indicators (subsidiary_id, period DESC);

--: Lecture transverse : un indicateur donné sur toutes les filiales, pour un
--: classement ou une comparaison.
CREATE INDEX IF NOT EXISTS idx_operator_market_metric
    ON operator_market_indicators (metric, period DESC);
