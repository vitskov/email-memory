ENTITY_SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS metadata (
    key VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS seq_person_id START 1;
CREATE TABLE IF NOT EXISTS people (
    person_id BIGINT PRIMARY KEY DEFAULT nextval('seq_person_id'),
    canonical_name VARCHAR NOT NULL,
    normalized_name VARCHAR NOT NULL,
    organization_hint VARCHAR,
    disambiguation_status VARCHAR DEFAULT 'resolved',
    email_count BIGINT DEFAULT 0,
    message_count BIGINT DEFAULT 0,
    first_seen_at TIMESTAMP,
    last_seen_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_people_name_org ON people(normalized_name, organization_hint);

CREATE SEQUENCE IF NOT EXISTS seq_person_email_id START 1;
CREATE TABLE IF NOT EXISTS person_emails (
    person_email_id BIGINT PRIMARY KEY DEFAULT nextval('seq_person_email_id'),
    person_id BIGINT NOT NULL,
    email_address VARCHAR NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_person_emails_person_id ON person_emails(person_id);

CREATE SEQUENCE IF NOT EXISTS seq_person_alias_id START 1;
CREATE TABLE IF NOT EXISTS person_aliases (
    person_alias_id BIGINT PRIMARY KEY DEFAULT nextval('seq_person_alias_id'),
    person_id BIGINT NOT NULL,
    alias_name VARCHAR NOT NULL,
    normalized_alias VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(person_id, normalized_alias)
);

CREATE INDEX IF NOT EXISTS idx_person_aliases_normalized_alias ON person_aliases(normalized_alias);

CREATE SEQUENCE IF NOT EXISTS seq_entity_resolution_event_id START 1;
CREATE TABLE IF NOT EXISTS entity_resolution_log (
    event_id BIGINT PRIMARY KEY DEFAULT nextval('seq_entity_resolution_event_id'),
    action VARCHAR NOT NULL,
    primary_person_id BIGINT,
    secondary_person_id BIGINT,
    new_person_id BIGINT,
    reason VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS seq_message_entity_index_id START 1;
CREATE TABLE IF NOT EXISTS message_entity_index (
    message_entity_index_id BIGINT PRIMARY KEY DEFAULT nextval('seq_message_entity_index_id'),
    person_id BIGINT NOT NULL,
    email_message_pk BIGINT NOT NULL,
    stable_message_id VARCHAR NOT NULL,
    canonical_name VARCHAR NOT NULL,
    normalized_name VARCHAR NOT NULL,
    role VARCHAR NOT NULL,
    email_address VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(person_id, stable_message_id, role, email_address)
);

CREATE INDEX IF NOT EXISTS idx_message_entity_index_person_id ON message_entity_index(person_id);
CREATE INDEX IF NOT EXISTS idx_message_entity_index_message_pk ON message_entity_index(email_message_pk);
"""
