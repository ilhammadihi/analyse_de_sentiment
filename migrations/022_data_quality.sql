-- ===========================================================================
-- 022 — Agent 3 : les tables du gardien de la qualité et de l'enrichissement
-- ===========================================================================
--
-- CE QUE CET AGENT AJOUTE, ET CE QU'IL NE REFAIT PAS
--   Le dépôt sait déjà mesurer beaucoup de choses : `v_target_coverage` compte
--   les sous-cibles couvertes, `v_subsidiary_volume` les avis par filiale,
--   `collection_jobs` l'état unité par unité, `run_metrics` le résultat de
--   chaque passage. Rien de tout cela n'est réécrit ici.
--
--   Ce qui manquait n'est pas la MESURE, c'est le CONSTAT : quelque part où
--   écrire « cette filiale n'a aucun avis, et voici POURQUOI », avec la preuve,
--   la date, et le degré de certitude. Sans cette trace, chaque passage
--   redécouvre le même trou et personne ne peut dire s'il se comble.
--
-- LA LEÇON QUI COMMANDE TOUTE LA CONCEPTION
--   Trustpilot a produit 43 alertes « scraper_zero » par semaine pour signaler
--   non pas un incident, mais une ABSENCE DE CIBLE : 3 domaines sur 4 n'avaient
--   aucune fiche. Le zéro était exact, l'alerte était fausse.
--
--   Un gardien de la qualité naïf rejouerait cette panne d'attention à
--   l'échelle des 135 filiales. D'où la règle qui structure ces tables : on
--   n'enregistre jamais un manque sans enregistrer À CÔTÉ le diagnostic qui
--   dit s'il est imputable à nous, à la source, ou à personne.
--
-- MESURÉ AU 17 AOÛT 2026, sur le corpus réel de 40 078 avis :
--       3 filiales à 0 avis client   (Comores Telecom, MTN RDC, Telma Comores)
--      15 filiales entre 1 et 9 avis
--     117 filiales au-dessus
--
--   Et le détail qui justifie l'existence du diagnostic : les unités Google
--   Maps de Comores Telecom sont en `success` avec `items_inserted = 0`. Le
--   collecteur n'est pas en panne — il a cherché et n'a rien trouvé. Relancer
--   la collecte, réflexe naturel devant un zéro, n'aurait rien donné et aurait
--   coûté une session de navigateur par passage, indéfiniment.
--
-- NON DESTRUCTIF — que des tables nouvelles, plus une colonne sur `llm_usage`.
-- Aucune donnée d'avis n'est touchée, ici ni par le code de l'agent.
-- ---------------------------------------------------------------------------


-- ===========================================================================
-- 1. COMPTABILITÉ LLM PAR PROFIL
-- ===========================================================================
--
-- POURQUOI LA CLÉ PRIMAIRE DOIT CHANGER
--   `llm_usage` avait pour clé le seul `day`. Tant qu'un unique fournisseur
--   servait tout le projet, c'était suffisant.
--
--   L'Agent 3 doit tourner sur un AUTRE fournisseur que Gemini — c'est une
--   exigence explicite, et elle a une raison mesurable : le budget quotidien
--   est de 200 appels, déjà partagés entre l'analyse sémantique (jusqu'à 200
--   appels programmés), le briefing quotidien et l'assistant conversationnel.
--   Un quatrième consommateur sur le même compteur ne prendrait pas « un peu
--   de marge » : il assécherait l'analyse sémantique, dont dépendent les
--   aspects, donc les motifs, donc l'Agent 1.
--
--   Sans cette colonne, les appels de l'Agent 3 s'additionneraient à ceux de
--   Gemini dans la même ligne : le garde-fou censé PROTÉGER le quota Gemini
--   serait précisément ce qui le consommerait. Le compteur doit donc être
--   cloisonné par profil, comme les budgets le sont.
--
-- LE DÉFAUT EST 'defaut' ET NON 'gemini' : ce compteur n'appartient pas à un
-- fournisseur mais à un USAGE. Changer `LLM_BASE_URL` ne doit pas remettre
-- l'historique à zéro ni scinder la ligne du jour en deux.
ALTER TABLE llm_usage
    ADD COLUMN IF NOT EXISTS profil VARCHAR(32) NOT NULL DEFAULT 'defaut';

-- Bascule de la clé primaire, en deux temps et de façon idempotente. Le nom de
-- la contrainte est celui que PostgreSQL a généré à la création de la table.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'llm_usage_pkey'
          AND conrelid = 'llm_usage'::regclass
          -- Une seule colonne dans la clé = l'ancienne forme, à migrer.
          AND array_length(conkey, 1) = 1
    ) THEN
        ALTER TABLE llm_usage DROP CONSTRAINT llm_usage_pkey;
        ALTER TABLE llm_usage ADD CONSTRAINT llm_usage_pkey
            PRIMARY KEY (day, profil);
    END IF;
END $$;

COMMENT ON COLUMN llm_usage.profil IS
    'Profil d''appel : ''defaut'' pour la couche sémantique, le briefing et '
    'l''assistant ; ''qualite'' pour l''Agent 3. Cloisonne les budgets, de '
    'sorte qu''un agent ne puisse pas assécher le quota d''un autre.';


-- ===========================================================================
-- 2. CONSTATS DE QUALITÉ
-- ===========================================================================
--
-- CE QU'EST UN CONSTAT, ET CE QU'IL N'EST PAS
--   Un constat DÉCRIT un problème présumé. Il ne le corrige jamais, et
--   surtout il ne supprime rien : l'énoncé de l'agent l'interdit explicitement,
--   et l'expérience du projet le confirme — les quatre alertes envoyées le
--   13 août sur des avis mal attribués n'ont pas pu être retirées du groupe.
--   Ce qui est effacé ne se rattrape pas ; ce qui est marqué se relit.
--
-- LES QUATRE STATUTS, ET POURQUOI PAS DEUX
--   FLAGGED          — repéré par une règle, pas encore instruit.
--   REVIEW_REQUIRED  — le modèle a été appelé et n'a pas tranché, ou sa
--                      réponse était invalide. C'est le statut de REPLI
--                      obligatoire : un verdict illisible ne doit jamais
--                      devenir un rejet silencieux.
--   ACCEPTED         — instruit, la donnée est bonne. Conservé, et ce n'est
--                      pas un gaspillage : sans lui, chaque passage
--                      re-soumettrait au modèle les avis déjà innocentés, et
--                      le quota partirait entièrement dans la re-vérification
--                      de ce qui va bien.
--   REJECTED         — instruit, la donnée est mauvaise. La ligne d'avis reste
--                      en base ; c'est un marquage, jamais une suppression.
CREATE TABLE IF NOT EXISTS data_quality_flags (
    flag_id       BIGSERIAL PRIMARY KEY,

    --: Nature du constat : doublon_semantique, hors_sujet, spam, publicite,
    --: mauvaise_filiale, texte_insuffisant, langue_inattendue, volume_anormal,
    --: source_muette, incoherence_temporelle, mapping_suspect.
    kind          VARCHAR(40)  NOT NULL,

    --: Sur QUOI porte le constat. Un même agent surveille des objets de
    --: nature différente — un avis, une filiale, une source — et les mélanger
    --: dans une table par objet obligerait à trois requêtes pour répondre à
    --: « que reproche-t-on à cette filiale ? ».
    scope         VARCHAR(20)  NOT NULL,   -- 'review' | 'subsidiary' | 'source'
    subject_key   TEXT         NOT NULL,   -- review_id | subsidiary_id | code source

    --: Contexte dénormalisé, volontairement. Il rend la table lisible telle
    --: quelle — « quels constats sur le Nigeria ? » sans jointure — exactement
    --: pour la raison qui l'a fait retenir dans `collection_jobs`.
    subsidiary_id INTEGER      REFERENCES dim_subsidiary(subsidiary_id),
    source_code   VARCHAR(50),

    status        VARCHAR(20)  NOT NULL DEFAULT 'FLAGGED',
    severity      VARCHAR(20)  NOT NULL DEFAULT 'warning',

    --: Certitude du constat, entre 0 et 1. NULL quand la règle est
    --: déterministe : un checksum identique n'a pas de « degré de certitude »,
    --: et lui en inventer un laisserait croire qu'il est discutable.
    confidence    REAL,

    --: 'regle' ou 'llm'. LE CHAMP LE PLUS IMPORTANT DE LA TABLE pour qui
    --: relit : un doublon détecté par égalité de checksum et un doublon
    --: présumé par un modèle n'appellent pas la même confiance, et rien
    --: d'autre dans la ligne ne permettrait de les distinguer.
    detected_by   VARCHAR(20)  NOT NULL DEFAULT 'regle',

    reason        TEXT         NOT NULL,

    --: Ce sur quoi le constat s'appuie, au format de la traçabilité commune
    --: (source, date, url/id, type de preuve). Jamais vide pour un constat
    --: produit par un modèle.
    evidence      JSONB        NOT NULL DEFAULT '[]'::jsonb,

    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),

    --: IDEMPOTENCE DES PASSAGES. L'agent repasse toutes les six heures sur le
    --: même corpus ; sans cette contrainte, le même doublon produirait une
    --: ligne par passage et la table deviendrait illisible en une semaine.
    CONSTRAINT data_quality_flags_unique UNIQUE (kind, scope, subject_key),
    CONSTRAINT data_quality_flags_status_check
        CHECK (status IN ('FLAGGED', 'REVIEW_REQUIRED', 'ACCEPTED', 'REJECTED')),
    CONSTRAINT data_quality_flags_scope_check
        CHECK (scope IN ('review', 'subsidiary', 'source')),
    CONSTRAINT data_quality_flags_detected_check
        CHECK (detected_by IN ('regle', 'llm'))
);

CREATE INDEX IF NOT EXISTS idx_dq_flags_subsidiary
    ON data_quality_flags (subsidiary_id, status);
CREATE INDEX IF NOT EXISTS idx_dq_flags_kind
    ON data_quality_flags (kind, created_at DESC);
--: Requête chaude de l'écran : « ce qui reste à instruire », toutes filiales
--: confondues. Index partiel — les lignes ACCEPTED sont de loin les plus
--: nombreuses et n'ont aucune raison de peser dessus.
CREATE INDEX IF NOT EXISTS idx_dq_flags_a_instruire
    ON data_quality_flags (created_at DESC)
    WHERE status IN ('FLAGGED', 'REVIEW_REQUIRED');


-- ===========================================================================
-- 3. SOURCES CANDIDATES
-- ===========================================================================
--
-- UNE CANDIDATE N'EST PAS UNE SOURCE
--   L'énoncé l'exige et l'architecture le rend vrai : rien ici n'est collecté.
--   Cette table est une LISTE DE PROPOSITIONS, instruites puis soumises à un
--   humain. Aucune ligne d'ici ne peut faire entrer un seul avis en base.
--
-- POURQUOI `probe_status` EST UNE COLONNE ET NON UN COMMENTAIRE
--   « Ne jamais considérer une source comme fiable simplement parce qu'elle
--   apparaît dans un résultat de recherche. » Le seul moyen non spéculatif de
--   tenir cette règle sans moteur de recherche est de MESURER : on interroge
--   l'URL en HTTP et on garde le code obtenu. Une candidate sans sonde reste
--   une hypothèse ; une candidate à 200 avec du contenu reconnu est un fait
--   daté, opposable, et reproductible par qui veut le vérifier.
--
--   C'est la transposition directe de ce que fait déjà
--   `tools/probe_gap_operators.py` pour les opérateurs manquants : une
--   application vivante prouve un opérateur vivant. Ici, une page qui répond
--   et porte des avis prouve une source exploitable.
CREATE TABLE IF NOT EXISTS source_candidates (
    candidate_id  BIGSERIAL PRIMARY KEY,

    source_name   TEXT         NOT NULL,
    url           TEXT         NOT NULL,
    country       CHAR(2),
    operator      TEXT,
    subsidiary_id INTEGER      REFERENCES dim_subsidiary(subsidiary_id),

    --: forum | plateforme_avis | reseau_social | presse_locale | specialise |
    --: communaute | regulateur
    source_type   VARCHAR(40),

    --: Ce que la sonde a appris de l'accès : api_publique, http_ouvert,
    --: bloque (403/Cloudflare), absent (404), inconnu (jamais sondée).
    --: `bloque` est une information UTILE et non un échec : c'est ce qui a
    --: fait écarter Techpoint Africa et MyBroadband des flux de presse.
    accessibility VARCHAR(30)  NOT NULL DEFAULT 'inconnu',

    estimated_relevance VARCHAR(10),        -- high | medium | low
    reason        TEXT,
    evidence      JSONB        NOT NULL DEFAULT '[]'::jsonb,

    --: Résultat MESURÉ de la sonde HTTP. NULL = jamais sondée, et l'écran doit
    --: le dire ainsi plutôt que d'afficher un zéro qui se lirait comme un échec.
    probe_status  INTEGER,
    probe_at      TIMESTAMPTZ,

    --: Un connecteur est-il nécessaire pour l'exploiter ?
    --:
    --: VRAI PAR DÉFAUT, et c'est le garde-fou de la section 8 de l'énoncé :
    --: « ne développe PAS automatiquement un scraper ». Le défaut prudent fait
    --: que toute candidate arrive avec la mention « connector required » tant
    --: que quelqu'un n'a pas établi qu'un connecteur générique suffit.
    connector_required BOOLEAN NOT NULL DEFAULT TRUE,

    --: CANDIDATE (proposée) | VERIFIED (sondée avec succès) |
    --: REJECTED (sondée, inexploitable) | INTEGRATED (un connecteur existe)
    status        VARCHAR(20)  NOT NULL DEFAULT 'CANDIDATE',
    confidence    REAL,

    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),

    --: Une même plateforme peut être candidate pour plusieurs filiales — c'est
    --: le cas normal d'un forum national. L'unicité porte donc sur le COUPLE.
    CONSTRAINT source_candidates_unique UNIQUE (subsidiary_id, url),
    CONSTRAINT source_candidates_status_check
        CHECK (status IN ('CANDIDATE', 'VERIFIED', 'REJECTED', 'INTEGRATED'))
);

CREATE INDEX IF NOT EXISTS idx_source_candidates_sub
    ON source_candidates (subsidiary_id, status);


-- ===========================================================================
-- 4. AFFIRMATIONS ET CORROBORATION
-- ===========================================================================
--
-- LE PROBLÈME EXACT QUE CETTE TABLE EMPÊCHE
--   Quarante clients écrivent « le réseau est coupé depuis hier ». La tentation
--   est d'en conclure « panne réseau confirmée » et de le dire à l'Agent 1, qui
--   le dira dans un briefing, qui sera lu comme un fait établi.
--
--   Or quarante avis concordants ne sont qu'UNE SEULE espèce de preuve. Ils
--   peuvent tous décrire la même rumeur, le même fil viral, ou la même panne
--   d'un quartier prise pour une panne nationale. Le nombre ne fait pas la
--   corroboration : c'est l'INDÉPENDANCE des sources qui la fait.
--
-- LES QUATRE NIVEAUX SONT ORDONNÉS PAR NATURE DE PREUVE, PAS PAR VOLUME
--   CONFIRMED     — une source officielle (opérateur, régulateur) le dit.
--   CORROBORATED  — au moins deux espèces INDÉPENDANTES concordent
--                   (p. ex. avis clients + presse datée).
--   PLAUSIBLE     — une seule espèce, mais un signal fort et daté.
--   UNCONFIRMED   — rien d'indépendant. C'est le défaut, et c'est le statut le
--                   plus important : il autorise l'Agent 1 et l'Agent 2 à
--                   TAIRE l'affirmation plutôt qu'à la relayer.
CREATE TABLE IF NOT EXISTS data_claims (
    claim_id      BIGSERIAL PRIMARY KEY,

    claim         TEXT         NOT NULL,
    --: Sujet normalisé, repris de la taxonomie d'aspects quand il en relève
    --: (coupures_pannes, facturation_prix…). Permet de rapprocher deux
    --: affirmations du même ordre à des dates différentes.
    topic         VARCHAR(40),

    subsidiary_id INTEGER      REFERENCES dim_subsidiary(subsidiary_id),
    country       CHAR(2),

    window_from   DATE         NOT NULL,
    window_to     DATE         NOT NULL,

    status        VARCHAR(20)  NOT NULL DEFAULT 'UNCONFIRMED',

    --: Entre 0 et 1. CALCULÉE, jamais demandée à un modèle : elle découle du
    --: nombre d'espèces de preuves indépendantes et de leur nature. Un modèle
    --: à qui l'on demande sa confiance rend un nombre corrélé à son aisance
    --: rédactionnelle, pas à la solidité du fait.
    confidence    REAL         NOT NULL DEFAULT 0.0,

    --: Le détail opposable : [{"source": "customer_reviews", "count": 42},
    --: {"source": "news", "url": "...", "date": "..."}]. C'est la structure
    --: demandée par la section 11 de l'énoncé, conservée telle quelle.
    evidence      JSONB        NOT NULL DEFAULT '[]'::jsonb,

    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),

    --: Une affirmation est datée par sa FENÊTRE. Le même sujet sur la même
    --: filiale à deux semaines d'écart sont deux affirmations distinctes ;
    --: sur la même fenêtre, c'est la même, et la ré-instruire doit la mettre
    --: à jour, pas la dupliquer.
    CONSTRAINT data_claims_unique UNIQUE (subsidiary_id, topic, window_from),
    CONSTRAINT data_claims_status_check
        CHECK (status IN ('CONFIRMED', 'CORROBORATED', 'PLAUSIBLE', 'UNCONFIRMED'))
);

CREATE INDEX IF NOT EXISTS idx_data_claims_lookup
    ON data_claims (subsidiary_id, created_at DESC);


-- ===========================================================================
-- 5. INSTANTANÉS DU SCORE DE QUALITÉ
-- ===========================================================================
--
-- POURQUOI ON GARDE L'HISTORIQUE PLUTÔT QU'UNE COLONNE SUR dim_subsidiary
--   Un score seul ne se défend pas. « Orange Mali : 42 % » n'appelle aucune
--   action ; « Orange Mali : 42 %, contre 61 % il y a deux semaines » en
--   appelle une immédiatement. C'est la même raison qui a fait stocker `score`
--   dans `agent_reports` : sans l'antérieur, on sait « mauvais », jamais
--   « en train de se dégrader » — et c'est le second qui fait agir.
--
-- `components` PORTE LE DÉTAIL DU CALCUL, ET C'EST OBLIGATOIRE
--   L'énoncé exige un score EXPLICABLE. Un nombre global sans ses composantes
--   ni leurs poids est indéfendable en réunion : la première question posée
--   sera « pourquoi 42 ? », et il faut pouvoir y répondre six semaines plus
--   tard, y compris après un changement de pondération. Les poids appliqués
--   sont donc figés DANS la ligne, pas seulement dans la configuration.
CREATE TABLE IF NOT EXISTS quality_snapshots (
    snapshot_id   BIGSERIAL PRIMARY KEY,
    subsidiary_id INTEGER      NOT NULL REFERENCES dim_subsidiary(subsidiary_id),
    computed_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),

    --: Les six composantes, chacune entre 0 et 1.
    coverage      REAL,
    freshness     REAL,
    completeness  REAL,
    consistency   REAL,
    diversity     REAL,
    reliability   REAL,

    global_score  REAL         NOT NULL,

    --: TRUSTED | ACCEPTABLE | DEGRADED | UNTRUSTED. C'est ce mot, et non le
    --: nombre, que consomment les Agents 1 et 2 : un seuil lu au même endroit
    --: par tous vaut mieux que trois comparaisons numériques qui divergeront.
    status        VARCHAR(20)  NOT NULL,

    --: Cas retenu par le diagnostic : collecteur_en_echec, source_vide,
    --: mapping_suspect, aucune_source_exploitable, jamais_tente, couvert.
    --: C'est LUI qui dit s'il faut relancer, corriger ou chercher ailleurs.
    diagnostic    VARCHAR(40),

    components    JSONB        NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT quality_snapshots_status_check
        CHECK (status IN ('TRUSTED', 'ACCEPTABLE', 'DEGRADED', 'UNTRUSTED'))
);

CREATE INDEX IF NOT EXISTS idx_quality_snapshots_lookup
    ON quality_snapshots (subsidiary_id, computed_at DESC);


-- --- Le dernier instantané de chaque filiale --------------------------------
--   DISTINCT ON est la forme PostgreSQL de « la ligne la plus récente par
--   groupe ». Une jointure sur MAX(computed_at) rendrait deux lignes pour deux
--   instantanés du même horodatage — improbable, mais un doublon silencieux
--   dans une vue de confiance est exactement ce qu'on ne veut pas déboguer.
DROP VIEW IF EXISTS v_quality_latest CASCADE;
CREATE VIEW v_quality_latest AS
SELECT DISTINCT ON (q.subsidiary_id)
    q.subsidiary_id, sub.name AS subsidiary,
    op.name AS operator, co.name AS country, co.iso2,
    q.computed_at, q.coverage, q.freshness, q.completeness,
    q.consistency, q.diversity, q.reliability,
    q.global_score, q.status, q.diagnostic, q.components
FROM quality_snapshots q
JOIN dim_subsidiary sub ON sub.subsidiary_id = q.subsidiary_id
JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
JOIN dim_country    co  ON co.country_id     = sub.country_id
ORDER BY q.subsidiary_id, q.computed_at DESC;


-- ===========================================================================
-- CONTRÔLES POST-MIGRATION (à exécuter à la main, ne modifient rien)
--
--   -- 1. Le cloisonnement des budgets fonctionne : deux lignes par jour dès
--   --    que l'Agent 3 a une clé propre.
--   SELECT day, profil, calls FROM llm_usage ORDER BY day DESC, profil;
--
--   -- 2. Ce qui reste à instruire. Doit rester petit ; s'il enfle, c'est que
--   --    le modèle n'est pas joignable et que tout part en REVIEW_REQUIRED.
--   SELECT status, count(*) FROM data_quality_flags GROUP BY 1;
--
--   -- 3. Les affirmations non corroborées NE DOIVENT JAMAIS ressortir dans un
--   --    briefing. Cette requête est celle que les Agents 1 et 2 doivent
--   --    pouvoir opposer à toute phrase qu'ils s'apprêtent à écrire.
--   SELECT claim, status, confidence FROM data_claims
--    WHERE status = 'UNCONFIRMED' ORDER BY created_at DESC;
--
--   -- 4. Les filiales que les autres agents doivent traiter avec prudence.
--   SELECT subsidiary, global_score, status, diagnostic
--     FROM v_quality_latest WHERE status IN ('DEGRADED', 'UNTRUSTED')
--    ORDER BY global_score;
-- ===========================================================================
