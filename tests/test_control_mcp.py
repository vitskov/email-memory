from __future__ import annotations

import pytest

from email_memory_store.control import mcp_server


def test_control_mcp_exposes_exact_bounded_tools() -> None:
    tools = mcp_server.tool_definitions()
    assert [tool.name for tool in tools] == [
        "system_status",
        "job_start",
        "job_status",
    ]
    assert tools[0].inputSchema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert tools[1].inputSchema["properties"]["action"]["enum"] == [
        "maintenance",
        "retry_failed_bodies",
        "reconcile",
    ]
    assert tools[1].inputSchema["additionalProperties"] is False
    assert tools[2].inputSchema["additionalProperties"] is False
    expected_annotations = {
        "system_status": (True, False, True, False),
        "job_start": (False, True, False, False),
        "job_status": (True, False, True, False),
    }
    for tool in tools:
        assert tool.annotations is not None
        assert (
            tool.annotations.readOnlyHint,
            tool.annotations.destructiveHint,
            tool.annotations.idempotentHint,
            tool.annotations.openWorldHint,
        ) == expected_annotations[tool.name]


def test_call_tool_rejects_extra_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server.jobs, "system_status", lambda: {"status": "ready"})
    with pytest.raises(ValueError, match="invalid arguments"):
        mcp_server.call_tool("system_status", {"path": "/private"})


def test_call_tool_dispatches_only_typed_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mcp_server.jobs,
        "start_job",
        lambda action: calls.append(("start", action)) or {"accepted": True},
    )
    monkeypatch.setattr(
        mcp_server.jobs,
        "job_status",
        lambda job_id: calls.append(("status", job_id)) or {"state": "queued"},
    )

    assert mcp_server.call_tool("job_start", {"action": "maintenance"}) == {
        "accepted": True
    }
    assert mcp_server.call_tool("job_status", {"job_id": "a" * 32}) == {
        "state": "queued"
    }
    assert calls == [("start", "maintenance"), ("status", "a" * 32)]
    with pytest.raises(ValueError, match="invalid action"):
        mcp_server.call_tool("job_start", {"action": "shell"})


def test_unknown_tool_error_does_not_reflect_untrusted_name() -> None:
    with pytest.raises(ValueError, match="^unknown tool$") as error:
        mcp_server.call_tool("private/path/token", {})
    assert "private" not in str(error.value)
