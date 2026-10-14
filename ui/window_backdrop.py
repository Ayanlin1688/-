"""Native backdrop selection and bounded interactive move/resize transitions."""

import ctypes
from ctypes.wintypes import RECT
import sys


WM_ENTERSIZEMOVE = 0x0231
WM_EXITSIZEMOVE = 0x0232
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_NONE = 1
DWMSBT_TRANSIENTWINDOW = 3
GUI_INMOVESIZE = 0x0002


class AccentPolicy(ctypes.Structure):
    _fields_ = [('state', ctypes.c_int), ('flags', ctypes.c_uint32),
                ('color', ctypes.c_uint32), ('animation', ctypes.c_uint32)]


class CompositionData(ctypes.Structure):
    _fields_ = [('attribute', ctypes.c_int), ('data', ctypes.c_void_p),
                ('size', ctypes.c_size_t)]


class FrameMargins(ctypes.Structure):
    _fields_ = [('left', ctypes.c_int), ('right', ctypes.c_int),
                ('top', ctypes.c_int), ('bottom', ctypes.c_int)]


class GuiThreadInfo(ctypes.Structure):
    _fields_ = [('size', ctypes.c_uint32), ('flags', ctypes.c_uint32),
                ('active', ctypes.c_void_p), ('focus', ctypes.c_void_p),
                ('capture', ctypes.c_void_p), ('menu_owner', ctypes.c_void_p),
                ('move_size', ctypes.c_void_p), ('caret', ctypes.c_void_p),
                ('caret_rect', RECT)]


class WindowsBackdropApi:
    """Injectable ctypes adapter; unsupported APIs fail into a static tint."""

    def __init__(self, build=None, dwmapi=None, user32=None, available=None):
        self.build = (sys.getwindowsversion().build if sys.platform == 'win32' else 0) if build is None else build
        if available is None:
            available = sys.platform == 'win32' or dwmapi is not None or user32 is not None
        self.dwmapi = dwmapi if available else None
        self.user32 = user32 if available else None
        self.frame_extended = False
        if available:
            for name in ('dwmapi', 'user32'):
                if getattr(self, name) is None:
                    try:
                        setattr(self, name, ctypes.WinDLL(name))
                    except (OSError, AttributeError):
                        pass
        if self.dwmapi is not None:
            try:
                self.dwmapi.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                                             ctypes.c_void_p, ctypes.c_uint32]
                self.dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
                self.dwmapi.DwmExtendFrameIntoClientArea.argtypes = [ctypes.c_void_p, ctypes.POINTER(FrameMargins)]
                self.dwmapi.DwmExtendFrameIntoClientArea.restype = ctypes.c_long
            except (OSError, AttributeError):
                self.dwmapi = None
        if self.user32 is not None and hasattr(self.user32, 'SetWindowCompositionAttribute'):
            self.user32.SetWindowCompositionAttribute.argtypes = [ctypes.c_void_p, ctypes.POINTER(CompositionData)]
            self.user32.SetWindowCompositionAttribute.restype = ctypes.c_int
        if self.user32 is not None:
            try:
                self.user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
                self.user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
                self.user32.GetGUIThreadInfo.argtypes = [ctypes.c_uint32, ctypes.POINTER(GuiThreadInfo)]
                self.user32.GetGUIThreadInfo.restype = ctypes.c_int
            except (OSError, AttributeError):
                pass
            try:
                self.user32.RedrawWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
                self.user32.RedrawWindow.restype = ctypes.c_int
            except (OSError, AttributeError):
                pass

    def extend_frame(self, hwnd):
        self.frame_extended = False
        if self.dwmapi is None:
            return False
        try:
            margins = FrameMargins(-1, -1, -1, -1)
            self.frame_extended = self.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins)) == 0
        except (OSError, AttributeError):
            pass
        return self.frame_extended

    def redraw(self, hwnd):
        if self.user32 is None:
            return False
        try:
            return bool(self.user32.RedrawWindow(hwnd, None, None, 0x0001 | 0x0080 | 0x0100 | 0x0400))
        except (OSError, AttributeError):
            return False

    def is_move_resize_active(self, hwnd):
        if self.user32 is None:
            return None
        try:
            thread_id = self.user32.GetWindowThreadProcessId(hwnd, None)
            if not thread_id:
                return None
            info = GuiThreadInfo()
            info.size = ctypes.sizeof(info)
            if not self.user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
                return None
            return bool(info.flags & GUI_INMOVESIZE)
        except (OSError, AttributeError):
            return None

    def _attribute(self, hwnd, attribute, value):
        if self.dwmapi is None:
            return False
        try:
            value = ctypes.c_int(value)
            return self.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0
        except (OSError, AttributeError):
            return False

    def _accent(self, hwnd, state, tint='00000000'):
        if self.user32 is None:
            return False
        try:
            color = int(''.join(tint[index:index + 2] for index in (6, 4, 2, 0)), 16)
            policy = AccentPolicy(state, 0x1e0 if state == 4 else 0, color, 0)
            data = CompositionData(19, ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p), ctypes.sizeof(policy))
            return bool(self.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data)))
        except (OSError, AttributeError, ValueError):
            return False

    def disable(self, hwnd, backend=None):
        if self.build >= 22000 and backend in (None, 'dwm'):
            self._attribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE)
            if self.build < 22523:
                self._attribute(hwnd, 1029, 0)
        if backend in (None, 'legacy'):
            self._accent(hwnd, 0)

    def enable_modern(self, hwnd, dark):
        if self.build < 22000 or self.dwmapi is None:
            return False
        try:
            if not self.extend_frame(hwnd):
                return False
            self._attribute(hwnd, 20, int(dark))
            return self._attribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_TRANSIENTWINDOW)
        except (OSError, AttributeError):
            return False

    def enable_legacy(self, hwnd, tint):
        if self.build < 17763:
            return False
        self.extend_frame(hwnd)
        return self._accent(hwnd, 4, tint)


class WindowBackdrop:
    """Keep DWM continuous; suspend only the legacy compositor during a drag."""

    def __init__(self, hwnd, api):
        self.hwnd = hwnd
        self.api = api
        self.enabled = False
        self.dragging = False
        self.backend = 'off'
        self.settings = None
        self._applied = None
        self._active = False
        self._initialized = False

    @property
    def paint_mode(self):
        if not self.enabled:
            return 'gradient'
        if self.backend == 'dwm' and self._active:
            return 'clear'
        if self.dragging or self.backend == 'tint':
            return 'tint'
        return 'clear'

    def configure(self, enabled, tint, dark):
        settings = (bool(enabled), tint, bool(dark))
        changed = settings != self.settings
        self.settings = settings
        if self.dragging:
            return changed
        if settings == self._applied:
            return False
        self._apply()
        return True

    def bind(self, hwnd):
        if hwnd == self.hwnd:
            return False
        self.hwnd = hwnd
        self._applied = None
        self._initialized = self._active = False
        self.api.frame_extended = False
        return True

    def _apply(self):
        self.enabled = self.settings[0]
        preferred = 'dwm' if self.api.build >= 22000 else 'legacy'
        if not self._initialized or (self._active and (not self.enabled or self.backend != preferred)):
            self.api.disable(self.hwnd, self.backend if self._initialized else None)
        self._initialized = True
        self._active = False
        self.backend = 'off'
        if self.enabled:
            _, tint, dark = self.settings
            if self.api.build >= 22000 and self.api.enable_modern(self.hwnd, dark):
                self.backend = 'dwm'
            else:
                if self.api.build >= 22000:
                    self.api.disable(self.hwnd, 'dwm')
                self.backend = 'legacy' if self.api.enable_legacy(self.hwnd, tint) else 'tint'
            self._active = self.backend in ('dwm', 'legacy')
        self._applied = self.settings

    def begin_drag(self, prepare=None):
        if self.dragging:
            return False
        self.dragging = True
        if self.backend == 'dwm' and self._active:
            return True
        if prepare is not None:
            prepare()
        if self.enabled and self.backend in ('legacy', 'tint'):
            self.api.disable(self.hwnd, self.backend)
            self._active = False
            self._applied = None
        return True

    def end_drag(self):
        if not self.dragging:
            return False
        self.dragging = False
        if self.settings != self._applied:
            self._apply()
        return True

    def close(self):
        if self._active:
            self.api.disable(self.hwnd, self.backend)
        self._active = False
        self.enabled = self.dragging = False
        self.backend = 'off'
