-- Carrier Bundles canonical catalog schema (v5)
--
-- This database is a build artifact. Extractors write a NEW database, seal it,
-- and publish it atomically. Applications must open the published file with:
--   file:carrier_bundles.sqlite3?mode=ro&immutable=1

PRAGMA foreign_keys = ON;
PRAGMA application_id = 1128419922; -- ASCII-ish marker for Carrier Bundles
PRAGMA user_version = 5;

BEGIN;

CREATE TABLE schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

INSERT INTO schema_metadata(key, value) VALUES
    ('schema_name', 'carrier_bundles'),
    ('schema_version', '5'),
    ('data_model', 'immutable_firmware_catalog');

-- Exactly one release row exists in a published database.
CREATE TABLE catalog_release (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    release_id TEXT NOT NULL UNIQUE,
    generated_at TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    sealed INTEGER NOT NULL DEFAULT 0 CHECK (sealed IN (0, 1)),
    content_sha256 TEXT CHECK (
        content_sha256 IS NULL OR
        (length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9A-Fa-f]*')
    ),
    notes TEXT
);

-- Firmware/config snapshots from which catalog facts were extracted.
CREATE TABLE source_snapshots (
    source_id INTEGER PRIMARY KEY,
    source_kind TEXT NOT NULL CHECK (source_kind IN (
        'ios_carrier_bundle', 'ios_country_bundle', 'android_carrier_config',
        'android_overlay', 'android_apn_database', 'qualcomm_mcfg',
        'samsung_csc', 'mediatek_modem_config', 'firmware_metadata',
        'standards_reference', 'operator_metadata', 'icon_catalog', 'other'
    )),
    platform TEXT NOT NULL CHECK (platform IN ('ios', 'android', 'modem', 'shared')),
    vendor TEXT,
    device_family TEXT,
    device_model TEXT,
    os_version TEXT,
    build_id TEXT,
    baseband_version TEXT,
    source_revision TEXT,
    source_uri TEXT,
    artifact_sha256 TEXT CHECK (
        artifact_sha256 IS NULL OR
        (length(artifact_sha256) = 64 AND artifact_sha256 NOT GLOB '*[^0-9A-Fa-f]*')
    ),
    extracted_at TEXT NOT NULL,
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    license_note TEXT,
    UNIQUE (source_kind, platform, vendor, device_model, build_id, source_revision, artifact_sha256)
);

CREATE TABLE visual_assets (
    asset_id TEXT PRIMARY KEY,
    asset_kind TEXT NOT NULL CHECK (asset_kind IN ('operator_logo', 'carrier_badge', 'country_flag', 'placeholder')),
    asset_data BLOB NOT NULL CHECK (length(asset_data) > 0),
    local_path TEXT,
    remote_url TEXT,
    media_type TEXT NOT NULL CHECK (media_type IN ('image/svg+xml', 'image/png', 'image/webp')),
    sha256 TEXT CHECK (
        sha256 IS NULL OR
        (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9A-Fa-f]*')
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

CREATE TABLE carriers (
    carrier_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    brand_name TEXT,
    legal_name TEXT,
    carrier_kind TEXT NOT NULL DEFAULT 'mno' CHECK (carrier_kind IN ('mno', 'mvno', 'global', 'test', 'unknown')),
    country_iso2 TEXT CHECK (country_iso2 IS NULL OR (length(country_iso2) = 2 AND country_iso2 = upper(country_iso2))),
    website TEXT,
    primary_asset_id TEXT REFERENCES visual_assets(asset_id) ON DELETE SET NULL,
    notes TEXT
);

CREATE TABLE carrier_aliases (
    carrier_id TEXT NOT NULL REFERENCES carriers(carrier_id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    language TEXT,
    PRIMARY KEY (carrier_id, alias)
) WITHOUT ROWID;

CREATE TABLE plmns (
    plmn TEXT PRIMARY KEY,
    carrier_id TEXT REFERENCES carriers(carrier_id) ON DELETE SET NULL,
    mcc TEXT NOT NULL CHECK (length(mcc) = 3 AND mcc NOT GLOB '*[^0-9]*'),
    mnc TEXT NOT NULL CHECK (length(mnc) IN (2, 3) AND mnc NOT GLOB '*[^0-9]*'),
    mnc_length INTEGER NOT NULL CHECK (mnc_length IN (2, 3)),
    country_iso2 TEXT CHECK (country_iso2 IS NULL OR (length(country_iso2) = 2 AND country_iso2 = upper(country_iso2))),
    tadig TEXT,
    CHECK (plmn = mcc || mnc),
    CHECK (mnc_length = length(mnc))
) WITHOUT ROWID;

-- A profile is the platform-neutral result consumed by external projects.
-- New firmware produces a new catalog file, not an in-place profile revision.
CREATE TABLE carrier_profiles (
    profile_id TEXT PRIMARY KEY,
    carrier_id TEXT REFERENCES carriers(carrier_id) ON DELETE SET NULL,
    display_name TEXT NOT NULL,
    profile_kind TEXT NOT NULL DEFAULT 'default' CHECK (profile_kind IN ('default', 'mvno', 'device_specific', 'roaming', 'test')),
    priority INTEGER NOT NULL DEFAULT 100,
    confidence INTEGER NOT NULL DEFAULT 0 CHECK (confidence BETWEEN 0 AND 100),
    valid_from TEXT,
    valid_until TEXT,
    profile_asset_id TEXT REFERENCES visual_assets(asset_id) ON DELETE SET NULL,
    notes TEXT
);

CREATE TABLE profile_sources (
    profile_id TEXT NOT NULL REFERENCES carrier_profiles(profile_id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES source_snapshots(source_id) ON DELETE CASCADE,
    source_profile_key TEXT,
    source_path TEXT NOT NULL,
    source_priority INTEGER NOT NULL DEFAULT 100,
    PRIMARY KEY (profile_id, source_id, source_path)
) WITHOUT ROWID;

-- Non-null match columns in one row are ANDed. Rows for a profile are ORed.
-- Prefixes are public carrier rules, never actual subscriber identifiers.
CREATE TABLE profile_match_rules (
    match_rule_id INTEGER PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES carrier_profiles(profile_id) ON DELETE CASCADE,
    priority INTEGER NOT NULL DEFAULT 100,
    plmn TEXT REFERENCES plmns(plmn) ON DELETE CASCADE,
    imsi_prefix TEXT CHECK (imsi_prefix IS NULL OR imsi_prefix NOT GLOB '*[^0-9]*'),
    iccid_prefix TEXT CHECK (iccid_prefix IS NULL OR iccid_prefix NOT GLOB '*[^0-9]*'),
    gid1 TEXT,
    gid2 TEXT,
    spn TEXT,
    android_carrier_id TEXT,
    device_model_pattern TEXT,
    os_build_pattern TEXT,
    is_exclusion INTEGER NOT NULL DEFAULT 0 CHECK (is_exclusion IN (0, 1))
);

CREATE INDEX idx_match_plmn ON profile_match_rules(plmn, priority);
CREATE INDEX idx_match_imsi_prefix ON profile_match_rules(imsi_prefix, priority);

-- LTE/EPC, NR/5GC and Wi-Fi access are siblings sharing one IMS profile.
CREATE TABLE access_configs (
    access_id INTEGER PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES carrier_profiles(profile_id) ON DELETE CASCADE,
    access_kind TEXT NOT NULL CHECK (access_kind IN ('lte_epc', 'nr_5gc', 'wifi_epdg', 'wifi_n3iwf')),
    purpose TEXT NOT NULL DEFAULT 'ims' CHECK (purpose IN ('ims', 'emergency', 'xcap', 'entitlement')),
    enabled INTEGER CHECK (enabled IS NULL OR enabled IN (0, 1)),
    apn_dnn TEXT,
    apn_auth_type TEXT CHECK (apn_auth_type IS NULL OR apn_auth_type IN ('none', 'pap', 'chap', 'pap_or_chap', 'unspecified')),
    apn_username TEXT,
    apn_password TEXT,
    ip_family TEXT CHECK (ip_family IS NULL OR ip_family IN ('ipv4', 'ipv6', 'ipv4v6', 'ipv4_or_ipv6')),
    roaming_ip_family TEXT CHECK (roaming_ip_family IS NULL OR roaming_ip_family IN ('ipv4', 'ipv6', 'ipv4v6', 'ipv4_or_ipv6')),
    mtu INTEGER CHECK (mtu IS NULL OR mtu > 0),
    always_on INTEGER CHECK (always_on IS NULL OR always_on IN (0, 1)),
    pcscf_required INTEGER CHECK (pcscf_required IS NULL OR pcscf_required IN (0, 1)),
    snssai_sst INTEGER CHECK (snssai_sst IS NULL OR snssai_sst BETWEEN 0 AND 255),
    snssai_sd TEXT CHECK (snssai_sd IS NULL OR (length(snssai_sd) = 6 AND snssai_sd NOT GLOB '*[^0-9A-Fa-f]*')),
    ssc_mode INTEGER CHECK (ssc_mode IS NULL OR ssc_mode BETWEEN 1 AND 3),
    eps_fallback_allowed INTEGER CHECK (eps_fallback_allowed IS NULL OR eps_fallback_allowed IN (0, 1)),
    UNIQUE (profile_id, access_kind, purpose),
    UNIQUE (profile_id, access_id)
);

CREATE TABLE pcscf_discovery_methods (
    access_id INTEGER NOT NULL REFERENCES access_configs(access_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    method TEXT NOT NULL CHECK (method IN ('pco', 'epco', 'ike_cfg', 'dhcpv4', 'dhcpv6', 'dns_srv', 'dns_naptr', 'static')),
    PRIMARY KEY (access_id, position),
    UNIQUE (access_id, method)
) WITHOUT ROWID;

CREATE TABLE dns_resolvers (
    access_id INTEGER NOT NULL REFERENCES access_configs(access_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    address TEXT NOT NULL,
    port INTEGER NOT NULL DEFAULT 53 CHECK (port BETWEEN 1 AND 65535),
    source TEXT NOT NULL CHECK (source IN ('static', 'bearer', 'ike_cfg', 'system')),
    PRIMARY KEY (access_id, position)
) WITHOUT ROWID;

CREATE TABLE network_endpoints (
    endpoint_id INTEGER PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES carrier_profiles(profile_id) ON DELETE CASCADE,
    access_id INTEGER REFERENCES access_configs(access_id) ON DELETE CASCADE,
    service TEXT NOT NULL CHECK (service IN ('epdg', 'n3iwf', 'pcscf', 'registrar', 'bsf', 'xcap', 'entitlement', 'e911')),
    position INTEGER NOT NULL DEFAULT 0 CHECK (position >= 0),
    address_kind TEXT NOT NULL CHECK (address_kind IN ('fqdn', 'ipv4', 'ipv6', 'uri', 'derived_template')),
    address TEXT NOT NULL,
    port INTEGER CHECK (port IS NULL OR port BETWEEN 1 AND 65535),
    transport TEXT CHECK (transport IS NULL OR transport IN ('udp', 'tcp', 'tls', 'ikev2', 'https')),
    discovery_method TEXT NOT NULL CHECK (discovery_method IN ('static', 'pco', 'epco', 'ike_cfg', 'dns', 'standard_derived', 'redirect')),
    roaming_scope TEXT NOT NULL DEFAULT 'both' CHECK (roaming_scope IN ('home', 'visited', 'both')),
    UNIQUE (profile_id, access_id, service, position)
);

CREATE TABLE ike_configs (
    access_id INTEGER PRIMARY KEY REFERENCES access_configs(access_id) ON DELETE CASCADE,
    initial_port INTEGER NOT NULL DEFAULT 500 CHECK (initial_port BETWEEN 1 AND 65535),
    natt_port INTEGER NOT NULL DEFAULT 4500 CHECK (natt_port BETWEEN 1 AND 65535),
    eap_method TEXT NOT NULL DEFAULT 'eap_aka' CHECK (eap_method IN ('eap_aka', 'eap_aka_prime', 'certificate', 'other')),
    local_identity_format TEXT,
    remote_identity_format TEXT,
    send_device_identity TEXT NOT NULL DEFAULT 'on_request' CHECK (send_device_identity IN ('never', 'on_request', 'always')),
    request_internal_address INTEGER NOT NULL DEFAULT 1 CHECK (request_internal_address IN (0, 1)),
    request_pcscf INTEGER NOT NULL DEFAULT 1 CHECK (request_pcscf IN (0, 1)),
    request_dns INTEGER NOT NULL DEFAULT 0 CHECK (request_dns IN (0, 1)),
    nat_traversal INTEGER NOT NULL DEFAULT 1 CHECK (nat_traversal IN (0, 1)),
    nat_keepalive_seconds INTEGER CHECK (nat_keepalive_seconds IS NULL OR nat_keepalive_seconds > 0),
    dpd_interval_seconds INTEGER CHECK (dpd_interval_seconds IS NULL OR dpd_interval_seconds > 0),
    reauth_interval_seconds INTEGER CHECK (reauth_interval_seconds IS NULL OR reauth_interval_seconds > 0),
    ike_sa_lifetime_seconds INTEGER CHECK (ike_sa_lifetime_seconds IS NULL OR ike_sa_lifetime_seconds > 0),
    child_sa_lifetime_seconds INTEGER CHECK (child_sa_lifetime_seconds IS NULL OR child_sa_lifetime_seconds > 0),
    rekey_margin_seconds INTEGER CHECK (rekey_margin_seconds IS NULL OR rekey_margin_seconds >= 0),
    retransmit_profile TEXT,
    mobike INTEGER CHECK (mobike IS NULL OR mobike IN (0, 1)),
    fragmentation INTEGER CHECK (fragmentation IS NULL OR fragmentation IN (0, 1)),
    certificate_policy TEXT CHECK (certificate_policy IS NULL OR certificate_policy IN ('system_trust', 'pinned_ca', 'pinned_spki', 'not_applicable')),
    trusted_ca_ref TEXT,
    local_traffic_selector_template TEXT,
    remote_traffic_selector_template TEXT
) WITHOUT ROWID;

-- IKE_AUTH identities are templates/policies. Actual IMSI-based NAI,
-- pseudonyms and authenticated identities exist only in the runtime client.
CREATE TABLE ike_identity_rules (
    ike_identity_rule_id INTEGER PRIMARY KEY,
    access_id INTEGER NOT NULL REFERENCES ike_configs(access_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('idi', 'idr', 'anonymous_id')),
    position INTEGER NOT NULL DEFAULT 0 CHECK (position >= 0),
    identity_type TEXT NOT NULL CHECK (identity_type IN (
        'nai', 'id_fqdn', 'id_rfc822_addr', 'id_key_id', 'network_pseudonym'
    )),
    source_policy TEXT NOT NULL CHECK (source_policy IN (
        'derived_imsi', 'configured_template', 'epdg_fqdn',
        'network_pseudonym', 'network_fast_reauth_identity'
    )),
    value_template TEXT,
    send_policy TEXT NOT NULL CHECK (send_policy IN ('always', 'on_request', 'never')),
    use_when TEXT NOT NULL DEFAULT 'primary' CHECK (use_when IN ('primary', 'if_unavailable', 'reauthentication')),
    required INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
    CHECK (
        source_policy IN ('network_pseudonym', 'network_fast_reauth_identity')
        OR value_template IS NOT NULL
    ),
    CHECK (
        source_policy <> 'derived_imsi'
        OR instr(value_template, '{imsi}') > 0
    ),
    UNIQUE (access_id, role, position)
);

CREATE TABLE crypto_proposals (
    access_id INTEGER NOT NULL REFERENCES ike_configs(access_id) ON DELETE CASCADE,
    phase TEXT NOT NULL CHECK (phase IN ('ike_sa', 'child_sa')),
    position INTEGER NOT NULL CHECK (position >= 0),
    canonical_value TEXT NOT NULL,
    encryption TEXT,
    integrity TEXT,
    prf TEXT,
    dh_group TEXT,
    PRIMARY KEY (access_id, phase, position)
) WITHOUT ROWID;

CREATE TABLE ims_configs (
    profile_id TEXT PRIMARY KEY REFERENCES carrier_profiles(profile_id) ON DELETE CASCADE,
    home_domain TEXT NOT NULL,
    realm TEXT,
    private_identity_source TEXT NOT NULL CHECK (private_identity_source IN ('isim', 'derived_imsi', 'usim', 'auto')),
    public_identity_source TEXT NOT NULL CHECK (public_identity_source IN ('isim', 'derived_imsi', 'usim', 'network_assigned', 'auto')),
    authentication_scheme TEXT NOT NULL DEFAULT 'ims_aka' CHECK (authentication_scheme IN ('ims_aka', 'digest', 'tls')),
    aka_algorithm TEXT,
    transport_preference TEXT NOT NULL DEFAULT 'auto' CHECK (transport_preference IN ('auto', 'udp', 'tcp', 'tls')),
    local_port INTEGER CHECK (local_port IS NULL OR local_port BETWEEN 1 AND 65535),
    ipsec_security_agreement TEXT NOT NULL DEFAULT 'auto' CHECK (ipsec_security_agreement IN ('auto', 'required', 'disabled')),
    tcp_keepalive_seconds INTEGER CHECK (tcp_keepalive_seconds IS NULL OR tcp_keepalive_seconds >= 0),
    options_ping_interval_seconds INTEGER CHECK (options_ping_interval_seconds IS NULL OR options_ping_interval_seconds >= 0)
) WITHOUT ROWID;

-- IMPI/IMPU/contact identity templates describe how runtime SIM values are
-- formatted. They never contain a concrete subscriber identity.
CREATE TABLE ims_identity_templates (
    identity_template_id INTEGER PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES ims_configs(profile_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('impi', 'impu', 'contact_user', 'preferred_identity')),
    position INTEGER NOT NULL DEFAULT 0 CHECK (position >= 0),
    source_policy TEXT NOT NULL CHECK (source_policy IN (
        'isim', 'usim', 'derived_imsi', 'network_assigned', 'configured_template'
    )),
    identity_type TEXT NOT NULL CHECK (identity_type IN ('nai', 'sip_uri', 'tel_uri', 'username')),
    value_template TEXT,
    use_when TEXT NOT NULL DEFAULT 'primary' CHECK (use_when IN ('primary', 'if_isim_missing', 'if_unavailable', 'network_selected')),
    required INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
    CHECK (
        source_policy IN ('isim', 'usim', 'network_assigned')
        OR value_template IS NOT NULL
    ),
    CHECK (
        source_policy <> 'derived_imsi'
        OR instr(value_template, '{imsi}') > 0
    ),
    UNIQUE (profile_id, role, position)
);

-- One common REGISTER config may be inherited by access-specific LTE, NR or
-- VoWiFi configs. NULL scalar values inherit from the parent/client default.
CREATE TABLE sip_register_configs (
    register_config_id INTEGER PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES ims_configs(profile_id) ON DELETE CASCADE,
    access_id INTEGER,
    parent_register_config_id INTEGER,
    scope TEXT NOT NULL CHECK (scope IN ('common', 'access')),
    request_uri_policy TEXT CHECK (request_uri_policy IS NULL OR request_uri_policy IN ('home_domain', 'registrar', 'pcscf', 'configured')),
    requested_expires_seconds INTEGER CHECK (requested_expires_seconds IS NULL OR requested_expires_seconds > 0),
    min_expires_seconds INTEGER CHECK (min_expires_seconds IS NULL OR min_expires_seconds > 0),
    initial_authorization TEXT CHECK (initial_authorization IS NULL OR initial_authorization IN ('none', 'aka_empty', 'digest_empty', 'implementation_variant')),
    contact_mode TEXT CHECK (contact_mode IS NULL OR contact_mode IN ('standard', 'android_default', 'legacy', 'custom')),
    access_network_info_template TEXT,
    include_pani_initial INTEGER CHECK (include_pani_initial IS NULL OR include_pani_initial IN (0, 1)),
    include_pani_authenticated INTEGER CHECK (include_pani_authenticated IS NULL OR include_pani_authenticated IN (0, 1)),
    visited_network_id_policy TEXT,
    include_p_preferred_identity INTEGER CHECK (include_p_preferred_identity IS NULL OR include_p_preferred_identity IN (0, 1)),
    user_agent_template TEXT,
    allow_methods TEXT,
    retry_after_default_seconds INTEGER CHECK (retry_after_default_seconds IS NULL OR retry_after_default_seconds >= 0),
    max_retry_attempts INTEGER CHECK (max_retry_attempts IS NULL OR max_retry_attempts >= 0),
    inherit_parent_headers INTEGER NOT NULL DEFAULT 1 CHECK (inherit_parent_headers IN (0, 1)),
    FOREIGN KEY (profile_id, access_id) REFERENCES access_configs(profile_id, access_id),
    FOREIGN KEY (profile_id, parent_register_config_id)
        REFERENCES sip_register_configs(profile_id, register_config_id),
    CHECK (
        (scope = 'common' AND access_id IS NULL AND parent_register_config_id IS NULL)
        OR (scope = 'access' AND access_id IS NOT NULL)
    ),
    CHECK (parent_register_config_id IS NULL OR parent_register_config_id <> register_config_id),
    UNIQUE (profile_id, access_id),
    UNIQUE (profile_id, register_config_id)
);

CREATE UNIQUE INDEX uq_common_sip_register_config
    ON sip_register_configs(profile_id) WHERE access_id IS NULL;

-- No row means inherit/default. action='omit' is an explicit instruction not
-- to send the header; empty strings are not used as control values.
CREATE TABLE sip_header_rules (
    register_config_id INTEGER NOT NULL REFERENCES sip_register_configs(register_config_id) ON DELETE CASCADE,
    phase TEXT NOT NULL DEFAULT 'all' CHECK (phase IN (
        'all', 'initial', 'authenticated', 'refresh', 'deregister', 'emergency'
    )),
    position INTEGER NOT NULL CHECK (position >= 0),
    header_name TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('add', 'replace', 'omit')),
    value_template TEXT,
    required INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
    CHECK (
        (action = 'omit' AND value_template IS NULL)
        OR (action IN ('add', 'replace') AND value_template IS NOT NULL)
    ),
    PRIMARY KEY (register_config_id, phase, position)
) WITHOUT ROWID;

CREATE TABLE sip_contact_parameters (
    register_config_id INTEGER NOT NULL REFERENCES sip_register_configs(register_config_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    name TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'add' CHECK (action IN ('add', 'replace', 'omit')),
    value_template TEXT,
    required INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
    CHECK (name <> ''),
    PRIMARY KEY (register_config_id, position)
) WITHOUT ROWID;

-- Static part of Security-Client. SPI and port-c/port-s are runtime values.
CREATE TABLE sip_security_mechanisms (
    register_config_id INTEGER NOT NULL REFERENCES sip_register_configs(register_config_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    mechanism TEXT NOT NULL DEFAULT 'ipsec-3gpp',
    integrity_algorithm TEXT,
    encryption_algorithm TEXT,
    protocol TEXT,
    mode TEXT,
    preference REAL,
    required INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
    PRIMARY KEY (register_config_id, position)
) WITHOUT ROWID;

CREATE TABLE sip_status_policies (
    register_config_id INTEGER NOT NULL REFERENCES sip_register_configs(register_config_id) ON DELETE CASCADE,
    status_code INTEGER NOT NULL CHECK (status_code BETWEEN 300 AND 699),
    action TEXT NOT NULL CHECK (action IN ('retry', 'stop', 'fallback_variant', 'reauthenticate', 'honor_retry_after')),
    retry_seconds INTEGER CHECK (retry_seconds IS NULL OR retry_seconds >= 0),
    PRIMARY KEY (register_config_id, status_code, action)
) WITHOUT ROWID;

CREATE TABLE service_capabilities (
    profile_id TEXT NOT NULL REFERENCES carrier_profiles(profile_id) ON DELETE CASCADE,
    service TEXT NOT NULL CHECK (service IN ('ims', 'volte', 'vonr', 'vowifi', 'smsoip', 'mmtel', 'emergency', 'ut_xcap', 'video')),
    supported INTEGER CHECK (supported IS NULL OR supported IN (0, 1)),
    entitlement_required INTEGER CHECK (entitlement_required IS NULL OR entitlement_required IN (0, 1)),
    provisioning_required INTEGER CHECK (provisioning_required IS NULL OR provisioning_required IN (0, 1)),
    PRIMARY KEY (profile_id, service)
) WITHOUT ROWID;

CREATE TABLE entitlement_configs (
    profile_id TEXT NOT NULL REFERENCES carrier_profiles(profile_id) ON DELETE CASCADE,
    service TEXT NOT NULL CHECK (service IN ('volte', 'vonr', 'vowifi', 'smsoip', 'e911')),
    protocol TEXT NOT NULL CHECK (protocol IN ('gsma_ts43', 'vendor_https', 'websheet', 'other')),
    endpoint_id INTEGER REFERENCES network_endpoints(endpoint_id) ON DELETE SET NULL,
    authentication_method TEXT,
    required INTEGER CHECK (required IS NULL OR required IN (0, 1)),
    PRIMARY KEY (profile_id, service)
) WITHOUT ROWID;

CREATE TABLE emergency_configs (
    profile_id TEXT PRIMARY KEY REFERENCES carrier_profiles(profile_id) ON DELETE CASCADE,
    supported INTEGER CHECK (supported IS NULL OR supported IN (0, 1)),
    ims_registration_required INTEGER CHECK (ims_registration_required IS NULL OR ims_registration_required IN (0, 1)),
    address_provisioning_required INTEGER CHECK (address_provisioning_required IS NULL OR address_provisioning_required IN (0, 1)),
    emergency_access_preference TEXT,
    emergency_numbers TEXT
) WITHOUT ROWID;

-- Only whitelisted, public, static source values may be retained here.
-- Whole firmware files and subscriber/runtime values are never stored.
CREATE TABLE raw_config_values (
    raw_value_id INTEGER PRIMARY KEY,
    profile_id TEXT REFERENCES carrier_profiles(profile_id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES source_snapshots(source_id) ON DELETE CASCADE,
    source_path TEXT NOT NULL,
    key_path TEXT NOT NULL,
    value_json TEXT NOT NULL CHECK (json_valid(value_json)),
    value_type TEXT NOT NULL CHECK (value_type IN ('null', 'boolean', 'integer', 'real', 'text', 'array', 'object')),
    classification TEXT NOT NULL DEFAULT 'public_static' CHECK (classification = 'public_static'),
    UNIQUE (source_id, source_path, key_path)
);

CREATE TABLE field_evidence (
    evidence_id INTEGER PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES carrier_profiles(profile_id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES source_snapshots(source_id) ON DELETE CASCADE,
    table_name TEXT NOT NULL,
    row_key TEXT,
    field_name TEXT NOT NULL,
    source_path TEXT,
    key_path TEXT,
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('extracted', 'standard_derived', 'operator_metadata')),
    confidence INTEGER NOT NULL CHECK (confidence BETWEEN 0 AND 100)
);

CREATE INDEX idx_profiles_carrier ON carrier_profiles(carrier_id, priority);
CREATE INDEX idx_access_profile ON access_configs(profile_id, access_kind);
CREATE INDEX idx_endpoints_profile ON network_endpoints(profile_id, service, position);
CREATE INDEX idx_evidence_profile ON field_evidence(profile_id, table_name, field_name);
CREATE INDEX idx_sip_register_profile ON sip_register_configs(profile_id, access_id);

-- Stable consumer views. External projects should prefer these over deep joins.
CREATE VIEW v_carrier_catalog AS
SELECT
    cp.profile_id,
    cp.display_name AS profile_name,
    cp.profile_kind,
    cp.priority,
    cp.confidence,
    c.carrier_id,
    c.canonical_name AS carrier_name,
    c.brand_name,
    c.country_iso2,
    p.plmn,
    p.mcc,
    p.mnc,
    p.mnc_length,
    COALESCE(cp.profile_asset_id, c.primary_asset_id) AS asset_id,
    va.local_path AS asset_path,
    va.remote_url AS asset_remote_url,
    ims.home_domain AS ims_domain,
    ims.realm AS ims_realm,
    ims.private_identity_source,
    ims.public_identity_source,
    ims.transport_preference,
    ims.ipsec_security_agreement
FROM carrier_profiles AS cp
LEFT JOIN carriers AS c ON c.carrier_id = cp.carrier_id
LEFT JOIN profile_match_rules AS mr
    ON mr.profile_id = cp.profile_id AND mr.is_exclusion = 0
LEFT JOIN plmns AS p ON p.plmn = mr.plmn
LEFT JOIN visual_assets AS va ON va.asset_id = COALESCE(cp.profile_asset_id, c.primary_asset_id)
LEFT JOIN ims_configs AS ims ON ims.profile_id = cp.profile_id;

-- Keep binary assets out of broad carrier scans. Consumers fetch this view by
-- asset_id only when they need to render an icon.
CREATE VIEW v_visual_asset_catalog AS
SELECT
    asset_id,
    asset_kind,
    media_type,
    sha256,
    width,
    height,
    source_name,
    source_url,
    license_spdx,
    attribution,
    is_official,
    asset_data
FROM visual_assets;

CREATE VIEW v_access_catalog AS
SELECT
    cp.profile_id,
    mr.plmn,
    ac.access_id,
    ac.access_kind,
    ac.purpose,
    ac.enabled,
    ac.apn_dnn,
    ac.apn_auth_type,
    ac.apn_username,
    ac.apn_password,
    ac.ip_family,
    ac.roaming_ip_family,
    ac.mtu,
    ac.snssai_sst,
    ac.snssai_sd,
    ac.ssc_mode,
    ac.eps_fallback_allowed
FROM carrier_profiles AS cp
JOIN access_configs AS ac ON ac.profile_id = cp.profile_id
LEFT JOIN profile_match_rules AS mr
    ON mr.profile_id = cp.profile_id AND mr.is_exclusion = 0;

CREATE VIEW v_endpoint_catalog AS
SELECT
    ne.profile_id,
    mr.plmn,
    ac.access_kind,
    ne.service,
    ne.position,
    ne.address_kind,
    ne.address,
    ne.port,
    ne.transport,
    ne.discovery_method,
    ne.roaming_scope
FROM network_endpoints AS ne
LEFT JOIN access_configs AS ac ON ac.access_id = ne.access_id
LEFT JOIN profile_match_rules AS mr
    ON mr.profile_id = ne.profile_id AND mr.is_exclusion = 0;

-- One row per common/access-specific REGISTER config. Consumers resolve an
-- access row over its parent; remaining NULLs use protocol/client defaults.
CREATE VIEW v_sip_register_catalog AS
SELECT
    src.register_config_id,
    src.profile_id,
    src.scope,
    src.access_id,
    ac.access_kind,
    src.parent_register_config_id,
    COALESCE(src.request_uri_policy, parent.request_uri_policy) AS request_uri_policy,
    COALESCE(src.requested_expires_seconds, parent.requested_expires_seconds) AS requested_expires_seconds,
    COALESCE(src.min_expires_seconds, parent.min_expires_seconds) AS min_expires_seconds,
    COALESCE(src.initial_authorization, parent.initial_authorization) AS initial_authorization,
    COALESCE(src.contact_mode, parent.contact_mode) AS contact_mode,
    COALESCE(src.access_network_info_template, parent.access_network_info_template) AS access_network_info_template,
    COALESCE(src.include_pani_initial, parent.include_pani_initial) AS include_pani_initial,
    COALESCE(src.include_pani_authenticated, parent.include_pani_authenticated) AS include_pani_authenticated,
    COALESCE(src.visited_network_id_policy, parent.visited_network_id_policy) AS visited_network_id_policy,
    COALESCE(src.include_p_preferred_identity, parent.include_p_preferred_identity) AS include_p_preferred_identity,
    COALESCE(src.user_agent_template, parent.user_agent_template) AS user_agent_template,
    COALESCE(src.allow_methods, parent.allow_methods) AS allow_methods,
    COALESCE(src.retry_after_default_seconds, parent.retry_after_default_seconds) AS retry_after_default_seconds,
    COALESCE(src.max_retry_attempts, parent.max_retry_attempts) AS max_retry_attempts,
    src.inherit_parent_headers
FROM sip_register_configs AS src
LEFT JOIN sip_register_configs AS parent
    ON parent.register_config_id = src.parent_register_config_id
LEFT JOIN access_configs AS ac ON ac.access_id = src.access_id;

COMMIT;
