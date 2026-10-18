"""Read-only Slurm snapshots for the desktop widget; no remote agent required."""
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
import json
from pathlib import Path
import re
import time

from .backend import Bridge, job_id
from .config import BridgeError, Settings
from .backend import remote_path
from .log_discovery import SCRIPT_MARKER, discovery_paths, log_read_script, read_log_records
from .eta import estimate_progress


TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
            "NODE_FAIL", "BOOT_FAIL", "PREEMPTED", "DEADLINE", "REVOKED"}
RUNNING = {"RUNNING", "COMPLETING", "STAGE_OUT"}
PENDING = {"PENDING", "CONFIGURING", "REQUEUED", "REQUEUE_HOLD", "RESV_DEL_HOLD"}
STATE_NAMES = {"RUNNING": "运行中", "PENDING": "排队中", "CONFIGURING": "启动中",
               "COMPLETING": "收尾中", "COMPLETED": "已完成", "FAILED": "失败",
               "CANCELLED": "已取消", "TIMEOUT": "超时", "OUT_OF_MEMORY": "内存不足",
               "NODE_FAIL": "节点故障", "PREEMPTED": "被抢占", "SUSPENDED": "已暂停",
               "UNKNOWN": "状态待确认"}
MARKERS = ("__SCNET_WIDGET_QUEUE__", "__SCNET_WIDGET_ACCOUNTING__",
           "__SCNET_WIDGET_WATCHED__", "__SCNET_WIDGET_LOG__", "__SCNET_WIDGET_END__")


def duration_seconds(text: str) -> int | None:
    """Slurm renders minutes:seconds, hours:minutes:seconds or days-hours:minutes:seconds."""
    text = text.strip()
    if not re.fullmatch(r"(?:\d+-)?\d+:\d{2}(?::\d{2})?", text):
        return None
    days, clock = text.split("-", 1) if "-" in text else ("0", text)
    parts = [int(value) for value in clock.split(":")]
    if len(parts) == 2:
        parts.insert(0, 0)
    return int(days)*86400 + parts[0]*3600 + parts[1]*60 + parts[2]


def duration_label(seconds: float | None, *, short: bool = False) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if short:
        return f"{hours}时{minutes:02d}分" if hours else f"{minutes}分{seconds:02d}秒"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def normalized_state(value: str) -> str:
    return value.strip().split(" ", 1)[0].rstrip("+") or "UNKNOWN"


def safe_text(value: str, maximum: int = 180) -> str:
    return "".join(c if ord(c) >= 32 else " " for c in value)[:maximum]


@dataclass
class Job:
    job_id: str
    name: str
    state: str
    elapsed_s: int | None
    cpus: int = 0
    nodes: int = 0
    memory: str = ""
    location: str = ""
    submitted: str = ""
    ended: str = ""
    exit_code: str = ""
    in_queue: bool = False

    @property
    def state_label(self) -> str:
        return STATE_NAMES.get(self.state, self.state)


@dataclass
class Snapshot:
    jobs: list[Job]
    remote_time: str
    accounting_available: bool = True
    progress: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    received_at: float = field(default_factory=time.time)
    received_monotonic: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("received_monotonic", None)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        return cls(jobs=[Job(**job) for job in data["jobs"]], remote_time=data["remote_time"],
                   accounting_available=data.get("accounting_available", False),
                   progress=data.get("progress", {}), warnings=data.get("warnings", []),
                   received_at=data.get("received_at", 0))

    def counts(self) -> dict:
        date = self.remote_time[:10]
        ended_today = [job for job in self.jobs if job.ended.startswith(date) and job.state in TERMINAL]
        return {"active": sum(job.in_queue for job in self.jobs),
                "running": sum(job.in_queue and job.state in RUNNING for job in self.jobs),
                "pending": sum(job.in_queue and job.state in PENDING for job in self.jobs),
                "completed": sum(job.state == "COMPLETED" for job in ended_today) if self.accounting_available else None,
                "failed": sum(job.state not in {"COMPLETED", "CANCELLED"} for job in ended_today) if self.accounting_available else None}


def parse_progress(text: str) -> dict:
    q = re.findall(r"Progression iq \(fine\)\s*=\s*(\d+)\s*/\s*(\d+)", text)
    iterations = re.findall(r"Iteration number:\s*(\d+)", text)
    errors = re.findall(r"([-+]?\d*\.\d+(?:[eEdD][-+]?\d+)?)\s+Max error", text)
    result = {}
    if q:
        current, total = map(int, q[-1])
        if 0 <= current <= total and total > 0:
            result.update(q_current=current, q_total=total)
    if iterations:
        result["iteration"] = int(iterations[-1])
    if errors:
        result["max_error"] = float(errors[-1].replace("D", "e").replace("d", "e"))
    if "maximum but did not converge" in text:
        result["iteration_not_converged"] = True
    if "Error in routine" in text:
        result["native_error"] = True
    if "Total program execution" in text:
        result["native_footer"] = True
    return result


def parse_snapshot(output: str, previous: Snapshot | None = None) -> Snapshot:
    sections = {}
    current = None
    for line in output.splitlines():
        if line in MARKERS:
            current = line
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line)
    if any(marker not in sections for marker in MARKERS):
        raise BridgeError("状态返回不完整；保留上次记录")
    header = sections[MARKERS[0]]
    if not header or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}", header[0]):
        raise BridgeError("无法确认超算时间")
    remote_time = header[0]
    jobs = {}
    for marker in MARKERS[1:3]:
        for line in sections[marker]:
            if not line or line == "__ACCOUNTING_UNAVAILABLE__":
                continue
            values = line.split("|")
            if len(values) == 11 and values[-1] == "":
                values.pop()
            if len(values) != 10:
                raise BridgeError("调度记账格式异常；保留上次记录")
            identifier, name, state, elapsed, cpus, nodes, memory, submitted, ended, code = values
            if not re.fullmatch(r"[1-9]\d*(?:_\d+)?(?:\+\d+)?", identifier):
                continue  # Ignore .batch/.extern steps and unexpanded array parents.
            jobs[identifier] = Job(identifier, safe_text(name), normalized_state(state),
                int(elapsed) if elapsed.isdigit() else None, int(cpus or 0), int(nodes or 0),
                memory, submitted=submitted, ended=ended, exit_code=code)
    for line in header[1:]:
        if not line:
            continue
        values = line.split("|")
        if len(values) != 10:
            raise BridgeError("作业列表格式异常；保留上次记录")
        identifier, name, state, elapsed, cpus, nodes, memory, location, submitted, _ = values
        existing = jobs.get(identifier)
        jobs[identifier] = Job(safe_text(identifier), safe_text(name), normalized_state(state),
            duration_seconds(elapsed), int(cpus or 0), int(nodes or 0), memory,
            safe_text(location), submitted, in_queue=True)
        if existing:
            jobs[identifier].exit_code = existing.exit_code
    warnings = []
    if previous:
        for old in previous.jobs:
            if old.job_id not in jobs and (old.in_queue or old.state == "UNKNOWN"):
                jobs[old.job_id] = replace(old, state="UNKNOWN", in_queue=False)
                warnings.append(f"作业 {old.job_id} 离开队列，正在等待记账确认")
    available = "__ACCOUNTING_UNAVAILABLE__" not in sections[MARKERS[1]]
    if not available:
        warnings.append("今日历史记账暂不可用")
    return Snapshot(list(jobs.values()), remote_time, available,
                    parse_progress("\n".join(sections[MARKERS[3]])), warnings)


class SlurmMonitor:
    def __init__(self, settings: Settings, cluster: str, *, bridge=None):
        self.bridge = bridge or Bridge(settings)
        self.cluster = cluster
        self.bridge.cluster(cluster)
        self.previous = None
        self.log_locations = {}

    async def fetch_progress(self, identifier: str, log_path: str | None) -> dict:
        identifier = job_id(identifier)
        if log_path:
            directory, candidates = None, [remote_path(log_path)]
        else:
            cached = self.log_locations.get(identifier)
            if cached is None or time.monotonic()-cached[0] > 300:
                metadata = await self.bridge.run(self.cluster,
                    'printf "%s\\n" "$root"\n'
                    f'scontrol show job -o {identifier} 2>/dev/null || true\n'
                    f"printf '%s\\n' '{SCRIPT_MARKER}'\n"
                    f'scontrol write batch_script {identifier} - 2>/dev/null | head -c 24576 || true\n')
                if metadata.truncated:
                    raise BridgeError("作业日志位置返回不完整")
                directory, candidates = discovery_paths(metadata.stdout, identifier)
                if directory is not None or candidates:
                    self.log_locations[identifier] = (time.monotonic(), directory, candidates)
            else:
                _, directory, candidates = cached
        progress = {"job_id": identifier, "log_source": "configured" if log_path else "automatic"}
        if not candidates and not directory:
            return {**progress, "log_status": "未发现日志位置，可在 job_logs 指定路径"}
        result = await self.bridge.run(self.cluster, log_read_script(identifier, directory, candidates))
        if result.truncated:
            raise BridgeError("日志返回超过读取上限")
        records = read_log_records(result.stdout)
        if not records:
            return {**progress, "log_status": "等待当前作业创建日志"}
        parsed = [(path, modified, parse_progress(text)) for path, modified, text in records]
        # Prefer scientific progress over driver banners; follow the newest phase.
        # Progress from different files is never combined.
        path, _, values = max(parsed, key=lambda item: (bool(item[2]), item[1]))
        return {**progress, **values, "log_path": path, "log_status": "已连接日志，等待可识别进度"}

    async def fetch(self, watched: list[str] = (), log_path: str | None = None, *, log_job_id: str | None = None, follow_active: bool = False) -> Snapshot:
        identifiers = list(dict.fromkeys(job_id(value) for value in watched))[:100]
        if self.previous:
            for job in self.previous.jobs:
                if (job.in_queue or job.state == "UNKNOWN") and re.fullmatch(r"[1-9]\d*(?:_\d+)?", job.job_id):
                    identifiers.append(job.job_id)
        identifiers = list(dict.fromkeys(identifiers))[:100]
        accounting = "--format=JobID%64,JobName%120,State%40,ElapsedRaw,AllocCPUS,AllocNodes,ReqMem,Submit,End,ExitCode"
        body = f'''printf '%s\\n' '{MARKERS[0]}'
date '+%Y-%m-%dT%H:%M:%S%z'
squeue --noheader --array --user="$(id -un)" --format='%i|%j|%T|%M|%C|%D|%m|%R|%V|'
printf '%s\\n' '{MARKERS[1]}'
if ! sacct --allocations --user="$(id -un)" --starttime="$(date +%F)" --endtime=now --parsable2 --noheader {accounting}; then
  printf '%s\\n' '__ACCOUNTING_UNAVAILABLE__'
fi
printf '%s\\n' '{MARKERS[2]}'
'''
        if identifiers:
            body += f'''if ! sacct --allocations --user="$(id -un)" --jobs={','.join(identifiers)} --starttime=1970-01-01 --endtime=now --parsable2 --noheader {accounting}; then
  printf '%s\\n' '__ACCOUNTING_UNAVAILABLE__'
fi
'''
        body += f"printf '%s\\n' '{MARKERS[3]}'\n"
        body += f"printf '\\n%s\\n' '{MARKERS[4]}'\n"
        result = await self.bridge.run(self.cluster, body)
        if result.truncated:
            raise BridgeError("返回数据超过上限，无法确认完整作业数；保留上次记录")
        snapshot = parse_snapshot(result.stdout, self.previous)
        chosen = next((job for job in snapshot.jobs if job.job_id == log_job_id), None)
        if chosen is None or (follow_active and not chosen.in_queue):
            active = sorted((job for job in snapshot.jobs if job.in_queue),
                            key=lambda job: (job.state not in RUNNING, -int(job.job_id.split('_')[0])))
            chosen = next(iter(active), chosen)
        if chosen is None and snapshot.jobs:
            chosen = max(snapshot.jobs, key=lambda job: (job.ended, job.submitted))
        if chosen:
            try:
                snapshot.progress = await self.fetch_progress(chosen.job_id, log_path if chosen.job_id == log_job_id else None)
            except (BridgeError, OSError) as exc:
                snapshot.progress = {"job_id": chosen.job_id, "log_status": "日志暂不可读，将自动重试"}
                snapshot.warnings.append(f"计算日志暂不可读：{safe_text(str(exc), 100)}")
            snapshot.received_at = time.time()
            snapshot.received_monotonic = time.monotonic()
            old_job = next((job for job in self.previous.jobs if job.job_id == chosen.job_id), None) if self.previous else None
            old_progress = self.previous.progress if old_job and old_job.in_queue and old_job.state in RUNNING else None
            snapshot.progress = estimate_progress(snapshot.progress, now=snapshot.received_at,
                previous=old_progress, previous_at=self.previous.received_at if self.previous else None,
                active=chosen.in_queue and chosen.state in RUNNING, elapsed_s=chosen.elapsed_s,
                previous_elapsed_s=old_job.elapsed_s if old_job else None)
        self.previous = snapshot
        return snapshot


def project_totals(ledger_path: Path | None, snapshot: Snapshot, advance: float = 0) -> tuple[float, float] | None:
    if ledger_path is None:
        return None
    try:
        ledger = json.loads(ledger_path.read_text("utf8"))
        live = {job.job_id: job for job in snapshot.jobs}
        elapsed = cores = 0.0
        seen = set()
        for job in ledger["jobs"]:
            identifier = job.get("id")
            if identifier in seen and identifier is not None:
                continue
            seen.add(identifier)
            seconds = job.get("actual_elapsed_s")
            if seconds is None:
                current = live.get(identifier)
                seconds = current.elapsed_s if current and current.elapsed_s is not None else job.get("observed_elapsed_s", 0)
                if current and current.in_queue and current.state in RUNNING:
                    seconds += advance
            elapsed += max(0, seconds)
            cores += max(0, seconds)*job.get("mpi_tasks", 0)/3600
        return elapsed, cores
    except (OSError, ValueError, TypeError, KeyError):
        return None


def pending_seconds(job: Job, snapshot: Snapshot, advance: float = 0) -> float | None:
    try:
        remote = datetime.strptime(snapshot.remote_time, "%Y-%m-%dT%H:%M:%S%z")
        submitted = datetime.fromisoformat(job.submitted).replace(tzinfo=remote.tzinfo)
        return max(0, (remote-submitted).total_seconds()+advance)
    except ValueError:
        return None
