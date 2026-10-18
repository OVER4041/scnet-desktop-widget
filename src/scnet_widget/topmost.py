"""Temporarily yield topmost status to a foreground fullscreen app on Windows."""
import os


def covers_monitor(client: tuple[int, int, int, int],
                   monitor: tuple[int, int, int, int], tolerance: int = 2) -> bool:
    """Compare screen coordinates, including monitors left/above the primary."""
    left, top, right, bottom = client
    ml, mt, mr, mb = monitor
    return (right > left and bottom > top and mr > ml and mb > mt
            and left <= ml+tolerance and top <= mt+tolerance
            and right >= mr-tolerance and bottom >= mb-tolerance)


class FullscreenDetector:
    def __init__(self):
        self.user = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        self.ctypes, self.types, self.MonitorInfo = ctypes, wintypes, MonitorInfo
        user = ctypes.WinDLL("user32", use_last_error=True)
        signatures = {
            "GetForegroundWindow": ([], wintypes.HWND),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetClientRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "ClientToScreen": ([wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "MonitorFromWindow": ([wintypes.HWND, wintypes.DWORD], wintypes.HANDLE),
            "GetMonitorInfoW": ([wintypes.HANDLE, ctypes.POINTER(MonitorInfo)], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(user, name)
            function.argtypes, function.restype = arguments, result
        self.user = user

    def __call__(self) -> bool | None:
        """None means a transient API/activation failure; keep the previous state."""
        if self.user is None:
            return False
        c, types, user = self.ctypes, self.types, self.user
        window = user.GetForegroundWindow()
        if not window:
            return None
        if not user.IsWindowVisible(window) or user.IsIconic(window):
            return False
        process = types.DWORD()
        if not user.GetWindowThreadProcessId(window, c.byref(process)):
            return None
        if process.value == os.getpid():
            return False  # The widget and its own dialogs are not fullscreen apps.
        name = c.create_unicode_buffer(256)
        user.GetClassNameW(window, name, len(name))
        if name.value in {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}:
            return False
        rect = types.RECT()
        if not user.GetClientRect(window, c.byref(rect)):
            return None
        origin, corner = types.POINT(rect.left, rect.top), types.POINT(rect.right, rect.bottom)
        if not (user.ClientToScreen(window, c.byref(origin))
                and user.ClientToScreen(window, c.byref(corner))):
            return None
        monitor = user.MonitorFromWindow(window, 2)  # MONITOR_DEFAULTTONEAREST
        info = self.MonitorInfo()
        info.cbSize = c.sizeof(info)
        if not monitor or not user.GetMonitorInfoW(monitor, c.byref(info)):
            return None
        if user.GetForegroundWindow() != window:
            return None
        screen = info.rcMonitor
        return covers_monitor((origin.x, origin.y, corner.x, corner.y),
                              (screen.left, screen.top, screen.right, screen.bottom))


class TopmostController:
    def __init__(self, setter, detector=None):
        self.setter = setter
        self.detector = detector if detector is not None else FullscreenDetector()
        self.suspended = False
        self.effective = None

    def update(self, preferred: bool):
        try:
            fullscreen = self.detector()
        except OSError:
            fullscreen = None
        if fullscreen is not None:
            self.suspended = bool(fullscreen)
        desired = bool(preferred) and not self.suspended
        if desired != self.effective:
            self.setter(desired)
            self.effective = desired
