from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import re
import subprocess

from .assets import read_packaged_default_soul_text


DEFAULT_SOUL_BASENAME = 'default.md'
GEMMA_4_CONTEXT_WINDOW_TOKENS = 256_000

# character-budget default well below that token bound so prompt scaffolding,
# JSON formatting overhead, and tokenizer variance still leave ample headroom.
GEMMA_4_REASONABLE_MAX_INPUT_CHARS = 250_000
# Codex/OpenAI CLI usage can usually sustain larger prompts than the Gemma-scale
# fallback we use for the generic Hermes path, but we still leave headroom for
# system prompts, tool framing, and strict JSON output.
CODEX_REASONABLE_MAX_INPUT_CHARS = 300_000
# Claude models are strong long-context readers, but in practice we keep a
# slightly smaller planning budget than Codex to leave room for prompt policy,
# response JSON, and tokenizer variance.
CLAUDE_REASONABLE_MAX_INPUT_CHARS = 180_000
GENERIC_REASONABLE_MAX_INPUT_CHARS = 150_000


DEFAULT_MAX_INPUT_POLICY: tuple[tuple[str, str | None, int], ...] = (
    ('model', 'claude', CLAUDE_REASONABLE_MAX_INPUT_CHARS),
    ('model', 'sonnet', CLAUDE_REASONABLE_MAX_INPUT_CHARS),
    ('model', 'opus', CLAUDE_REASONABLE_MAX_INPUT_CHARS),
    ('model', 'haiku', CLAUDE_REASONABLE_MAX_INPUT_CHARS),
    ('model', 'gpt-5', CODEX_REASONABLE_MAX_INPUT_CHARS),
    ('model', 'gpt-4.1', CODEX_REASONABLE_MAX_INPUT_CHARS),
    ('model', 'gpt-4o', CODEX_REASONABLE_MAX_INPUT_CHARS),
    ('model', 'codex', CODEX_REASONABLE_MAX_INPUT_CHARS),
    ('model', 'gemma-4', GEMMA_4_REASONABLE_MAX_INPUT_CHARS),
    ('model', 'gemma 4', GEMMA_4_REASONABLE_MAX_INPUT_CHARS),
    ('provider', 'claude-code-cli', CLAUDE_REASONABLE_MAX_INPUT_CHARS),
    ('provider', 'codex-cli', CODEX_REASONABLE_MAX_INPUT_CHARS),
    ('provider', 'hermes-default', GEMMA_4_REASONABLE_MAX_INPUT_CHARS),
)


def infer_reasonable_max_input_chars(spec: 'LLMProviderSpec | None' = None) -> int:
    provider_name = (spec.name if spec else 'hermes-default').strip().lower()
    model_name = (spec.model if spec and spec.model else '').strip().lower()
    for scope, needle, budget in DEFAULT_MAX_INPUT_POLICY:
        haystack = model_name if scope == 'model' else provider_name
        if needle and needle in haystack:
            return budget
    return GENERIC_REASONABLE_MAX_INPUT_CHARS


@dataclass(frozen=True)
class LLMProviderSpec:
    name: str = 'hermes-default'
    model: str | None = None
    executable: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {'name': self.name, 'model': self.model}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> 'LLMProviderSpec':
        data = data or {}
        return cls(
            name=data.get('name', 'hermes-default'),
            model=data.get('model'),
        )

    def bind_executable(self, executable: str | Path) -> 'LLMProviderSpec':
        """Return a runtime-bound copy without making the path serializable."""
        return LLMProviderSpec(name=self.name, model=self.model, executable=str(executable))


@dataclass(frozen=True)
class BatchPlanner:
    max_candidates_per_batch: int = 20
    max_input_chars: int = GEMMA_4_REASONABLE_MAX_INPUT_CHARS
    soul_text: str = ''

    def _estimate_batch_chars(self, items: list[dict[str, Any]]) -> int:
        if not items:
            return 0
        return len(render_batch_prompt(soul_text=self.soul_text, batch={'items': items}))

    @staticmethod
    def _coerce_sort_str(value: Any) -> str:
        if not value:
            return ''
        from datetime import datetime
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    def _group_sort_key(self, item: dict[str, Any], index: int) -> tuple[str, str, int]:
        return (
            self._coerce_sort_str(item.get('batch_group_sort_at') or item.get('sort_at')),
            item.get('batch_group_key') or f'__single__:{index}',
            index,
        )

    def _item_sort_key(self, item: dict[str, Any], index: int) -> tuple[str, int]:
        return (self._coerce_sort_str(item.get('item_sort_at') or item.get('sort_at')), index)

    def _split_group(self, group: dict[str, Any]) -> list[dict[str, Any]]:
        items = list(group['items'])
        if not items:
            return []
        chunks: list[dict[str, Any]] = []
        current_items: list[dict[str, Any]] = []
        current_chars = 0
        for item in items:
            exceeds_count = len(current_items) >= self.max_candidates_per_batch
            next_chars = self._estimate_batch_chars(current_items + [item])
            exceeds_chars = bool(current_items) and next_chars > self.max_input_chars
            if exceeds_count or exceeds_chars:
                chunks.append({
                    'group_key': group['group_key'],
                    'group_sort_at': group['group_sort_at'],
                    'items': current_items,
                    'candidate_count': len(current_items),
                    'input_chars': current_chars,
                })
                current_items = []
                current_chars = 0
            current_items.append(item)
            current_chars = self._estimate_batch_chars(current_items)
        if current_items:
            chunks.append({
                'group_key': group['group_key'],
                'group_sort_at': group['group_sort_at'],
                'items': current_items,
                'candidate_count': len(current_items),
                'input_chars': current_chars,
            })
        return chunks

    def _group_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ordered = sorted(
            enumerate(items),
            key=lambda pair: (
                self._group_sort_key(pair[1], pair[0]),
                self._item_sort_key(pair[1], pair[0]),
            ),
        )
        grouped: list[dict[str, Any]] = []
        current_key: str | None = None
        current_items: list[dict[str, Any]] = []
        current_sort_at: Any = None
        for original_index, item in ordered:
            group_key = item.get('batch_group_key') or f'__single__:{original_index}'
            group_sort_at = item.get('batch_group_sort_at') or item.get('sort_at') or ''
            if current_key is not None and group_key != current_key:
                grouped.append({
                    'group_key': current_key,
                    'group_sort_at': current_sort_at,
                    'items': current_items,
                    'candidate_count': len(current_items),
                    'input_chars': self._estimate_batch_chars(current_items),
                })
                current_items = []
            current_key = group_key
            current_sort_at = group_sort_at
            current_items.append(item)
        if current_items:
            grouped.append({
                'group_key': current_key,
                'group_sort_at': current_sort_at,
                'items': current_items,
                'candidate_count': len(current_items),
                'input_chars': self._estimate_batch_chars(current_items),
            })
        return grouped

    def plan(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not items:
            return []
        batches: list[dict[str, Any]] = []
        current: list[dict[str, Any]] = []
        current_chars = 0
        for group in self._group_items(items):
            group_count = group['candidate_count']
            group_chars = group['input_chars']
            group_fits_empty = group_count <= self.max_candidates_per_batch and group_chars <= self.max_input_chars
            planned_groups = [group] if group_fits_empty else self._split_group(group)
            for planned_group in planned_groups:
                would_exceed_count = bool(current) and len(current) + planned_group['candidate_count'] > self.max_candidates_per_batch
                would_exceed_chars = bool(current) and self._estimate_batch_chars(current + planned_group['items']) > self.max_input_chars
                if would_exceed_count or would_exceed_chars:
                    batches.append({
                        'items': current,
                        'candidate_count': len(current),
                        'input_chars': current_chars,
                    })
                    current = []
                    current_chars = 0
                current.extend(planned_group['items'])
                current_chars = self._estimate_batch_chars(current)
        if current:
            batches.append({
                'items': current,
                'candidate_count': len(current),
                'input_chars': current_chars,
            })
        return batches

    def to_dict(self) -> dict[str, Any]:
        return {
            'max_candidates_per_batch': self.max_candidates_per_batch,
            'max_input_chars': self.max_input_chars,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> 'BatchPlanner':
        data = data or {}
        return cls(
            max_candidates_per_batch=int(data.get('max_candidates_per_batch', 20)),
            max_input_chars=int(data['max_input_chars']) if data.get('max_input_chars') is not None else GEMMA_4_REASONABLE_MAX_INPUT_CHARS,
        )


@dataclass(frozen=True)
class SoulSpec:
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {'path': self.path}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> 'SoulSpec':
        data = data or {}
        return cls(path=data.get('path'))


@dataclass(frozen=True)
class RulebookSpec:
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {'path': self.path}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> 'RulebookSpec':
        data = data or {}
        return cls(path=data.get('path'))


@dataclass(frozen=True)
class PromotionLLMConfig:
    provider: LLMProviderSpec
    batching: BatchPlanner
    soul: SoulSpec
    rulebook: RulebookSpec

    @classmethod
    def default(
        cls,
        *,
        default_soul_path: str | None = None,
        default_rulebook_path: str | None = None,
    ) -> 'PromotionLLMConfig':
        return cls(
            provider=LLMProviderSpec(),
            batching=BatchPlanner(),
            soul=SoulSpec(path=default_soul_path),
            rulebook=RulebookSpec(path=default_rulebook_path),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            'provider': self.provider.to_dict(),
            'batching': self.batching.to_dict(),
            'soul': self.soul.to_dict(),
            'rulebook': self.rulebook.to_dict(),
        }

    def bind_provider_executable(self, executable: str | Path) -> 'PromotionLLMConfig':
        """Bind a trusted runtime executable while preserving persisted config shape."""
        return PromotionLLMConfig(
            provider=self.provider.bind_executable(executable),
            batching=self.batching,
            soul=self.soul,
            rulebook=self.rulebook,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> 'PromotionLLMConfig':
        data = data or {}
        provider = LLMProviderSpec.from_dict(data.get('provider'))
        batching_data = dict(data.get('batching') or {})
        if batching_data.get('max_input_chars') is None:
            batching_data['max_input_chars'] = infer_reasonable_max_input_chars(provider)
        return cls(
            provider=provider,
            batching=BatchPlanner.from_dict(batching_data),
            soul=SoulSpec.from_dict(data.get('soul')),
            rulebook=RulebookSpec.from_dict(data.get('rulebook')),
        )


class BaseLLMProvider:
    requires_explicit_model = False

    def __init__(self, spec: LLMProviderSpec):
        self.spec = spec
        self.validate()

    def validate(self) -> None:
        if self.requires_explicit_model and not self.spec.model:
            raise ValueError(f'Provider {self.spec.name} requires an explicit model')

    def build_command(self, prompt: str) -> list[str]:
        raise NotImplementedError

    def _executable(self) -> str:
        if not self.spec.executable:
            raise ValueError(f'Provider {self.spec.name} executable is not configured')
        return self.spec.executable

    def _extract_json_payload(self, text: str) -> dict[str, Any]:
        text = text.strip()
        candidates = []
        if text:
            candidates.append(text)
        for match in re.finditer(r'\{', text):
            candidates.append(text[match.start():])
        for candidate in candidates:
            decoder = json.JSONDecoder()
            try:
                payload, _ = decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and 'results' in payload:
                return payload
        raise ValueError('Provider output must contain strict JSON with top-level key results')

    def evaluate_prompt(self, prompt: str) -> dict[str, Any]:
        completed = subprocess.run(self.build_command(prompt), text=True, capture_output=True, check=True)
        return self._extract_json_payload(completed.stdout)


class HermesDefaultProvider(BaseLLMProvider):
    def build_command(self, prompt: str) -> list[str]:
        command = [self._executable(), 'chat', '-Q']
        if self.spec.model:
            command.extend(['-m', self.spec.model])
        command.extend(['-q', prompt])
        return command


class CodexCLIProvider(BaseLLMProvider):
    requires_explicit_model = True

    def build_command(self, prompt: str) -> list[str]:
        return [self._executable(), 'exec', '--model', self.spec.model or '', prompt]


class ClaudeCodeCLIProvider(BaseLLMProvider):
    requires_explicit_model = True

    def build_command(self, prompt: str) -> list[str]:
        return [self._executable(), '--model', self.spec.model or '', prompt]


def create_provider(spec: LLMProviderSpec) -> BaseLLMProvider:
    provider_map = {
        'hermes-default': HermesDefaultProvider,
        'codex-cli': CodexCLIProvider,
        'claude-code-cli': ClaudeCodeCLIProvider,
    }
    if spec.name not in provider_map:
        raise ValueError(f'Unsupported provider: {spec.name}')
    return provider_map[spec.name](spec)


def load_soul_text(path: str | Path | None, *, default_soul_path: str | Path | None = None) -> str:
    soul_path = Path(path).expanduser() if path else (Path(default_soul_path).expanduser() if default_soul_path else None)
    if soul_path is None:
        return read_packaged_default_soul_text()
    if not soul_path.exists():
        raise FileNotFoundError(str(soul_path))
    return soul_path.read_text(encoding='utf-8').strip()



def render_batch_prompt(*, soul_text: str, batch: dict[str, Any]) -> str:
    candidates = []
    for index, item in enumerate(batch.get('items', []), start=1):
        candidates.append(
            {
                'index': index,
                'source_object_type': item.get('source_object_type'),
                'source_object_id': item.get('source_object_id'),
                'promoted_text': item.get('promoted_text'),
                'promoted_category': item.get('promoted_category'),
                'promoted_tags': item.get('promoted_tags'),
            }
        )
    return (
        f"{soul_text}\n\n"
        "You must evaluate each candidate and decide whether to promote, reject, demote, or edit it.\n"
        "Return strict JSON with top-level key 'results'. Each result must include: source_object_id, action, memory_text, rationale.\n\n"
        f"Candidates:\n{json.dumps(candidates, indent=2, sort_keys=True)}"
    )


def normalize_promotion_llm_config(
    data: dict[str, Any] | None,
    *,
    default_soul_path: str | None = None,
    default_rulebook_path: str | None = None,
) -> dict[str, Any]:
    config = PromotionLLMConfig.from_dict(data)
    create_provider(config.provider)
    return PromotionLLMConfig(
        provider=config.provider,
        batching=config.batching,
        soul=SoulSpec(path=config.soul.path or default_soul_path),
        rulebook=RulebookSpec(path=config.rulebook.path or default_rulebook_path),
    ).to_dict()


def promotion_llm_config_from_json(
    text: str | None,
    *,
    default_soul_path: str | None = None,
    default_rulebook_path: str | None = None,
) -> dict[str, Any]:
    if not text:
        return PromotionLLMConfig.default(
            default_soul_path=default_soul_path,
            default_rulebook_path=default_rulebook_path,
        ).to_dict()
    return normalize_promotion_llm_config(
        json.loads(text),
        default_soul_path=default_soul_path,
        default_rulebook_path=default_rulebook_path,
    )
