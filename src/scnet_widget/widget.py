"""A small, independently running Tk desktop window for the SSH/Slurm bridge."""
import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
import traceback
import tkinter as tk
from tkinter import messagebox, ttk

from .config import BridgeError, Settings
from .monitor import (Job, PENDING, RUNNING, TERMINAL, SlurmMonitor, Snapshot,
                      duration_label, pending_seconds, project_totals)

BG = "#101722"
CARD = "#182231"
LINE = "#29364a"
TEXT = "#edf3fc"
MUTED = "#8e9eb5"
GREEN = "#67d9ad"
BLUE = "#83b7ff"
AMBER = "#f2c47c"
RED = "#ff999d"
FONT = "Microsoft YaHei UI"
WIDTH = 408


def enable_dpi_awareness():
    if os.name == "nt":
        import ctypes
        try:
            function = ctypes.windll.user32.SetProcessDpiAwarenessContext
            function.argtypes = [ctypes.c_void_p]
            function.restype = ctypes.c_bool
            function(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


@dataclass
class Options:
    config: Path
    cluster: str = "scnet"
    refresh_seconds: int = 60
    always_on_top: bool = True
    opacity: float = 0.97
    watched_jobs: list[str] = field(default_factory=list)
    job_logs: dict[str, str] = field(default_factory=dict)
    ledger: Path | None = None

    @classmethod
    def load(cls, path: Path | None, config: Path | None = None, cluster: str | None = None):
        data = json.loads(path.read_text("utf8")) if path else {}
        base = path.resolve().parent if path else Path.cwd()
        server_config = config or base / data.get("server_config", "config.toml")
        ledger = base / data["project_ledger"] if data.get("project_ledger") else None
        interval = int(data.get("refresh_seconds", 60))
        if not 30 <= interval <= 3600:
            raise BridgeError("刷新间隔应在 30 到 3600 秒之间")
        return cls(server_config.resolve(), cluster or data.get("cluster", "scnet"), interval,
                   bool(data.get("always_on_top", True)),
                   max(0.8, min(1.0, float(data.get("opacity", .97)))),
                   [str(value) for value in data.get("watched_jobs", [])],
                   dict(data.get("job_logs", {})), ledger.resolve() if ledger else None)


def memory_label(value: str) -> str:
    match = re.fullmatch(r"([\d.]+)([KMGTP]?)[nc]?", value, re.I)
    if not match:
        return value or "—"
    number, unit = float(match[1]), match[2].upper()
    gib = number * {"": 1/1024, "K": 1/1024**2, "M": 1/1024, "G": 1, "T": 1024, "P": 1024**2}[unit]
    return f"{gib:g} GiB" if gib >= 1 else f"{gib*1024:g} MiB"


def save_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf8")
    temporary.replace(path)


class Widget:
    def __init__(self, root: tk.Tk, monitor: SlurmMonitor, options: Options,
                 state_dir: Path, *, autostart: bool = True):
        self.root, self.monitor, self.options = root, monitor, options
        key = hashlib.sha256((str(options.config)+options.cluster).encode()).hexdigest()[:10]
        self.cache = state_dir / f"widget-{key}-snapshot.json"
        self.preferences = state_dir / f"widget-{key}-preferences.json"
        self.snapshot = None
        self.online = False
        self.busy = False
        self.closed = False
        self.error = ""
        self.focus = options.watched_jobs[0] if options.watched_jobs else None
        self.manual_focus = False
        self.messages = queue.Queue()
        self.after_refresh = None
        self.compact = True
        self.notifications = set()
        self.notification_text = ""
        self.notification_until = 0
        self._drag = None
        self._rows = ()
        try:
            preferences = json.loads(self.preferences.read_text("utf8"))
            options.always_on_top = bool(preferences.get("always_on_top", options.always_on_top))
            options.refresh_seconds = max(30, min(3600, int(preferences.get("refresh_seconds", options.refresh_seconds))))
            options.opacity = max(.8, min(1., float(preferences.get("opacity", options.opacity))))
            self.compact = bool(preferences.get("compact", self.compact))
            self.notifications = set(preferences.get("reported_terminal", []))
        except (OSError, ValueError, TypeError):
            preferences = {}
        root.title(f"超算任务监视 · {options.cluster}")
        root.configure(bg=LINE)
        root.overrideredirect(True)
        root.attributes("-topmost", options.always_on_top)
        root.attributes("-alpha", options.opacity)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda event: self.close())
        self.shell = tk.Frame(root, bg=BG)
        self.shell.pack(fill="both", expand=True, padx=1, pady=1)
        self.build()
        if self.compact:
            self.body.pack_forget()
            self.compact_bar.pack(fill="x", padx=18, pady=(0, 12), before=self.footer)
            self.fold_button.configure(text="展开")
        root.update_idletasks()
        x = max(0, min(root.winfo_screenwidth()-WIDTH, int(preferences.get("x", root.winfo_screenwidth()-WIDTH-24))))
        y = max(0, min(root.winfo_screenheight()-100, int(preferences.get("y", 72))))
        root.geometry(f"{WIDTH}x{root.winfo_reqheight()}+{x}+{y}")
        try:
            self.snapshot = Snapshot.from_dict(json.loads(self.cache.read_text("utf8")))
            self.monitor.previous = self.snapshot
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.paint()
        root.after(150, self.receive)
        root.after(1000, self.tick)
        if autostart:
            root.after(200, self.refresh)

    def label(self, parent, text="", *, color=TEXT, size=10, bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=(FONT, size, "bold" if bold else "normal"), **kwargs)

    def button(self, parent, text, command, *, width=3):
        return tk.Button(parent, text=text, command=command, bg=BG, fg=MUTED,
            activebackground=LINE, activeforeground=TEXT, relief="flat", bd=0,
            highlightthickness=0, font=(FONT, 10), width=width, cursor="hand2", takefocus=False)

    def build(self):
        top = tk.Frame(self.shell, bg=BG)
        top.pack(fill="x", padx=16, pady=(12, 0))
        self.dot = tk.Canvas(top, width=13, height=14, bg=BG, highlightthickness=0)
        self.dot.create_oval(2, 4, 9, 11, fill=AMBER, outline="", tags="dot")
        self.dot.pack(side="left", padx=(0, 7))
        title = self.label(top, "超算任务", size=12, bold=True)
        title.pack(side="left")
        self.button(top, "×", self.close).pack(side="right")
        self.fold_button = self.button(top, "收起", self.toggle_compact, width=4)
        self.fold_button.pack(side="right")
        self.button(top, "⚙", self.settings).pack(side="right")
        self.refresh_button = self.button(top, "↻", self.refresh)
        self.refresh_button.pack(side="right")
        for item in (top, title, self.dot):
            item.bind("<ButtonPress-1>", self.drag_start)
            item.bind("<B1-Motion>", self.drag_move)
            item.bind("<ButtonRelease-1>", lambda event: self.save_preferences())
        self.connection = self.label(self.shell, "正在连接超算…", color=MUTED, size=9, anchor="w")
        self.connection.pack(fill="x", padx=18, pady=(6, 12))
        counters = tk.Frame(self.shell, bg=BG)
        counters.pack(fill="x", padx=12)
        self.counter_labels = {}
        for index, (key, caption, color) in enumerate((("running", "运行", GREEN), ("pending", "排队", AMBER),
                                                       ("completed", "今日完成", BLUE), ("failed", "今日异常", RED))):
            counters.columnconfigure(index, weight=1)
            item = tk.Frame(counters, bg=BG)
            item.grid(row=0, column=index, sticky="ew")
            number = self.label(item, "—", color=color, size=23, bold=True)
            number.pack()
            self.label(item, caption, color=MUTED, size=9).pack(pady=(0, 10))
            self.counter_labels[key] = number
        self.compact_bar = tk.Frame(self.shell, bg=BG)
        self.compact_job = self.label(self.compact_bar, "当前作业", color=MUTED, size=9)
        self.compact_job.pack(side="left")
        self.compact_timer = tk.Label(self.compact_bar, text="--:--:--", bg=BG, fg=TEXT,
                                      font=("Consolas", 19, "bold"))
        self.compact_timer.pack(side="right")
        self.body = tk.Frame(self.shell, bg=BG)
        self.body.pack(fill="both", expand=True)
        focus_card = tk.Frame(self.body, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        focus_card.pack(fill="x", padx=15, pady=(4, 14))
        inner = tk.Frame(focus_card, bg=CARD)
        inner.pack(fill="x", padx=14, pady=12)
        focus_top = tk.Frame(inner, bg=CARD)
        focus_top.pack(fill="x")
        self.focus_id = self.label(focus_top, "当前关注", color=MUTED, size=9)
        self.focus_id.pack(side="left")
        self.focus_state = self.label(focus_top, "—", color=GREEN, size=9, bold=True)
        self.focus_state.pack(side="right")
        self.focus_name = self.label(inner, "等待作业数据", size=11, bold=True, anchor="w", wraplength=340, justify="left")
        self.focus_name.pack(fill="x", pady=(9, 0))
        self.timer = tk.Label(inner, text="--:--:--", font=("Consolas", 32, "bold"), bg=CARD, fg=TEXT, anchor="w")
        self.timer.pack(fill="x", pady=(5, 0))
        self.timer_caption = self.label(inner, "已运行", color=MUTED, size=9, anchor="w")
        self.timer_caption.pack(fill="x")
        self.resources = self.label(inner, "—", color=MUTED, size=9, anchor="w", wraplength=340, justify="left")
        self.resources.pack(fill="x", pady=(11, 11))
        progress_head = tk.Frame(inner, bg=CARD)
        progress_head.pack(fill="x")
        self.progress_caption = self.label(progress_head, "等待计算日志", size=9, color=BLUE)
        self.progress_caption.pack(side="left")
        self.progress_value = self.label(progress_head, "", size=9, color=MUTED)
        self.progress_value.pack(side="right")
        self.progress = tk.Canvas(inner, width=340, height=6, bg=LINE, highlightthickness=0)
        self.progress.pack(fill="x", pady=(8, 0))
        self.progress_note = self.label(inner, "", size=8, color=MUTED, anchor="w")
        self.progress_note.pack(fill="x", pady=(6, 0))
        table_head = tk.Frame(self.body, bg=BG)
        table_head.pack(fill="x", padx=18)
        self.label(table_head, "作业列表", size=10, bold=True).pack(side="left")
        self.active_count = self.label(table_head, "", size=9, color=MUTED)
        self.active_count.pack(side="right")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("HPC.Treeview", background=BG, fieldbackground=BG, foreground=TEXT,
                        borderwidth=0, rowheight=27, font=(FONT, 9))
        style.configure("HPC.Treeview.Heading", background=BG, foreground=MUTED,
                        borderwidth=0, font=(FONT, 8))
        style.map("HPC.Treeview", background=[("selected", LINE)], foreground=[("selected", TEXT)])
        style.layout("HPC.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure("HPC.Vertical.TScrollbar", background=LINE, troughcolor=BG,
                        bordercolor=BG, arrowcolor=MUTED, lightcolor=LINE, darkcolor=LINE)
        table_frame = tk.Frame(self.body, bg=BG)
        table_frame.pack(fill="both", padx=15, pady=(6, 10))
        self.tree = ttk.Treeview(table_frame, columns=("job", "state", "elapsed"), show="headings",
                                 height=4, selectmode="browse", style="HPC.Treeview")
        for name, caption, width in (("job", "作业号", 110), ("state", "状态", 102), ("elapsed", "已用时间", 115)):
            self.tree.heading(name, text=caption, anchor="w")
            self.tree.column(name, width=width, anchor="w", stretch=True)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview, style="HPC.Vertical.TScrollbar")
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self.select_job)
        totals = tk.Frame(self.body, bg=CARD)
        totals.pack(fill="x", padx=15, pady=(0, 12))
        self.label(totals, "本轮累计 · 含失败与续算", color=MUTED, size=8, anchor="w").pack(fill="x", padx=12, pady=(10, 6))
        values = tk.Frame(totals, bg=CARD)
        values.pack(fill="x", padx=12, pady=(0, 10))
        self.total_time = self.label(values, "—", size=12, bold=True)
        self.total_time.pack(side="left")
        self.core_hours = self.label(values, "— 核时", color=MUTED, size=9)
        self.core_hours.pack(side="right")
        self.footer = self.label(self.shell, "等待首次同步", color=MUTED, size=8, anchor="w", cursor="hand2")
        self.footer.pack(fill="x", padx=18, pady=(0, 12))
        self.footer.bind("<Button-1>", lambda event: self.show_error() if self.error else self.refresh())

    def drag_start(self, event):
        self._drag = (event.x_root-self.root.winfo_x(), event.y_root-self.root.winfo_y())

    def drag_move(self, event):
        if self._drag:
            x = max(0, min(self.root.winfo_screenwidth()-80, event.x_root-self._drag[0]))
            y = max(0, min(self.root.winfo_screenheight()-50, event.y_root-self._drag[1]))
            self.root.geometry(f"+{x}+{y}")

    def save_preferences(self):
        try:
            save_json(self.preferences, {"x": self.root.winfo_x(), "y": self.root.winfo_y(),
                "always_on_top": self.options.always_on_top, "opacity": self.options.opacity,
                "refresh_seconds": self.options.refresh_seconds, "compact": self.compact,
                "reported_terminal": sorted(self.notifications)[-200:]})
        except OSError:
            pass

    def toggle_compact(self):
        self.compact = not self.compact
        if self.compact:
            self.body.pack_forget()
            self.compact_bar.pack(fill="x", padx=18, pady=(0, 12), before=self.footer)
        else:
            self.compact_bar.pack_forget()
            self.body.pack(fill="both", expand=True, before=self.footer)
        self.fold_button.configure(text="展开" if self.compact else "收起")
        self.root.update_idletasks()
        self.root.geometry(f"{WIDTH}x{self.root.winfo_reqheight()}")
        self.save_preferences()

    def select_job(self, event=None):
        selected = self.tree.selection()
        if selected and selected[0] != self.focus:
            self.manual_focus = True
            self.focus = selected[0]
            self.paint()
            self.refresh()

    def refresh(self):
        if self.closed or self.busy:
            return
        if self.after_refresh:
            self.root.after_cancel(self.after_refresh)
            self.after_refresh = None
        self.busy = True
        self.refresh_button.configure(fg=BLUE)
        watched = list(dict.fromkeys(self.options.watched_jobs + ([self.focus] if self.focus else [])))
        focus = self.focus
        log_path = self.options.job_logs.get(focus)
        follow_active = not self.manual_focus
        def work():
            try:
                result = asyncio.run(self.monitor.fetch(watched, log_path, log_job_id=focus, follow_active=follow_active))
                self.messages.put((result, ""))
            except Exception as exc:
                self.messages.put((None, str(exc)))
        threading.Thread(target=work, daemon=True, name="Slurm-status-poll").start()

    def receive(self):
        if self.closed:
            return
        try:
            snapshot, error = self.messages.get_nowait()
        except queue.Empty:
            self.root.after(150, self.receive)
            return
        self.busy = False
        self.refresh_button.configure(fg=MUTED)
        self.error = error
        if snapshot is not None:
            previous = {job.job_id: job for job in self.snapshot.jobs} if self.snapshot else {}
            for job in snapshot.jobs:
                old = previous.get(job.job_id)
                event_key = job.job_id+":"+job.state
                if old and old.state not in TERMINAL and job.state in TERMINAL and event_key not in self.notifications:
                    self.notifications.add(event_key)
                    self.notification_text = f"作业 {job.job_id}：{job.state_label}"
                    self.notification_until = time.monotonic()+30
                    if os.name == "nt":
                        import winsound
                        winsound.MessageBeep(winsound.MB_ICONASTERISK)
            self.snapshot = snapshot
            if not self.manual_focus and snapshot.progress.get("job_id"):
                self.focus = snapshot.progress["job_id"]
            self.online = True
            try:
                save_json(self.cache, snapshot.to_dict())
            except OSError:
                pass
            self.save_preferences()
        else:
            self.online = False
        self.paint()
        self.after_refresh = self.root.after(self.options.refresh_seconds*1000, self.refresh)
        self.root.after(150, self.receive)

    def focus_job(self):
        if not self.snapshot:
            return None
        jobs = self.snapshot.jobs
        chosen = next((job for job in jobs if job.job_id == self.focus), None)
        if chosen and not chosen.in_queue and not self.manual_focus:
            chosen = next((job for job in jobs if job.in_queue and job.state in RUNNING), chosen)
            self.focus = chosen.job_id
        if not chosen:
            chosen = next((job for job in jobs if job.in_queue), None)
            chosen = chosen or (sorted(jobs, key=lambda job: job.ended, reverse=True)[0] if jobs else None)
            self.focus = chosen.job_id if chosen else None
        return chosen

    def paint(self):
        if self.closed:
            return
        snap = self.snapshot
        recent = snap is not None and self.online and time.monotonic()-snap.received_monotonic <= self.options.refresh_seconds*2+30
        advance = max(0, time.monotonic()-snap.received_monotonic) if recent else 0
        color = GREEN if recent else (RED if self.error else AMBER)
        self.dot.itemconfigure("dot", fill=color)
        if not snap:
            self.connection.configure(text="连接失败 · 点击底部查看原因" if self.error else "正在连接超算…", fg=color)
            self.footer.configure(text="点击查看连接详情" if self.error else "等待首次同步", fg=MUTED)
            return
        counts = snap.counts()
        tag = "在线" if recent else ("连接中 · 上次记录" if not self.error else "连接中断 · 上次记录")
        if self.online and not recent:
            tag = "数据已过期 · 等待同步"
        self.connection.configure(text=f"{self.options.cluster.upper()}  ·  {tag}  ·  活跃 {counts['active']}", fg=color)
        for key, label in self.counter_labels.items():
            label.configure(text="—" if counts[key] is None else str(counts[key]))
        job = self.focus_job()
        if job:
            self.focus_id.configure(text=f"当前关注  #{job.job_id}")
            state_color = RED if job.state in TERMINAL-{"COMPLETED", "CANCELLED"} else (GREEN if job.state in RUNNING or job.state == "COMPLETED" else AMBER)
            self.focus_state.configure(text=job.state_label, fg=state_color)
            self.focus_name.configure(text=job.name)
            seconds = job.elapsed_s
            caption = "已运行" if job.state in RUNNING else "作业用时"
            if job.state in RUNNING:
                seconds = seconds+advance if seconds is not None else None
            elif job.state in PENDING:
                seconds = pending_seconds(job, snap, advance)
                caption = "自提交起等待"
            self.timer.configure(text=duration_label(seconds))
            self.compact_job.configure(text=f"#{job.job_id} · {caption}")
            self.compact_timer.configure(text=duration_label(seconds), fg=TEXT if recent else MUTED)
            self.timer_caption.configure(text=caption+(" · 待同步" if not recent else ""))
            self.resources.configure(text=f"{job.cpus} 核  ·  申请 {memory_label(job.memory)}"+(f"  ·  {job.location}" if job.location and job.state in RUNNING else ""))
            progress = snap.progress if snap.progress.get("job_id") == job.job_id else {}
            fraction = 0
            if progress.get("iteration"):
                self.progress_caption.configure(text=f"输运迭代 · 第 {progress['iteration']} 步", fg=BLUE)
                self.progress_value.configure(text=f"误差 {progress['max_error']:.2e}" if progress.get("max_error") is not None else "")
                self.progress_note.configure(text="自动跟踪日志 · 迭代阶段；收敛仍需核验" if progress.get("log_source") == "automatic" else "迭代阶段；结果收敛仍需核验")
            elif progress.get("q_total"):
                fraction = progress["q_current"]/progress["q_total"]
                self.progress_caption.configure(text="散射积分 · q 点进度", fg=BLUE)
                self.progress_value.configure(text=f"{fraction:.1%}")
                source = "自动跟踪 · " if progress.get("log_source") == "automatic" else ""
                self.progress_note.configure(text=f"{source}{progress['q_current']:,} / {progress['q_total']:,}")
            else:
                self.progress_caption.configure(text="等待计算日志" if job.in_queue else "调度器状态", fg=MUTED)
                self.progress_value.configure(text="")
                self.progress_note.configure(text="排队原因："+job.location if job.state in PENDING else progress.get("log_status", "正在自动查找计算日志"))
            if progress.get("native_error"):
                self.progress_caption.configure(text="计算日志报告错误", fg=RED)
                self.progress_note.configure(text="请检查计算日志中的 Error in routine")
            if job.state in TERMINAL:
                self.progress_caption.configure(text="调度已完成" if job.state == "COMPLETED" else job.state_label, fg=state_color)
                self.progress_value.configure(text="")
                self.progress_note.configure(text="计算结果仍需独立核验" if job.state == "COMPLETED" else f"退出码：{job.exit_code or '待确认'}")
                fraction = 1 if job.state == "COMPLETED" else 0
            self.progress.delete("all")
            self.progress.create_rectangle(0, 0, max(0, (self.progress.winfo_width() or 340)*fraction), 6, fill=state_color, outline="")
        else:
            self.focus_id.configure(text="当前关注")
            self.focus_state.configure(text="空闲", fg=MUTED)
            self.focus_name.configure(text="当前没有作业")
            self.timer.configure(text="--:--:--")
            self.compact_job.configure(text="当前没有作业")
            self.compact_timer.configure(text="--:--:--", fg=MUTED)
            self.timer_caption.configure(text="提交新作业后自动显示")
            self.resources.configure(text="")
            self.progress_caption.configure(text="暂无作业进度", fg=MUTED)
            self.progress_value.configure(text="")
            self.progress_note.configure(text="")
            self.progress.delete("all")
        visible = sorted(snap.jobs, key=lambda item: (not item.in_queue, item.state == "UNKNOWN", -int(re.sub(r"\D", "", item.job_id) or 0)))
        rows = tuple((job.job_id, job.state_label, duration_label(job.elapsed_s)) for job in visible)
        if rows != self._rows:
            self._rows = rows
            self.tree.delete(*self.tree.get_children())
            for row in rows:
                self.tree.insert("", "end", iid=row[0], values=row)
            if self.focus in self.tree.get_children():
                self.tree.selection_set(self.focus)
        self.active_count.configure(text=f"{counts['active']} 个活跃作业")
        totals = project_totals(self.options.ledger, snap, advance)
        self.total_time.configure(text=duration_label(totals[0], short=True) if totals else "未关联项目账本")
        self.core_hours.configure(text=f"{totals[1]:,.1f} 核时" if totals else "")
        stamp = datetime.fromtimestamp(snap.received_at).strftime("%H:%M:%S")
        footer = f"{stamp} 已同步  ·  每 {self.options.refresh_seconds} 秒刷新" if recent else f"{stamp} 上次记录  ·  计时已冻结"
        if snap.warnings and recent:
            footer = snap.warnings[0]
        if self.error:
            footer = "连接失败 · 保留上次数据 · 点击查看详情"
        if self.notification_until > time.monotonic():
            footer = self.notification_text
        self.footer.configure(text=footer, fg=RED if self.error else MUTED)

    def tick(self):
        if not self.closed:
            self.paint()
            self.root.after(1000, self.tick)

    def show_error(self):
        if self.error:
            messagebox.showinfo("连接详情", "暂时无法同步超算，已保留上次数据。\n\n"+self.error, parent=self.root)

    def settings(self):
        window = tk.Toplevel(self.root)
        window.title("挂件设置")
        window.configure(bg=BG)
        window.attributes("-topmost", True)
        window.resizable(False, False)
        window.geometry(f"320x235+{max(0, self.root.winfo_x()-80)}+{self.root.winfo_y()+40}")
        self.label(window, "显示设置", size=12, bold=True).pack(anchor="w", padx=20, pady=(18, 12))
        row = tk.Frame(window, bg=BG)
        row.pack(fill="x", padx=20)
        self.label(row, "同步间隔（秒）", color=MUTED).pack(side="left")
        interval = tk.StringVar(value=str(self.options.refresh_seconds))
        ttk.Combobox(row, textvariable=interval, values=(30, 60, 120, 300), state="readonly", width=8).pack(side="right")
        topmost = tk.BooleanVar(value=self.options.always_on_top)
        tk.Checkbutton(window, text="始终置顶", variable=topmost, bg=BG, fg=TEXT,
            selectcolor=CARD, activebackground=BG, activeforeground=TEXT, font=(FONT, 10)).pack(anchor="w", padx=16, pady=12)
        self.label(window, "关闭挂件不会停止超算作业。", color=MUTED, size=9).pack(anchor="w", padx=20)
        def apply():
            self.options.refresh_seconds = int(interval.get())
            self.options.always_on_top = topmost.get()
            self.root.attributes("-topmost", topmost.get())
            self.save_preferences()
            window.destroy()
            self.refresh()
        self.button(window, "保存", apply, width=9).pack(anchor="e", padx=20, pady=14)

    def close(self):
        self.save_preferences()
        self.closed = True
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="SCNet desktop job widget")
    parser.add_argument("--widget-config", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--cluster")
    parser.add_argument("--once", action="store_true", help="Print one read-only live snapshot without a window")
    args = parser.parse_args()
    try:
        options = Options.load(args.widget_config, args.config, args.cluster)
        settings = Settings.load(options.config)
        monitor = SlurmMonitor(settings, options.cluster)
        if args.once:
            focus = options.watched_jobs[0] if options.watched_jobs else None
            snapshot = asyncio.run(monitor.fetch(options.watched_jobs, options.job_logs.get(focus), log_job_id=focus, follow_active=True))
            print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2))
            return
        mutex = None
        if os.name == "nt":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel.CreateMutexW.restype = ctypes.c_void_p
            key = hashlib.sha256((str(options.config)+options.cluster).encode()).hexdigest()[:16]
            mutex = kernel.CreateMutexW(None, False, "Local\\SCNetWidget_"+key)
            if mutex and ctypes.get_last_error() == 183:
                user = ctypes.WinDLL("user32", use_last_error=True)
                user.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
                user.FindWindowW.restype = ctypes.c_void_p
                user.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
                user.SetForegroundWindow.argtypes = [ctypes.c_void_p]
                window = user.FindWindowW(None, f"超算任务监视 · {options.cluster}")
                if window:
                    user.ShowWindow(window, 5)
                    user.SetForegroundWindow(window)
                return
        enable_dpi_awareness()
        root = tk.Tk()
        def report_callback(exception, value, tb):
            settings.state_dir.mkdir(parents=True, exist_ok=True)
            with (settings.state_dir / "widget-errors.log").open("a", encoding="utf8") as handle:
                traceback.print_exception(exception, value, tb, file=handle)
        root.report_callback_exception = report_callback
        Widget(root, monitor, options, settings.state_dir)
        root.mainloop()
        if mutex:
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(mutex)
    except Exception as exc:
        if args.once:
            raise
        try:
            error_root = tk.Tk()
            error_root.withdraw()
            messagebox.showerror("超算挂件启动失败", str(exc), parent=error_root)
            error_root.destroy()
        except tk.TclError:
            raise


if __name__ == "__main__":
    main()
