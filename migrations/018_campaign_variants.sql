-- ===========================================================================
-- 018 — campagnes : contenus multi-formats, stratégies, et révisions
-- ===========================================================================
--
-- POURQUOI UNE RÉVISION EST UNE NOUVELLE LIGNE, ET NON UNE MISE À JOUR
--   « Fais-moi une version plus agressive » ne remplace pas la précédente : il
--   la met en concurrence. Écraser la première interdirait de comparer les deux,
--   c'est-à-dire d'exercer le seul jugement que l'utilisateur soit venu porter.
--
--   Conséquence voulue : une chaîne de révisions reste lisible dans l'ordre, et
--   le refroidissement de quatorze jours s'applique à la CIBLE, pas à la ligne —
--   réviser trois fois la même proposition ne consomme donc pas trois campagnes.
--
-- POURQUOI LES CONTENUS SONT EN JSONB ET NON EN COLONNES
--   Cinq formats, dont l'e-mail qui a quatre parties : quinze colonnes dont
--   douze NULL sur une campagne SMS. La structure de chaque format vit dans
--   `domain/marketing.FORMATS`, en un seul endroit ; la base n'a pas à la
--   dupliquer pour la voir diverger au premier format ajouté.
--
--   Ce qui reste en colonnes — `hook`, `message` — est ce qui se lit SANS
--   déplier un objet : la liste des campagnes en a besoin, pas du reste.
--
-- POURQUOI LES STRATÉGIES SONT STOCKÉES PLUTÔT QUE RECALCULÉES
--   Les trois angles A/B/C dépendent des mesures du jour. Un utilisateur qui
--   choisit l'option B trois jours plus tard doit obtenir l'option B qu'on lui a
--   MONTRÉE, pas celle que les chiffres d'aujourd'hui produiraient. Sans cette
--   colonne, un choix pourrait porter sur une proposition qui n'existe plus.

ALTER TABLE campaigns
    --: Contenus par format : {"sms": {"texte": "…"}, "email": {"objet": …}}.
    --: Vide tant que la génération multi-format n'a pas été demandée.
    ADD COLUMN IF NOT EXISTS contents JSONB NOT NULL DEFAULT '{}'::jsonb,

    --: Les trois angles proposés, figés au moment de la proposition.
    ADD COLUMN IF NOT EXISTS strategies JSONB NOT NULL DEFAULT '[]'::jsonb,

    --: Angle retenu (« A », « B », « C »), NULL tant qu'aucun n'est choisi.
    ADD COLUMN IF NOT EXISTS strategy VARCHAR(4),

    --: Registre d'écriture demandé. Voir `domain/marketing.TONS`.
    --: UN TON N'ÉLARGIT JAMAIS CE QUI PEUT ÊTRE DIT : les vérifications de
    --: promesse commerciale s'appliquent à l'identique quel qu'il soit.
    ADD COLUMN IF NOT EXISTS tone VARCHAR(32) NOT NULL DEFAULT 'factuel',

    --: Campagne dont celle-ci est une révision. NULL pour une proposition
    --: d'origine. ON DELETE SET NULL plutôt que CASCADE : supprimer une
    --: proposition d'origine ne doit pas emporter les versions qu'on lui a
    --: préférées.
    ADD COLUMN IF NOT EXISTS parent_id BIGINT
        REFERENCES campaigns(campaign_id) ON DELETE SET NULL;

--: Retrouver la chaîne de révisions d'une proposition.
CREATE INDEX IF NOT EXISTS idx_campaigns_parent
    ON campaigns (parent_id) WHERE parent_id IS NOT NULL;
