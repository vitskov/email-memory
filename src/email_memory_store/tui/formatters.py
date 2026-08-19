"""Formatters for TUI display.

No external deps beyond stdlib + json.
"""
from __future__ import annotations

import json
from datetime import datetime


def truncate(text: str | None, max_len: int) -> str:
    """Truncate text to max_len, appending ellipsis if truncated."""
    if text is None:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "\u2026"


def format_date(value) -> str:
    """Convert datetime/str/None to 'YYYY-MM-DD' or ''."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str):
        # Try to parse ISO datetime strings
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value[:19], fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        # Fallback: return first 10 chars if looks date-like
        if len(value) >= 10:
            return value[:10]
        return value
    return str(value)[:10]


def format_confidence(value) -> str:
    """Convert 0.0–1.0 confidence to '87%' or ''."""
    if value is None:
        return ""
    try:
        pct = float(value) * 100
        return f"{pct:.0f}%"
    except (TypeError, ValueError):
        return ""


def format_json_list(json_value, label: str) -> str:
    """Convert a JSON array to a Markdown bullet list with a label header."""
    if json_value is None:
        return ""
    try:
        if isinstance(json_value, str):
            items = json.loads(json_value)
        else:
            items = json_value
        if not isinstance(items, list) or len(items) == 0:
            return ""
        lines = [f"**{label}**"]
        for item in items:
            lines.append(f"- {item}")
        return "\n".join(lines)
    except (json.JSONDecodeError, TypeError):
        return ""


def format_detail(row_dict: dict, fact_type: str) -> str:
    """Render a fact row as readable Markdown for DetailScreen."""
    # 'all' tab rows carry a 'fact_type' field — dispatch on that instead
    if fact_type == "all":
        inner = row_dict.get("fact_type", "")
        if inner == "action":
            return _format_action_item(row_dict)
        elif inner == "deadline":
            return _format_deadline(row_dict)
        elif inner == "decision":
            return _format_decision(row_dict)
        elif inner == "summary":
            return _format_thread_summary(row_dict)
    if fact_type == "action_items":
        return _format_action_item(row_dict)
    elif fact_type == "deadlines":
        return _format_deadline(row_dict)
    elif fact_type == "decisions":
        return _format_decision(row_dict)
    elif fact_type == "thread_summaries":
        return _format_thread_summary(row_dict)
    elif fact_type == "promotions":
        return _format_promotion(row_dict)
    # Generic fallback
    lines = ["# Detail"]
    for k, v in row_dict.items():
        lines.append(f"**{k}**: {v}")
    return "\n\n".join(lines)


def _format_action_item(row: dict) -> str:
    lines = []
    thread_subject = row.get("thread_subject") or ""
    if thread_subject:
        lines.append(f"# {thread_subject}")
    lines.append("## Action Item")
    lines.append("")
    owner = row.get("owner") or "_unassigned_"
    lines.append(f"**Owner:** {owner}")
    lines.append("")
    action_text = row.get("action_text") or ""
    lines.append(action_text)
    lines.append("")
    due = format_date(row.get("due_date"))
    if due:
        lines.append(f"**Due:** {due}")
    status = row.get("status") or ""
    if status:
        lines.append(f"**Status:** {status}")
    conf = format_confidence(row.get("confidence"))
    if conf:
        lines.append(f"**Confidence:** {conf}")
    extracted = format_date(row.get("extracted_at"))
    if extracted:
        lines.append(f"**Extracted:** {extracted}")
    return "\n".join(lines)


def _format_deadline(row: dict) -> str:
    lines = []
    thread_subject = row.get("thread_subject") or ""
    if thread_subject:
        lines.append(f"# {thread_subject}")
    lines.append("## Deadline")
    lines.append("")
    label = row.get("label") or ""
    if label:
        lines.append(f"**Label:** {label}")
    due = format_date(row.get("due_date"))
    if due:
        lines.append(f"**Due:** {due}")
    project = row.get("related_project") or ""
    if project:
        lines.append(f"**Project:** {project}")
    status = row.get("status") or ""
    if status:
        lines.append(f"**Status:** {status}")
    conf = format_confidence(row.get("confidence"))
    if conf:
        lines.append(f"**Confidence:** {conf}")
    return "\n".join(lines)


def _format_decision(row: dict) -> str:
    lines = []
    thread_subject = row.get("thread_subject") or ""
    if thread_subject:
        lines.append(f"# {thread_subject}")
    title = row.get("title") or ""
    lines.append(f"## {title}" if title else "## Decision")
    lines.append("")
    decision_text = row.get("decision_text") or ""
    lines.append(decision_text)
    lines.append("")
    decided_by = format_json_list(row.get("decided_by_json"), "Decided By")
    if decided_by:
        lines.append(decided_by)
        lines.append("")
    decided_at = format_date(row.get("decided_at"))
    if decided_at:
        lines.append(f"**Date:** {decided_at}")
    status = row.get("status") or ""
    if status:
        lines.append(f"**Status:** {status}")
    conf = format_confidence(row.get("confidence"))
    if conf:
        lines.append(f"**Confidence:** {conf}")
    superseded = row.get("superseded_by")
    if superseded:
        lines.append(f"**Superseded By:** {superseded}")
    return "\n".join(lines)


def _format_thread_summary(row: dict) -> str:
    lines = []
    thread_subject = row.get("thread_subject") or ""
    if thread_subject:
        lines.append(f"# {thread_subject}")
    summary_type = row.get("summary_type") or ""
    lines.append(f"## Thread Summary ({summary_type})" if summary_type else "## Thread Summary")
    lines.append("")
    summary_text = row.get("summary_text") or ""
    lines.append(summary_text)
    lines.append("")
    key_points = format_json_list(row.get("key_points_json"), "Key Points")
    if key_points:
        lines.append(key_points)
        lines.append("")
    action_items = format_json_list(row.get("action_items_json"), "Action Items")
    if action_items:
        lines.append(action_items)
        lines.append("")
    decisions = format_json_list(row.get("decisions_json"), "Decisions")
    if decisions:
        lines.append(decisions)
        lines.append("")
    deadlines = format_json_list(row.get("deadlines_json"), "Deadlines")
    if deadlines:
        lines.append(deadlines)
        lines.append("")
    participants = format_json_list(row.get("participants_json"), "Participants")
    if participants:
        lines.append(participants)
        lines.append("")
    generated = format_date(row.get("generated_at"))
    if generated:
        lines.append(f"**Generated:** {generated}")
    msg_count = row.get("source_message_count")
    if msg_count is not None:
        lines.append(f"**Messages:** {msg_count}")
    return "\n".join(lines)


def _format_promotion(row: dict) -> str:
    lines = []
    lines.append("## Promoted Fact")
    lines.append("")
    candidate_type = row.get("candidate_type") or ""
    if candidate_type:
        lines.append(f"**Type:** {candidate_type}")
    category = row.get("promoted_category") or ""
    if category:
        lines.append(f"**Category:** {category}")
    status = row.get("status") or ""
    if status:
        lines.append(f"**Status:** {status}")
    created = format_date(row.get("created_at"))
    if created:
        lines.append(f"**Date:** {created}")
    lines.append("")
    promoted_text = row.get("promoted_text") or ""
    lines.append(promoted_text)
    tags = row.get("promoted_tags") or ""
    if tags:
        lines.append("")
        lines.append(f"**Tags:** {tags}")
    return "\n".join(lines)
