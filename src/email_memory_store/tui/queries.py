"""Pure DuckDB query functions for the TUI browser.

No Textual imports — only duckdb and stdlib.
"""
from __future__ import annotations

import duckdb


def _rows_to_dicts(cursor: duckdb.DuckDBPyConnection) -> list[dict]:
    """Convert cursor results to list of dicts using description."""
    if cursor.description is None:
        return []
    col_names = [d[0] for d in cursor.description]
    return [dict(zip(col_names, row)) for row in cursor.fetchall()]


def fetch_action_items(
    conn: duckdb.DuckDBPyConnection,
    *,
    search: str | None = None,
    status: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Fetch action items, joining threads for subject."""
    params: list = []
    where_clauses: list[str] = []

    if search:
        where_clauses.append("""(
            lower(ai.action_text) LIKE lower('%' || ? || '%')
            OR lower(COALESCE(ai.owner,'')) LIKE lower('%' || ? || '%')
            OR lower(COALESCE(t.canonical_subject,'')) LIKE lower('%' || ? || '%')
            OR EXISTS (
                SELECT 1 FROM email_entity_index ei2
                WHERE ei2.message_pk = ai.message_pk
                AND (lower(COALESCE(ei2.canonical_name,'')) LIKE lower('%' || ? || '%')
                     OR lower(COALESCE(ei2.email_address,'')) LIKE lower('%' || ? || '%'))
            )
        )""")
        params.extend([search] * 5)
    if status:
        where_clauses.append("ai.status = ?")
        params.append(status)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT
            ai.action_item_id,
            ai.thread_id,
            ai.message_pk,
            COALESCE(
                CASE WHEN ai.owner IS NOT NULL AND ei.canonical_name IS NOT NULL
                     THEN ei.canonical_name || ' <' || ei.email_address || '>'
                     ELSE ai.owner END,
                ai.owner
            ) AS owner,
            ai.action_text,
            ai.due_date,
            ai.status,
            ai.confidence,
            ai.extracted_at,
            t.canonical_subject AS thread_subject
        FROM action_items ai
        LEFT JOIN threads t ON t.thread_id = ai.thread_id
        LEFT JOIN (
            SELECT canonical_name, email_address, normalized_name
            FROM email_entity_index
            WHERE email_address IS NOT NULL
            QUALIFY ROW_NUMBER() OVER (PARTITION BY normalized_name ORDER BY email_entity_index_id) = 1
        ) ei ON ai.owner IS NOT NULL
             AND lower(ei.normalized_name) LIKE '%' || lower(ai.owner) || '%'
        {where_sql}
        ORDER BY ai.due_date ASC NULLS LAST, ai.confidence DESC
        LIMIT ?
    """
    params.append(limit)
    cursor = conn.execute(sql, params)
    return _rows_to_dicts(cursor)


def fetch_deadlines(
    conn: duckdb.DuckDBPyConnection,
    *,
    search: str | None = None,
    status: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Fetch deadlines, joining threads for subject."""
    params: list = []
    where_clauses: list[str] = []

    if search:
        where_clauses.append("""(
            lower(COALESCE(d.label,'')) LIKE lower('%' || ? || '%')
            OR lower(COALESCE(d.related_project,'')) LIKE lower('%' || ? || '%')
            OR lower(COALESCE(t.canonical_subject,'')) LIKE lower('%' || ? || '%')
        )""")
        params.extend([search] * 3)
    if status:
        where_clauses.append("d.status = ?")
        params.append(status)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT
            d.deadline_id,
            d.thread_id,
            d.message_pk,
            d.label,
            d.due_date,
            d.related_project,
            d.confidence,
            d.status,
            d.extracted_at,
            t.canonical_subject AS thread_subject
        FROM deadlines d
        LEFT JOIN threads t ON t.thread_id = d.thread_id
        {where_sql}
        ORDER BY d.due_date ASC NULLS LAST, d.confidence DESC
        LIMIT ?
    """
    params.append(limit)
    cursor = conn.execute(sql, params)
    return _rows_to_dicts(cursor)


def fetch_decisions(
    conn: duckdb.DuckDBPyConnection,
    *,
    search: str | None = None,
    status: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Fetch decisions, joining threads for subject."""
    params: list = []
    where_clauses: list[str] = []

    if search:
        where_clauses.append("""(
            lower(COALESCE(d.title,'')) LIKE lower('%' || ? || '%')
            OR lower(COALESCE(d.decision_text,'')) LIKE lower('%' || ? || '%')
            OR lower(COALESCE(d.decided_by_json,'')) LIKE lower('%' || ? || '%')
            OR lower(COALESCE(t.canonical_subject,'')) LIKE lower('%' || ? || '%')
        )""")
        params.extend([search] * 4)
    if status:
        where_clauses.append("d.status = ?")
        params.append(status)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT
            d.decision_id,
            d.thread_id,
            d.title,
            d.decision_text,
            d.decided_by_json,
            d.decided_at,
            d.confidence,
            d.status,
            d.superseded_by,
            d.source_message_pk,
            d.created_at,
            d.updated_at,
            t.canonical_subject AS thread_subject
        FROM decisions d
        LEFT JOIN threads t ON t.thread_id = d.thread_id
        {where_sql}
        ORDER BY d.decided_at DESC NULLS LAST, d.confidence DESC
        LIMIT ?
    """
    params.append(limit)
    cursor = conn.execute(sql, params)
    return _rows_to_dicts(cursor)


def fetch_thread_summaries(
    conn: duckdb.DuckDBPyConnection,
    *,
    search: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Fetch thread summaries, joining threads for subject."""
    params: list = []
    where_clauses: list[str] = []

    if search:
        where_clauses.append("""(
            lower(COALESCE(ts.summary_text,'')) LIKE lower('%' || ? || '%')
            OR lower(COALESCE(ts.participants_json,'')) LIKE lower('%' || ? || '%')
            OR lower(COALESCE(t.canonical_subject,'')) LIKE lower('%' || ? || '%')
        )""")
        params.extend([search] * 3)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT
            ts.summary_id,
            ts.thread_id,
            ts.summary_type,
            ts.summary_text,
            ts.key_points_json,
            ts.action_items_json,
            ts.decisions_json,
            ts.deadlines_json,
            ts.participants_json,
            ts.generated_at,
            ts.source_message_count,
            t.canonical_subject AS thread_subject,
            COALESCE(
                t.last_message_at,
                (SELECT MAX(m.sent_at) FROM messages m
                 JOIN action_items ai ON ai.message_pk = m.message_pk AND ai.thread_id = ts.thread_id),
                (SELECT MAX(m.sent_at) FROM messages m
                 JOIN deadlines dl ON dl.message_pk = m.message_pk AND dl.thread_id = ts.thread_id)
            ) AS last_message_at
        FROM thread_summaries ts
        LEFT JOIN threads t ON t.thread_id = ts.thread_id
        {where_sql}
        ORDER BY COALESCE(
            t.last_message_at,
            (SELECT MAX(m.sent_at) FROM messages m
             JOIN action_items ai ON ai.message_pk = m.message_pk AND ai.thread_id = ts.thread_id),
            (SELECT MAX(m.sent_at) FROM messages m
             JOIN deadlines dl ON dl.message_pk = m.message_pk AND dl.thread_id = ts.thread_id)
        ) DESC NULLS LAST
        LIMIT ?
    """
    params.append(limit)
    cursor = conn.execute(sql, params)
    return _rows_to_dicts(cursor)


def fetch_promotions(
    conn: duckdb.DuckDBPyConnection,
    *,
    search: str | None = None,
    status: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Fetch promotion log entries."""
    params: list = []
    where_clauses: list[str] = []

    if search:
        where_clauses.append("lower(p.promoted_text) LIKE lower('%' || ? || '%')")
        params.append(search)
    if status:
        where_clauses.append("p.status = ?")
        params.append(status)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT
            p.promotion_id,
            p.source_object_type AS candidate_type,
            p.source_object_id AS candidate_id,
            p.promoted_text,
            p.promoted_category,
            p.promoted_tags,
            p.status,
            p.promoted_at AS created_at,
            p.holographic_fact_id
        FROM promotion_log p
        {where_sql}
        ORDER BY p.promoted_at DESC
        LIMIT ?
    """
    params.append(limit)
    cursor = conn.execute(sql, params)
    return _rows_to_dicts(cursor)


def fetch_full_fact(
    conn: duckdb.DuckDBPyConnection,
    fact_type: str,
    fact_id: int,
) -> dict | None:
    """Fetch the complete row for a fact from its source table."""
    if fact_type == 'action':
        sql = """
            SELECT ai.*, t.canonical_subject AS thread_subject
            FROM action_items ai
            LEFT JOIN threads t ON t.thread_id = ai.thread_id
            WHERE ai.action_item_id = ?
        """
    elif fact_type == 'deadline':
        sql = """
            SELECT d.*, t.canonical_subject AS thread_subject
            FROM deadlines d
            LEFT JOIN threads t ON t.thread_id = d.thread_id
            WHERE d.deadline_id = ?
        """
    elif fact_type == 'decision':
        sql = """
            SELECT d.*, t.canonical_subject AS thread_subject
            FROM decisions d
            LEFT JOIN threads t ON t.thread_id = d.thread_id
            WHERE d.decision_id = ?
        """
    elif fact_type == 'summary':
        sql = """
            SELECT ts.*, t.canonical_subject AS thread_subject
            FROM thread_summaries ts
            LEFT JOIN threads t ON t.thread_id = ts.thread_id
            WHERE ts.summary_id = ?
        """
    else:
        return None
    rows = _rows_to_dicts(conn.execute(sql, [fact_id]))
    return rows[0] if rows else None


def fetch_all_facts(
    conn: duckdb.DuckDBPyConnection,
    *,
    search: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """UNION of action items, deadlines, decisions and thread summaries.

    Each row has: fact_type, fact_id, text, date, status, confidence, thread_subject.
    Ordered by date DESC so the most recent items appear first.
    """
    params: list = []
    search_clause_ai = """AND (
        lower(ai.action_text) LIKE lower('%' || ? || '%')
        OR lower(COALESCE(ai.owner,'')) LIKE lower('%' || ? || '%')
        OR lower(COALESCE(t.canonical_subject,'')) LIKE lower('%' || ? || '%')
        OR EXISTS (SELECT 1 FROM email_entity_index ei
                   WHERE ei.message_pk = ai.message_pk
                   AND (lower(COALESCE(ei.canonical_name,'')) LIKE lower('%' || ? || '%')
                        OR lower(COALESCE(ei.email_address,'')) LIKE lower('%' || ? || '%')))
    )""" if search else ""
    search_clause_dl = """AND (
        lower(COALESCE(d.label,'')) LIKE lower('%' || ? || '%')
        OR lower(COALESCE(d.related_project,'')) LIKE lower('%' || ? || '%')
        OR lower(COALESCE(t.canonical_subject,'')) LIKE lower('%' || ? || '%')
    )""" if search else ""
    search_clause_dec = """AND (
        lower(COALESCE(dc.title,'')) LIKE lower('%' || ? || '%')
        OR lower(COALESCE(dc.decision_text,'')) LIKE lower('%' || ? || '%')
        OR lower(COALESCE(dc.decided_by_json,'')) LIKE lower('%' || ? || '%')
        OR lower(COALESCE(t.canonical_subject,'')) LIKE lower('%' || ? || '%')
    )""" if search else ""
    search_clause_ts = """AND (
        lower(COALESCE(ts.summary_text,'')) LIKE lower('%' || ? || '%')
        OR lower(COALESCE(ts.participants_json,'')) LIKE lower('%' || ? || '%')
        OR lower(COALESCE(t.canonical_subject,'')) LIKE lower('%' || ? || '%')
    )""" if search else ""
    if search:
        params = [search]*5 + [search]*3 + [search]*4 + [search]*3 + [limit]
    else:
        params = [limit]

    sql = f"""
        SELECT fact_type, fact_id, text, date, status, confidence, thread_subject
        FROM (
            SELECT
                'action'   AS fact_type,
                ai.action_item_id AS fact_id,
                ai.action_text    AS text,
                ai.due_date       AS date,
                ai.status         AS status,
                ai.confidence     AS confidence,
                t.canonical_subject AS thread_subject
            FROM action_items ai
            LEFT JOIN threads t ON t.thread_id = ai.thread_id
            WHERE 1=1 {search_clause_ai}

            UNION ALL

            SELECT
                'deadline' AS fact_type,
                d.deadline_id     AS fact_id,
                d.label           AS text,
                d.due_date        AS date,
                d.status          AS status,
                d.confidence      AS confidence,
                t.canonical_subject AS thread_subject
            FROM deadlines d
            LEFT JOIN threads t ON t.thread_id = d.thread_id
            WHERE 1=1 {search_clause_dl}

            UNION ALL

            SELECT
                'decision' AS fact_type,
                dc.decision_id    AS fact_id,
                dc.title          AS text,
                dc.decided_at     AS date,
                dc.status         AS status,
                dc.confidence     AS confidence,
                t.canonical_subject AS thread_subject
            FROM decisions dc
            LEFT JOIN threads t ON t.thread_id = dc.thread_id
            WHERE 1=1 {search_clause_dec}

            UNION ALL

            SELECT
                'summary'  AS fact_type,
                ts.summary_id     AS fact_id,
                ts.summary_text   AS text,
                COALESCE(
                    t.last_message_at,
                    (SELECT MAX(m.sent_at) FROM messages m
                     JOIN action_items ai ON ai.message_pk = m.message_pk AND ai.thread_id = ts.thread_id),
                    (SELECT MAX(m.sent_at) FROM messages m
                     JOIN deadlines dl ON dl.message_pk = m.message_pk AND dl.thread_id = ts.thread_id),
                    (SELECT MAX(m.sent_at) FROM messages m
                     JOIN decisions dc ON dc.source_message_pk = m.message_pk AND dc.thread_id = ts.thread_id)
                ) AS date,
                NULL              AS status,
                NULL              AS confidence,
                t.canonical_subject AS thread_subject
            FROM thread_summaries ts
            LEFT JOIN threads t ON t.thread_id = ts.thread_id
            WHERE 1=1 {search_clause_ts}
        ) all_facts
        ORDER BY date DESC NULLS LAST
        LIMIT ?
    """
    cursor = conn.execute(sql, params)
    return _rows_to_dicts(cursor)


def fetch_people(
    conn: duckdb.DuckDBPyConnection,
    *,
    search: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Fetch people from the entity database.

    Takes the entity connection (``store.entity_store.conn``), not the email one.
    Matches the name/alias/email search predicate used by
    ``EntityMemoryStore.search_people``, but aggregates emails in a single
    query rather than one per row.
    """
    params: list = []
    where_sql = ""

    if search:
        where_sql = """
        WHERE lower(p.canonical_name) LIKE lower('%' || ? || '%')
           OR EXISTS (SELECT 1 FROM person_aliases a
                      WHERE a.person_id = p.person_id
                        AND lower(a.alias_name) LIKE lower('%' || ? || '%'))
           OR EXISTS (SELECT 1 FROM person_emails e
                      WHERE e.person_id = p.person_id
                        AND lower(e.email_address) LIKE lower('%' || ? || '%'))
        """
        params.extend([search, search, search])

    sql = f"""
        SELECT
            p.person_id,
            p.canonical_name,
            p.organization_hint,
            p.disambiguation_status,
            p.email_count,
            p.message_count,
            p.first_seen_at,
            p.last_seen_at,
            (SELECT string_agg(e.email_address, ', ' ORDER BY e.email_address)
             FROM person_emails e WHERE e.person_id = p.person_id) AS emails
        FROM people p
        {where_sql}
        ORDER BY p.message_count DESC, p.person_id ASC
        LIMIT ?
    """
    params.append(limit)
    cursor = conn.execute(sql, params)
    return _rows_to_dicts(cursor)


def fetch_calendar_events(
    conn: duckdb.DuckDBPyConnection,
    *,
    search: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Fetch calendar events, carrying thread_id so 't' can drill into the thread."""
    params: list = []
    where_clauses: list[str] = []

    if search:
        where_clauses.append(
            "(lower(c.summary) LIKE lower('%' || ? || '%')"
            " OR lower(c.organizer) LIKE lower('%' || ? || '%')"
            " OR lower(c.location) LIKE lower('%' || ? || '%'))"
        )
        params.extend([search, search, search])

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT
            c.calendar_event_id,
            c.summary,
            c.starts_at,
            c.ends_at,
            c.organizer,
            c.organizer_email,
            c.location,
            c.status,
            c.method,
            c.uid,
            c.description,
            c.attendees_json,
            t.thread_id,
            t.canonical_subject AS thread_subject
        FROM calendar_events c
        LEFT JOIN messages m ON m.message_pk = c.message_pk
        LEFT JOIN threads t
               ON t.thread_key = m.thread_key
              AND t.account_id = m.account_id
        {where_sql}
        ORDER BY c.starts_at DESC NULLS LAST
        LIMIT ?
    """
    params.append(limit)
    cursor = conn.execute(sql, params)
    return _rows_to_dicts(cursor)


def fetch_pipeline_health(
    conn: duckdb.DuckDBPyConnection,
    *,
    search: str | None = None,
    include_resolved: bool = False,
    limit: int = 500,
) -> list[dict]:
    """Union open ingestion failures with per-folder sync state.

    Two distinct row kinds share one table:

    * ``failure`` — rows from ``failed_message_ingestions``. Filtered to
      unresolved unless ``include_resolved``, so the tab stays quiet when
      the pipeline is healthy.
    * ``sync`` — rows from ``ingest_sync_state``. Envelope kinds
      (``initial_envelopes``, ``nightly_envelopes``) and body kinds
      (``initial_bodies``, ``nightly_bodies``, ``repair_bodies``) are both
      listed: envelope completion alone does not mean a folder is
      body-complete.
    """
    failure_where: list[str] = []
    sync_where: list[str] = []
    failure_params: list = []
    sync_params: list = []

    if not include_resolved:
        failure_where.append("f.status <> 'resolved'")

    if search:
        failure_where.append(
            "(lower(f.folder_name) LIKE lower('%' || ? || '%')"
            " OR lower(f.failure_kind) LIKE lower('%' || ? || '%')"
            " OR lower(COALESCE(f.error, '')) LIKE lower('%' || ? || '%'))"
        )
        failure_params.extend([search, search, search])
        sync_where.append(
            "(lower(s.folder_name) LIKE lower('%' || ? || '%')"
            " OR lower(s.sync_kind) LIKE lower('%' || ? || '%'))"
        )
        sync_params.extend([search, search])

    failure_where_sql = ("WHERE " + " AND ".join(failure_where)) if failure_where else ""
    sync_where_sql = ("WHERE " + " AND ".join(sync_where)) if sync_where else ""

    sql = f"""
        SELECT * FROM (
            SELECT
                'failure' AS kind,
                f.folder_name,
                f.failure_kind AS detail,
                f.status,
                f.error,
                CAST(f.retry_count AS BIGINT) AS retry_count,
                NULL AS next_page,
                NULL AS last_completed_page,
                f.last_failed_at AS last_activity_at
            FROM failed_message_ingestions f
            {failure_where_sql}

            UNION ALL

            SELECT
                'sync' AS kind,
                s.folder_name,
                s.sync_kind AS detail,
                s.status,
                NULL AS error,
                NULL AS retry_count,
                CAST(s.next_page AS BIGINT) AS next_page,
                CAST(s.last_completed_page AS BIGINT) AS last_completed_page,
                s.last_run_at AS last_activity_at
            FROM ingest_sync_state s
            {sync_where_sql}
        )
        ORDER BY kind ASC, last_activity_at DESC NULLS LAST
        LIMIT ?
    """
    params = failure_params + sync_params + [limit]
    cursor = conn.execute(sql, params)
    return _rows_to_dicts(cursor)


def fetch_thread_detail(
    conn: duckdb.DuckDBPyConnection,
    thread_id: int,
) -> dict | None:
    """Fetch a thread summary (most recent) by thread_id."""
    sql = """
        SELECT
            ts.summary_id,
            ts.thread_id,
            ts.summary_type,
            ts.summary_text,
            ts.key_points_json,
            ts.action_items_json,
            ts.decisions_json,
            ts.deadlines_json,
            ts.participants_json,
            ts.generated_at,
            ts.source_message_count,
            t.canonical_subject AS thread_subject
        FROM thread_summaries ts
        LEFT JOIN threads t ON t.thread_id = ts.thread_id
        WHERE ts.thread_id = ?
        ORDER BY ts.generated_at DESC
        LIMIT 1
    """
    cursor = conn.execute(sql, [thread_id])
    rows = _rows_to_dicts(cursor)
    return rows[0] if rows else None


def fetch_thread_id_for_fact(
    conn: duckdb.DuckDBPyConnection,
    fact_type: str,
    fact_id: int,
) -> int | None:
    """Return the thread_id for a given fact type and id."""
    if fact_type == 'action_items':
        sql = "SELECT thread_id FROM action_items WHERE action_item_id = ?"
    elif fact_type == 'deadlines':
        sql = "SELECT thread_id FROM deadlines WHERE deadline_id = ?"
    elif fact_type == 'decisions':
        sql = "SELECT thread_id FROM decisions WHERE decision_id = ?"
    elif fact_type == 'thread_summaries':
        sql = "SELECT thread_id FROM thread_summaries WHERE summary_id = ?"
    elif fact_type == 'calendar_events':
        sql = """
            SELECT t.thread_id
            FROM calendar_events c
            LEFT JOIN messages m ON m.message_pk = c.message_pk
            LEFT JOIN threads t
                   ON t.thread_key = m.thread_key
                  AND t.account_id = m.account_id
            WHERE c.calendar_event_id = ?
        """
    else:
        return None

    cursor = conn.execute(sql, [fact_id])
    row = cursor.fetchone()
    return row[0] if row else None


def list_status_values(
    conn: duckdb.DuckDBPyConnection,
    table: str,
) -> list[str]:
    """Return distinct non-NULL status values from a table."""
    # Allowlist to avoid SQL injection
    allowed = {'action_items', 'deadlines', 'decisions', 'promotion_log'}
    if table not in allowed:
        return []
    cursor = conn.execute(f"SELECT DISTINCT status FROM {table} WHERE status IS NOT NULL ORDER BY status")
    return [row[0] for row in cursor.fetchall()]
