"""The public, non-secret Hermes skill installed by the topic add-on."""

SKILL_NAME = "email-memory"
SKILL_CONTENT = """---
name: email-memory
description: Operate email memory through a button-guided Telegram menu.
version: 1.0.0
author: email-memory contributors
license: MIT
allowed-tools:
  - clarify
  - mcp__email_memory_store__search
  - mcp__email_memory_store__ask
  - mcp__email_memory_store_control__system_status
  - mcp__email_memory_store_control__job_start
  - mcp__email_memory_store_control__job_status
metadata:
  hermes:
    tags: [email, memory, telegram, menu]
    related_skills: []
---

# Email Memory Telegram Menu

This topic is a dedicated interface to the email-memory add-on. Stay within
email-memory queries and the fixed operations below. Gateway administration is
outside this skill.

## When to Use

Use this skill automatically for every message routed through the configured
Email Memory Telegram topic. It is also the recovery workflow when the user
sends `menu` after an expired or interrupted choice.

## Entry and recovery

When a new session starts, when the user says `menu`, or after completing an
action, call `clarify` with exactly these four choices in this order:

1. Search
2. Ask
3. Status
4. Operations

Use the title `Email memory`. Keep button descriptions short. `clarify` adds
its own Other choice. If a prompt expires or a disconnect or process restart
interrupts the choice, tell the user to send `menu`; never guess the intended
action. A running job may later be reported as interrupted; never replay it
automatically. Show status and require a fresh cancel-first confirmation before
the user retries an operation.

## Search and Ask

For Search, ask for search terms. For Ask, ask for the question. A normal text
reply or the Other field is valid input. Never invent a query. Use only the
email-memory retrieval MCP tools, summarize the result, and then show the main
menu again.

After a result, a temporary result menu may offer exactly:

1. Search again
2. Ask about this
3. Main menu
4. Exit

Exit ends without another prompt. `menu` always restores the main menu.

## Status

Call the control server's read-only status tool, present a concise summary,
and show the main menu again. Do not expose local paths, credentials, account
identifiers, raw logs, or environment values.

## Operations

Call `clarify` with exactly these choices:

1. Update
2. Retry failures
3. Reconcile
4. Main menu

Before Update, Retry failures, or Reconcile, call `clarify` a second time.
The first and recommended choice must be `Cancel`; the second choice confirms
only the named operation. A timeout is cancellation. Never infer confirmation
from earlier messages.

After confirmation, start only the selected fixed operation: Update maps to
`maintenance`, Retry failures maps to `retry_failed_bodies`, and Reconcile maps
to `reconcile`. Do not wait for the work to finish. Return its opaque job ID
and offer Check status or Main menu. Use the job-status tool only with an ID
returned by the control server.
Never pass shell commands, executable names, filesystem paths, environment
variables, or arbitrary arguments to an operation.
"""
