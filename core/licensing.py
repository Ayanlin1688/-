"""离线许可：试用期 + 激活码校验 + 启动闸门。

激活码格式（两种）：
- YL2（推荐，非对称）：YL2.<base64url(payload)>.<base64url(rsa_pkcs1v15_sha256(payload))>
  私钥仅保留在厂商工具；应用内嵌公钥验签，第三方无法伪造。
- YL1（遗留，共享密钥）：YL1.<base64url(payload)>.<base64url(hmac_sha256(payload, SECRET))>
  仅供演示/兼容；对外发码请使用 --private-key 生成 YL2。
payload JSON：{"customer": "...", "edition": "pro", "expires": "YYYY-MM-DD" 或 ""}

- 校验完全离线；后续可平滑升级为在线激活服务（接口保持不变）。
- 发码工具：scripts/make_license.py --private-key <私钥.pem>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
from datetime import date, timedelta

KEY_PREFIX = 'YL1'
RSA_PREFIX = 'YL2'
DEFAULT_TRIAL_DAYS = 14

# YL2 验签公钥（2026-09 生成；私钥仅保留在厂商侧，不入仓库）。
_VERIFY_PUBLIC_PEM = """-----BEGIN PUBLIC KEY-----
MIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEAvaed6WOUWl4tP/5smtM6
fGh+OILl5brlrex+9uLqZVbibMTKYUu74HXlQn/w+gtRzs09GEI+wzMmMs9lCy0M
2J6L1dvyddkA5mjLPJNxhrrM0PgwPpw65HJisczCZw9QFBXbGNGjYrBxisFbMRlc
e9qOtr67VbGvEdf/OGAkhr5TUjiPBo+v4RuxpSOQDLufZVWjK1fwBQk8hmpi4FgR
UazzSsw/+xse4i40kAqFCiMi1RoBI8IsBzMzTFyfC6m3KddYnVRNOziFc38WaPVz
LP1JNkbU4MbU0ytS6wg+90v14osDklET+uZhyCBLoz/G4QLBsqPTW+4TPA6MdSYC
fiIRDtPCBaHudDmwmxJrBAcdUY5Sh72RWaFBEU4BZ80BaGHdnezXnnBsqlGOzj7W
xlQw2AHp75Tl1S5CYzt3mc+i6dfJbsCKTpNlsUVqCXv5Vlk6MNLOlBBq+QAZf8mu
jrhyPiJ4SFv2GLXqqMqbPv+qVB1g3a/tRJYQhXR/jSN1AgMBAAE=
-----END PUBLIC KEY-----"""

# 最小版共享密钥：仅用于离线验证；正式对外发布前建议升级为非对称签名或在线激活。
_VERIFY_SECRET = b'yanlin-smart-creation-matrix.license.v1'


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def _unb64(text: str) -> bytes:
    padded = text + '=' * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode())


def _sign(payload: bytes) -> str:
    return _b64(hmac.new(_VERIFY_SECRET, payload, hashlib.sha256).digest())


def _legacy_hmac_allowed() -> bool:
    """YL1（共享密钥演示格式）仅在非冻结环境（源码运行/测试）可用。

    发行版（PyInstaller 冻结产物）只接受 YL2 非对称签名激活码：共享密钥内置在
    发行包中，任何人都可据此自签，必须只保留在开发/演示链路。
    """
    return not getattr(sys, 'frozen', False)


def _verify_rsa(payload: bytes, signature: bytes, public_pem: str | None = None) -> bool:
    """用内嵌公钥验证 RSA PKCS#1 v1.5 (SHA-256) 签名；任何异常按验证失败处理。"""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        public_key = serialization.load_pem_public_key((public_pem or _VERIFY_PUBLIC_PEM).encode('utf-8'))
        public_key.verify(signature, payload, padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def make_key_asymmetric(customer: str, edition: str = 'pro', expires: str = '', private_key_pem=b'') -> str:
    """生成 YL2 非对称签名激活码（厂商工具使用；应用侧不需要此函数）。"""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    payload = json.dumps({'customer': customer, 'edition': edition, 'expires': expires},
                         ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    raw = private_key_pem if isinstance(private_key_pem, bytes) else str(private_key_pem).encode('utf-8')
    private_key = serialization.load_pem_private_key(raw, password=None)
    signature = private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    return f'{RSA_PREFIX}.{_b64(payload)}.{_b64(signature)}'


def make_key(customer: str, edition: str = 'pro', expires: str = '') -> str:
    """生成激活码（厂商工具；expires 为空表示不过期）。"""
    payload = json.dumps({'customer': customer, 'edition': edition, 'expires': expires},
                         ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return f'{KEY_PREFIX}.{_b64(payload)}.{_sign(payload)}'


def parse_key(key: str) -> dict:
    """解析并校验激活码；非法时抛出 ValueError（中文原因）。"""
    text = (key or '').strip().replace(' ', '')
    try:
        text.encode('ascii')
    except UnicodeEncodeError:
        # 非 ASCII（粘贴带入中文/全角字符）会让 hmac.compare_digest 抛 TypeError 逃逸，此处统一转为可提示的 ValueError。
        raise ValueError('激活码包含无法识别的字符，请检查是否混入了中文或全角符号')
    parts = text.split('.')
    if len(parts) != 3 or parts[0] not in {KEY_PREFIX, RSA_PREFIX}:
        raise ValueError('激活码格式不正确')
    try:
        payload = _unb64(parts[1])
    except Exception:
        raise ValueError('激活码格式不正确')
    if parts[0] == RSA_PREFIX:
        try:
            signature = _unb64(parts[2])
        except Exception:
            raise ValueError('激活码格式不正确')
        if not _verify_rsa(payload, signature):
            raise ValueError('激活码校验失败（签名不匹配）')
    else:
        if not _legacy_hmac_allowed():
            raise ValueError('此激活码为旧版演示格式，正式版已不再支持；请联系供应商获取新版激活码')
        if not hmac.compare_digest(_sign(payload), parts[2]):
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
