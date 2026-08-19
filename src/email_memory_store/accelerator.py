"""Resolve the public torch device for retrieval components."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping


TORCH_DEVICE_ENV = "EMAIL_MEMORY_STORE_TORCH_DEVICE"
ACCELERATOR_RECEIPT_SCHEMA_VERSION = 1
_ALLOWED_TORCH_DEVICES = frozenset({"cpu", "cuda", "mps"})
_ACCELERATOR_RECEIPT_FIELDS = {
    "schema_version",
    "backend",
    "device",
    "torch_version",
}
_ACCELERATOR_RECEIPT_RELATIVE_PATH = Path("share/email-memory-store/accelerator.json")


def _normalize_torch_device(value: object, *, source: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{source} must be one of: cpu, cuda, mps")

    device = value.strip()
    if device not in _ALLOWED_TORCH_DEVICES:
        raise ValueError(f"{source} must be one of: cpu, cuda, mps")
    return device


def accelerator_receipt_path(*, sys_prefix: str | Path | None = None) -> Path:
    prefix = Path(sys.prefix if sys_prefix is None else sys_prefix)
    return prefix / _ACCELERATOR_RECEIPT_RELATIVE_PATH


def _load_accelerator_receipt(*, sys_prefix: str | Path | None = None) -> str | None:
    receipt_path = accelerator_receipt_path(sys_prefix=sys_prefix)
    try:
        with receipt_path.open(encoding="utf-8") as receipt_file:
            raw: Any = json.load(receipt_file)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("accelerator receipt could not be read") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("accelerator receipt must contain valid JSON") from exc

    if not isinstance(raw, dict):
        raise ValueError("accelerator receipt must contain a JSON object")

    unknown_fields = set(raw) - _ACCELERATOR_RECEIPT_FIELDS
    if unknown_fields:
        raise ValueError(
            "accelerator receipt has unsupported field(s): "
            + ", ".join(sorted(map(str, unknown_fields)))
        )

    missing_fields = _ACCELERATOR_RECEIPT_FIELDS - set(raw)
    if missing_fields:
        raise ValueError(
            "accelerator receipt is missing required field(s): "
            + ", ".join(sorted(missing_fields))
        )

    if raw.get("schema_version") != ACCELERATOR_RECEIPT_SCHEMA_VERSION:
        raise ValueError(
            f"accelerator receipt must use schema_version {ACCELERATOR_RECEIPT_SCHEMA_VERSION}"
        )

    backend = _normalize_torch_device(raw.get("backend"), source="accelerator receipt backend")
    device = _normalize_torch_device(raw.get("device"), source="accelerator receipt device")
    torch_version = raw.get("torch_version")
    if not isinstance(torch_version, str) or not torch_version.strip():
        raise ValueError("accelerator receipt torch_version must be a non-empty string")
    if backend != device:
        raise ValueError("accelerator receipt backend and device must match")
    return device


def resolve_public_runtime_device(
    device: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    sys_prefix: str | Path | None = None,
) -> str | None:
    """Resolve the public torch device without changing absent-source behavior."""

    if device is not None:
        return _normalize_torch_device(device, source="device")

    env = os.environ if environ is None else environ
    env_device = env.get(TORCH_DEVICE_ENV)
    if env_device is not None:
        return _normalize_torch_device(env_device, source=TORCH_DEVICE_ENV)

    return _load_accelerator_receipt(sys_prefix=sys_prefix)
