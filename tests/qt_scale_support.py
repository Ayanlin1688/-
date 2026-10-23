"""Explicit teardown and layout settling for shared-QApplication tests."""
from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QElapsedTimer, QEvent, QEventLoop
from PyQt5.QtWidgets import QLayout

from ui import tokens
from ui.responsive import CoalescedCall


def reset_interface_scaling(app):
    scaler = getattr(app, '_interface_scaler', None)
    if scaler is not None and not sip.isdeleted(scaler):
        scaler.dispose()
    tokens.set_interface_scale(1.0)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents(QEventLoop.AllEvents, 50)


def flush_layout(app, root):
    previous = None
    deadline = QElapsedTimer()
    deadline.start()
    # processEvents may return immediately; wait for the idle predicate instead
    # of exhausting a fixed number of turns before delayed layout events run.
    while deadline.elapsed() < 5000:
        app.processEvents(QEventLoop.AllEvents, 50)
        scaler = getattr(app, '_interface_scaler', None)
        if scaler is not None and scaler.pending:
            scaler._refresh_pending()
        calls = root.findChildren(CoalescedCall)
        for call in calls:
            if call.timer.isActive():
                call.flush()
        layouts = root.findChildren(QLayout)
        for layout in layouts:
            layout.activate()
        QCoreApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        geometry = tuple((layout.geometry().getRect(), getattr(layout, 'columns', None)) for layout in layouts)
        scale_pending = scaler is not None and (scaler.pending or scaler.timer.isActive())
        if geometry == previous and not scale_pending and not any(call.timer.isActive() for call in calls):
            return
        previous = geometry
    raise AssertionError('layout did not settle after explicit flushes')
