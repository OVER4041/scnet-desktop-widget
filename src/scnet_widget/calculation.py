"""Program-specific log phases; numerical completion is not physical convergence."""
import json
import math
import re

NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eEdD][-+]?\d+)?"


def number(text):
    value = float(text.replace("D", "e").replace("d", "e"))
    return value if math.isfinite(value) else None


def parse_calculation(text: str, path: str = "") -> dict:
    # Header + bounded tail may omit the middle. Program banners and filenames
    # identify the parser; events are processed in order so later phases win.
    if re.search(r"Program EPW|Progression iq \(fine\)|Iteration number:", text):
        return {}  # Preserve the EPW parser and its existing stage semantics.
    is_sheng = bool(re.search(r"^\s*ShengBTE\s+\d|matrix elements:.*% done|Info:\s*Relative change", text, re.I | re.M))
    is_ph = bool(re.search(r"Program PHONON|Representation\s*#|Calculation of q\s*=", text))
    is_pw = bool(re.search(r"Program PWSCF|iteration #\s*\d+|estimated scf accuracy", text, re.I))
    is_phonopy = bool(re.search(r"phonopy", text, re.I))
    program = "ShengBTE" if is_sheng else "QE PH" if is_ph else "QE PW" if is_pw else "Phonopy" if is_phonopy else None
    if not program:
        failure = re.search(r"^.*(?:Traceback \(most recent call last\)|MPI_ABORT|SIGSEGV|out.of.memory|Error in routine).*$", text, re.M | re.I)
        if failure:
            return {"program": "运行器", "phase": "error", "phase_label": "计算运行异常",
                    "native_error": True, "error_message": failure[0].strip()[:110]}
        return {}
    force = bool(re.search(r"force|displ|thirdorder", path, re.I))
    result = {"program": program, "phase": "initializing", "phase_label": program+" · 初始化"}
    temperature = None
    cycle = 0

    def phase(name, label, **values):
        nonlocal result
        result = {"program": program, "phase": name, "phase_label": label, **values}
        if temperature is not None:
            result["temperature_k"] = temperature
        if cycle:
            result["phase_cycle"] = cycle

    for line in text.splitlines():
        if is_sheng:
            match = re.search(r"Temperature\s*=\s*("+NUMBER+r")", line, re.I)
            if match:
                temperature = number(match[1])
                cycle += 1
                phase("bte_temperature", "声子 BTE · 温度计算")
            if "about to obtain the spectrum" in line:
                phase("spectrum", "声子谱 · 计算中")
            elif "spectrum calculation finished" in line:
                phase("harmonic", "谐性声子 · 谱计算结束")
            elif "calculating Vp_plus" in line:
                phase("matrix_elements", "三声子散射 · 矩阵元")
            match = re.search(r"matrix elements:\s*(\d+)% done", line, re.I)
            if match and 0 <= int(match[1]) <= 100:
                phase("matrix_elements", "三声子散射 · 矩阵元", work_current=int(match[1]), work_total=100, work_unit="%")
            if "matrix elements computed" in line:
                phase("matrix_complete", "三声子散射 · 矩阵元已完成")
            if "start calculating kappa" in line:
                phase("bte_rta", "声子 BTE · RTA 热导")
            match = re.search(r"Iteration\s*:\s*(\d+)", line)
            if match:
                phase("bte_iteration", "声子 BTE · 迭代求解", iteration=int(match[1]))
            match = re.search(r"Relative change\s*=\s*("+NUMBER+r")", line, re.I)
            if match:
                result["relative_change"] = number(match[1])
            if "converged kappa tensor" in line:
                result["solver_converged"] = True
            if "normal exit after" in line or "onlyharmonic=.true., stopping here" in line:
                summary = {key: result[key] for key in ("iteration", "relative_change", "solver_converged") if key in result}
                phase("finished", "ShengBTE · 程序正常结束", native_footer=True, **summary)
        elif is_ph:
            match = re.search(r"(\d+)\s*/\s*(\d+)\s*q-points for this run, from\s+(\d+)\s+to\s+(\d+)", line)
            if match:
                result["ph_q_range"] = f"{match[3]}–{match[4]}（全网格 {match[2]}）"
            match = re.search(r"Calculation of q\s*=\s*(.*)", line)
            if match:
                phase("dfpt", "谐性声子 · DFPT 响应", q_coordinates=match[1].strip()[:70])
            match = re.search(r"Representation\s*#\s*(\d+)", line)
            if match:
                result.update(phase="dfpt", phase_label="谐性声子 · DFPT 响应", representation=int(match[1]))
                result.pop("scf_iteration", None)
            match = re.search(r"iter\s*#\s*(\d+)", line)
            if match:
                result["scf_iteration"] = int(match[1])
            if "Diagonalizing the dynamical matrix" in line:
                result.update(phase="diagonalization", phase_label="谐性声子 · 动力学矩阵对角化")
        elif is_pw:
            match = re.search(r"iteration #\s*(\d+)", line, re.I)
            if match:
                phase("force_scf" if force else "scf", "位移受力 · SCF" if force else "QE · SCF", scf_iteration=int(match[1]))
            match = re.search(r"estimated scf accuracy\s*[<=>]+\s*("+NUMBER+r")", line, re.I)
            if match:
                result["scf_accuracy_ry"] = number(match[1])
            if "convergence has been achieved" in line:
                result["scf_converged"] = True
            if "Forces acting on atoms" in line:
                converged = result.get("scf_converged", False)
                phase("forces", "位移受力 · 计算原子力", scf_converged=converged)
        elif is_phonopy:
            for marker, name, label in (
                ("displacement", "displacements", "Phonopy · 位移准备"),
                ("force constants", "force_constants", "Phonopy · 二阶力常数"),
                ("band structure", "bands", "Phonopy · 声子谱"),
                ("mesh sampling", "mesh", "Phonopy · q 网格"),
                ("thermal properties", "thermal", "Phonopy · 热力学量"),
                ("density of states", "dos", "Phonopy · 声子态密度")):
                if marker in line.lower():
                    phase(name, label)
        if not is_sheng and "JOB DONE." in line:
            phase("finished", program+" · 程序正常结束", native_footer=True)
        if re.search(r"Error in routine|^\s*(?:Error:|ERROR:|Traceback \(most recent call last\))|MPI_ABORT|SIGSEGV|out.of.memory", line, re.I):
            result.update(native_error=True, error_message=line.strip()[:110])
        if re.search(r"convergence NOT achieved|did not converge|maximum.*iterations.*reached", line, re.I):
            result["iteration_not_converged"] = True
    # A later timing/footer line must not erase a reported numerical failure.
    failure = re.search(r"^.*(?:Error in routine|Traceback \(most recent call last\)|MPI_ABORT|SIGSEGV|out.of.memory).*$", text, re.M | re.I)
    if failure:
        result.update(native_error=True, error_message=failure[0].strip()[:110])
    if re.search(r"convergence NOT achieved|did not converge|maximum.*iterations.*reached", text, re.I):
        result['iteration_not_converged'] = True
    return result


def parse_sidecar(text: str, identifier: str) -> dict:
    """Optional progress contract. A matching job ID is mandatory."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict) or str(data.get("job_id", "")) != identifier:
        return {}
    if not isinstance(data.get("program"), str) or data["program"] not in {"Phonopy", "ShengBTE", "QE PH", "force_batch"}:
        return {}
    name = data.get("phase")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]{1,60}", name):
        return {}
    result = {"program": data["program"], "phase": name, "phase_label": {
        "force_batch": "位移受力 · 批次", "Phonopy": "Phonopy · 批次",
        "QE PH": "谐性声子 · q 点批次", "ShengBTE": "声子 BTE · 批次"}[data["program"]]}
    current, total = data.get("completed"), data.get("total")
    if type(current) is int and type(total) is int and 0 <= current <= total and total > 0:
        result.update(work_current=current, work_total=total, work_unit="项")
    if data.get("status") == "failed":
        result["native_error"] = True
    return result


def calculation_labels(progress: dict) -> tuple[str, str, str, float]:
    title = progress.get("phase_label", "计算阶段待确认")
    details = []
    if progress.get("temperature_k") is not None:
        details.append(f"{progress['temperature_k']:g} K")
    value, fraction = "", 0
    if progress.get("work_total"):
        current, total = progress["work_current"], progress["work_total"]
        fraction = current/total
        value = f"{fraction:.1%}"
        details.append(f"已完成 {current:,}/{total:,} {progress.get('work_unit', '项')}")
    elif progress.get("iteration"):
        value = f"第 {progress['iteration']} 步"
        if progress.get("relative_change") is not None:
            details.append(f"相对变化 {progress['relative_change']:.2e}")
    elif progress.get("scf_iteration"):
        value = f"SCF 第 {progress['scf_iteration']} 步"
    if progress.get("representation"):
        details.append(f"不可约表示 #{progress['representation']}")
    if progress.get("q_coordinates"):
        details.append("q = "+progress["q_coordinates"])
    if progress.get("scf_accuracy_ry") is not None:
        details.append(f"精度 {progress['scf_accuracy_ry']:.2e} Ry")
    if progress.get("native_footer"):
        details.append("数值结束；物理收敛仍需核验")
    elif not details:
        details.append("日志已连接；当前阶段暂无可计数总量")
    if progress.get("iteration_not_converged"):
        details.append("日志报告未收敛")
    return title, value, " · ".join(details), fraction


def evidence_rank(progress: dict) -> int:
    if progress.get("native_error"):
        return 3
    if progress.get("program") and progress.get("phase") == "initializing":
        return 1
    return 2 if progress else 0
