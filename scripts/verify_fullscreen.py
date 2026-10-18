"""Interactive Windows smoke test using a short-lived, separate test window."""
import ctypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from scnet_widget.widget import Options, Widget, enable_dpi_awareness


def test_window():
    root = tk.Tk()
    root.title("Fullscreen detection test")
    root.configure(bg="#182231")
    tk.Label(root, text="Fullscreen detection test\nThis window closes automatically.",
             fg="white", bg="#182231", font=("Segoe UI", 20)).pack(expand=True)
    commands = queue.Queue()
    def read():
        for line in sys.stdin: commands.put(line.strip())
        commands.put("exit")
    threading.Thread(target=read, daemon=True).start()
    def poll():
        try:
            command = commands.get_nowait()
        except queue.Empty:
            root.after(20, poll)
            return
        if command == "exit":
            root.destroy()
            return
        root.attributes("-fullscreen", False)
        root.overrideredirect(False)
        root.state("normal")
        root.geometry("600x400+50+50")
        if command == "full":
            root.attributes("-fullscreen", True)
        elif command == "borderless":
            root.overrideredirect(True)
            root.geometry(f"{root.winfo_screenwidth()}x{root.winfo_screenheight()}+0+0")
        elif command == "maximized":
            root.state("zoomed")
        root.update_idletasks()
        root.focus_force()
        if command == "minimized":
            root.iconify()
        root.after(250, lambda: print(command, flush=True))
        root.after(20, poll)
    root.after(20, poll)
    root.after(30000, root.destroy)  # Always close, even if the parent test fails.
    root.mainloop()


def main():
    if os.name != "nt":
        raise SystemExit("This smoke test needs an interactive Windows desktop.")
    enable_dpi_awareness()
    if "--test-window" in sys.argv:
        test_window()
        return
    with tempfile.TemporaryDirectory() as directory:
        root = tk.Tk()
        options = Options(Path(directory)/"unused.toml", always_on_top=True)
        app = Widget(root, SimpleNamespace(previous=None), options, Path(directory), autostart=False)
        root.update()
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user.GetAncestor.restype = ctypes.c_void_p
        user.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user.GetWindowLongW.restype = ctypes.c_long
        handle = user.GetAncestor(root.winfo_id(), 2)
        child = subprocess.Popen([sys.executable, __file__, "--test-window"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        replies = queue.Queue()
        def collect():
            for line in child.stdout: replies.put(line.strip())
        threading.Thread(target=collect, daemon=True).start()
        results = []
        try:
            for command, expected in (("normal", True), ("full", False),
                                      ("normal", True), ("maximized", True),
                                      ("borderless", False), ("minimized", True)):
                child.stdin.write(command+"\n")
                child.stdin.flush()
                assert replies.get(timeout=5) == command
                deadline = time.monotonic()+4
                while time.monotonic() < deadline:
                    root.update()
                    native_topmost = bool(user.GetWindowLongW(handle, -20) & 8)
                    if app.topmost.effective == expected and native_topmost == expected:
                        break
                    time.sleep(.02)
                else:
                    raise AssertionError(f"{command}: expected topmost={expected}, actual={app.topmost.effective}")
                results.append({"window": command, "topmost": native_topmost})
            options.always_on_top = False
            app.topmost.update(False)
            root.update()
            assert not bool(user.GetWindowLongW(handle, -20) & 8)
            results.append({"preference_disabled": True, "topmost": False})
            print(json.dumps({"verified": True, "transitions": results}))
        finally:
            try:
                child.stdin.write("exit\n")
                child.stdin.flush()
                child.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                child.kill()
                child.wait()
            app.close()


if __name__ == "__main__":
    main()
