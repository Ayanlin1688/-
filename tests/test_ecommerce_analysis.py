"""Chat SSE and JSON contracts, using loopback fixtures only."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
import unittest
from unittest.mock import patch

import requests

from core.ecommerce import analysis
from core.ecommerce.analysis import (
    DEFAULT_ANALYSIS_MODEL, EcommerceAnalysisError, analyze_and_parse,
    analyze_stream, parse_prompt_items,
)
from core.http_client import HttpClient


FIXTURE_KEY = 'fixture-analysis-key'


def prompt_items():
    return [
        dict(type='carousel', index=1, prompt='A studio photograph of a bottle.', desc='主图展示商品'),
        dict(type='carousel', index=2, prompt='A bottle in a natural scene.', desc='展示生活场景'),
        dict(type='detail', index=1, prompt='A close-up of the bottle lid.', desc='展示瓶盖细节'),
        dict(type='detail', index=2, prompt='An infographic with bottle dimensions.', desc='展示尺寸规格'),
    ]


def sse_frame(payload):
    return 'data: ' + json.dumps(payload, ensure_ascii=False) + '\r\n\r\n'


def replay_sse(text):
    # Deliberately split JSON words and Chinese descriptions across content frames.
    cuts = [0, 5, 37, 91, len(text)]
    pieces = [text[start:end] for start, end in zip(cuts, cuts[1:]) if start < len(text)]
    return (
        ': keep-alive\r\nevent: message\r\n\r\ndata:   \r\n\r\n'
        + sse_frame({'choices': [{'delta': {'role': 'assistant', 'content': None}}]})
        + sse_frame({'choices': [{'delta': {'reasoning_content': '先检查产品。'}}]})
        + ''.join(sse_frame({'choices': [{'delta': {'content': piece}}]}) for piece in pieces)
        + sse_frame({'choices': [{'delta': {'reasoning_content': '再规划图片。', 'content': ''}}]})
        + sse_frame({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})
        + sse_frame({'choices': [], 'usage': {'total_tokens': 100}})
        + 'data: [DONE]\r\n\r\n'
        + 'data: this must never be parsed after DONE\r\n\r\n'
    ).encode('utf-8')


class AnalysisFixtureHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.calls.append((self.command, self.path, dict(self.headers), json.loads(body)))
        self.send_response(self.server.status)
        self.send_header('Content-Type', 'text/event-stream' if self.server.status == 200 else 'text/plain; charset=utf-8')
        self.send_header('Connection', 'close')
        self.close_connection = True
        data = self.server.response_body
        if self.server.mode in ('truncated', 'timeout'):
            self.send_header('Content-Length', str(len(data) + 100))
            self.end_headers()
            if self.server.mode == 'timeout':
                self.wfile.flush()
                self.server.release.wait(timeout=2)
                return
            self.wfile.write(data)
            self.wfile.flush()
            return
        # HTTP chunks split SSE lines and UTF-8 bytes independently of frames.
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        try:
            for position in range(0, len(data), 7):
                chunk = data[position:position + 7]
                self.wfile.write(f'{len(chunk):x}\r\n'.encode('ascii') + chunk + b'\r\n')
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # A DONE event or rejected frame lets the client close early.
            pass


class LocalAnalysisServer:
    def __init__(self, response_body, *, status=200, mode='normal'):
        self.response_body = response_body
        self.status = status
        self.mode = mode

    def __enter__(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), AnalysisFixtureHandler)
        self.server.calls = []
        self.server.status = self.status
        self.server.mode = self.mode
        self.server.response_body = self.response_body
        self.server.release = threading.Event()
        self.server.base_url = f'http://127.0.0.1:{self.server.server_port}/v1'
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, *args):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class PromptItemsParsingTests(unittest.TestCase):
    def assert_invalid(self, items, *fragments, d=2, g=2):
        with self.assertRaises(EcommerceAnalysisError) as caught:
            parse_prompt_items(json.dumps(items, ensure_ascii=False), d, g)
        message = caught.exception.message
        self.assertEqual(str(caught.exception), message)
        for fragment in ('期望', '实际', *fragments):
            self.assertIn(fragment, message)

    def test_valid_fenced_json_with_surrounding_text(self):
        items = prompt_items()
        raw = '下面是规划：\n```json\n' + json.dumps(items, ensure_ascii=False) + '\n```\n以上是全部。'
        self.assertEqual(parse_prompt_items(raw, 2, 2), items)

    def test_normalizes_fields_and_whitespace_without_changing_order(self):
        items = prompt_items()
        items[0].update(prompt='  Product 林牌, on a table. \n', desc='  展示商品  ', extra='discard')
        original = deepcopy(items)
        normalized = parse_prompt_items(json.dumps(items, ensure_ascii=False), 2, 2)
        self.assertEqual(normalized[0], dict(type='carousel', index=1,
                                            prompt='Product 林牌, on a table.', desc='展示商品'))
        self.assertEqual(items, original)
        shuffled = [items[2], items[0], items[3], items[1]]
        self.assertEqual([item['type'] for item in
                          parse_prompt_items(json.dumps(shuffled), 2, 2)],
                         ['detail', 'carousel', 'detail', 'carousel'])

    def test_length_mismatch_reports_expected_and_actual(self):
        self.assert_invalid(prompt_items()[:3], '期望 4', '实际 3', '总数')

    def test_type_count_mismatch_reports_the_specific_type(self):
        items = prompt_items()
        items[1].update(type='detail', index=3)
        self.assert_invalid(items, 'carousel', '期望 2', '实际 1')
        self.assert_invalid(prompt_items(), 'carousel', '期望 1', '实际 2', d=1, g=3)

    def test_missing_duplicate_and_non_independent_indexes(self):
        for kind, positions, values in (
            ('carousel', (0, 1), (1, 3)),
            ('carousel', (0, 1), (1, 1)),
            ('detail', (2, 3), (1, 3)),
            ('detail', (2, 3), (1, 1)),
            ('carousel', (0, 1), (2, 3)),
            ('detail', (2, 3), (3, 4)),
        ):
            with self.subTest(kind=kind, values=values):
                items = prompt_items()
                for position, value in zip(positions, values):
                    items[position]['index'] = value
                self.assert_invalid(items, kind, '序号', '期望 [1, 2]')

    def test_index_must_be_a_positive_integer_and_never_bool(self):
        for value in (0, -1, True, 1.0, '1', None):
            with self.subTest(value=value):
                items = prompt_items()
                items[0]['index'] = value
                self.assert_invalid(items, 'carousel', '序号', '正整数')

    def test_missing_fields_invalid_items_and_invalid_types(self):
        for field in ('type', 'index', 'prompt', 'desc'):
            with self.subTest(field=field):
                items = prompt_items()
                del items[0][field]
                self.assert_invalid(items, '第 1 项', '缺少', field)
        for value in (None, [], 'not an object'):
            with self.subTest(value=value):
                items = prompt_items()
                items[0] = value
                self.assert_invalid(items, '第 1 项', 'JSON 对象')
        for value in ('image', None, [], {}):
            with self.subTest(kind=value):
                items = prompt_items()
                items[0]['type'] = value
                self.assert_invalid(items, '第 1 项', 'type')

    def test_prompt_and_desc_must_have_nonempty_correct_language_text(self):
        for field in ('prompt', 'desc'):
            for value in ('', '  \n', None, 123, [], {}):
                with self.subTest(field=field, value=value):
                    items = prompt_items()
                    items[0][field] = value
                    self.assert_invalid(items, '第 1 项', field, '非空')
        for field, value in (('prompt', '纯中文提示词'), ('desc', 'English only'), ('prompt', '123')):
            with self.subTest(field=field, value=value):
                items = prompt_items()
                items[0][field] = value
                self.assert_invalid(items, field, '语言错误')

    def test_non_json_and_non_array_are_rejected(self):
        for text in ('not JSON', '```json\n[broken]\n```', '[{"type":', ''):
            with self.subTest(text=text):
                with self.assertRaisesRegex(EcommerceAnalysisError, 'AI 返回不是合法 JSON'):
                    parse_prompt_items(text, 2, 2)
        for text in ('{}', 'null', '"text"', '123'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(EcommerceAnalysisError, '期望 JSON 数组、实际'):
                    parse_prompt_items(text, 2, 2)

    def test_zero_counts_allow_empty_type_numbering(self):
        items = prompt_items()
        self.assertEqual(parse_prompt_items(json.dumps(items[:2]), 2, 0), items[:2])
        self.assertEqual(parse_prompt_items(json.dumps(items[2:]), 0, 2), items[2:])
        self.assertEqual(parse_prompt_items('[]', 0, 0), [])

    def test_invalid_count_arguments_and_non_string_response(self):
        for d, g in ((-1, 2), (True, 2), (1.5, 2), (2, None)):
            with self.subTest(d=d, g=g):
                with self.assertRaisesRegex(EcommerceAnalysisError, '数量配置错误.*期望.*实际'):
                    parse_prompt_items('[]', d, g)
        with self.assertRaisesRegex(EcommerceAnalysisError, 'AI 返回不是合法 JSON'):
            parse_prompt_items(None, 2, 2)


class AnalysisStreamTests(unittest.TestCase):
    def analyze_via_local_fixture(self, server, base_url, model, *, thinking=False):
        original_request = HttpClient.request
        captured_urls = []

        def route_to_fixture(client, method, url, *args, **kwargs):
            captured_urls.append(url)
            return original_request(client, method, server.base_url + '/chat/completions', *args, **kwargs)

        with patch.object(HttpClient, 'request', route_to_fixture):
            result = analyze_stream(base_url, FIXTURE_KEY, model, 'system', 'user', thinking=thinking)
        return result, captured_urls

    def test_reference_payload_keeps_all_nine_in_order_for_both_protocols(self):
        import base64
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / f'image-{i}.png' for i in range(9)]
            for i, path in enumerate(paths):
                path.write_bytes(bytes([i + 1]) * 5)
            for gemini in (False, True):
                with self.subTest(gemini=gemini), LocalAnalysisServer(b'data: [DONE]\n\n') as server:
                    base = server.base_url.replace('/v1', '/v1beta') if gemini else server.base_url
                    analyze_stream(base, FIXTURE_KEY, 'gemini' if gemini else 'fixture-model',
                                   'system', 'user', reference_paths=paths)
                    self.assertEqual(len(server.calls), 1)
                    body = server.calls[0][3]
                    if gemini:
                        parts = body['contents'][0]['parts']
                        data = [part['inlineData']['data'] for part in parts[1:]]
                        self.assertEqual(parts[0], {'text': 'user'})
                    else:
                        parts = body['messages'][1]['content']
                        data = [part['image_url']['url'].split(',', 1)[1] for part in parts[1:]]
                        self.assertEqual(parts[0], {'type': 'text', 'text': 'user'})
                    self.assertEqual([base64.b64decode(item) for item in data],
                                     [path.read_bytes() for path in paths])

    def test_sse_joins_frame_fragments_reasoning_and_skips_usage(self):
        text = json.dumps(prompt_items(), ensure_ascii=False)
        content, reasoning = [], []
        with LocalAnalysisServer(replay_sse(text)) as server:
            full, thoughts = analyze_stream(
                server.base_url + '/', FIXTURE_KEY, DEFAULT_ANALYSIS_MODEL, 'system', 'user',
                on_content=content.append, on_reasoning=reasoning.append)
            self.assertEqual(len(server.calls), 1)
        self.assertEqual(full, text)
        self.assertEqual(thoughts, '先检查产品。再规划图片。')
        self.assertEqual(''.join(content), text)
        self.assertGreater(len(content), 1)
        self.assertEqual(reasoning, ['先检查产品。', '再规划图片。'])

    def test_http_post_contract_and_analyze_and_parse(self):
        text = json.dumps(prompt_items(), ensure_ascii=False)
        content, reasoning = [], []
        with LocalAnalysisServer(replay_sse(text)) as server:
            items, thoughts = analyze_and_parse(
                server.base_url, FIXTURE_KEY, 'gemini-3.1-pro-preview', '系统提示', '用户提示', 2, 2,
                on_content=content.append, on_reasoning=reasoning.append)
            self.assertEqual(len(server.calls), 1)
            method, path, headers, body = server.calls[0]
        self.assertEqual(method, 'POST')
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(headers['Authorization'], 'Bearer ' + FIXTURE_KEY)
        self.assertEqual(headers['Content-Type'], 'application/json')
        self.assertEqual(headers['Accept'], 'text/event-stream')
        self.assertEqual(body, {
            'model': 'gemini-3.1-pro-preview',
            'messages': [{'role': 'system', 'content': '系统提示'},
                         {'role': 'user', 'content': '用户提示'}],
            'stream': True, 'temperature': 0.7,
            # 关闭思考时 openai 兼容线路的 reasoning 模型会收到显式关闭参数。
            'enable_thinking': False,
            'chat_template_kwargs': {'enable_thinking': False},
        })
        self.assertEqual(items, prompt_items())
        self.assertEqual(thoughts, ''.join(reasoning))
        self.assertEqual(''.join(content), text)
        self.assertEqual(DEFAULT_ANALYSIS_MODEL, 'gemini-3.8-flash')

    def test_deepseek_v4_pro_keeps_sampling_parameters_when_switching_from_chat(self):
        with LocalAnalysisServer(b'data: [DONE]\n\n') as server:
            with self.assertLogs('storyboard', level='INFO') as logs:
                _result, urls = self.analyze_via_local_fixture(
                    server, 'https://api.deepseek.com/v1', 'deepseek-chat', thinking=True)
            body = server.calls[0][3]
        self.assertEqual(urls, ['https://api.deepseek.com/v1/chat/completions'])
        self.assertEqual(body['model'], 'deepseek-v4-pro')
        self.assertEqual(body['temperature'], 0.7)
        self.assertEqual(body['reasoning_effort'], 'high')
        self.assertEqual(body['thinking'], {'type': 'enabled'})
        self.assertNotIn('enable_thinking', body)
        self.assertNotIn('extra_body', body)
        self.assertTrue(any('deepseek-chat 已切换为 deepseek-v4-pro' in line for line in logs.output))

    def test_actual_model_name_is_used_in_deepseek_error(self):
        with LocalAnalysisServer(b'No available channel', status=503) as server:
            with self.assertLogs('storyboard', level='INFO'):
                with self.assertRaises(EcommerceAnalysisError) as caught:
                    self.analyze_via_local_fixture(
                        server, 'https://api.deepseek.com/v1', 'deepseek-chat', thinking=True)
        self.assertIn('deepseek-v4-pro', str(caught.exception))
        self.assertNotIn('deepseek-chat', str(caught.exception))

    def test_deepseek_flash_thinking_switches_to_v4_pro(self):
        with LocalAnalysisServer(b'data: [DONE]\n\n') as server:
            self.analyze_via_local_fixture(
                server, 'https://api.deepseek.com/v1', 'deepseek-flash', thinking=True)
            body = server.calls[0][3]
        self.assertEqual(body['model'], 'deepseek-v4-pro')
        self.assertEqual(body['temperature'], 0.7)
        self.assertEqual(body['reasoning_effort'], 'high')
        self.assertEqual(body['thinking'], {'type': 'enabled'})

    def test_reasoning_only_sse_is_parsed(self):
        body = (sse_frame({'choices': [{'delta': {'reasoning_content': '只思考'}}]})
                + 'data: [DONE]\n\n').encode('utf-8')
        with LocalAnalysisServer(body) as server:
            content, reasoning = analyze_stream(
                server.base_url, FIXTURE_KEY, DEFAULT_ANALYSIS_MODEL, '', '')
        self.assertEqual(content, '')
        self.assertEqual(reasoning, '只思考')

    def test_all_reasoning_alias_keys_are_extracted(self):
        pairs = (('reasoning_content', '甲'), ('reasoning', '乙'),
                 ('reasoningContent', '丙'), ('reasoning_text', '丁'))
        frames = ''.join(sse_frame({'choices': [{'delta': {key: value}}]})
                         for key, value in pairs)
        callbacks = []
        with LocalAnalysisServer((frames + 'data: [DONE]\n\n').encode('utf-8')) as server:
            content, reasoning = analyze_stream(
                server.base_url, FIXTURE_KEY, DEFAULT_ANALYSIS_MODEL, '', '',
                on_reasoning=callbacks.append)
        self.assertEqual(content, '')
        self.assertEqual(reasoning, '甲乙丙丁')
        self.assertEqual(callbacks, ['甲', '乙', '丙', '丁'])

    def test_openai_compatible_line_no_longer_sends_blind_claude_thinking(self):
        with LocalAnalysisServer(b'data: [DONE]\n\n') as server:
            self.analyze_via_local_fixture(
                server, 'https://text.example/v1', 'claude-sonnet-4-6', thinking=True)
            body = server.calls[0][3]
        for key in ('thinking', 'reasoning_effort', 'enable_thinking',
                    'chat_template_kwargs', 'extra_body'):
            self.assertNotIn(key, body)
        self.assertEqual(body['temperature'], 0.7)

    def test_openai_compatible_gemini_models_send_google_thinking_config(self):
        cases = (
            # gemini-3 系：thinkingLevel=medium。
            ('gemini-3.8-flash',
             {'include_thoughts': True, 'thinking_level': 'medium'}),
            # 其他 gemini：thinkingBudget=-1。
            ('gemini-2.5-flash',
             {'include_thoughts': True, 'thinking_budget': -1}),
        )
        for model, thinking_config in cases:
            with self.subTest(model=model), LocalAnalysisServer(b'data: [DONE]\n\n') as server:
                self.analyze_via_local_fixture(
                    server, 'https://text.example/v1', model, thinking=True)
                body = server.calls[0][3]
            self.assertEqual(body['reasoning_effort'], 'medium')
            self.assertIs(body['enable_thinking'], True)
            self.assertEqual(body['chat_template_kwargs'], {'enable_thinking': True})
            self.assertEqual(body['extra_body'],
                             {'google': {'thinking_config': thinking_config}})
            # thinkingConfig 逻辑参数只映射到 extra_body，不留在顶层。
            self.assertNotIn('thinkingConfig', body)
            self.assertEqual(body['temperature'], 0.7)

    def test_openai_compatible_other_reasoning_models_send_enable_thinking(self):
        for model in ('o3-mini', 'qwen3-thinking'):
            with self.subTest(model=model), LocalAnalysisServer(b'data: [DONE]\n\n') as server:
                self.analyze_via_local_fixture(
                    server, 'https://text.example/v1', model, thinking=True)
                body = server.calls[0][3]
            self.assertEqual(body['reasoning_effort'], 'medium')
            self.assertIs(body['enable_thinking'], True)
            self.assertEqual(body['chat_template_kwargs'], {'enable_thinking': True})
            self.assertNotIn('thinking', body)
            self.assertNotIn('extra_body', body)

    def test_non_reasoning_models_receive_no_thinking_parameters(self):
        for model in ('gpt-image-2', 'gpt-4o', 'video-v3'):
            with self.subTest(model=model), LocalAnalysisServer(b'data: [DONE]\n\n') as server:
                self.analyze_via_local_fixture(
                    server, 'https://text.example/v1', model, thinking=True)
                body = server.calls[0][3]
            for key in ('thinking', 'reasoning_effort', 'enable_thinking',
                        'chat_template_kwargs', 'extra_body'):
                self.assertNotIn(key, body)

    def test_thinking_off_sends_closed_parameters_only_for_reasoning_models(self):
        cases = (
            ('deepseek-chat', {'thinking': {'type': 'disabled'}}),
            ('gemini-3.8-flash', {'enable_thinking': False,
                                  'chat_template_kwargs': {'enable_thinking': False}}),
            ('gpt-image-2', {}),
        )
        for model, expected in cases:
            with self.subTest(model=model), LocalAnalysisServer(b'data: [DONE]\n\n') as server:
                self.analyze_via_local_fixture(
                    server, 'https://text.example/v1', model, thinking=False)
                body = server.calls[0][3]
            for key, value in expected.items():
                self.assertEqual(body[key], value)
            self.assertEqual(set(body) - {'model', 'messages', 'stream', 'temperature'},
                             set(expected))

    def test_no_reasoning_and_no_callbacks_are_valid(self):
        body = (sse_frame({'choices': [{'delta': {'content': '正文'}}]})
                + 'data: [DONE]\n\n').encode('utf-8')
        with LocalAnalysisServer(body) as server:
            self.assertEqual(analyze_stream(server.base_url, FIXTURE_KEY,
                                            DEFAULT_ANALYSIS_MODEL, 'system', 'user'), ('正文', ''))

    def test_all_choice_deltas_are_read_and_data_space_is_optional(self):
        body = ('data:' + json.dumps({'choices': [
            {'delta': {'content': 'A', 'reasoning_content': '一'}},
            {'delta': {'content': 'B', 'reasoning_content': '二'}},
        ]}, ensure_ascii=False) + '\n\ndata:[DONE]\n\n').encode('utf-8')
        with LocalAnalysisServer(body) as server:
            self.assertEqual(analyze_stream(server.base_url, FIXTURE_KEY,
                                            DEFAULT_ANALYSIS_MODEL, '', ''), ('AB', '一二'))

    def test_http_503_has_status_redacted_bounded_body_and_no_retry(self):
        body = ('No available channel for model; ' + FIXTURE_KEY + ' 渠道不可用 ' + 'x' * 600).encode('utf-8')
        with LocalAnalysisServer(body, status=503) as server:
            with self.assertRaises(EcommerceAnalysisError) as caught:
                analyze_stream(server.base_url, FIXTURE_KEY, DEFAULT_ANALYSIS_MODEL, '', '')
            self.assertEqual(len(server.calls), 1)
        error = caught.exception
        self.assertEqual(error.status_code, 503)
        self.assertIn('No available channel', error.body_excerpt)
        self.assertIn('渠道不可用', error.body_excerpt)
        self.assertEqual(len(error.body_excerpt), 500)
        self.assertNotIn(FIXTURE_KEY, error.body_excerpt)
        self.assertNotIn(FIXTURE_KEY, str(error))
        self.assertIn('[REDACTED]', error.body_excerpt)

    def test_malformed_frames_and_stream_error_are_domain_errors(self):
        payloads = (
            (b'data: not JSON\n\n', '帧不是合法 JSON'),
            (sse_frame([]).encode('utf-8'), '帧格式错误：期望 JSON 对象'),
            (sse_frame({'choices': 'bad'}).encode('utf-8'), 'choices 格式错误：期望数组'),
            (sse_frame({'choices': [None]}).encode('utf-8'), 'delta 格式错误：期望对象'),
            (sse_frame({'choices': [{'delta': {'content': []}}]}).encode('utf-8'),
             'content 格式错误：期望字符串'),
            (sse_frame({'choices': [{'delta': {'reasoning_content': []}}]}).encode('utf-8'),
             'reasoning_content 格式错误：期望字符串'),
            (sse_frame({'choices': [{'delta': {'reasoning': []}}]}).encode('utf-8'),
             'reasoning 格式错误：期望字符串'),
            (sse_frame({'choices': [{'delta': {'reasoning_text': 1}}]}).encode('utf-8'),
             'reasoning_text 格式错误：期望字符串'),
            (sse_frame({'error': {'message': 'channel failed ' + FIXTURE_KEY}}).encode('utf-8'), None),
        )
        for payload, protocol_message in payloads:
            with self.subTest(payload=payload):
                with LocalAnalysisServer(payload) as server:
                    with self.assertRaises(EcommerceAnalysisError) as caught:
                        analyze_stream(server.base_url, FIXTURE_KEY, DEFAULT_ANALYSIS_MODEL, '', '')
                    self.assertEqual(len(server.calls), 1)
                self.assertEqual(caught.exception.status_code, 200)
                self.assertNotIn(FIXTURE_KEY, str(caught.exception))
                self.assertNotIn(FIXTURE_KEY, caught.exception.body_excerpt)
                self.assertFalse(caught.exception.retryable)
                self.assertFalse(caught.exception.switchable)
                self.assertNotIn('无法连接到中转站', caught.exception.message)
                if protocol_message is not None:
                    self.assertEqual(caught.exception.category, 'analysis_error')
                    self.assertIn(protocol_message, caught.exception.message)
                    self.assertEqual(caught.exception.user_message, caught.exception.message)

    def test_truncated_http_body_is_wrapped_without_replay(self):
        with LocalAnalysisServer(b'data: ', mode='truncated') as server:
            with self.assertRaisesRegex(EcommerceAnalysisError, '连接中转站|无法连接到中转站') as caught:
                analyze_stream(server.base_url, FIXTURE_KEY, DEFAULT_ANALYSIS_MODEL, '', '')
            self.assertEqual(len(server.calls), 1)
        self.assertEqual(caught.exception.status_code, 200)

    def test_read_timeout_is_wrapped(self):
        with LocalAnalysisServer(b'', mode='timeout') as server:
            with self.assertRaisesRegex(EcommerceAnalysisError, '连接中转站|无法连接到中转站'):
                analyze_stream(server.base_url, FIXTURE_KEY, DEFAULT_ANALYSIS_MODEL, '', '', timeout=(1, 0.05))
            self.assertEqual(len(server.calls), 1)

    def test_long_read_timeout_is_chinese_and_never_retries_or_switches(self):
        with patch.object(HttpClient, 'request',
                          side_effect=requests.exceptions.ReadTimeout('read timed out')) as request:
            with self.assertRaises(EcommerceAnalysisError) as caught:
                analyze_stream('https://analysis.example/v1', FIXTURE_KEY,
                               DEFAULT_ANALYSIS_MODEL, '', '', timeout=(15, 180))
        request.assert_called_once()
        error = caught.exception
        self.assertEqual(error.category, 'analysis_timeout')
        self.assertEqual(error.user_message,
                         '模型 gemini-3.8-flash：分析超时（180 秒），请切换模型或线路后重试')
        self.assertFalse(error.retryable)
        self.assertFalse(error.switchable)
        self.assertNotIn(FIXTURE_KEY, str(error))

    def test_connection_error_is_wrapped(self):
        # Reserve a loopback port and close it, producing a local connection refusal.
        with LocalAnalysisServer(b'') as server:
            base_url = server.base_url
        with self.assertLogs('storyboard', level='ERROR'):
            with self.assertRaisesRegex(EcommerceAnalysisError, '连接中转站|无法连接到中转站') as caught:
                analyze_stream(base_url, FIXTURE_KEY, DEFAULT_ANALYSIS_MODEL, '', '', timeout=(1, 1))
        self.assertIsNone(caught.exception.status_code)
        self.assertNotIn(FIXTURE_KEY, str(caught.exception))
        self.assertEqual(caught.exception.category, 'connection')
        self.assertTrue(caught.exception.retryable)
        self.assertTrue(caught.exception.switchable)

    def test_connect_timeout_remains_an_explicit_connection_error(self):
        with patch.object(HttpClient, 'request', side_effect=requests.exceptions.ConnectTimeout('fixture timeout')) as request:
            with self.assertRaises(EcommerceAnalysisError) as caught:
                analyze_stream('http://127.0.0.1:1/v1', FIXTURE_KEY, DEFAULT_ANALYSIS_MODEL, '', '')
        request.assert_called_once()
        error = caught.exception
        self.assertIsNone(error.status_code)
        self.assertEqual(error.category, 'connection')
        self.assertTrue(error.retryable)
        self.assertTrue(error.switchable)
        self.assertIn('连接中转站超时', error.message)

    def test_wrapper_propagates_parser_errors(self):
        with LocalAnalysisServer(replay_sse('[]')) as server:
            with self.assertRaisesRegex(EcommerceAnalysisError, '期望 4、实际 0') as caught:
                analyze_and_parse(server.base_url, FIXTURE_KEY, DEFAULT_ANALYSIS_MODEL, '', '', 2, 2)
        self.assertEqual(caught.exception.status_code, 200)

    def test_error_fields_and_module_contains_no_embedded_credentials_or_urls(self):
        error = EcommerceAnalysisError('失败', body_excerpt='文' * 600)
        self.assertEqual(error.message, '失败')
        self.assertEqual(str(error), '失败')
        self.assertIsNone(error.status_code)
        self.assertEqual(error.body_excerpt, '文' * 500)
        self.assertEqual(error.category, 'analysis_error')
        self.assertEqual(error.user_message, '失败')
        self.assertFalse(error.retryable)
        self.assertFalse(error.switchable)
        source = Path(analysis.__file__).read_text(encoding='utf-8')
        self.assertNotIn('sk' + '-', source)
        self.assertIsNone(re.search(r'https?://', source))



class GeminiAnalysisTests(unittest.TestCase):
    def test_protocol_detection(self):
        for base in ('https://generativelanguage.googleapis.com/v1beta',
                     'http://127.0.0.1/v1beta/', 'http://127.0.0.1/v1beta'):
            self.assertTrue(analysis.is_gemini_native(base))
        self.assertFalse(analysis.is_gemini_native('https://api.deepseek.com'))
        self.assertFalse(analysis.is_gemini_native('https://compatible.example/v1'))

    def test_gemini_sse_payload_headers_and_prompt_parsing(self):
        text = json.dumps(prompt_items(), ensure_ascii=False)
        frames = ''.join(sse_frame({'candidates': [{'content': {'parts': [{'text': piece}]}},
                                                  {'content': {'parts': [{'text': 'ignored'}]}}]})
                         for piece in (text[:40], text[40:]))
        chunks = []
        with LocalAnalysisServer(frames.encode('utf-8')) as server:
            base = server.base_url.replace('/v1', '/v1beta')
            items, reasoning = analyze_and_parse(base, FIXTURE_KEY, 'gemini-flash', 'system', 'user', 2, 2,
                                                 on_content=chunks.append)
        self.assertEqual(items, prompt_items())
        self.assertEqual(reasoning, '')
        self.assertEqual(''.join(chunks), text)
        method, path, headers, payload = server.calls[0]
        self.assertEqual((method, path), ('POST', '/v1beta/models/gemini-flash:streamGenerateContent?alt=sse'))
        self.assertEqual(headers['Authorization'], 'Bearer ' + FIXTURE_KEY)
        self.assertEqual(headers['x-goog-api-key'], FIXTURE_KEY)
        self.assertEqual(payload, {
            'systemInstruction': {'parts': [{'text': 'system'}]},
            'contents': [{'role': 'user', 'parts': [{'text': 'user'}]}],
            'generationConfig': {'temperature': 0.7, 'responseMimeType': 'application/json'}})
        self.assertEqual(len(server.calls), 1)

    def test_native_gemini_thinking_config_merges_into_generation_config(self):
        cases = (
            ('gemini-3.8-flash',
             {'includeThoughts': True, 'include_thoughts': True,
              'thinkingLevel': 'medium', 'thinking_level': 'medium'}),
            ('gemini-2.5-flash',
             {'includeThoughts': True, 'include_thoughts': True,
              'thinkingBudget': -1, 'thinking_budget': -1}),
        )
        for model, thinking_config in cases:
            with self.subTest(model=model), LocalAnalysisServer(b'data: [DONE]\n\n') as server:
                analyze_stream(server.base_url.replace('/v1', '/v1beta'), FIXTURE_KEY,
                               model, 'system', 'user', thinking=True)
                payload = server.calls[0][3]
            # 基础 generationConfig 字段保留，thinkingConfig 只做合并。
            self.assertEqual(payload['generationConfig'], {
                'temperature': 0.7, 'responseMimeType': 'application/json',
                'thinkingConfig': thinking_config})
            self.assertEqual(set(payload),
                             {'systemInstruction', 'contents', 'generationConfig'})
            self.assertEqual(len(server.calls), 1)

    def test_gemini_error_frame_is_final_after_200(self):
        frame = sse_frame({'error': {'code': 503, 'message': 'temporarily unavailable ' + FIXTURE_KEY}})
        with LocalAnalysisServer(frame.encode()) as server:
            with self.assertRaises(EcommerceAnalysisError) as caught:
                analyze_stream(server.base_url.replace('/v1', '/v1beta'), FIXTURE_KEY, 'gemini', 's', 'u')
        error = caught.exception
        self.assertEqual(error.status_code, 200)
        self.assertFalse(error.retryable)
        self.assertFalse(error.switchable)
        self.assertIn('上游服务暂时不可用', error.message)
        self.assertNotIn(FIXTURE_KEY, error.message + error.body_excerpt)
        self.assertNotIn('{', error.message)
        self.assertEqual(len(server.calls), 1)

    def test_gemini_protocol_errors_preserve_description_and_never_retry(self):
        for frame, message in (
                ({'candidates': 'bad'}, 'candidates 格式错误：期望数组'),
                ({'candidates': [{'content': {'parts': 'bad'}}]}, 'parts 格式错误：期望数组'),
                ({'candidates': [{'content': {'parts': [{'text': []}]}}]},
                 'text 格式错误：期望字符串')):
            with self.subTest(frame=frame):
                with LocalAnalysisServer(sse_frame(frame).encode('utf-8')) as server:
                    with self.assertRaises(EcommerceAnalysisError) as caught:
                        analyze_stream(server.base_url.replace('/v1', '/v1beta'), FIXTURE_KEY,
                                       'gemini', 's', 'u')
                error = caught.exception
                self.assertEqual(error.status_code, 200)
                self.assertEqual(error.category, 'analysis_error')
                self.assertFalse(error.retryable)
                self.assertFalse(error.switchable)
                self.assertIn(message, error.message)
                self.assertEqual(error.user_message, error.message)
                self.assertNotIn('无法连接到中转站', error.message)
                self.assertEqual(len(server.calls), 1)

    def test_gemini_401_is_classified_and_not_retried(self):
        with LocalAnalysisServer(b'{"error":{"code":401,"message":"UNAUTHENTICATED"}}', status=401) as server:
            with self.assertRaises(EcommerceAnalysisError) as caught:
                analyze_stream(server.base_url.replace('/v1', '/v1beta'), FIXTURE_KEY, 'gemini', 's', 'u')
        self.assertIn('密钥无效', caught.exception.message)
        self.assertFalse(caught.exception.retryable)
        self.assertFalse(caught.exception.switchable)
        self.assertEqual(len(server.calls), 1)

    def test_error_frames_without_numeric_code_are_chinese_and_final(self):
        for detail, expected in (({'message': 'channel failed'}, '上游服务暂时不可用'),
                                 ({'code': 'bad_request', 'type': 'upstream_text_reply',
                                   'message': 'desnudez'}, '安全审核拒绝')):
            for path in ('/v1', '/v1beta'):
                with self.subTest(detail=detail, path=path):
                    frame = sse_frame({'error': detail})
                    with LocalAnalysisServer(frame.encode()) as server:
                        with self.assertRaises(EcommerceAnalysisError) as caught:
                            analyze_stream(server.base_url.replace('/v1', path), FIXTURE_KEY,
                                           'fixture-model', 's', 'u')
                    self.assertIn(expected, caught.exception.message)
                    self.assertEqual(caught.exception.status_code, 200)
                    self.assertFalse(caught.exception.retryable)
                    self.assertFalse(caught.exception.switchable)
                    self.assertEqual(len(server.calls), 1)

if __name__ == '__main__':
    unittest.main()
