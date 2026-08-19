from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from .entity_schema import ENTITY_SCHEMA_SQL


def normalize_person_name(value: str | None) -> str:
    text = ' '.join((value or '').strip().split())
    return text.lower()


def organization_hint_from_email(email_address: str | None) -> str | None:
    if not email_address or '@' not in email_address:
        return None
    return email_address.split('@', 1)[1].lower() or None


class EntityMemoryStore:
    def __init__(self, path: str | Path, read_only: bool = False):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(self.path), read_only=read_only)

    def initialize(self) -> None:
        self.conn.execute("SET memory_limit='2GB'")
        self.conn.execute("SET threads TO 2")
        self.conn.execute(ENTITY_SCHEMA_SQL)

    def close(self) -> None:
        self.conn.close()

    def ensure_person(self, canonical_name: str, organization_hint: str | None = None) -> tuple[int, str]:
        normalized_name = normalize_person_name(canonical_name)
        row = self.conn.execute(
            "SELECT person_id, canonical_name FROM people WHERE normalized_name = ? AND COALESCE(organization_hint, '') = COALESCE(?, '')",
            [normalized_name, organization_hint],
        ).fetchone()
        if row:
            return int(row[0]), row[1]
        status = 'ambiguous' if self._has_name_collision(normalized_name, organization_hint) else 'resolved'
        inserted = self.conn.execute(
            """
            INSERT INTO people(canonical_name, normalized_name, organization_hint, disambiguation_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            RETURNING person_id
            """,
            [canonical_name, normalized_name, organization_hint, status],
        ).fetchone()
        if status == 'ambiguous':
            self.conn.execute(
                "UPDATE people SET disambiguation_status = 'ambiguous', updated_at = CURRENT_TIMESTAMP WHERE normalized_name = ?",
                [normalized_name],
            )
        return int(inserted[0]), canonical_name

    def ensure_person_alias(self, person_id: int, alias_name: str | None) -> None:
        if not alias_name:
            return
        normalized_alias = normalize_person_name(alias_name)
        if not normalized_alias:
            return
        row = self.conn.execute(
            "SELECT person_alias_id FROM person_aliases WHERE person_id = ? AND normalized_alias = ?",
            [person_id, normalized_alias],
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE person_aliases SET alias_name = ?, updated_at = CURRENT_TIMESTAMP WHERE person_alias_id = ?",
                [alias_name, row[0]],
            )
            return
        self.conn.execute(
            "INSERT INTO person_aliases(person_id, alias_name, normalized_alias, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            [person_id, alias_name, normalized_alias],
        )

    def ensure_person_email(self, person_id: int, email_address: str | None) -> None:
        if not email_address:
            return
        normalized_email = email_address.lower()
        existing = self.conn.execute(
            "SELECT person_id FROM person_emails WHERE email_address = ?",
            [normalized_email],
        ).fetchone()
        if existing and int(existing[0]) == person_id:
            return
        if existing and int(existing[0]) != person_id:
            return
        self.conn.execute(
            "INSERT INTO person_emails(person_id, email_address, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            [person_id, normalized_email],
        )

    def replace_message_entities(self, *, email_message_pk: int, stable_message_id: str, people: list[dict[str, Any]]) -> None:
        desired_rows: dict[tuple[Any, ...], dict[str, Any]] = {}
        for person in people:
            dedup_key = (
                person['person_id'],
                stable_message_id,
                person['role'],
                person.get('email_address'),
            )
            desired_rows[dedup_key] = person

        existing_rows = self.conn.execute(
            """
            SELECT email_message_pk, person_id, stable_message_id, role, email_address, canonical_name, normalized_name
            FROM message_entity_index
            WHERE stable_message_id = ?
            """,
            [stable_message_id],
        ).fetchall()
        existing_by_key = {
            (row[1], row[2], row[3], row[4]): {
                'email_message_pk': row[0],
                'canonical_name': row[5],
                'normalized_name': row[6],
            }
            for row in existing_rows
        }

        self.conn.begin()
        try:
            for key, person in desired_rows.items():
                existing = existing_by_key.get(key)
                if existing is None:
                    self.conn.execute(
                        """
                        INSERT INTO message_entity_index(
                            person_id, email_message_pk, stable_message_id, canonical_name,
                            normalized_name, role, email_address, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        [
                            person['person_id'],
                            email_message_pk,
                            stable_message_id,
                            person['canonical_name'],
                            person['normalized_name'],
                            person['role'],
                            person.get('email_address'),
                        ],
                    )
                    continue
                if (
                    existing['email_message_pk'] != email_message_pk
                    or existing['canonical_name'] != person['canonical_name']
                    or existing['normalized_name'] != person['normalized_name']
                ):
                    self.conn.execute(
                        """
                        UPDATE message_entity_index
                        SET email_message_pk = ?,
                            canonical_name = ?,
                            normalized_name = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE stable_message_id = ?
                          AND person_id = ?
                          AND role = ?
                          AND email_address IS NOT DISTINCT FROM ?
                        """,
                        [
                            email_message_pk,
                            person['canonical_name'],
                            person['normalized_name'],
                            stable_message_id,
                            person['person_id'],
                            person['role'],
                            person.get('email_address'),
                        ],
                    )

            for key in existing_by_key:
                if key in desired_rows:
                    continue
                person_id, existing_stable_message_id, role, email_address = key
                self.conn.execute(
                    """
                    DELETE FROM message_entity_index
                    WHERE stable_message_id = ?
                      AND person_id = ?
                      AND role = ?
                      AND email_address IS NOT DISTINCT FROM ?
                    """,
                    [existing_stable_message_id, person_id, role, email_address],
                )

            person_ids = sorted({item['person_id'] for item in desired_rows.values()})
            if person_ids:
                placeholders = ','.join(['?'] * len(person_ids))
                self.conn.execute(
                    f"""
                    UPDATE people
                    SET email_count = (SELECT COUNT(*) FROM person_emails WHERE person_emails.person_id = people.person_id),
                        message_count = (SELECT COUNT(*) FROM message_entity_index WHERE message_entity_index.person_id = people.person_id),
                        first_seen_at = COALESCE(first_seen_at, CURRENT_TIMESTAMP),
                        last_seen_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE person_id IN ({placeholders})
                    """,
                    person_ids,
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def update_message_identity(self, *, email_message_pk: int, stable_message_id: str) -> None:
        self.conn.execute(
            "UPDATE message_entity_index SET stable_message_id = ?, updated_at = CURRENT_TIMESTAMP WHERE email_message_pk = ?",
            [stable_message_id, email_message_pk],
        )

        person_ids = [row[0] for row in self.conn.execute(
            "SELECT DISTINCT person_id FROM message_entity_index WHERE email_message_pk = ?",
            [email_message_pk],
        ).fetchall()]
        if person_ids:
            placeholders = ','.join(['?'] * len(person_ids))
            self.conn.execute(
                f"""
                UPDATE people
                SET email_count = (SELECT COUNT(*) FROM person_emails WHERE person_emails.person_id = people.person_id),
                    message_count = (SELECT COUNT(*) FROM message_entity_index WHERE message_entity_index.person_id = people.person_id),
                    first_seen_at = COALESCE(first_seen_at, CURRENT_TIMESTAMP),
                    last_seen_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE person_id IN ({placeholders})
                """,
                person_ids,
            )

    def search_people(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        like = f"%{query.lower()}%"
        rows = self.conn.execute(
            """
            SELECT p.person_id, p.canonical_name, p.normalized_name, p.organization_hint,
                   p.disambiguation_status, p.email_count, p.message_count
            FROM people p
            WHERE lower(p.canonical_name) LIKE ?
               OR EXISTS (SELECT 1 FROM person_aliases a WHERE a.person_id = p.person_id AND lower(a.alias_name) LIKE ?)
               OR EXISTS (SELECT 1 FROM person_emails e WHERE e.person_id = p.person_id AND lower(e.email_address) LIKE ?)
            ORDER BY p.message_count DESC, p.person_id ASC
            LIMIT ?
            """,
            [like, like, like, limit],
        ).fetchall()
        results = []
        for row in rows:
            emails = [item[0] for item in self.conn.execute(
                "SELECT email_address FROM person_emails WHERE person_id = ? ORDER BY email_address",
                [row[0]],
            ).fetchall()]
            results.append(
                {
                    'person_id': int(row[0]),
                    'canonical_name': row[1],
                    'normalized_name': row[2],
                    'organization_hint': row[3],
                    'disambiguation_status': row[4],
                    'email_count': int(row[5]),
                    'message_count': int(row[6]),
                    'emails': emails,
                }
            )
        return results

    def select_people_for_promotion(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT person_id, canonical_name, organization_hint, disambiguation_status, message_count FROM people WHERE message_count > 0 ORDER BY message_count DESC, person_id ASC LIMIT ?",
            [limit],
        ).fetchall()
        results = []
        for person_id, canonical_name, organization_hint, disambiguation_status, message_count in rows:
            emails = [item[0] for item in self.conn.execute(
                "SELECT email_address FROM person_emails WHERE person_id = ? ORDER BY email_address",
                [person_id],
            ).fetchall()]
            content = f"Email contact {canonical_name} uses {', '.join(emails)} and appears in {message_count} indexed messages."
            if organization_hint:
                content += f" Organization hint: {organization_hint}."
            if disambiguation_status != 'resolved':
                content += f" Identity status: {disambiguation_status}."
            results.append(
                {
                    'source_object_type': 'person',
                    'source_object_id': str(person_id),
                    'promoted_text': content,
                    'promoted_category': 'general',
                    'promoted_tags': 'email,person,entity',
                    'fact_store_payload': {
                        'content': content,
                        'category': 'general',
                        'tags': 'email,person,entity',
                    },
                }
            )
        return results

    def merge_people(self, *, primary_person_id: int, secondary_person_id: int, reason: str) -> int:
        self.conn.execute(
            """
            DELETE FROM person_emails AS secondary
            USING person_emails AS primary_email
            WHERE secondary.person_id = ?
              AND primary_email.person_id = ?
              AND secondary.email_address = primary_email.email_address
            """,
            [secondary_person_id, primary_person_id],
        )
        self.conn.execute(
            """
            DELETE FROM person_aliases AS secondary
            USING person_aliases AS primary_alias
            WHERE secondary.person_id = ?
              AND primary_alias.person_id = ?
              AND secondary.normalized_alias = primary_alias.normalized_alias
            """,
            [secondary_person_id, primary_person_id],
        )
        self.conn.execute(
            """
            DELETE FROM message_entity_index AS secondary
            USING message_entity_index AS primary_idx
            WHERE secondary.person_id = ?
              AND primary_idx.person_id = ?
              AND secondary.stable_message_id = primary_idx.stable_message_id
              AND secondary.role = primary_idx.role
              AND COALESCE(secondary.email_address, '') = COALESCE(primary_idx.email_address, '')
            """,
            [secondary_person_id, primary_person_id],
        )
        self.conn.execute("UPDATE person_emails SET person_id = ? WHERE person_id = ?", [primary_person_id, secondary_person_id])
        self.conn.execute("UPDATE person_aliases SET person_id = ? WHERE person_id = ?", [primary_person_id, secondary_person_id])
        self.conn.execute("UPDATE message_entity_index SET person_id = ? WHERE person_id = ?", [primary_person_id, secondary_person_id])
        self.conn.execute(
            "UPDATE people SET disambiguation_status = 'merged', updated_at = CURRENT_TIMESTAMP WHERE person_id = ?",
            [primary_person_id],
        )
        self.conn.execute("DELETE FROM people WHERE person_id = ?", [secondary_person_id])
        self.conn.execute(
            "INSERT INTO entity_resolution_log(action, primary_person_id, secondary_person_id, reason) VALUES ('merge', ?, ?, ?)",
            [primary_person_id, secondary_person_id, reason],
        )
        self._refresh_people_counts([primary_person_id])
        return primary_person_id

    def split_person(self, *, source_person_id: int, new_canonical_name: str, email_addresses: list[str], reason: str) -> int:
        organization_hint = organization_hint_from_email(email_addresses[0]) if email_addresses else None
        inserted = self.conn.execute(
            """
            INSERT INTO people(canonical_name, normalized_name, organization_hint, disambiguation_status, created_at, updated_at)
            VALUES (?, ?, ?, 'split', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            RETURNING person_id
            """,
            [new_canonical_name, normalize_person_name(new_canonical_name), organization_hint],
        ).fetchone()
        new_person_id = int(inserted[0])
        for email_address in email_addresses:
            normalized_email = email_address.lower()
            self.conn.execute("UPDATE person_emails SET person_id = ? WHERE email_address = ?", [new_person_id, normalized_email])
            self.conn.execute("UPDATE message_entity_index SET person_id = ? WHERE email_address = ?", [new_person_id, normalized_email])
        self.conn.execute(
            "INSERT INTO entity_resolution_log(action, primary_person_id, new_person_id, reason) VALUES ('split', ?, ?, ?)",
            [source_person_id, new_person_id, reason],
        )
        self._refresh_people_counts([source_person_id, new_person_id])
        return new_person_id

    def _refresh_people_counts(self, person_ids: list[int]) -> None:
        if not person_ids:
            return
        placeholders = ','.join(['?'] * len(person_ids))
        self.conn.execute(
            f"""
            UPDATE people
            SET email_count = (SELECT COUNT(*) FROM person_emails WHERE person_emails.person_id = people.person_id),
                message_count = (SELECT COUNT(*) FROM message_entity_index WHERE message_entity_index.person_id = people.person_id),
                updated_at = CURRENT_TIMESTAMP
            WHERE person_id IN ({placeholders})
            """,
            person_ids,
        )

    def _has_name_collision(self, normalized_name: str, organization_hint: str | None) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM people WHERE normalized_name = ? AND COALESCE(organization_hint, '') <> COALESCE(?, '')",
            [normalized_name, organization_hint],
        ).fetchone()
        return bool(row and row[0] > 0)
