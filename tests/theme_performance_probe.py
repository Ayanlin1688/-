"""Run the unchanged click/theme budgets with one isolated QApplication."""
from tests.test_theme_consistency import ThemeConsistencyTests


def main():
    case = ThemeConsistencyTests('test_switch_and_theme_latency_without_growth')
    case.setUpClass()
    case.setUp()
    try:
        case.measure_switch_and_theme_latency_without_growth()
    finally:
        case.tearDown()


if __name__ == '__main__':
    main()
