-- ===========================================================================
-- 023 — Réattribution des avis orphelins, avec retour arrière possible
-- ===========================================================================
--
-- LE PROBLÈME, MESURÉ LE 17 AOÛT 2026
--   1 215 avis portent `subsidiary_id IS NULL` : 1 200 articles RSS, 13 avis
--   Google Maps, 2 articles de presse tech. Ils sont collectés, stockés, payés
--   en temps de scraping — et INVISIBLES dans toutes les vues du dashboard,
--   qui joignent toutes sur `dim_subsidiary`. Aucune erreur ne le signale :
--   c'est le mode de panne que la migration 002 documentait déjà en creux.
--
-- CE QUE L'ANALYSE A RÉVÉLÉ, ET QUI CHANGE TOUT LE DIMENSIONNEMENT
--   Ces avis ne sont PAS ambigus. Sur les 1 215 :
--
--       1 202  correspondent EXACTEMENT à un alias de dim_subsidiary
--       1 043  correspondent en plus au NOM de la filiale
--          13  ne correspondent à rien — tous « Orange Senegal », c'est-à-dire
--              « Orange Sénégal » sans son accent
--
--   Il n'y a donc aucune décision à prendre : il y a un rattachement à
--   REJOUER. Confier cela à un modèle serait payer du quota pour redécouvrir
--   une égalité de chaînes, avec un risque d'erreur là où il n'y en a aucun.
--
-- LA FUITE EST DÉJÀ COLMATÉE — ET C'EST CE QUI REND L'OPÉRATION SÛRE
--   Le prédicat d'insertion (`repository.py`, `v.company = ANY(s.aliases)`)
--   fonctionne aujourd'hui. Mesuré : 24 256 avis collectés depuis le 5 août,
--   ZÉRO orphelin ; le plus récent des orphelins date du 4 août. Ces lignes
--   sont un ARRIÉRÉ HISTORIQUE FIGÉ, né de la période où les filiales et leurs
--   alias étaient encore en cours de déclaration (migrations 003 et suivantes).
--
--   On traite donc un stock borné qui ne grossit plus, et non une hémorragie.
--
-- POURQUOI UNE TABLE PLUTÔT QU'UN UPDATE DIRECT
--   Un `UPDATE reviews SET subsidiary_id = ...` serait irréversible : l'état
--   antérieur — NULL — disparaîtrait, et avec lui toute possibilité de dire
--   quels avis ont été rattachés par une machine plutôt que par la collecte.
--   Six semaines plus tard, personne ne pourrait distinguer les deux.
--
--   Cette table conserve l'avis, son état antérieur, la filiale proposée, la
--   méthode, la confiance, les preuves et la date. Le retour arrière est une
--   requête, pas une restauration de sauvegarde.
--
-- NON DESTRUCTIF — une table nouvelle. `reviews` n'est modifiée que par une
-- application explicite, tracée ligne à ligne ici.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS orphan_resolutions (
    resolution_id   BIGSERIAL PRIMARY KEY,

    review_id       TEXT        NOT NULL REFERENCES reviews(review_id)
                                ON DELETE CASCADE,

    --: Ce que l'avis disait de lui-même. Conservé tel quel : c'est la seule
    --: trace de ce sur quoi la décision a porté, si `dim_subsidiary` change.
    company         TEXT,
    source_code     VARCHAR(50),

    --: ÉTAT ANTÉRIEUR. NULL dans la quasi-totalité des cas — c'est le sujet —
    --: mais la colonne existe pour que la table serve aussi à une éventuelle
    --: RE-attribution (corriger un rattachement faux), où l'ancien
    --: identifiant est précisément ce qu'il faut pouvoir restaurer.
    previous_subsidiary_id INTEGER REFERENCES dim_subsidiary(subsidiary_id),

    --: Filiale PROPOSÉE. Tant que `applied_at` est nul, ce n'est qu'une
    --: proposition : rien n'a été écrit dans `reviews`.
    proposed_subsidiary_id INTEGER REFERENCES dim_subsidiary(subsidiary_id),

    --: Comment la proposition a été obtenue. LA COLONNE LA PLUS IMPORTANTE
    --: POUR QUI RELIT : « alias_exact » et « llm » n'appellent pas la même
    --: confiance, et rien d'autre dans la ligne ne permettrait de les
    --: distinguer six semaines plus tard.
    --:   alias_exact    — égalité stricte avec un alias déclaré
    --:   alias_normalise— égalité après repli de casse et d'accents
    --:   nom_exact      — égalité avec dim_subsidiary.name
    --:   llm            — verdict d'un modèle, sur les cas restants
    method          VARCHAR(30) NOT NULL,

    confidence      REAL        NOT NULL DEFAULT 0.0,

    --: AUTO_SAFE        — déterministe et sans ambiguïté ; applicable seul
    --: HIGH_CONFIDENCE  — très probable, mais une normalisation est intervenue
    --: REVIEW_REQUIRED  — plusieurs candidates, ou verdict de modèle incertain
    --: UNRESOLVED       — aucune candidate
    status          VARCHAR(20) NOT NULL DEFAULT 'REVIEW_REQUIRED',

    --: Les candidates écartées et pourquoi. Une proposition sans les options
    --: qu'elle a rejetées n'est pas vérifiable.
    evidence        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    reason          TEXT,

    --: Horodatage de l'écriture RÉELLE dans `reviews`. Nul = proposition non
    --: appliquée. C'est ce champ, et lui seul, qui distingue une analyse d'une
    --: modification de données.
    applied_at      TIMESTAMPTZ,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    --: Une seule proposition vivante par avis. Ré-analyser met à jour, ne
    --: duplique pas : sans cela, chaque passage ajouterait 1 215 lignes.
    CONSTRAINT orphan_resolutions_unique UNIQUE (review_id),
    CONSTRAINT orphan_resolutions_status_check
        CHECK (status IN ('AUTO_SAFE', 'HIGH_CONFIDENCE',
                          'REVIEW_REQUIRED', 'UNRESOLVED'))
);

CREATE INDEX IF NOT EXISTS idx_orphan_resolutions_status
    ON orphan_resolutions (status, created_at DESC);

--: Requête chaude de l'application : ce qui est proposé et pas encore écrit.
CREATE INDEX IF NOT EXISTS idx_orphan_resolutions_a_appliquer
    ON orphan_resolutions (status)
    WHERE applied_at IS NULL;


-- ===========================================================================
-- CONTRÔLES POST-MIGRATION (à exécuter à la main, ne modifient rien)
--
--   -- 1. L'arriéré, par résolvabilité. Doit se vider par le haut.
--   SELECT status, method, count(*) FROM orphan_resolutions
--    GROUP BY 1, 2 ORDER BY 1, 3 DESC;
--
--   -- 2. Ce qui a été RÉELLEMENT écrit dans `reviews`, et par quelle méthode.
--   --    C'est la requête d'audit : toute ligne appliquée doit y répondre.
--   SELECT method, count(*), min(applied_at), max(applied_at)
--     FROM orphan_resolutions WHERE applied_at IS NOT NULL GROUP BY 1;
--
--   -- 3. RETOUR ARRIÈRE d'une application (aucune sauvegarde nécessaire) :
--   --    UPDATE reviews r SET subsidiary_id = o.previous_subsidiary_id
--   --      FROM orphan_resolutions o
--   --     WHERE o.review_id = r.review_id AND o.applied_at IS NOT NULL
--   --       AND o.method = '<la méthode à annuler>';
--   --    UPDATE orphan_resolutions SET applied_at = NULL
--   --     WHERE method = '<la méthode à annuler>';
--
--   -- 4. Le rattachement a-t-il rendu les avis visibles ? Ce compte doit
--   --    CHUTER après application, et les vues du dashboard doivent gagner
--   --    exactement le même nombre de lignes.
--   SELECT count(*) FROM reviews WHERE subsidiary_id IS NULL;
-- ===========================================================================
