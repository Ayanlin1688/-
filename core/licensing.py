"""离线许可（最小版）：试用期 + 签名激活码校验 + 启动闸门。

激活码格式：YL1.<base64url(payload)>.<base64url(hmac_sha256(payload, SECRET))>
payload JSON：{"customer": "...", "edition": "pro", "expires": "YYYY-MM-DD" 或 ""}

- 校验完全离线；后续可平滑升级为在线激活服务或非对称签名（接口保持不变）。
- 生成工具：scripts/make_license.py（厂商侧使用；最小版为共享密钥方案，
  对外发布前建议升级：私钥只留在厂商工具、应用内仅放公钥验签）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import date, timedelta

KEY_PREFIX = 'YL1'
DEFAULT_TRIAL_DAYS = 14

# 最小版共享密钥：仅用于离线验证；正式对外发布前建议升级为非对称签名或在线激活。
_VERIFY_SECRET = b'yanlin-smart-creation-matrix.license.v1'


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def _unb64(text: str) -> bytes:
    padded = text + '=' * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode())


def _sign(payload: bytes) -> str:
    return _b64(hmac.new(_VERIFY_SECRET, payload, hashlib.sha256).digest())


def make_key(customer: str, edition: str = 'pro', expires: str = '') -> str:
    """生成激活码（厂商工具；expires 为空表示不过期）。"""
    payload = json.dumps({'customer': customer, 'edition': edition, 'expires': expires},
                         ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return f'{KEY_PREFIX}.{_b64(payload)}.{_sign(payload)}'


def parse_key(key: str) -> dict:
    """解析并校验激活码；非法时抛出 ValueError（中文原因）。"""
    text = (key or '').strip().replace(' ', '')
    parts = text.split('.')
    if len(parts) != 3 or parts[0] != KEY_PREFIX:
        raise ValueError('激活码格式不正确')
    try:
        payload = _unb64(parts[1])
        signature = parts[2]
    except Exception:
        raise ValueError('激活码格式不正确')
    if not hmac.compare_digest(_sign(payload), signature):
        raise ValueError('激活码校验失败（签名不匹配）')
    try:
        data = json.loads(payload.decode('utf-8'))
    except ValueError:
        raise ValueError('激活码内容无法解析')
    if not isinstance(data, dict) or not str(data.get('customer') or '').strip():
        raise ValueError('激活码内容不完整')
    expires = str(data.get('expires') or '')
    if expires:
        try:
            limit = date.fromisoformat(expires)
        except ValueError:
            raise ValueError('激活码有效期格式不正确')
        if date.today() > limit:
            raise ValueError(f'激活码已于 {expires} 到期')
    return {'customer': str(data['customer']), 'edition': str(data.get('edition') or 'pro'), 'expires': expires}


def license_status(config, today=None) -> dict:
    """计算许可状态：active / trial / expired / invalid。

    首次调用会写入 license.trial_started（调用方负责在变更后保存配置）。
    """
    today = today or date.today()
    license_config = config.setdefault('license', {})
    key = str(license_config.get('key') or '').strip()
    if key:
        try:
            data = parse_key(key)
            suffix = f'（有效期至 {data["expires"]}）' if data['expires'] else '（永久）'
            return {'state': 'active', 'days_left': None, 'detail': f'已激活：{data["customer"]}{suffix}'}
        except ValueError as error:
            return {'state': 'invalid', 'days_left': None, 'detail': str(error)}
    started = str(license_config.get('trial_started') or '').strip()
    if not started:
        license_config['trial_started'] = today.isoformat()
        started = license_config['trial_started']
    try:
        start = date.fromisoformat(started)
    except ValueError:
        start = today
        license_config['trial_started'] = today.isoformat()
    try:
        days = int(license_config.get('trial_days') or DEFAULT_TRIAL_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_TRIAL_DAYS
    left = (start + timedelta(days=days) - today).days
    if left >= 0:
        return {'state': 'trial', 'days_left': left, 'detail': f'试用中：剩余 {left} 天（共 {days} 天）'}
    return {'state': 'expired', 'days_left': 0, 'detail': f'试用已结束（共 {days} 天），请激活后继续使用'}


def gate_block(config, today=None):
    """启动闸门：返回拦截原因字符串；None 表示放行。

    仅当 license.enforce 为真且处于 expired/invalid 时拦截；
    默认演示模式（enforce=False）只提示不拦截。
    """
    license_config = config.get('license') or {}
    if not license_config.get('enforce', False):
        return None
    status = license_status(config, today=today)
    if status['state'] in {'expired', 'invalid'}:
        return status['detail']
    return None
