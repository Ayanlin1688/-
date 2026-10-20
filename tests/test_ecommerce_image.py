"""Synchronous image contracts, verified only against loopback HTTP fixtures."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import tempfile
import threading
import unittest

from PyQt5.QtCore import QByteArray, QBuffer, QIODevice
from PyQt5.QtGui import QImage

from core.ecommerce import image_client
from core.ecommerce.image_client import (
    DEFAULT_IMAGE_MODEL, TIER_SHORT_EDGE, EcommerceImageError,
    _even, _is_size_rejection, _quality, _standard_tile,
    build_size_plan, generate_image, generate_image_edit,
    generate_text_to_image, image_dimensions,
)
from core.ecommerce.platforms import RATIOS


FIXTURE_KEY = 'fixture-image-key'
PNG_BYTES = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6'
    'HGkAAAAASUVORK5CYII=')


def image_payload(*, image_tokens=None, revised_prompt=None):
    item = {'b64_json': base64.b64encode(PNG_BYTES).decode('ascii')}
    if revised_prompt is not None:
        item['revised_prompt'] = revised_prompt
    payload = {'data': [item]}
    if image_tokens is not None:
        payload['usage'] = {'input_tokens_details': {'image_tokens': image_tokens}}
    return payload


def encoded_image(fmt):
    image = QImage(1, 1, QImage.Format_ARGB32)
    image.fill(0xff44c877)
    output = QByteArray()
    buffer = QBuffer(output)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, fmt)
    buffer.close()
    return bytes(output)


class ImageFixtureHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        call = {'method': self.command, 'path': self.path,
                'headers': dict(self.headers), 'body': raw}
        self.server.calls.append(call)
        status, payload = self.server.responder(call, len(self.server.calls))
        if self.server.mode == 'before_headers':
            self.close_connection = True
            return
        if isinstance(payload, bytes):
            body = payload
        elif isinstance(payload, str):
            body = payload.encode('utf-8')
        else:
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Connection', 'close')
        self.close_connection = True
        incomplete = self.server.mode in ('truncated', 'timeout')
        self.send_header('Content-Length', str(len(body) + (100 if incomplete else 0)))
        self.end_headers()
        try:
            if self.server.mode == 'timeout':
                self.wfile.flush()
                self.server.release.wait(timeout=2)
                return
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        self.server.get_calls.append({'path': self.path, 'headers': dict(self.headers)})
        status, payload, redirect = self.server.get_responder(self.path)
        if redirect:
            self.send_response(status)
            self.send_header('Location', redirect)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self.send_response(status)
        self.send_header('Content-Type', 'image/png')
        self.send_header('Connection', 'close')
        incomplete = self.server.get_mode == 'truncated'
        self.send_header('Content-Length', str(len(payload) + (100 if incomplete else 0)))
        self.end_headers()
        try:
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class LocalImageServer:
    def __init__(self, payload=None, *, status=200, responder=None, mode='normal'):
        self.payload = image_payload() if payload is None else payload
        self.status = status
        self.responder = responder
        self.mode = mode

    def __enter__(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ImageFixtureHandler)
        self.server.calls = []
        self.server.get_calls = []
        self.server.get_mode = 'normal'
        self.server.mode = self.mode
        self.server.release = threading.Event()
        self.server.responder = self.responder or (lambda call, count: (self.status, self.payload))
        self.server.get_responder = lambda path: (200, PNG_BYTES, None)
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


def multipart_parts(call):
    """Read the fixture's raw multipart bytes without the removed cgi module."""
    content_type = call['headers']['Content-Type']
    if not content_type.startswith('multipart/form-data;'):
        raise AssertionError(content_type)
    match = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type)
    if match is None:
        raise AssertionError('multipart boundary missing')
    boundary = (match.group(1) or match.group(2)).encode('ascii')
    parts = []
    for raw in call['body'].split(b'--' + boundary)[1:]:
        if raw.startswith(b'--'):
            break
        headers, content = raw.removeprefix(b'\r\n').split(b'\r\n\r\n', 1)
        if not content.endswith(b'\r\n'):
            raise AssertionError('multipart separator missing')
        disposition = headers.decode('utf-8')
        name = re.search(r'(?:^|;\s*)name="([^"]+)"',
                         disposition.split('\r\n', 1)[0])
        filename = re.search(r'filename="([^"]+)"', disposition)
        if name is None:
            raise AssertionError(disposition)
        parts.append({'name': name.group(1),
                      'filename': filename.group(1) if filename else None,
                      'headers': disposition, 'body': content[:-2]})
    return parts


class ImageDimensionsTests(unittest.TestCase):
    def test_defaults_and_all_nine_1k_exact_dimensions(self):
        self.assertEqual(DEFAULT_IMAGE_MODEL, 'gpt-image-2.5-sunburst')
        self.assertEqual(TIER_SHORT_EDGE, {'1K': 1024, '2K': 2048, '4K': 4096})
        expected = {
            '1:1': (1024, 1024), '9:16': (1024, 1820), '16:9': (1820, 1024),
            '3:4': (1024, 1364), '4:3': (1364, 1024), '4:5': (1024, 1280),
            '5:4': (1280, 1024), '2:3': (1024, 1536), '3:2': (1536, 1024),
        }
        self.assertEqual(set(expected), set(RATIOS))
        for ratio, dimensions in expected.items():
            with self.subTest(ratio=ratio):
                self.assertEqual(image_dimensions(ratio, '1K'), dimensions)

    def test_2k_and_4k_use_each_tiers_short_edge_and_even_long_edge(self):
        for tier, short_edge in (('2K', 2048), ('4K', 4096)):
            for ratio in RATIOS:
                with self.subTest(tier=tier, ratio=ratio):
                    w, h = image_dimensions(ratio, tier)
                    rw, rh = map(int, ratio.split(':'))
                    self.assertEqual(min(w, h), short_edge)
                    self.assertEqual(w % 2, 0)
                    self.assertEqual(h % 2, 0)
                    exact_long_edge = short_edge * max(rw, rh) / min(rw, rh)
                    self.assertLessEqual(abs(max(w, h) - exact_long_edge), 1.5)
                    self.assertEqual((w > h) - (w < h), (rw > rh) - (rw < rh))
        # Round after scaling, so 3:4 at 2K is 2730, rather than 1364 * 2.
        self.assertEqual(image_dimensions('3:4', '2K'), (2048, 2730))
        self.assertEqual(image_dimensions('9:16', '4K'), (4096, 7282))

    def test_even_rounding_matches_required_formula(self):
        for value, expected in ((1365.333, 1364), (1820.444, 1820),
                                (7281.777, 7282), (3.9, 4), (3, 2)):
            self.assertEqual(_even(value), expected)

    def test_invalid_ratios_and_tiers_raise_value_error(self):
        for ratio in ('', '0:1', '1:0', '7:5', '4/3', '3:4:5', None, []):
            with self.subTest(ratio=ratio):
                with self.assertRaises(ValueError):
                    image_dimensions(ratio, '1K')
        for tier in ('', '3K', '1k', None, []):
            with self.subTest(tier=tier):
                with self.assertRaises(ValueError):
                    image_dimensions('1:1', tier)

    def test_standard_tiles_and_quality(self):
        self.assertEqual(_standard_tile('1:1', 1024), (1024, 1024))
        self.assertEqual(_standard_tile('9:16', 2048), (2048, 3072))
        self.assertEqual(_standard_tile('16:9', 4096), (6144, 4096))
        self.assertEqual(_quality('1K'), 'auto')
        self.assertEqual(_quality('2K'), 'high')
        self.assertEqual(_quality('4K'), 'high')

    def test_plan_preserves_exact_standard_and_descending_tier_order(self):
        self.assertEqual(build_size_plan('9:16', '4K'), [
            {'size': '4096x7282', 'tier': '4K', 'quality': 'high'},
            {'size': '4096x6144', 'tier': '4K', 'quality': 'high'},
            {'size': '2048x3640', 'tier': '2K', 'quality': 'high'},
            {'size': '2048x3072', 'tier': '2K', 'quality': 'high'},
            {'size': '1024x1820', 'tier': '1K', 'quality': 'auto'},
            {'size': '1024x1536', 'tier': '1K', 'quality': 'auto'},
        ])
        self.assertEqual(build_size_plan('16:9', '1K'), [
            {'size': '1820x1024', 'tier': '1K', 'quality': 'auto'},
            {'size': '1536x1024', 'tier': '1K', 'quality': 'auto'},
        ])
        self.assertEqual([p['tier'] for p in build_size_plan('9:16', '2K')],
                         ['2K', '2K', '1K', '1K'])

    def test_plan_deduplicates_square_and_standard_ratios(self):
        for ratio in ('1:1', '2:3', '3:2'):
            with self.subTest(ratio=ratio):
                plan = build_size_plan(ratio, '4K')
                self.assertEqual(len(plan), 3)
                self.assertEqual([p['tier'] for p in plan], ['4K', '2K', '1K'])
                self.assertEqual(len({p['size'] for p in plan}), len(plan))
                self.assertEqual(len(build_size_plan(ratio, '1K')), 1)

    def test_only_4xx_size_errors_allow_fallback(self):
        for body in ('invalid SIZE', 'unsupported dimension', '分辨率超限',
                     '尺寸不支持', 'image too large'):
            with self.subTest(body=body):
                self.assertTrue(_is_size_rejection(400, body))
        for status, body in ((500, 'invalid size'), (200, 'invalid size'),
                             (401, 'invalid key'), (403, 'forbidden'),
                             (429, 'quota exhausted')):
            with self.subTest(status=status, body=body):
                self.assertFalse(_is_size_rejection(status, body))


class ImageClientHTTPTests(unittest.TestCase):
    def generate(self, server, **kwargs):
        options = dict(prompt='Studio photograph of a bottle.', ratio='1:1', tier='1K')
        options.update(kwargs)
        return generate_image(server.base_url, FIXTURE_KEY, DEFAULT_IMAGE_MODEL, **options)

    def make_references(self, directory, names):
        paths = []
        for index, name in enumerate(names):
            path = Path(directory) / name
            path.write_bytes(PNG_BYTES + str(index).encode('ascii'))
            paths.append(path)
        return paths

    def test_json_generation_request_and_result_fields(self):
        with LocalImageServer(image_payload(revised_prompt='A refined prompt.')) as server:
            result = self.generate(server, ratio='4:5', tier='2K')
        self.assertEqual(len(server.calls), 1)
        call = server.calls[0]
        self.assertEqual(call['method'], 'POST')
        self.assertEqual(call['path'], '/v1/images/generations')
        self.assertEqual(call['headers']['Authorization'], 'Bearer ' + FIXTURE_KEY)
        self.assertEqual(call['headers']['Content-Type'], 'application/json')
        self.assertEqual(json.loads(call['body']), {
            'model': DEFAULT_IMAGE_MODEL, 'prompt': 'Studio photograph of a bottle.',
            'n': 1, 'size': '2048x2560', 'quality': 'high',
            'response_format': 'b64_json',
        })
        self.assertEqual(result.png_bytes, PNG_BYTES)
        self.assertEqual(result.model, DEFAULT_IMAGE_MODEL)
        self.assertEqual(result.size, '2048x2560')
        self.assertEqual(result.tier_used, '2K')
        self.assertEqual(result.quality, 'high')
        self.assertEqual(result.endpoint, '/images/generations')
        self.assertEqual(result.revised_prompt, 'A refined prompt.')
        self.assertIsNone(result.image_tokens)
        self.assertEqual(result.warnings, [])

    def test_url_delivery_follows_redirect_without_credentials(self):
        with LocalImageServer() as server:
            server.responder = lambda call, count: (200, {
                'data': [{'url': server.base_url + '/download',
                          'revised_prompt': 'URL prompt'}]})
            server.get_responder = lambda path: (
                (302, b'', '/v1/final') if path == '/v1/download' else (200, PNG_BYTES, None))
            result = self.generate(server)
        self.assertEqual(result.delivery, 'url')
        self.assertEqual(result.png_bytes, PNG_BYTES)
        self.assertEqual(result.revised_prompt, 'URL prompt')
        self.assertEqual([call['path'] for call in server.get_calls], ['/v1/download', '/v1/final'])
        self.assertTrue(all('Authorization' not in call['headers'] for call in server.get_calls))

    def test_url_delivery_converts_jpeg_and_webp_to_png(self):
        for fmt in ('JPEG', 'WEBP'):
            with self.subTest(fmt=fmt), LocalImageServer() as server:
                source = encoded_image(fmt)
                server.responder = lambda call, count: (200, {
                    'data': [{'url': server.base_url + '/source'}]})
                server.get_responder = lambda path, source=source: (200, source, None)
                result = self.generate(server)
            self.assertEqual(result.delivery, 'url')
            self.assertTrue(result.png_bytes.startswith(b'\x89PNG\r\n\x1a\n'))
            self.assertIn(f'源图为 {fmt}，已转为 PNG', result.warnings)

    def test_url_download_failures_are_billed_200_and_not_retried(self):
        cases = [
            ('ftp://example.invalid/image.png', None),
            ('http://[malformed', None),
            ('http://fixture-user:fixture-pass@example.invalid/image.png', None),
            ('http://127.0.0.1:1/image.png', None),
        ]
        for image_url, responder in cases:
            with self.subTest(image_url=image_url), LocalImageServer() as server:
                server.responder = lambda call, count, image_url=image_url: (200, {
                    'data': [{'url': image_url}]})
                with self.assertRaises(EcommerceImageError) as caught:
                    self.generate(server)
            self.assertEqual(caught.exception.status_code, 200)
            self.assertEqual(len(server.calls), 1)
        for status in (403, 404):
            with self.subTest(status=status), LocalImageServer() as server:
                server.responder = lambda call, count: (200, {
                    'data': [{'url': server.base_url + '/missing'}]})
                server.get_responder = lambda path, status=status: (status, b'nope', None)
                with self.assertRaises(EcommerceImageError) as caught:
                    self.generate(server)
            self.assertEqual(caught.exception.status_code, 200)
            self.assertEqual(len(server.calls), 1)
        with LocalImageServer() as server:
            server.responder = lambda call, count: (200, {
                'data': [{'url': server.base_url + '/truncated'}]})
            server.get_mode = 'truncated'
            with self.assertRaises(EcommerceImageError) as caught:
                self.generate(server, timeout=(1, 0.1))
        self.assertEqual(caught.exception.status_code, 200)
        self.assertEqual(len(server.calls), 1)

    def test_response_format_rejection_retries_once_without_parameter(self):
        def responder(call, count):
            if count == 1:
                return 400, 'unknown parameter: response_format'
            return 200, image_payload()

        with LocalImageServer(responder=responder) as server:
            result = self.generate(server)
        self.assertEqual(len(server.calls), 2)
        self.assertIn('response_format', json.loads(server.calls[0]['body']))
        self.assertNotIn('response_format', json.loads(server.calls[1]['body']))
        self.assertTrue(any('移除参数重试' in text for text in result.warnings))

    def test_content_policy_rejection_is_not_retried(self):
        with LocalImageServer('upstream_text_reply: content policy', status=400) as server:
            with self.assertRaises(EcommerceImageError) as caught:
                self.generate(server)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(len(server.calls), 1)

    def test_empty_reference_list_uses_generations_and_wrapper(self):
        with LocalImageServer() as server:
            result = self.generate(server, reference_paths=[])
            wrapped = generate_text_to_image(
                server.base_url, FIXTURE_KEY, DEFAULT_IMAGE_MODEL, 'A bottle.', '1:1', '1K')
        self.assertEqual([call['path'] for call in server.calls],
                         ['/v1/images/generations', '/v1/images/generations'])
        self.assertEqual(result.png_bytes, PNG_BYTES)
        self.assertEqual(wrapped.png_bytes, PNG_BYTES)
        self.assertIsNone(wrapped.revised_prompt)

    def test_multipart_preserves_product_style_order_bytes_and_mime(self):
        names = ['product-front.PNG', 'product-back.jpg', 'product-side.jpeg',
                 'product-close.webp', 'product-other.bin', 'style.png']
        with tempfile.TemporaryDirectory() as directory:
            paths = self.make_references(directory, names)
            with LocalImageServer(image_payload(image_tokens=16)) as server:
                result = generate_image_edit(
                    server.base_url, FIXTURE_KEY, DEFAULT_IMAGE_MODEL,
                    'Use the last image as style only.', '3:4', '1K', paths)
            parts = multipart_parts(server.calls[0])
            files = [p for p in parts if p['filename'] is not None]
            fields = {p['name']: p['body'].decode('utf-8')
                      for p in parts if p['filename'] is None}
            self.assertEqual(len(files), len(paths))
            self.assertEqual([p['name'] for p in files], ['image'] * len(paths))
            self.assertEqual([p['filename'] for p in files], names)
            self.assertEqual([p['body'] for p in files], [p.read_bytes() for p in paths])
            expected_mimes = ['image/png', 'image/jpeg', 'image/jpeg', 'image/webp',
                              'image/png', 'image/png']
            for part, mime in zip(files, expected_mimes):
                self.assertIn('Content-Type: ' + mime, part['headers'])
        self.assertEqual(server.calls[0]['method'], 'POST')
        self.assertEqual(server.calls[0]['path'], '/v1/images/edits')
        self.assertEqual(server.calls[0]['headers']['Authorization'], 'Bearer ' + FIXTURE_KEY)
        self.assertEqual(fields, {'model': DEFAULT_IMAGE_MODEL,
                                 'prompt': 'Use the last image as style only.',
                                 'n': '1', 'size': '1024x1364', 'quality': 'auto',
                                 'response_format': 'b64_json'})
        self.assertEqual(result.png_bytes, PNG_BYTES)
        self.assertEqual(result.endpoint, '/images/edits')
        self.assertEqual(result.image_tokens, 16)
        self.assertFalse(any('image_tokens=0' in warning for warning in result.warnings))

    def test_seven_reference_files_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.make_references(directory, [f'product-{i}.png' for i in range(6)] + ['style.png'])
            with LocalImageServer() as server:
                self.generate(server, reference_paths=paths)
            files = [part for part in multipart_parts(server.calls[0]) if part['filename']]
        self.assertEqual(len(files), 7)
        self.assertEqual(files[-1]['filename'], 'style.png')

    def test_invalid_prompt_rejected_before_network(self):
        with LocalImageServer() as server:
            for prompt in ('', '  \n', None, 17, []):
                with self.subTest(prompt=prompt):
                    with self.assertRaises(ValueError):
                        self.generate(server, prompt=prompt)
        self.assertEqual(server.calls, [])

    def test_too_many_references_rejected_before_read_or_network(self):
        with LocalImageServer() as server:
            with self.assertRaises(ValueError):
                self.generate(server, reference_paths=['does-not-exist.png'] * 8)
        self.assertEqual(server.calls, [])

    def test_missing_and_unreadable_reference_files_are_image_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            for path in (Path(directory) / 'missing.png', Path(directory)):
                with self.subTest(path=path):
                    with LocalImageServer() as server:
                        with self.assertRaises(EcommerceImageError) as caught:
                            self.generate(server, reference_paths=[path])
                    self.assertFalse(caught.exception.failed_before_response)
                    self.assertEqual(server.calls, [])

    def test_data_b64_and_png_validation_never_returns_bad_images(self):
        invalid_payloads = [
            {}, {'data': None}, {'data': {}}, {'data': []}, {'data': ['text']},
            {'data': [{}]}, {'data': [{'b64_json': ''}]},
            {'data': [{'b64_json': 12}]}, {'data': [{'b64_json': '!!!invalid!!!'}]},
            {'data': [{'b64_json': base64.b64encode(b'not a PNG image').decode('ascii')}]},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with LocalImageServer(payload) as server:
                    with self.assertRaises(EcommerceImageError) as caught:
                        self.generate(server, ratio='9:16', tier='4K')
                self.assertFalse(caught.exception.failed_before_response)
                self.assertEqual(caught.exception.status_code, 200)
                self.assertEqual(len(server.calls), 1)

    def test_invalid_json_and_root_are_image_errors(self):
        for payload in (b'not-json', [], 'null'):
            with self.subTest(payload=payload):
                with LocalImageServer(payload) as server:
                    with self.assertRaises(EcommerceImageError) as caught:
                        self.generate(server, ratio='9:16', tier='4K')
                self.assertFalse(caught.exception.failed_before_response)
                self.assertEqual(caught.exception.status_code, 200)
                self.assertEqual(len(server.calls), 1)

    def test_zero_image_tokens_warn_only_when_references_present(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.make_references(directory, ['product.png'])
            for tokens in (0, 25, None):
                with self.subTest(tokens=tokens):
                    with LocalImageServer(image_payload(image_tokens=tokens)) as server:
                        result = self.generate(server, reference_paths=paths)
                    self.assertEqual(result.image_tokens, tokens)
                    warning = '参考图可能未被模型采用（image_tokens=0）'
                    self.assertEqual(warning in result.warnings, tokens == 0)
        with LocalImageServer(image_payload(image_tokens=0)) as server:
            result = self.generate(server)
        self.assertEqual(result.image_tokens, 0)
        self.assertFalse(any('image_tokens=0' in warning for warning in result.warnings))

    def test_missing_or_malformed_usage_is_optional(self):
        for usage in (None, [], {}, {'input_tokens_details': None},
                      {'input_tokens_details': []}, {'input_tokens_details': {}}):
            with self.subTest(usage=usage):
                payload = image_payload()
                payload['usage'] = usage
                with LocalImageServer(payload) as server:
                    result = self.generate(server)
                self.assertIsNone(result.image_tokens)

    def test_4k_exact_then_standard_then_2k_fallback(self):
        def responder(call, count):
            size = json.loads(call['body'])['size']
            if size.startswith('4096x'):
                return 400, {'error': {'message': 'unsupported size: ' + size}}
            return 200, image_payload()

        with LocalImageServer(responder=responder) as server:
            result = self.generate(server, ratio='9:16', tier='4K')
        self.assertEqual([json.loads(call['body'])['size'] for call in server.calls],
                         ['4096x7282', '4096x6144', '2048x3640'])
        self.assertEqual(result.size, '2048x3640')
        self.assertEqual(result.tier_used, '2K')
        self.assertEqual(result.quality, 'high')
        warnings = '\n'.join(result.warnings)
        for text in ('4096x7282', '4096x6144', 'unsupported size',
                     '4K', '2K', '回退', '2048x3640'):
            self.assertIn(text, warnings)

    def test_same_tier_standard_tile_fallback(self):
        def responder(call, count):
            return ((400, 'dimension is not supported') if count == 1
                    else (200, image_payload()))

        with LocalImageServer(responder=responder) as server:
            result = self.generate(server, ratio='9:16', tier='1K')
        self.assertEqual(len(server.calls), 2)
        self.assertEqual(result.size, '1024x1536')
        self.assertEqual(result.tier_used, '1K')
        self.assertIn('1024x1820', '\n'.join(result.warnings))
        self.assertIn('1024x1536', '\n'.join(result.warnings))

    def test_multipart_fallback_reuses_all_reference_bytes_in_order(self):
        def responder(call, count):
            return (400, 'size unsupported') if count == 1 else (200, image_payload())

        with tempfile.TemporaryDirectory() as directory:
            paths = self.make_references(directory, ['product.png', 'style.jpg'])
            with LocalImageServer(responder=responder) as server:
                result = self.generate(server, ratio='9:16', tier='1K', reference_paths=paths)
            ordered_files = [[(p['filename'], p['body']) for p in multipart_parts(call)
                              if p['filename']] for call in server.calls]
        self.assertEqual(len(ordered_files), 2)
        self.assertEqual(ordered_files[0], ordered_files[1])
        self.assertEqual([name for name, content in ordered_files[1]], ['product.png', 'style.jpg'])
        self.assertEqual(result.size, '1024x1536')

    def test_non_size_errors_do_not_try_more_candidates(self):
        for status, message in ((500, 'size service failed'), (400, 'invalid prompt'),
                                (403, 'not authorized'), (429, 'quota exhausted')):
            with self.subTest(status=status):
                with LocalImageServer(message, status=status) as server:
                    with self.assertRaises(EcommerceImageError) as caught:
                        self.generate(server, ratio='9:16', tier='4K')
                self.assertEqual(len(server.calls), 1)
                self.assertEqual(caught.exception.status_code, status)
                self.assertIn(message, caught.exception.body_excerpt)
                self.assertFalse(caught.exception.failed_before_response)

    def test_all_size_rejections_stop_after_last_candidate(self):
        with LocalImageServer('unsupported size', status=400) as server:
            with self.assertRaises(EcommerceImageError) as caught:
                self.generate(server, ratio='9:16', tier='4K')
        self.assertEqual(len(server.calls), 6)
        self.assertEqual([json.loads(call['body'])['size'] for call in server.calls],
                         [p['size'] for p in build_size_plan('9:16', '4K')])
        self.assertEqual(caught.exception.status_code, 400)

    def test_http_error_redacts_key_before_excerpt_is_limited(self):
        body = 'x' * 495 + FIXTURE_KEY + ' size service error: ' + 'x' * 700
        with LocalImageServer(body, status=500) as server:
            with self.assertRaises(EcommerceImageError) as caught:
                self.generate(server)
        error = caught.exception
        self.assertEqual(str(error), error.message)
        self.assertNotIn(FIXTURE_KEY, str(error))
        self.assertNotIn(FIXTURE_KEY, error.body_excerpt)
        self.assertEqual(error.body_excerpt, 'x' * 495 + '[REDA')
        self.assertEqual(str(error), 'HTTP 500: ' + error.body_excerpt)
        self.assertEqual(len(error.body_excerpt), 500)

    def test_fallback_warnings_redact_server_error_credentials(self):
        def responder(call, count):
            return ((400, 'size unavailable for ' + FIXTURE_KEY) if count == 1
                    else (200, image_payload()))

        with LocalImageServer(responder=responder) as server:
            result = self.generate(server, ratio='9:16', tier='4K')
        self.assertEqual(len(server.calls), 2)
        warnings = '\n'.join(result.warnings)
        self.assertNotIn(FIXTURE_KEY, warnings)
        self.assertIn('[REDACTED]', warnings)

    def test_connection_failure_after_size_rejection_has_no_response_flag(self):
        def responder(call, count):
            if count == 1:
                return 400, 'size unsupported'
            server.mode = 'before_headers'
            return 200, image_payload()

        with LocalImageServer(responder=responder) as server:
            with self.assertLogs('storyboard', level='ERROR'):
                with self.assertRaises(EcommerceImageError) as caught:
                    self.generate(server, ratio='9:16', tier='4K', timeout=(1, 1))
        self.assertEqual(len(server.calls), 2)
        self.assertTrue(caught.exception.failed_before_response)
        self.assertIsNone(caught.exception.status_code)

    def test_closed_port_is_failure_before_response_without_key(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        with self.assertLogs('storyboard', level='ERROR'):
            with self.assertRaises(EcommerceImageError) as caught:
                generate_image(f'http://127.0.0.1:{port}/v1', FIXTURE_KEY,
                               DEFAULT_IMAGE_MODEL, 'A bottle.', '9:16', '4K',
                               timeout=(0.2, 0.2))
        self.assertTrue(caught.exception.failed_before_response)
        self.assertIsNone(caught.exception.status_code)
        self.assertNotIn(FIXTURE_KEY, str(caught.exception))
        self.assertNotIn(FIXTURE_KEY, caught.exception.body_excerpt)

    def test_disconnect_before_headers_is_not_retried(self):
        with LocalImageServer(mode='before_headers') as server:
            with self.assertLogs('storyboard', level='ERROR'):
                with self.assertRaises(EcommerceImageError) as caught:
                    self.generate(server, ratio='9:16', tier='4K', timeout=(1, 1))
        self.assertTrue(caught.exception.failed_before_response)
        self.assertEqual(len(server.calls), 1)

    def test_body_failure_after_headers_is_not_retryable_or_retried(self):
        for mode in ('truncated', 'timeout'):
            with self.subTest(mode=mode):
                with LocalImageServer(mode=mode) as server:
                    with self.assertRaises(EcommerceImageError) as caught:
                        self.generate(server, ratio='9:16', tier='4K', timeout=(1, 0.1))
                self.assertFalse(caught.exception.failed_before_response)
                self.assertEqual(caught.exception.status_code, 200)
                self.assertEqual(len(server.calls), 1)
                self.assertNotIn(FIXTURE_KEY, str(caught.exception))

    def test_source_has_no_hardcoded_key_or_service_url(self):
        source = Path(image_client.__file__).read_text(encoding='utf-8')
        self.assertNotIn('sk-', source)
        self.assertIsNone(re.search(r'https?://', source))


if __name__ == '__main__':
    unittest.main()
