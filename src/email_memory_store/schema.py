SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS metadata (
    key VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS seq_account_id START 1;
CREATE TABLE IF NOT EXISTS accounts (
    account_id BIGINT PRIMARY KEY DEFAULT nextval('seq_account_id'),
    account_name VARCHAR NOT NULL UNIQUE,
    email_address VARCHAR NOT NULL,
    provider VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS seq_folder_id START 1;
CREATE TABLE IF NOT EXISTS folders (
    folder_id BIGINT PRIMARY KEY DEFAULT nextval('seq_folder_id'),
    account_id BIGINT NOT NULL,
    folder_name VARCHAR NOT NULL,
    parent_folder_name VARCHAR,
    folder_type VARCHAR,
    last_scanned_at TIMESTAMP,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, folder_name)
);

CREATE SEQUENCE IF NOT EXISTS seq_message_pk START 1;
CREATE TABLE IF NOT EXISTS messages (
    message_pk BIGINT PRIMARY KEY DEFAULT nextval('seq_message_pk'),
    account_id BIGINT NOT NULL,
    folder_id BIGINT,
    mailbox_message_id VARCHAR,
    stable_message_id VARCHAR NOT NULL UNIQUE,
    identity_source VARCHAR DEFAULT 'provisional',
    internet_message_id VARCHAR,
    rfc_in_reply_to VARCHAR,
    rfc_references_json JSON,
    thread_key VARCHAR,
    subject VARCHAR,
    normalized_subject VARCHAR,
    from_name VARCHAR,
    from_addr VARCHAR,
    to_addrs JSON,
    cc_addrs JSON,
    bcc_addrs JSON,
    sent_at TIMESTAMP,
    received_at TIMESTAMP,
    cleaned_text TEXT,
    text_hash VARCHAR,
    has_attachments BOOLEAN DEFAULT FALSE,
    importance_score DOUBLE DEFAULT 0,
    direction VARCHAR,
    is_read BOOLEAN,
    raw_path VARCHAR,
    ingestion_run_id VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_messages_thread_key ON messages(thread_key);
CREATE INDEX IF NOT EXISTS idx_messages_internet_message_id ON messages(internet_message_id);
CREATE INDEX IF NOT EXISTS idx_messages_received_at ON messages(received_at);
CREATE INDEX IF NOT EXISTS idx_messages_from_addr ON messages(from_addr);

CREATE SEQUENCE IF NOT EXISTS seq_message_label_id START 1;
CREATE TABLE IF NOT EXISTS message_labels (
    message_label_id BIGINT PRIMARY KEY DEFAULT nextval('seq_message_label_id'),
    message_pk BIGINT NOT NULL,
    label VARCHAR NOT NULL,
    label_type VARCHAR NOT NULL DEFAULT 'folder',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(message_pk, label, label_type)
);

CREATE INDEX IF NOT EXISTS idx_message_labels_message_pk ON message_labels(message_pk);
CREATE INDEX IF NOT EXISTS idx_message_labels_label ON message_labels(label);

CREATE TABLE IF NOT EXISTS ingest_sync_state (
    account_name VARCHAR NOT NULL,
    folder_name VARCHAR NOT NULL,
    sync_kind VARCHAR NOT NULL,
    next_page BIGINT DEFAULT 1,
    last_completed_page BIGINT,
    status VARCHAR DEFAULT 'pending',
    last_run_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(account_name, folder_name, sync_kind)
);

CREATE SEQUENCE IF NOT EXISTS seq_failed_message_ingestion_id START 1;
CREATE TABLE IF NOT EXISTS failed_message_ingestions (
    failed_message_ingestion_id BIGINT PRIMARY KEY DEFAULT nextval('seq_failed_message_ingestion_id'),
    account_name VARCHAR NOT NULL,
    folder_name VARCHAR NOT NULL,
    mailbox_message_id VARCHAR NOT NULL,
    stable_message_id VARCHAR,
    failure_kind VARCHAR NOT NULL,
    error TEXT,
    retry_count BIGINT DEFAULT 0,
    status VARCHAR DEFAULT 'pending',
    first_failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_name, folder_name, mailbox_message_id, failure_kind)
);

CREATE INDEX IF NOT EXISTS idx_failed_message_ingestions_status ON failed_message_ingestions(status);
CREATE INDEX IF NOT EXISTS idx_failed_message_ingestions_account_folder ON failed_message_ingestions(account_name, folder_name);

CREATE SEQUENCE IF NOT EXISTS seq_email_entity_index_id START 1;
CREATE TABLE IF NOT EXISTS email_entity_index (
    email_entity_index_id BIGINT PRIMARY KEY DEFAULT nextval('seq_email_entity_index_id'),
    message_pk BIGINT NOT NULL,
    stable_message_id VARCHAR NOT NULL,
    person_id BIGINT NOT NULL,
    canonical_name VARCHAR NOT NULL,
    normalized_name VARCHAR NOT NULL,
    role VARCHAR NOT NULL,
    email_address VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(person_id, stable_message_id, role, email_address)
);

CREATE INDEX IF NOT EXISTS idx_email_entity_index_message_pk ON email_entity_index(message_pk);
CREATE INDEX IF NOT EXISTS idx_email_entity_index_person_id ON email_entity_index(person_id);

CREATE SEQUENCE IF NOT EXISTS seq_thread_id START 1;
CREATE TABLE IF NOT EXISTS threads (
    thread_id BIGINT PRIMARY KEY DEFAULT nextval('seq_thread_id'),
    account_id BIGINT NOT NULL,
    canonical_subject VARCHAR,
    thread_key VARCHAR NOT NULL UNIQUE,
    first_message_at TIMESTAMP,
    last_message_at TIMESTAMP,
    message_count BIGINT DEFAULT 0,
    participant_count BIGINT DEFAULT 0,
    last_summary_at TIMESTAMP,
    current_status VARCHAR,
    priority_score DOUBLE DEFAULT 0,
    latest_decision_state VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS seq_contact_id START 1;
CREATE TABLE IF NOT EXISTS contacts (
    contact_id BIGINT PRIMARY KEY DEFAULT nextval('seq_contact_id'),
    primary_email VARCHAR NOT NULL UNIQUE,
    display_name VARCHAR,
    organization VARCHAR,
    role_guess VARCHAR,
    first_seen_at TIMESTAMP,
    last_seen_at TIMESTAMP,
    message_count_in BIGINT DEFAULT 0,
    message_count_out BIGINT DEFAULT 0,
    contact_strength DOUBLE DEFAULT 0,
    notes_summary TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS seq_thread_summary_id START 1;
CREATE TABLE IF NOT EXISTS thread_summaries (
    summary_id BIGINT PRIMARY KEY DEFAULT nextval('seq_thread_summary_id'),
    thread_id BIGINT NOT NULL,
    summary_type VARCHAR NOT NULL,
    summary_text TEXT NOT NULL,
    key_points_json JSON,
    action_items_json JSON,
    decisions_json JSON,
    deadlines_json JSON,
    participants_json JSON,
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source_message_count BIGINT DEFAULT 0
);

CREATE SEQUENCE IF NOT EXISTS seq_decision_id START 1;
CREATE TABLE IF NOT EXISTS decisions (
    decision_id BIGINT PRIMARY KEY DEFAULT nextval('seq_decision_id'),
    thread_id BIGINT,
    title VARCHAR,
    decision_text TEXT NOT NULL,
    decided_by_json JSON,
    decided_at TIMESTAMP,
    confidence DOUBLE DEFAULT 0,
    status VARCHAR,
    superseded_by BIGINT,
    source_message_pk BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS seq_action_item_id START 1;
CREATE TABLE IF NOT EXISTS action_items (
    action_item_id BIGINT PRIMARY KEY DEFAULT nextval('seq_action_item_id'),
    thread_id BIGINT,
    message_pk BIGINT,
    owner VARCHAR,
    action_text TEXT NOT NULL,
    due_date TIMESTAMP,
    status VARCHAR,
    confidence DOUBLE DEFAULT 0,
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS seq_deadline_id START 1;
CREATE TABLE IF NOT EXISTS deadlines (
    deadline_id BIGINT PRIMARY KEY DEFAULT nextval('seq_deadline_id'),
    thread_id BIGINT,
    message_pk BIGINT,
    label VARCHAR,
    due_date TIMESTAMP,
    related_project VARCHAR,
    confidence DOUBLE DEFAULT 0,
    status VARCHAR,
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE IF NOT EXISTS seq_calendar_event_id START 1;
CREATE TABLE IF NOT EXISTS calendar_events (
    calendar_event_id BIGINT PRIMARY KEY DEFAULT nextval('seq_calendar_event_id'),
    message_pk BIGINT NOT NULL,
    filename VARCHAR,
    mime_type VARCHAR,
    method VARCHAR,
    uid VARCHAR,
    summary VARCHAR,
    description TEXT,
    status VARCHAR,
    sequence BIGINT,
    recurrence_id VARCHAR,
    recurrence_rule VARCHAR,
    starts_at TIMESTAMP,
    ends_at TIMESTAMP,
    starts_at_tzid VARCHAR,
    ends_at_tzid VARCHAR,
    recurrence_id_tzid VARCHAR,
    organizer VARCHAR,
    organizer_email VARCHAR,
    location VARCHAR,
    attendees_json JSON,
    raw_ics TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_calendar_events_message_pk ON calendar_events(message_pk);

CREATE SEQUENCE IF NOT EXISTS seq_promotion_id START 1;
CREATE TABLE IF NOT EXISTS promotion_log (
    promotion_id BIGINT PRIMARY KEY DEFAULT nextval('seq_promotion_id'),
    source_object_type VARCHAR NOT NULL,
    source_object_id VARCHAR NOT NULL,
    promoted_text TEXT NOT NULL,
    promoted_category VARCHAR,
    promoted_tags VARCHAR,
    fact_store_dedup_key VARCHAR,
    fact_store_batch_id VARCHAR,
    fact_store_written_at TIMESTAMP,
    promoted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    holographic_fact_id BIGINT,
    status VARCHAR DEFAULT 'pending'
);
"""
