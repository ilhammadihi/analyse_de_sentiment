-- ===========================================================================
-- 016 — campaigns : les campagnes proposées par l'Agent 2
-- ===========================================================================
--
-- POURQUOI UNE TABLE À PART, ALORS QUE `agent_reports` EXISTE DÉJÀ
--   La migration 013 annonçait que « l'agent de campagne écrira dans la même
--   table ». C'est vrai de la RÈGLE de non-répétition, qui est reprise telle
--   quelle (`should_report`), et faux de la table : `agent_reports` est un
--   journal APPEND-ONLY de ce qui a été dit. Une campagne, elle, a un CYCLE DE
--   VIE — proposée, validée ou rejetée par un humain, puis mesurée. Elle change
--   d'état après avoir été écrite.
--
--   Faire porter cela par `agent_reports` aurait demandé d'y ajouter un statut,
--   une date de validation et un rapport, c'est-à-dire quatre colonnes toujours
--   NULL pour l'agent de veille — et surtout deux sources de vérité pour le
--   même texte. La règle est partagée, la table ne l'est pas.
--
-- CE QUE CETTE TABLE NE CONTIENT PAS, ET NE CONTIENDRA JAMAIS SANS CRM
--   Aucune donnée d'envoi, d'ouverture, de clic ou de conversion. La plateforme
--   ne collecte que des avis PUBLICS : elle sait qui s'est plaint et de quoi,
--   elle ne sait pas à qui un message a été envoyé. Le rapport de campagne
--   mesure donc l'évolution de la SATISFACTION du segment visé, et le dit
--   explicitement plutôt que de laisser croire à une mesure de performance
--   marketing. Un taux d'ouverture inventé serait indétectable et suffirait à
--   décrédibiliser tout le dispositif.
--
-- CE QUI EST FIGÉ À LA CRÉATION, ET POURQUOI
--   `payload` conserve les mesures qui ont JUSTIFIÉ la campagne : taille du
--   segment, part de négatifs, motifs dominants, composition des sources. Sans
--   elles, le rapport comparerait l'état d'aujourd'hui à un souvenir. C'est la
--   même raison qui fait conserver `llm_insights.payload` et
--   `agent_reports.payload` : une décision prise six semaines plus tôt doit
--   rester rapprochable des chiffres qui l'ont produite.

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id    BIGSERIAL PRIMARY KEY,

    --: CIBLE, dans les identifiants du CONTRAT DE FILTRE — un pays se désigne
    --: par son ISO alpha-2, jamais par son country_id. Même convention que
    --: `agent_reports`, pour que les deux agents parlent des mêmes entités.
    entity_level   VARCHAR(20)  NOT NULL,
    entity_key     TEXT         NOT NULL,
    entity_label   TEXT         NOT NULL,

    --: SEGMENT, OBJECTIF, CANAL : trois clés de vocabulaires FERMÉS
    --: (`domain/marketing.py`). Stockées en clé technique et non en libellé :
    --: un libellé retouché rendrait incomparables les campagnes d'avant et
    --: d'après la retouche, exactement comme un aspect renommé.
    segment        VARCHAR(32)  NOT NULL,
    objective      VARCHAR(32)  NOT NULL,
    channel        VARCHAR(32)  NOT NULL,

    --: Taille MESURÉE du segment à la création : nombre d'avis clients qui le
    --: composent. Sert de note d'arbitrage à la règle de non-répétition — d'où
    --: le REAL, aligné sur `agent_reports.score`.
    segment_size   REAL         NOT NULL DEFAULT 0,

    --: Fenêtre d'observation, en jours, ayant servi à mesurer le segment. Le
    --: rapport doit la connaître : comparer « depuis le lancement » à une
    --: fenêtre de durée différente produirait un écart dû à la seule durée.
    window_days    INTEGER      NOT NULL,

    --: Description libre saisie par l'utilisateur, telle qu'il l'a écrite.
    --: Conservée BRUTE, en plus des paramètres qu'on en a tirés : quand une
    --: campagne surprend, la demande d'origine est la première chose à relire,
    --: et le modèle ne retraduira pas deux fois à l'identique.
    brief          TEXT,

    --: Contenu proposé. `hook` est l'accroche, `message` le corps adapté au
    --: canal. Séparés plutôt que fondus en un seul texte : le canal impose ses
    --: longueurs, et un SMS n'a pas d'accroche détachable.
    hook           TEXT         NOT NULL,
    message        TEXT         NOT NULL,

    --: Vrai si le modèle a rédigé, faux si c'est le gabarit déterministe. Se
    --: relit en démonstration : distingue « aucune clé configurée » de « le
    --: modèle a promis une remise et a été écarté ».
    written_by_llm BOOLEAN      NOT NULL DEFAULT FALSE,

    --: Mesures et arbitrage figés à la création. Voir l'en-tête.
    payload        JSONB        NOT NULL DEFAULT '{}'::jsonb,

    --: CYCLE DE VIE. `proposed` -> `approved` | `rejected`.
    --:
    --: AUCUNE CAMPAGNE NE PART SANS UN HUMAIN. Le statut initial n'est donc pas
    --: « active » : un texte commercial engage l'opérateur vis-à-vis de ses
    --: clients, et le faire dépendre d'un modèle de langage serait la seule
    --: décision de tout le projet qu'on ne pourrait pas défendre. L'agent
    --: PROPOSE ; la validation est un acte tracé, avec son auteur et sa date.
    status         VARCHAR(16)  NOT NULL DEFAULT 'proposed',
    decided_at     TIMESTAMPTZ,
    decided_by     TEXT,

    --: Trace d'acheminement de la PROPOSITION (vers Telegram), pas de la
    --: campagne elle-même. Une proposition rédigée et jamais partie est un
    --: incident silencieux — celui qui avait fait taire l'alerting trois jours.
    delivered      BOOLEAN      NOT NULL DEFAULT FALSE,

    --: Dernier rapport produit, et sa date. Écrasé à chaque nouveau rapport :
    --: c'est une PHOTO de l'écart depuis le lancement, pas une série. La série,
    --: elle, reste reconstructible depuis les avis, qui ne bougent pas.
    report         JSONB,
    reported_at    TIMESTAMPTZ,

    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

--: La requête chaude est « ai-je déjà proposé quelque chose pour cette entité,
--: et quand ? » — posée pour chaque cible candidate, à chaque passage.
CREATE INDEX IF NOT EXISTS idx_campaigns_lookup
    ON campaigns (entity_level, entity_key, created_at DESC);

--: Le fil « campagnes en attente de validation », lu par la commande Telegram.
CREATE INDEX IF NOT EXISTS idx_campaigns_status
    ON campaigns (status, created_at DESC);
