"""Offline storefront profiles. Shell language is independent of app language."""
import re
from dataclasses import dataclass

from core.ecommerce.platforms import all_platforms
from core.i18n_catalog_ecommerce import PREVIEW_LABELS


@dataclass(frozen=True)
class PlatformProfile:
    platform_id: str
    display_name: str
    group: str
    shell: str
    language: str
    currency: str
    primary: str
    labels: dict

    def text(self, source):
        return self.labels[source]

    def price(self, old=False):
        # Static illustrative prices, never persisted as product/business data.
        amount = ('299.00' if old else '199.00') if self.language == 'zh' else ('29.98' if old else '19.99')
        if self.currency == 'Rp':
            amount = '299.000' if old else '199.000'
        elif self.currency in ('₩', '¥') and self.language == 'en':
            amount = '2,998' if old else '1,999'
        return self.currency + ' ' + amount

    def title(self, value):
        value = str(value or '').strip()
        if self.language == 'en' and re.search(r'[\u3400-\u9fff]', value):
            value = self.text('product')
        return value[:79] + '…' if len(value) > 80 else value


def _profiles():
    currencies = {'amazon': '$', 'tiktok': 'US$', 'shopee': '₱', 'lazada': 'Rp',
                  'rakuten': '¥', 'coupang': '₩', 'wildberries': '₽', 'ozon': '₽',
                  'yandex': '₽'}
    result = {}
    for record in all_platforms():
        domestic = record['group'] == 'domestic'
        language = 'zh' if domestic else 'en'
        labels = {key: pair[0 if domestic else 1] for key, pair in PREVIEW_LABELS.items()}
        result[record['id']] = PlatformProfile(
            record['id'], record['name'], record['group'],
            'desktop' if domestic else 'phone', language,
            '¥' if domestic else currencies.get(record['id'], '$'),
            record['primaryColor'], labels)
    return result


PLATFORM_PROFILES = _profiles()


def get_profile(platform):
    key = platform.get('id') if isinstance(platform, dict) else platform
    return PLATFORM_PROFILES.get(key, PLATFORM_PROFILES['taobao'])
