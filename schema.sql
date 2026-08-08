-- Carrier Bundles canonical catalog schema (v7)
--
-- A catalog is generated from one firmware source, sealed, and published as
-- an immutable artifact. Consumer applications open it with:
--   file:carrier-bundles.sqlite3?mode=ro&immutable=1

PRAGMA foreign_keys = ON;
PRAGMA application_id = 1128419922;
PRAGMA user_version = 7;

BEGIN;

-- 1. Exactly one metadata row exists in every catalog.
CREATE TABLE catalog_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_name TEXT NOT NULL DEFAULT 'carrier_bundles'
        CHECK (schema_name = 'carrier_bundles'),
    schema_version INTEGER NOT NULL DEFAULT 7 CHECK (schema_version = 7),
    config_contract TEXT NOT NULL DEFAULT 'carrier-bundles-ims-v1'
        CHECK (config_contract = 'carrier-bundles-ims-v1'),
    release_id TEXT NOT NULL UNIQUE,
    generated_at TEXT NOT NULL,
    generator_name TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    source_manifest_sha256 TEXT CHECK (
        source_manifest_sha256 IS NULL OR
        (length(source_manifest_sha256) = 64 AND
         source_manifest_sha256 NOT GLOB '*[^0-9A-Fa-f]*')
    ),
    sealed INTEGER NOT NULL DEFAULT 0 CHECK (sealed IN (0, 1)),
    notes TEXT
);

-- 2. Source identity is deliberately device-neutral. Device names, codenames,
-- OS versions and build labels belong in the external build summary.
CREATE TABLE source_artifacts (
    source_id INTEGER PRIMARY KEY,
    source_kind TEXT NOT NULL CHECK (source_kind IN (
        'carrier_bundle', 'carrier_settings', 'carrier_config',
        'modem_config', 'firmware_manifest', 'standards_reference',
        'operator_metadata', 'icon_catalog', 'other'
    )),
    source_uri TEXT,
    artifact_sha256 TEXT CHECK (
        artifact_sha256 IS NULL OR
        (length(artifact_sha256) = 64 AND
         artifact_sha256 NOT GLOB '*[^0-9A-Fa-f]*')
    ),
    source_revision TEXT,
    extracted_at TEXT NOT NULL,
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    license_note TEXT,
    UNIQUE (
        source_kind, source_uri, artifact_sha256, source_revision,
        parser_name, parser_version
    )
);

-- 3. Icons remain separate so normal profile reads never copy large BLOBs.
CREATE TABLE visual_assets (
    asset_id TEXT PRIMARY KEY,
    asset_kind TEXT NOT NULL CHECK (asset_kind IN (
        'operator_logo', 'carrier_badge', 'country_flag', 'placeholder'
    )),
    asset_data BLOB NOT NULL CHECK (length(asset_data) > 0),
    local_path TEXT,
    remote_url TEXT,
    media_type TEXT NOT NULL CHECK (media_type IN (
        'image/svg+xml', 'image/png', 'image/webp'
    )),
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9A-Fa-f]*'
    ),
    width INTEGER CHECK (width IS NULL OR width > 0),
    height INTEGER CHECK (height IS NULL OR height > 0),
    source_name TEXT NOT NULL,
    source_url TEXT,
    license_spdx TEXT,
    attribution TEXT,
    is_official INTEGER NOT NULL DEFAULT 0 CHECK (is_official IN (0, 1)),
    CHECK (local_path IS NOT NULL OR remote_url IS NOT NULL)
);

-- 4. Aliases are a small read-mostly list, so they are stored as JSON rather
-- than as a separate relation.
CREATE TABLE carriers (
    carrier_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    brand_name TEXT,
    legal_name TEXT,
    carrier_kind TEXT NOT NULL DEFAULT 'unknown' CHECK (carrier_kind IN (
        'mno', 'mvno', 'global', 'test', 'unknown'
    )),
    country_iso2 TEXT CHECK (
        country_iso2 IS NULL OR
        (length(country_iso2) = 2 AND country_iso2 = upper(country_iso2))
    ),
    tadig TEXT,
    website TEXT,
    aliases_json TEXT NOT NULL DEFAULT '[]' CHECK (
        json_valid(aliases_json) AND json_type(aliases_json) = 'array'
    ),
    primary_asset_id TEXT REFERENCES visual_assets(asset_id) ON DELETE SET NULL,
    notes TEXT
);

-- 5. One row contains the complete static IMS client override document.
CREATE TABLE carrier_profiles (
    profile_id TEXT PRIMARY KEY,
    carrier_id TEXT NOT NULL REFERENCES carriers(carrier_id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    profile_kind TEXT NOT NULL DEFAULT 'default' CHECK (profile_kind IN (
        'default', 'mvno', 'roaming', 'test'
    )),
    priority INTEGER NOT NULL DEFAULT 100,
    confidence INTEGER NOT NULL DEFAULT 0 CHECK (confidence BETWEEN 0 AND 100),
    lte_ims_status TEXT NOT NULL DEFAULT 'unknown' CHECK (lte_ims_status IN (
        'ready', 'partial', 'unsupported', 'unknown'
    )),
    nr_ims_status TEXT NOT NULL DEFAULT 'unknown' CHECK (nr_ims_status IN (
        'ready', 'partial', 'unsupported', 'unknown'
    )),
    vowifi_status TEXT NOT NULL DEFAULT 'unknown' CHECK (vowifi_status IN (
        'ready', 'partial', 'unsupported', 'unknown'
    )),
    profile_asset_id TEXT REFERENCES visual_assets(asset_id) ON DELETE SET NULL,
    config_json TEXT NOT NULL CHECK (
        json_valid(config_json) AND json_type(config_json) = 'object'
    ),
    notes TEXT
);

CREATE INDEX idx_profiles_carrier ON carrier_profiles(carrier_id, priority);
CREATE INDEX idx_profiles_readiness
    ON carrier_profiles(lte_ims_status, nr_ims_status, vowifi_status);

-- 6. Non-null conditions in one row are ANDed. Rows for a profile are ORed.
-- These are static carrier/MVNO rules, never values collected from a live SIM.
CREATE TABLE profile_match_rules (
    match_rule_id INTEGER PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES carrier_profiles(profile_id) ON DELETE CASCADE,
    priority INTEGER NOT NULL DEFAULT 100,
    plmn TEXT CHECK (
        plmn IS NULL OR
        (length(plmn) IN (5, 6) AND plmn NOT GLOB '*[^0-9]*')
    ),
    imsi_prefix TEXT CHECK (
        imsi_prefix IS NULL OR
        (length(imsi_prefix) BETWEEN 5 AND 14 AND
         imsi_prefix NOT GLOB '*[^0-9]*')
    ),
    iccid_prefix TEXT CHECK (
        iccid_prefix IS NULL OR
        (length(iccid_prefix) BETWEEN 5 AND 17 AND
         iccid_prefix NOT GLOB '*[^0-9]*')
    ),
    gid1 TEXT,
    gid2 TEXT,
    spn TEXT,
    is_exclusion INTEGER NOT NULL DEFAULT 0 CHECK (is_exclusion IN (0, 1)),
    CHECK (
        plmn IS NOT NULL OR imsi_prefix IS NOT NULL OR
        iccid_prefix IS NOT NULL OR gid1 IS NOT NULL OR
        gid2 IS NOT NULL OR spn IS NOT NULL
    )
);

CREATE INDEX idx_match_plmn ON profile_match_rules(plmn, priority);
CREATE INDEX idx_match_imsi_prefix ON profile_match_rules(imsi_prefix, priority);
CREATE INDEX idx_match_iccid_prefix ON profile_match_rules(iccid_prefix, priority);

-- 7. A compiled profile can inherit multiple layers from the same firmware.
CREATE TABLE profile_sources (
    profile_id TEXT NOT NULL REFERENCES carrier_profiles(profile_id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES source_artifacts(source_id) ON DELETE CASCADE,
    source_profile_key TEXT,
    source_path TEXT NOT NULL,
    contribution_kind TEXT NOT NULL CHECK (contribution_kind IN (
        'firmware_default', 'carrier_policy', 'standard_default',
        'device_override'
    )),
    precedence INTEGER NOT NULL DEFAULT 100,
    PRIMARY KEY (profile_id, source_id, source_path, contribution_kind)
) WITHOUT ROWID;

-- 8. Evidence points to config JSON, match rules, profile metadata or carrier
-- metadata. source_value_json retains only public static source values.
CREATE TABLE field_evidence (
    evidence_id INTEGER PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES carrier_profiles(profile_id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES source_artifacts(source_id) ON DELETE CASCADE,
    target_kind TEXT NOT NULL CHECK (target_kind IN (
        'config', 'match_rule', 'profile', 'carrier'
    )),
    target_path TEXT NOT NULL,
    source_path TEXT,
    source_key_path TEXT,
    source_value_json TEXT CHECK (
        source_value_json IS NULL OR json_valid(source_value_json)
    ),
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN (
        'extracted', 'standard_derived', 'operator_metadata'
    )),
    confidence INTEGER NOT NULL CHECK (confidence BETWEEN 0 AND 100),
    selected INTEGER NOT NULL DEFAULT 1 CHECK (selected IN (0, 1))
);

CREATE INDEX idx_evidence_profile ON field_evidence(profile_id, target_kind, target_path);
CREATE INDEX idx_evidence_source ON field_evidence(source_id);

CREATE VIEW v_profile_catalog AS
SELECT
    cp.profile_id,
    cp.carrier_id,
    c.canonical_name,
    COALESCE(c.brand_name, c.canonical_name) AS carrier_name,
    cp.display_name,
    cp.profile_kind,
    cp.priority,
    cp.confidence,
    cp.lte_ims_status,
    cp.nr_ims_status,
    cp.vowifi_status,
    mr.match_rule_id,
    mr.plmn,
    mr.imsi_prefix,
    mr.iccid_prefix,
    mr.gid1,
    mr.gid2,
    mr.spn,
    COALESCE(cp.profile_asset_id, c.primary_asset_id) AS asset_id,
    cp.config_json
FROM carrier_profiles AS cp
JOIN carriers AS c USING (carrier_id)
LEFT JOIN profile_match_rules AS mr USING (profile_id);

CREATE VIEW v_visual_asset_catalog AS
SELECT
    asset_id, asset_kind, media_type, sha256, width, height,
    source_name, source_url, license_spdx, attribution, is_official,
    asset_data
FROM visual_assets;

COMMIT;
