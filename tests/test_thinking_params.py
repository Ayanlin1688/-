"""深度思考参数矩阵纯函数测试：无网络、无 Qt、无文件 I/O。

逐条覆盖交接文档 §5.2「深度思考参数矩阵」：模型族判定、开启/关闭/不识别三态、
各线路请求体映射、已有 extra_body/generationConfig 的保序合并、UA 无关。
"""
from copy import deepcopy
import inspect
import unittest

from core.thinking_params import (
    REASONING_KEYS, apply_thinking_to_payload, extract_reasoning,
    gemini_thinking_config, is_deepseek, is_reasoning, request_model,
    thinking_payload,
)


GEMINI_3_CONFIG = {
    'includeThoughts': True,
    'include_thoughts': True,
    'thinkingLevel': 'medium',
    'thinking_level': 'medium',
}
GEMINI_BUDGET_CONFIG = {
    'includeThoughts': True,
    'include_thoughts': True,
    'thinkingBudget': -1,
    'thinking_budget': -1,
}
GEMINI_3_GOOGLE_CONFIG = {'include_thoughts': True, 'thinking_level': 'medium'}
GEMINI_BUDGET_GOOGLE_CONFIG = {'include_thoughts': True, 'thinking_budget': -1}


class ModelFamilyDetectionTests(unittest.TestCase):
    def test_is_deepseek_positive_and_case_insensitive(self):
        for model in ('deepseek', 'deepseek-v4-pro', 'DeepSeek-Chat', 'deepseek-r1',
                      'deepseek-reasoner', 'vendor/deepseek-v3'):
            with self.subTest(model=model):
                self.assertTrue(is_deepseek(model))

    def test_is_deepseek_negative(self):
        for model in ('', None, 'gemini-3.8-flash', 'gpt-4o', 'qwq-32b',
                      'claude-sonnet-4-6', 'qwen3-thinking'):
            with self.subTest(model=model):
                self.assertFalse(is_deepseek(model))

    def test_is_reasoning_accepts_every_handover_matrix_family(self):
        for model in (
            # deepseek 全族。
            'deepseek-v4-pro', 'deepseek-chat', 'deepseek-r1',
            # reasoner / qwq / r1 夹在分隔符之间。
            'deepseek-reasoner', 'qwq-32b', 'qwen-r1-preview',
            # o1/o3/o4 家族。
            'o1-mini', 'o1-preview', 'o3-mini', 'o3', 'o3-pro', 'o4-mini',
            # gemini 全族。
            'gemini-3.8-flash', 'gemini-2.5-pro', 'GEMINI-3.8-FLASH',
            # qwen thinking/reasoner 与显式 think 标记。
            'qwen3-thinking', 'Qwen3-Thinking', 'qwen-max-reasoner',
            'some-model-thinking', 'xxx-think-mini',
        ):
            with self.subTest(model=model):
                self.assertTrue(is_reasoning(model))

    def test_is_reasoning_rejects_non_reasoning_models(self):
        for model in (
            '', None, 'gpt-image-2', 'gpt-4o', 'gpt-4.1', 'claude-sonnet-4-6',
            'claude-opus-4-1', 'video-v3', 'MiniMax-H3', 'grok-imagine-1.5-video',
            'seedance-2.5', 'qwen-max', 'qwen2.5-7b-instruct', 'text-embedding-3-large',
        ):
            with self.subTest(model=model):
                self.assertFalse(is_reasoning(model))

    def test_is_reasoning_includes_all_deepseek_names(self):
        for model in ('deepseek-v4-pro', 'deepseek-chat', 'deepseek-flash'):
            with self.subTest(model=model):
                self.assertTrue(is_reasoning(model) and is_deepseek(model))

    def test_gemini_config_switches_on_gemini_3_only(self):
        self.assertEqual(gemini_thinking_config('gemini-3.8-flash'), GEMINI_3_CONFIG)
        self.assertEqual(gemini_thinking_config('GEMINI-3-pro'), GEMINI_3_CONFIG)
        self.assertEqual(gemini_thinking_config('gemini-2.5-flash'), GEMINI_BUDGET_CONFIG)
        self.assertEqual(gemini_thinking_config('gemini-flash'), GEMINI_BUDGET_CONFIG)


class ThinkingPayloadTests(unittest.TestCase):
    def test_openai_deepseek_enabled_uses_high_reasoning_effort(self):
        self.assertEqual(thinking_payload('openai', 'deepseek-v4-pro', True), {
            'reasoning_effort': 'high',
            'thinkingMode': {'type': 'enabled'},
        })
        self.assertEqual(thinking_payload('openai', 'DeepSeek-Chat', True), {
            'reasoning_effort': 'high',
            'thinkingMode': {'type': 'enabled'},
        })

    def test_openai_disabled_is_clean_for_every_model(self):
        for model in ('deepseek-chat', 'gemini-3.8-flash', 'o3-mini'):
            with self.subTest(model=model):
                self.assertEqual(thinking_payload('openai', model, False), {})

    def test_openai_gemini_model_enabled_includes_thinking_config(self):
        self.assertEqual(thinking_payload('openai', 'gemini-3.8-flash', True), {
            'thinkingConfig': GEMINI_3_CONFIG,
        })
        self.assertEqual(thinking_payload('openai', 'gemini-2.5-flash', True), {
            'thinkingConfig': GEMINI_BUDGET_CONFIG,
        })

    def test_openai_other_reasoning_enabled_uses_medium_without_thinking_config(self):
        for model in ('o3-mini', 'qwq-32b', 'qwen3-thinking', 'some-model-thinking'):
            with self.subTest(model=model):
                self.assertEqual(thinking_payload('openai', model, True), {
                    'reasoning_effort': 'medium',
                    'enable_thinking': True,
                    'chat_template_kwargs': {'enable_thinking': True},
                })

    def test_openai_other_reasoning_disabled_is_clean(self):
        self.assertEqual(thinking_payload('openai', 'o3-mini', False), {})

    def test_openai_non_reasoning_models_get_no_parameters_in_both_states(self):
        for model in ('gpt-image-2', 'gpt-4o', 'video-v3', 'claude-sonnet-4-6'):
            for enabled in (True, False):
                with self.subTest(model=model, enabled=enabled):
                    self.assertEqual(thinking_payload('openai', model, enabled), {})

    def test_anthropic_enabled_uses_budget_and_max_tokens(self):
        for model in ('claude-sonnet-4-6', 'claude-opus-4-1'):
            with self.subTest(model=model):
                self.assertEqual(thinking_payload('anthropic', model, True), {
                    'thinking': {'type': 'enabled', 'budget_tokens': 8000},
                    'max_tokens': 16000,
                })

    def test_anthropic_disabled_sends_nothing(self):
        self.assertEqual(thinking_payload('anthropic', 'claude-sonnet-4-6', False), {})

    def test_native_gemini_enabled_uses_thinking_config_for_any_model(self):
        self.assertEqual(thinking_payload('gemini', 'gemini-3.8-flash', True),
                         {'thinkingConfig': GEMINI_3_CONFIG})
        self.assertEqual(thinking_payload('gemini', 'gemini-2.5-pro', True),
                         {'thinkingConfig': GEMINI_BUDGET_CONFIG})
        # 参考实现 y1e 对 gemini 线路不看 is_reasoning，直接下发 thinkingConfig。
        self.assertEqual(thinking_payload('gemini', 'imagen-3.0', True),
                         {'thinkingConfig': GEMINI_BUDGET_CONFIG})

    def test_native_gemini_disabled_sends_nothing(self):
        self.assertEqual(thinking_payload('gemini', 'gemini-3.8-flash', False), {})

    def test_unknown_kind_is_rejected(self):
        for kind in ('openai-compatible', '', None, 'OpenAI', 'claude'):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    thinking_payload(kind, 'deepseek-chat', True)
                with self.assertRaises(ValueError):
                    apply_thinking_to_payload({}, kind, 'deepseek-chat', True)

    def test_returned_parameters_are_fresh_dicts(self):
        first = thinking_payload('openai', 'deepseek-chat', True)
        first['thinkingMode']['type'] = 'tampered'
        first['reasoning_effort'] = 'low'
        self.assertEqual(thinking_payload('openai', 'deepseek-chat', True), {
            'reasoning_effort': 'high',
            'thinkingMode': {'type': 'enabled'},
        })


class ApplyThinkingPayloadTests(unittest.TestCase):
    def test_openai_maps_thinking_mode_to_top_level_thinking(self):
        payload = {'model': 'deepseek-v4-pro', 'messages': [], 'stream': True,
                   'temperature': 0.7}
        result = apply_thinking_to_payload(payload, 'openai', 'deepseek-v4-pro', True)
        self.assertEqual(result['thinking'], {'type': 'enabled'})
        self.assertEqual(result['reasoning_effort'], 'high')
        self.assertNotIn('thinkingMode', result)
        self.assertEqual(result['temperature'], 0.7)
        self.assertEqual(payload, {'model': 'deepseek-v4-pro', 'messages': [],
                                   'stream': True, 'temperature': 0.7})

    def test_openai_disabled_removes_all_thinking_fields(self):
        result = apply_thinking_to_payload({}, 'openai', 'deepseek-chat', False)
        self.assertEqual(result, {})

    def test_openai_gemini_maps_thinking_config_to_extra_body_google(self):
        result = apply_thinking_to_payload(
            {'model': 'gemini-3.8-flash'}, 'openai', 'gemini-3.8-flash', True)
        self.assertEqual(result['extra_body'], {
            'google': {'thinking_config': GEMINI_3_GOOGLE_CONFIG}})
        self.assertNotIn('reasoning_effort', result)
        self.assertNotIn('enable_thinking', result)
        self.assertNotIn('chat_template_kwargs', result)
        self.assertNotIn('thinkingConfig', result)
        self.assertNotIn('generationConfig', result)
        self.assertEqual(result['model'], 'gemini-3.8-flash')

    def test_openai_gemini_keeps_existing_extra_body_other_keys(self):
        payload = {
            'model': 'gemini-2.5-flash',
            'extra_body': {'trace': {'id': 't-1'}, 'google': {'stale': True}},
        }
        original = deepcopy(payload)
        result = apply_thinking_to_payload(payload, 'openai', 'gemini-2.5-flash', True)
        self.assertEqual(result['extra_body']['trace'], {'id': 't-1'})
        self.assertEqual(result['extra_body']['google'], {
            'thinking_config': GEMINI_BUDGET_GOOGLE_CONFIG})
        self.assertEqual(payload, original)

    def test_openai_other_reasoning_merges_flat_fields(self):
        result = apply_thinking_to_payload(
            {'model': 'o3-mini'}, 'openai', 'o3-mini', True)
        self.assertEqual(result, {
            'model': 'o3-mini',
            'reasoning_effort': 'medium',
            'enable_thinking': True,
            'chat_template_kwargs': {'enable_thinking': True},
        })

    def test_openai_non_reasoning_returns_equal_copy(self):
        payload = {'model': 'gpt-image-2', 'messages': []}
        result = apply_thinking_to_payload(payload, 'openai', 'gpt-image-2', True)
        self.assertIsNot(result, payload)
        self.assertEqual(result, payload)

    def test_native_gemini_merges_thinking_config_into_generation_config(self):
        payload = {
            'systemInstruction': {'parts': [{'text': 's'}]},
            'contents': [{'role': 'user', 'parts': [{'text': 'u'}]}],
            'generationConfig': {'temperature': 0.7,
                                 'responseMimeType': 'application/json'},
        }
        original = deepcopy(payload)
        result = apply_thinking_to_payload(payload, 'gemini', 'gemini-3.8-flash', True)
        self.assertEqual(result['generationConfig'], {
            'temperature': 0.7,
            'responseMimeType': 'application/json',
            'thinkingConfig': GEMINI_3_CONFIG,
        })
        self.assertEqual(result['systemInstruction'], payload['systemInstruction'])
        self.assertEqual(result['contents'], payload['contents'])
        self.assertEqual(payload, original)

    def test_native_gemini_creates_generation_config_when_missing(self):
        result = apply_thinking_to_payload({}, 'gemini', 'gemini-2.5-pro', True)
        self.assertEqual(result, {'generationConfig': {
            'thinkingConfig': GEMINI_BUDGET_CONFIG}})

    def test_native_gemini_disabled_returns_equal_copy(self):
        payload = {'generationConfig': {'temperature': 0.2}}
        result = apply_thinking_to_payload(payload, 'gemini', 'gemini-3.8-flash', False)
        self.assertEqual(result, payload)
        self.assertIsNot(result, payload)

    def test_anthropic_merges_thinking_and_max_tokens_top_level(self):
        payload = {'model': 'claude-sonnet-4-6', 'stream': True, 'temperature': 0.3}
        original = deepcopy(payload)
        result = apply_thinking_to_payload(payload, 'anthropic', 'claude-sonnet-4-6', True)
        self.assertEqual(result['thinking'], {'type': 'enabled', 'budget_tokens': 8000})
        self.assertEqual(result['max_tokens'], 16000)
        self.assertEqual(result['temperature'], 0.3)
        self.assertEqual(payload, original)

    def test_user_agent_fields_are_irrelevant_and_preserved(self):
        payload = {
            'model': 'gemini-3.8-flash',
            'messages': [],
            'user-agent': 'web/1.0',
            'User-Agent': 'electron/1.4',
        }
        result = apply_thinking_to_payload(payload, 'openai', 'gemini-3.8-flash', True)
        self.assertEqual(result['user-agent'], 'web/1.0')
        self.assertEqual(result['User-Agent'], 'electron/1.4')
        # 矩阵只依赖 kind + model + enabled，不接受任何 UA 输入。
        for function in (thinking_payload, apply_thinking_to_payload, is_reasoning,
                         is_deepseek, request_model):
            names = {name.lower() for name in inspect.signature(function).parameters}
            with self.subTest(function=function.__name__):
                self.assertFalse(names & {'ua', 'user_agent', 'useragent', 'headers'})


class RequestModelTests(unittest.TestCase):
    def test_deepseek_chat_switches_to_v4_pro_when_thinking_enabled(self):
        with self.assertLogs('storyboard', level='INFO') as logs:
            model = request_model('https://api.deepseek.com/v1', 'deepseek-chat', True)
        self.assertEqual(model, 'deepseek-v4-pro')
        self.assertTrue(any('deepseek-chat 已切换为 deepseek-v4-pro' in line
                            for line in logs.output))
        self.assertEqual(len(logs.output), 1)

    def test_deepseek_flash_switches_to_v4_pro_when_thinking_enabled(self):
        with self.assertLogs('storyboard', level='INFO'):
            self.assertEqual(
                request_model('https://api.deepseek.com', 'deepseek-flash', True),
                'deepseek-v4-pro')

    def test_switching_requires_thinking_enabled(self):
        with self.assertNoLogs('storyboard', level='INFO'):
            self.assertEqual(
                request_model('https://api.deepseek.com/v1', 'deepseek-chat', False),
                'deepseek-chat')

    def test_switching_requires_deepseek_host(self):
        for base_url in ('https://api.kkone.vip/v1', 'https://text.example/v1',
                         'http://127.0.0.1:8080/v1'):
            with self.subTest(base_url=base_url):
                with self.assertNoLogs('storyboard', level='INFO'):
                    self.assertEqual(
                        request_model(base_url, 'deepseek-chat', True), 'deepseek-chat')

    def test_non_chat_deepseek_models_and_other_models_are_never_switched(self):
        for model in ('deepseek-reasoner', 'deepseek-v4-pro', 'gemini-3.8-flash',
                      'gpt-4o'):
            with self.subTest(model=model):
                self.assertEqual(
                    request_model('https://api.deepseek.com/v1', model, True), model)

    def test_display_label_is_normalized_before_switching(self):
        with self.assertLogs('storyboard', level='INFO'):
            self.assertEqual(
                request_model('https://api.deepseek.com/v1',
                              'deepseek-chat · DeepSeek Chat', True),
                'deepseek-v4-pro')
        self.assertEqual(
            request_model('https://text.example/v1', 'gpt-4o · GPT-4o', False),
            'gpt-4o')

    def test_switch_host_requires_deepseek_com_inside_hostname(self):
        self.assertEqual(
            request_model('https://api.deepseek.com/v1', 'deepseek-chat', True),
            'deepseek-v4-pro')
        self.assertEqual(
            request_model('https://deepseek.example.com/v1', 'deepseek-chat', True),
            'deepseek-chat')


class ExtractReasoningTests(unittest.TestCase):
    def test_reasoning_keys_contract(self):
        self.assertEqual(
            REASONING_KEYS,
            ('reasoning_content', 'reasoning', 'reasoningContent', 'reasoning_text'))

    def test_each_alias_key_is_extracted(self):
        for key in REASONING_KEYS:
            with self.subTest(key=key):
                self.assertEqual(extract_reasoning({key: '思考'}), '思考')

    def test_first_non_empty_key_wins_in_reference_order(self):
        delta = {'reasoning_text': '丁', 'reasoningContent': '丙',
                 'reasoning': '乙', 'reasoning_content': '甲'}
        self.assertEqual(extract_reasoning(delta), '甲')
        self.assertEqual(extract_reasoning({'reasoning_content': '', 'reasoning': '乙'}),
                         '乙')

    def test_empty_and_non_string_values_are_ignored(self):
        for delta in (None, {}, 'delta', 0, [], {'reasoning_content': []},
                      {'reasoning': 1}, {'reasoningContent': None},
                      {'reasoning_text': {}}, {'content': '正文'}):
            with self.subTest(delta=delta):
                self.assertEqual(extract_reasoning(delta), '')


if __name__ == '__main__':
    unittest.main()
