"""Estimate the remaining q-point phase from bounded recent observations."""
import math
from statistics import median


def _q_phase(progress):
    current, total = progress.get("q_current"), progress.get("q_total")
    return (type(current) is int and type(total) is int and 0 <= current <= total
            and total > 0 and not progress.get("iteration")
            and not progress.get("native_error") and not progress.get("native_footer"))


def estimate_progress(progress: dict, *, now: float, previous: dict | None = None,
                      previous_at: float | None = None, active: bool = True,
                      elapsed_s: int | None = None, previous_elapsed_s: int | None = None) -> dict:
    result = {key: value for key, value in progress.items() if not key.startswith("eta_")}
    if not active:
        return {**result, "eta_state": "inactive"}
    if progress.get("native_error"):
        return {**result, "eta_state": "error"}
    if progress.get("iteration"):
        return {**result, "eta_state": "iteration"}
    if not _q_phase(progress):
        return {**result, "eta_state": "unavailable"}
    current, total = progress["q_current"], progress["q_total"]
    if current == total:
        return {**result, "eta_state": "phase_complete"}
    previous = previous or {}
    same_phase = (_q_phase(previous) and all(progress.get(key) == previous.get(key)
                  for key in ("job_id", "log_path", "q_total"))
                  and current >= previous["q_current"]
                  and not (elapsed_s is not None and previous_elapsed_s is not None
                           and elapsed_s < previous_elapsed_s))
    samples = []
    if same_phase:
        raw = previous.get("eta_samples", [])
        if not raw and previous_at is not None:
            raw = [[previous_at, previous["q_current"]]]
        for sample in raw[-32:]:
            if (not isinstance(sample, (list, tuple)) or len(sample) != 2
                    or not isinstance(sample[0], (int, float)) or not math.isfinite(sample[0])
                    or type(sample[1]) is not int or not 0 <= sample[1] <= current):
                samples = []
                break
            if samples and (sample[0] <= samples[-1][0] or sample[1] < samples[-1][1]):
                samples = []
                break
            samples.append([sample[0], sample[1]])
    intervals = [b[0]-a[0] for a, b in zip(samples, samples[1:])]
    interval = median(intervals[-5:]) if intervals else 60
    # Discard clock changes and long disconnected gaps; do not infer speed there.
    if samples and (now <= samples[-1][0] or now-samples[-1][0] > max(600, 4*interval)):
        samples = []
    samples = [sample for sample in samples if sample[0] >= now-600]
    samples.append([now, current])
    samples = samples[-32:]
    result["eta_samples"] = samples
    last_change = samples[0][0]
    for a, b in zip(samples, samples[1:]):
        if b[1] > a[1]:
            last_change = b[0]
    if now-last_change >= min(600, max(180, 3*interval)):
        return {**result, "eta_state": "stalled"}
    span = now-samples[0][0]
    advanced = current-samples[0][1]
    if span < 30 or advanced <= 0:
        return {**result, "eta_state": "collecting"}
    speed = advanced/span
    return {**result, "eta_state": "estimated", "eta_seconds": (total-current)/speed,
            "eta_rate": speed, "eta_window_s": span}


def eta_labels(progress: dict, *, recent: bool, running: bool) -> tuple[str, str]:
    if not running:
        return "预计剩余：—", ""
    if not recent:
        return "预计剩余：等待同步", "数据更新后继续估算"
    state = progress.get("eta_state")
    if state == "estimated":
        minutes = max(1, math.ceil(progress["eta_seconds"]/60))
        days, rest = divmod(minutes, 1440)
        hours, minutes = divmod(rest, 60)
        label = (f"{days}天" if days else "")+(f"{hours}小时" if hours else "")
        label += f"{minutes}分" if minutes else ""
        return ("本阶段剩余约 "+label,
                f"最近 {max(1, math.ceil(progress['eta_window_s']/60))} 分钟速度 · {progress['eta_rate']*60:,.1f} 点/分")
    labels = {
        "collecting": ("预计剩余：采样中", "等待下一次进度变化"),
        "stalled": ("预计剩余：等待进度更新", "近期进度未推进，暂不估算"),
        "phase_complete": ("本阶段已完成", "等待后续阶段；总剩余时间未知"),
        "iteration": ("预计剩余：暂无法估算", "迭代收敛步数未知"),
        "error": ("预计剩余：暂停估算", "计算日志报告错误"),
    }
    return labels.get(state, ("预计剩余：等待进度", "识别到有明确总量的阶段后估算"))
