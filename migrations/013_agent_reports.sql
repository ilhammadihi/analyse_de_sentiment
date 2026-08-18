-- ===========================================================================
-- 013 — agent_reports : la mémoire de l'agent d'insight
-- ===========================================================================
--
-- CE QUI SÉPARE UN AGENT D'UN ENDPOINT
--   `/insights/diagnose` répond quand on l'appelle et oublie tout entre deux
--   appels. C'est correct pour un écran : le lecteur choisit son périmètre et
--   sait ce qu'il vient de demander.
--
--   Un agent, lui, parle sans qu'on le lui demande. Sans mémoire, il signale
--   MTN Ghana lundi, puis mardi, puis mercredi, avec le même texte sous les
--   mêmes chiffres — et l'équipe cesse de le lire le jeudi. Le fil d'alertes a
--   déjà montré ce qu'il en coûte : au 10 août il affichait encore un pic du
--   27 juillet, présenté comme actuel au milieu d'alertes du matin même.
--
--   Cette table est donc ce qui rend l'agent supportable au quotidien, pas un
--   confort d'implémentation.
--
-- LA RÈGLE QU'ELLE PERMET D'APPLIQUER
--   Ne pas répéter un signalement identique avant `report_cooldown_days`,
--   MAIS toujours reparler d'une entité dont la situation s'est AGGRAVÉE
--   depuis le dernier signalement. C'est pourquoi `score` est stocké : sans
--   lui, on ne saurait que « déjà dit », jamais « pire qu'avant », et l'agent
--   se tairait précisément quand il devient utile.
--
-- POURQUOI LE PAYLOAD EST CONSERVÉ
--   Même raison que `llm_insights.payload` : une phrase affichée six semaines
--   plus tôt doit rester rapprochable des chiffres qui l'ont produite. Un
--   briefing qu'on ne peut plus vérifier ne se défend pas en réunion.

CREATE TABLE IF NOT EXISTS agent_reports (
    report_id     BIGSERIAL PRIMARY KEY,

    --: Quel agent a parlé. Prévu au pluriel dès maintenant : l'agent de
    --: campagne écrira dans la même table, et un journal par agent obligerait
    --: à dupliquer la règle de non-répétition.
    agent         VARCHAR(32)  NOT NULL,

    --: Sujet du signalement, dans les identifiants du CONTRAT DE FILTRE —
    --: un pays se désigne par son ISO alpha-2, jamais par son country_id.
    entity_level  VARCHAR(20)  NOT NULL,
    entity_key    TEXT         NOT NULL,
    entity_label  TEXT         NOT NULL,

    --: Note d'arbitrage au moment du signalement. Sert EXCLUSIVEMENT à
    --: détecter une aggravation ; elle n'est jamais montrée à l'utilisateur,
    --: qui n'a que faire d'un score composite sans unité.
    score         REAL         NOT NULL,

    --: Ce qui a été mesuré, et ce qui a été écrit.
    payload       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    text          TEXT         NOT NULL,

    --: Trace d'acheminement : un briefing rédigé mais jamais parti est un
    --: incident silencieux, exactement celui qui a fait taire l'alerting
    --: pendant trois jours sans que rien ne le signale.
    delivered     BOOLEAN      NOT NULL DEFAULT FALSE,

    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

--: La recherche du dernier signalement d'une entité est LA requête chaude :
--: elle est faite pour chaque candidat, à chaque passage.
CREATE INDEX IF NOT EXISTS idx_agent_reports_lookup
    ON agent_reports (agent, entity_level, entity_key, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_reports_created
    ON agent_reports (created_at DESC);
