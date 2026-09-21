"""i18n 护栏：ui 层未接入 tr() 的中文字面量不得超过基线（只准降、不准升）。"""
import ast
import re
import unittest
from pathlib import Path

CJK = re.compile(r'[\u4e00-\u9fff]')
UI_ROOT = Path(__file__).resolve().parent.parent / 'ui'
# 2026-09-21 基线（设置页等分批翻译后请下调此值）。
BASELINE = 515


def _count_file(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    doc_ids, tr_ids = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                doc_ids.add(id(node.body[0].value))
        if isinstance(node, ast.Call):
            name = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)
            if name == 'tr':
                for arg in node.args:
                    if isinstance(arg, ast.Constant):
                        tr_ids.add(id(arg))
    return sum(
        1 for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in doc_ids and id(node) not in tr_ids
        and CJK.search(node.value))


class I18nRatchetTests(unittest.TestCase):
    def test_raw_cjk_literals_under_baseline(self):
        total = sum(_count_file(path) for path in sorted(UI_ROOT.rglob('*.py')))
        self.assertLessEqual(
            total, BASELINE,
            f'ui 层未接线中文字面量增加：{total} > {BASELINE}；新增界面文案请使用 tr() 并补 i18n_catalog 词条')


if __name__ == '__main__':
    unittest.main()
