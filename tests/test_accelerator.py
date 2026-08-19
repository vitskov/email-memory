from __future__ import annotations

import json

import pytest

import email_memory_store.accelerator as accelerator
import email_memory_store.retrieval.embedder as embedder


def _write_receipt(
    tmp_path,
    payload: dict[str, object],
    *,
    prefix_name: str = "prefix",
):
    prefix = tmp_path / prefix_name
    receipt = prefix / "share" / "email-memory-store" / "accelerator.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    return prefix, receipt


def test_resolve_public_runtime_device_prefers_explicit_device_over_env_and_receipt(tmp_path):
    prefix, _ = _write_receipt(
        tmp_path,
        {
            "schema_version": 1,
            "backend": "cpu",
            "device": "cpu",
            "torch_version": "2.9.0",
        },
    )

    resolved = accelerator.resolve_public_runtime_device(
        "cuda",
        environ={accelerator.TORCH_DEVICE_ENV: "mps"},
        sys_prefix=prefix,
    )

    assert resolved == "cuda"


def test_resolve_public_runtime_device_prefers_env_over_receipt(tmp_path):
    prefix, _ = _write_receipt(
        tmp_path,
        {
            "schema_version": 1,
            "backend": "cpu",
            "device": "cpu",
            "torch_version": "2.9.0",
        },
    )

    resolved = accelerator.resolve_public_runtime_device(
        environ={accelerator.TORCH_DEVICE_ENV: "mps"},
        sys_prefix=prefix,
    )

    assert resolved == "mps"


def test_resolve_public_runtime_device_uses_receipt_when_present(tmp_path):
    prefix, _ = _write_receipt(
        tmp_path,
        {
            "schema_version": 1,
            "backend": "cuda",
            "device": "cuda",
            "torch_version": "2.9.0",
        },
    )

    assert accelerator.resolve_public_runtime_device(sys_prefix=prefix) == "cuda"


def test_resolve_public_runtime_device_returns_none_when_no_source_exists(tmp_path):
    assert accelerator.resolve_public_runtime_device(sys_prefix=tmp_path / "missing") is None


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"device": "tpu"}, "must be one of: cpu, cuda, mps"),
        (
            {"environ": {accelerator.TORCH_DEVICE_ENV: "tpu"}},
            "must be one of: cpu, cuda, mps",
        ),
    ],
)
def test_resolve_public_runtime_device_rejects_invalid_explicit_and_env_values(
    kwargs,
    message,
):
    with pytest.raises(ValueError, match=message):
        accelerator.resolve_public_runtime_device(**kwargs)


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "backend": "cpu", "device": "cpu"},
        {
            "schema_version": 2,
            "backend": "cpu",
            "device": "cpu",
            "torch_version": "2.9.0",
        },
        {
            "schema_version": 1,
            "backend": "cpu",
            "device": "cuda",
            "torch_version": "2.9.0",
        },
        {
            "schema_version": 1,
            "backend": "cpu",
            "device": "cpu",
            "torch_version": "2.9.0",
            "unexpected": True,
        },
    ],
)
def test_resolve_public_runtime_device_rejects_invalid_receipts_without_path_leaks(
    tmp_path,
    payload,
):
    prefix, receipt = _write_receipt(tmp_path, payload)

    with pytest.raises(ValueError) as excinfo:
        accelerator.resolve_public_runtime_device(sys_prefix=prefix)

    assert str(receipt) not in str(excinfo.value)


def test_resolve_public_runtime_device_rejects_malformed_receipt_json_without_path_leaks(
    tmp_path,
):
    prefix = tmp_path / "prefix"
    receipt = prefix / "share" / "email-memory-store" / "accelerator.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="valid JSON") as excinfo:
        accelerator.resolve_public_runtime_device(sys_prefix=prefix)

    assert str(receipt) not in str(excinfo.value)


def test_embedder_resolves_public_runtime_device_before_constructing_model(monkeypatch):
    captured: dict[str, object] = {}

    class FakeSentenceTransformer:
        def __init__(self, model_name: str, device: str | None = None) -> None:
            captured["model_name"] = model_name
            captured["device"] = device

    monkeypatch.setenv(accelerator.TORCH_DEVICE_ENV, "mps")
    monkeypatch.setattr(embedder, "SentenceTransformer", FakeSentenceTransformer)

    embedder.Embedder()

    assert captured["model_name"] == embedder.DEFAULT_MODEL
    assert captured["device"] == "mps"
