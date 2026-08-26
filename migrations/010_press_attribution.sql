-- =============================================================================
-- 010 — Attribution de la presse
--
-- LE PROBLÈME CORRIGÉ
--   `rss_feed` interroge Google News avec « <opérateur> <mot-clé> » puis écrit
--   `company = <terme de recherche>`, sans jamais vérifier que l'article
--   concerne cet opérateur. Google News étant un moteur flou, la conséquence
--   est mesurée sur les 7 718 articles collectés :
--
--     confirmés (la filiale est nommée) ......... 3 041   39,4 %
--     à réattribuer (une AUTRE est nommée) .........484    6,3 %
--     actualité de groupe (aucun pays nommé) .... 1 167   15,1 %
--     bruit (personne n'est nommé) .............. 3 026   39,2 %
--
--   Deux filiales illustrent l'ampleur : Telesom et TN Mobile totalisent 526
--   articles, dont AUCUN ne les mentionne.
--
-- CE QUE CETTE MIGRATION AJOUTE, ET POURQUOI
--
--   1. `attribution` — l'état de chaque article, plutôt qu'une suppression.
--      Les 3 026 articles de bruit sont MARQUÉS, pas effacés : si la règle de
--      reconnaissance se révèle fautive, la donnée est toujours là et le tri se
--      rejoue. Effacer rendrait l'erreur définitive.
--
--   2. `operator_id` sur `reviews` — pour l'actualité de GROUPE.
--      « Orange Money réduit ses frais de retrait de 1 % » ou « Airtel Africa
--      prépare l'introduction en bourse d'Airtel Money » nomment l'opérateur et
--      aucun pays. Ce sont précisément les événements qui expliquent un
--      mouvement de satisfaction sur TOUTES les filiales d'un opérateur, et le
--      modèle ne savait pas les écrire : `reviews` ne connaissait que
--      `subsidiary_id`, donc un article devait mentir sur son pays ou
--      disparaître. 1 167 articles étaient dans ce cas.
--
--   3. `attribution_version` — quelle règle a jugé la ligne, pour pouvoir
--      rejouer un tri après correction du lexique sans confondre deux verdicts.
--
-- CE QU'ELLE NE FAIT PAS
--   Elle ne classe rien : elle ouvre les colonnes. Le classement est appliqué
--   par `tools/reattribute_press.py`, qui s'exécute séparément et sait tourner
--   à blanc.
-- =============================================================================

ALTER TABLE reviews
    ADD COLUMN IF NOT EXISTS operator_id INTEGER REFERENCES dim_operator(operator_id),
    ADD COLUMN IF NOT EXISTS attribution VARCHAR(16),
    ADD COLUMN IF NOT EXISTS attribution_version SMALLINT;

COMMENT ON COLUMN reviews.operator_id IS
    'Opérateur, quand la filiale est indéterminée (actualité de groupe). '
    'Redondant avec dim_subsidiary quand subsidiary_id est renseigné : la vue '
    'privilégie toujours la filiale.';

COMMENT ON COLUMN reviews.attribution IS
    'confirmed | reattributed | group | noise. NULL = jamais vérifié.';

-- Le filtrage du bruit passe par cette colonne sur chaque lecture de la vue :
-- sans index, 7 718 lignes aujourd'hui et bien plus demain seraient balayées à
-- chaque appel d'agrégat.
CREATE INDEX IF NOT EXISTS idx_reviews_attribution
    ON reviews (attribution)
    WHERE attribution IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_reviews_operator
    ON reviews (operator_id)
    WHERE operator_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- Vue enrichie
--
-- DEUX CHANGEMENTS, ET RIEN D'AUTRE :
--
--   * l'opérateur se replie sur `reviews.operator_id` quand aucune filiale
--     n'est rattachée — le pays reste NULL, ce qui est la vérité : une
--     actualité de groupe n'en a pas ;
--   * les articles marqués « noise » sortent de la vue, donc de TOUS les écrans
--     et de tous les agrégats d'un coup. `IS DISTINCT FROM` et non `<>` :
--     avec `<>`, les lignes jamais vérifiées (NULL) disparaîtraient aussi.
--
-- Les lignes restent dans `reviews` et se consultent directement pour audit.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_reviews_enriched AS
SELECT r.review_id,
       r.title,
       r.text,
       r.rating,
       r.verified,
       r.created_at,
       r.collected_at,
       r.run_id,
       COALESCE(r.llm_sentiment, r.sentiment) AS sentiment,
       CASE
           WHEN r.llm_sentiment IS NOT NULL THEN 'llm'::text
           ELSE 'lexicon'::text
       END AS sentiment_source,
       r.sentiment AS lexicon_sentiment,
       r.llm_sentiment,
       r.llm_confidence,
       r.aspect_version,
       r.llm_analyzed_at,
       r.sentiment_score,
       r.neg_terms,
       r.pos_terms,
       r.lexicon_version,
       r.neg_aspects,
       r.pos_aspects,
       sub.subsidiary_id,
       sub.name AS subsidiary,
       op.operator_id,
       op.name AS operator,
       op.parent_group,
       co.country_id,
       co.iso2,
       co.name AS country,
       co.region,
       src.source_id,
       src.code AS source_code,
       src.name AS source,
       src.kind AS source_kind,
       COALESCE(src.comparable, true) AS source_comparable,
       r.attribution
  FROM reviews r
  LEFT JOIN dim_subsidiary sub ON sub.subsidiary_id = r.subsidiary_id
  -- Filiale d'abord, opérateur de l'article en repli.
  LEFT JOIN dim_operator op ON op.operator_id = COALESCE(sub.operator_id, r.operator_id)
  LEFT JOIN dim_country co ON co.country_id = sub.country_id
  LEFT JOIN dim_source src ON src.source_id = r.source_id
 WHERE r.attribution IS DISTINCT FROM 'noise';
