"""Bind existing Fluent controls to the same options used by submission validation."""
from PyQt5.QtCore import QSignalBlocker
from core.model_parameters import model_options


def apply_model_options(model, ratio, resolution, duration, audio=None, seed=None, catalog=None):
    options = model_options(model, catalog)
    for control, choices, preferred in ((ratio, options['ratios'], '16:9'),
                                        (resolution, options['resolutions'], '720p')):
        if not choices:
            blocker = QSignalBlocker(control)
            control.clear()
            del blocker
            control.setEnabled(False)
            control.setToolTip('此模型未提供此参数的可选值')
            continue
        current = control.currentText()
        if current not in choices:
            current = preferred if preferred in choices else ('768p' if '768p' in choices else choices[0])
        blocker = QSignalBlocker(control)
        control.clear()
        for choice in choices:
            # SettingCard reads itemData, so preserve it when rebuilding choices.
            control.addItem(choice, userData=choice)
        control.setCurrentText(current)
        del blocker
        control.setEnabled(len(choices) > 1)
        control.setToolTip('支持：' + ' / '.join(choices))
    allowed = options['durations']
    allowed = sorted(allowed or [])
    duration.setEnabled(bool(allowed))
    if not allowed:
        for control in (audio, seed):
            if control is not None:
                control.setEnabled(False)
        return dict(aspect_ratio=ratio.currentText(), resolution=resolution.currentText(), duration=duration.value())
    current = min(allowed, key=lambda value: (abs(value - duration.value()), -value))
    blocker = QSignalBlocker(duration)
    duration.setRange(allowed[0], allowed[-1])
    duration.setSingleStep(5 if len(allowed) == 3 else 1)
    duration.setKeyboardTracking(False)
    duration.setValue(current)
    duration.setToolTip('仅支持 5 / 10 / 15 秒' if len(allowed) == 3 else f'{allowed[0]}–{allowed[-1]} 秒')
    del blocker
    for control, supported in ((audio, options['audio']), (seed, options['seed'])):
        if control is not None:
            control.setEnabled(supported)
            control.setToolTip('' if supported else '此模型不支持此参数，不会发送')
    return dict(aspect_ratio=ratio.currentText(), resolution=resolution.currentText(), duration=duration.value())
