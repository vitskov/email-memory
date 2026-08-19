from pathlib import Path

import pytest

from email_memory_store.cli import build_parser
from email_memory_store.promotion.assets import read_packaged_default_soul_text, read_packaged_rulebook_text, seed_runtime_promotion_assets
from email_memory_store.promotion.llm import (
    BatchPlanner,
    CLAUDE_REASONABLE_MAX_INPUT_CHARS,
    CODEX_REASONABLE_MAX_INPUT_CHARS,
    GEMMA_4_REASONABLE_MAX_INPUT_CHARS,
    LLMProviderSpec,
    PromotionLLMConfig,
    create_provider,
    load_soul_text,
    render_batch_prompt,
)


def test_promotion_provider_spec_round_trips():
    spec = LLMProviderSpec(name='hermes-default', model=None)
    restored = LLMProviderSpec.from_dict(spec.to_dict())
    assert restored == spec


def test_hermes_default_provider_allows_missing_model():
    provider = create_provider(LLMProviderSpec(name='hermes-default', model=None))
    assert provider.spec.name == 'hermes-default'
    assert provider.spec.model is None


def test_hermes_default_provider_builds_noninteractive_quiet_command():
    provider = create_provider(LLMProviderSpec(name='hermes-default', model=None))
    command = provider.build_command('PROMPT')
    assert command == ['hermes', 'chat', '-Q', '-q', 'PROMPT']


def test_hermes_default_provider_includes_explicit_model_when_present():
    provider = create_provider(LLMProviderSpec(name='hermes-default', model='nous/hermes-test'))
    command = provider.build_command('PROMPT')
    assert command == ['hermes', 'chat', '-Q', '-m', 'nous/hermes-test', '-q', 'PROMPT']


def test_codex_cli_provider_requires_explicit_model():
    with pytest.raises(ValueError, match='model'):
        create_provider(LLMProviderSpec(name='codex-cli', model=None))


def test_claude_code_provider_requires_explicit_model():
    with pytest.raises(ValueError, match='model'):
        create_provider(LLMProviderSpec(name='claude-code-cli', model=None))


def test_packaged_defaults_are_available_for_runtime_seeding():
    text = read_packaged_default_soul_text()
    assert 'durable' in text.lower()
    assert 'reject' in text.lower()
    rulebook = read_packaged_rulebook_text()
    assert 'promotion rules' in rulebook.lower()
    assert 'demotion rules' in rulebook.lower()


def test_default_soul_text_loads_from_runtime_seeded_file(tmp_path: Path):
    seeded = seed_runtime_promotion_assets(runtime_root=tmp_path / 'config' / 'promotion')
    soul = Path(str(seeded['soul_path']))
    soul.write_text('runtime seeded soul', encoding='utf-8')
    text = load_soul_text(None, default_soul_path=soul)
    assert text == 'runtime seeded soul'


def test_seed_runtime_promotion_assets_copies_future_templates_too(tmp_path: Path):
    seeded = seed_runtime_promotion_assets(runtime_root=tmp_path / 'config' / 'promotion')
    seeded_paths = seeded['seeded_paths']
    assert str(tmp_path / 'config' / 'promotion' / 'souls' / 'default.md') in seeded_paths
    assert str(tmp_path / 'config' / 'promotion' / 'rulebooks' / 'MEMORY_PROMOTION_RULEBOOK.md') in seeded_paths
    assert str(tmp_path / 'config' / 'promotion' / 'templates' / 'batch_review_prompt.md') in seeded_paths


def test_custom_soul_file_overrides_default(tmp_path: Path):
    soul = tmp_path / 'custom-soul.md'
    soul.write_text('custom soul rules', encoding='utf-8')
    assert load_soul_text(soul) == 'custom soul rules'


def test_missing_custom_soul_file_raises_clear_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_soul_text(tmp_path / 'missing.md')


def test_batch_planner_respects_max_candidates_per_batch():
    planner = BatchPlanner(max_candidates_per_batch=2, max_input_chars=1000)
    items = [{'promoted_text': 'a'}, {'promoted_text': 'b'}, {'promoted_text': 'c'}]
    batches = planner.plan(items)
    assert [len(batch['items']) for batch in batches] == [2, 1]


def test_batch_planner_default_max_input_chars_uses_hermes_default_provider_policy():
    planner = BatchPlanner()
    assert planner.max_input_chars == GEMMA_4_REASONABLE_MAX_INPUT_CHARS
    assert planner.max_input_chars >= 100000


def test_promotion_llm_config_uses_provider_aware_batch_defaults_when_unset():
    codex_config = PromotionLLMConfig.from_dict({
        'provider': {'name': 'codex-cli', 'model': 'gpt-5-codex'},
        'batching': {'max_candidates_per_batch': 3},
    })
    claude_config = PromotionLLMConfig.from_dict({
        'provider': {'name': 'claude-code-cli', 'model': 'claude-3-7-sonnet'},
        'batching': {'max_candidates_per_batch': 3},
    })
    hermes_config = PromotionLLMConfig.from_dict({
        'provider': {'name': 'hermes-default', 'model': 'gemma-4'},
        'batching': {'max_candidates_per_batch': 3},
    })

    assert codex_config.batching.max_input_chars == CODEX_REASONABLE_MAX_INPUT_CHARS
    assert claude_config.batching.max_input_chars == CLAUDE_REASONABLE_MAX_INPUT_CHARS
    assert hermes_config.batching.max_input_chars == GEMMA_4_REASONABLE_MAX_INPUT_CHARS


def test_model_name_heuristics_win_over_provider_defaults_when_more_specific():
    config = PromotionLLMConfig.from_dict({
        'provider': {'name': 'hermes-default', 'model': 'claude-3-7-sonnet'},
        'batching': {},
    })

    assert config.batching.max_input_chars == CLAUDE_REASONABLE_MAX_INPUT_CHARS


def test_explicit_max_input_chars_override_is_preserved():
    config = PromotionLLMConfig.from_dict({
        'provider': {'name': 'codex-cli', 'model': 'gpt-5-codex'},
        'batching': {'max_candidates_per_batch': 3, 'max_input_chars': 12345},
    })

    assert config.batching.max_input_chars == 12345


def test_cli_leaves_max_input_chars_unset_until_config_normalization():
    parser = build_parser()
    args = parser.parse_args([
        'set-promotion-llm-config',
        '--provider', 'codex-cli',
        '--model', 'gpt-5-codex',
    ])

    assert args.max_input_chars is None


def test_batch_planner_respects_max_input_chars_based_on_rendered_prompt_size():
    items = [
        {'source_object_type': 'decision', 'source_object_id': '1', 'promoted_text': 'x'},
        {'source_object_type': 'decision', 'source_object_id': '2', 'promoted_text': 'y'},
    ]
    max_input_chars = len(render_batch_prompt(soul_text='Character budget soul', batch={'items': [items[0]]}))
    planner = BatchPlanner(max_candidates_per_batch=10, max_input_chars=max_input_chars, soul_text='Character budget soul')

    batches = planner.plan(items)

    assert [len(batch['items']) for batch in batches] == [1, 1]
    assert [batch['input_chars'] for batch in batches] == [
        len(render_batch_prompt(soul_text='Character budget soul', batch={'items': [items[0]]})),
        len(render_batch_prompt(soul_text='Character budget soul', batch={'items': [items[1]]})),
    ]


def test_batch_planner_counts_rendered_prompt_overhead_not_only_candidate_text():
    item = {'source_object_type': 'decision', 'source_object_id': '1', 'promoted_text': 'tiny'}
    prompt_chars = len(render_batch_prompt(soul_text='Overhead soul', batch={'items': [item]}))
    planner = BatchPlanner(max_candidates_per_batch=10, max_input_chars=prompt_chars - 1, soul_text='Overhead soul')

    batches = planner.plan([item])

    assert len(batches) == 1
    assert batches[0]['candidate_count'] == 1
    assert batches[0]['input_chars'] == prompt_chars
    assert batches[0]['input_chars'] > len(item['promoted_text'])


def test_batch_planner_preserves_order_and_empty_input():
    planner = BatchPlanner(max_candidates_per_batch=2, max_input_chars=1000)
    assert planner.plan([]) == []
    items = [{'promoted_text': 'first'}, {'promoted_text': 'second'}, {'promoted_text': 'third'}]
    batches = planner.plan(items)
    assert [item['promoted_text'] for batch in batches for item in batch['items']] == ['first', 'second', 'third']


def test_promotion_llm_config_round_trip():
    config = PromotionLLMConfig.from_dict({
        'provider': {'name': 'codex-cli', 'model': 'gpt-5-codex'},
        'batching': {'max_candidates_per_batch': 3, 'max_input_chars': 1234},
        'soul': {'path': '/tmp/soul.md'},
        'rulebook': {'path': '/tmp/rulebook.md'},
    })
    assert config.to_dict()['provider']['model'] == 'gpt-5-codex'
    assert config.to_dict()['batching']['max_input_chars'] == 1234
    assert config.to_dict()['rulebook']['path'] == '/tmp/rulebook.md'


def test_render_batch_prompt_includes_rulebook_concepts_and_candidates():
    prompt = render_batch_prompt(
        soul_text='Prefer durable memories. Use demotion or editing when needed.',
        batch={
            'items': [
                {'source_object_type': 'decision', 'source_object_id': '1', 'promoted_text': 'Parking changed.'},
                {'source_object_type': 'person', 'source_object_id': '2', 'promoted_text': 'Adrienne matters.'},
            ]
        },
    )
    assert 'Prefer durable memories' in prompt
    assert 'promote, reject, demote, or edit' in prompt
    assert 'Parking changed.' in prompt
    assert 'Adrienne matters.' in prompt


def test_codex_cli_provider_builds_expected_command():
    provider = create_provider(LLMProviderSpec(name='codex-cli', model='gpt-5-codex'))
    command = provider.build_command('PROMPT')
    assert command[:4] == ['codex', 'exec', '--model', 'gpt-5-codex']
    assert command[-1] == 'PROMPT'


def test_claude_code_provider_builds_expected_command():
    provider = create_provider(LLMProviderSpec(name='claude-code-cli', model='sonnet'))
    command = provider.build_command('PROMPT')
    assert command[:3] == ['claude', '--model', 'sonnet']
    assert command[-1] == 'PROMPT'


def test_codex_cli_provider_executes_and_parses_json(monkeypatch):
    provider = create_provider(LLMProviderSpec(name='codex-cli', model='gpt-5-codex'))

    class Result:
        stdout = '{"results":[{"source_object_id":"1","action":"promote","memory_text":"Durable memory","rationale":"Important"}]}'

    def fake_run(command, text, capture_output, check):
        assert command[:2] == ['codex', 'exec']
        return Result()

    monkeypatch.setattr('subprocess.run', fake_run)
    result = provider.evaluate_prompt('PROMPT')
    assert result['results'][0]['action'] == 'promote'
    assert result['results'][0]['memory_text'] == 'Durable memory'


def test_hermes_default_provider_executes_and_extracts_json_from_hermes_output(monkeypatch):
    provider = create_provider(LLMProviderSpec(name='hermes-default', model=None))

    class Result:
        stdout = '╭─ ⚕ Hermes ─────────────────────────\n{"results":[{"source_object_id":"1","action":"promote","memory_text":"Durable memory","rationale":"Important"}]}\n\nsession_id: 20260405_213501_2ebfb4\n'

    def fake_run(command, text, capture_output, check):
        assert command == ['hermes', 'chat', '-Q', '-q', 'PROMPT']
        return Result()

    monkeypatch.setattr('subprocess.run', fake_run)
    result = provider.evaluate_prompt('PROMPT')
    assert result == {
        'results': [
            {
                'source_object_id': '1',
                'action': 'promote',
                'memory_text': 'Durable memory',
                'rationale': 'Important',
            }
        ]
    }


def test_provider_rejects_payload_without_top_level_results(monkeypatch):
    provider = create_provider(LLMProviderSpec(name='hermes-default', model=None))

    class Result:
        stdout = '{"ok": true}'

    monkeypatch.setattr('subprocess.run', lambda *args, **kwargs: Result())
    with pytest.raises(ValueError, match='top-level.*results'):
        provider.evaluate_prompt('PROMPT')
