"""Bounded log discovery. Submitted scripts are parsed as text, never executed."""
from pathlib import PurePosixPath
import posixpath
import re
import shlex

from .backend import quote

SCRIPT_MARKER = "__SCNET_WIDGET_SCRIPT__"


def relative_path(value: str, root: str, directory: str = "") -> str | None:
    if not value or any(ord(c) < 32 for c in value) or "\\" in value:
        return None
    path = posixpath.normpath(posixpath.join(root, directory, value))
    if not path.startswith(root.rstrip("/")+"/"):
        return None
    return posixpath.relpath(path, root)


def discovery_paths(output: str, identifier: str) -> tuple[str | None, list[str]]:
    """Resolve scheduler paths and simple stdout redirects without shell evaluation."""
    header, _, script = output.partition(SCRIPT_MARKER+"\n")
    root, _, metadata = header.partition("\n")
    if not root.startswith("/") or root == "/":
        return None, []
    fields = dict(re.findall(r"(?:^|\s)([A-Za-z][A-Za-z0-9]*)=(.*?)(?=\s[A-Za-z][A-Za-z0-9]*=|$)", metadata.strip()))
    work = fields.get("WorkDir", "")
    directory = relative_path(work, root) if work.startswith("/") else None
    # WorkDir equal to remote_root is valid, but scanning the entire home is not.
    if posixpath.normpath(work) == root:
        directory = "."
    candidates = []
    variables = {"SLURM_JOB_ID": identifier, "SLURM_JOBID": identifier,
                 "SLURM_SUBMIT_DIR": work, "PWD": work}
    if "_" in identifier:
        variables["SLURM_ARRAY_JOB_ID"], variables["SLURM_ARRAY_TASK_ID"] = identifier.split("_", 1)

    def expand(value):
        value = re.sub(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))",
                       lambda match: variables.get(match[1] or match[2], "$UNKNOWN"), value)
        return value if not any(c in value for c in "$`*?\n") else ""

    cwd = directory
    # Only straight-line literal assignments/cd and simple redirects are supported.
    # Unknown control flow/cd invalidates cwd until another literal absolute cd.
    for line in script.replace("\\\n", " ").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            continue
        if not tokens:
            continue
        if tokens[0] in {"if", "for", "while", "until", "case", "function"}:
            cwd = None
            continue
        assignment = re.fullmatch(r"(?:export\s+)?([A-Za-z_]\w*)=(.*)", line)
        if assignment:
            try:
                values = shlex.split(assignment[2], comments=True)
                if len(values) == 1:
                    variables[assignment[1]] = expand(values[0])
            except ValueError:
                pass
        if tokens[0] == "cd":
            target = expand(tokens[-1])
            cwd = relative_path(target, root, cwd or "") if target and (cwd is not None or target.startswith("/")) else None
            continue
        if cwd is None:
            continue
        # A lexer separates attached redirects too, while keeping quoted paths intact.
        lexer = shlex.shlex(line, posix=True, punctuation_chars="<>;&|")
        lexer.whitespace_split = True
        try:
            parts = list(lexer)
        except ValueError:
            continue
        for index, token in enumerate(parts[:-1]):
            if token not in {">", ">>", "&>"} or (index and parts[index-1] == "2"):
                continue
            target = expand(parts[index+1])
            path = relative_path(target, root, cwd) if target else None
            if path and PurePosixPath(path).suffix.lower() in {".out", ".log", ".txt"}:
                candidates.append(path)
    for key in ("StdOut", "StdErr"):
        target = fields.get(key, "").replace("%j", identifier)
        if "_" in identifier:
            parent, task = identifier.split("_", 1)
            target = target.replace("%A", parent).replace("%a", task)
        path = relative_path(target, root, directory or "") if target and "%" not in target else None
        if path:
            candidates.append(path)
    return directory, list(dict.fromkeys(candidates))[-6:]


def log_read_script(identifier: str, directory: str | None, candidates: list[str]) -> str:
    """Read at most six small tails, checking real paths including symlinks."""
    body = r'''
widget_read_log() {
  local file
  file=$(realpath -e -- "$1" 2>/dev/null) || return 0
  case "$file" in "$root"/*) ;; *) return 0;; esac
  test -f "$file" && test -r "$file" || return 0
  printf '%s\0%s\0' "${file#"$root"/}" "$(stat -c %Y -- "$file")"
  tail -c 8192 -- "$file" | tail -n 160
  printf '\0'
}
'''
    # Explicit/scheduler paths and job-id-matched files are sorted together by mtime.
    body += "{\n"
    for path in candidates:
        body += f"printf '%s\\0' {quote(path)}\n"
    if directory and directory != ".":
        for folder in (directory, directory+"/logs"):
            body += f'''widget_dir=$(realpath -e -- {quote(folder)} 2>/dev/null) || widget_dir=''
case "$widget_dir" in "$root"/*)
  if test -d "$widget_dir"; then
    find "$widget_dir" -maxdepth 1 -type f \
      \\( -name '*.out' -o -name '*.log' -o -name '*.txt' -o -name '*.err' \\) \
      -name '*{identifier}*' -print0 2>/dev/null |
    while IFS= read -r -d '' widget_file; do
      widget_name=${{widget_file##*/}}
      if [[ "$widget_name" =~ (^|[^0-9]){identifier}([^0-9]|$) ]]; then
        printf '%s\\0' "$widget_file"
      fi
    done
  fi;;
esac
'''
    body += r'''} | sort -zu | while IFS= read -r -d '' widget_file; do
  widget_real=$(realpath -e -- "$widget_file" 2>/dev/null) || continue
  case "$widget_real" in "$root"/*) ;; *) continue;; esac
  test -f "$widget_real" || continue
  printf '%s\t%s\0' "$(stat -c %Y -- "$widget_real")" "$widget_real"
done | sort -zrn | {
widget_count=0
while IFS= read -r -d '' widget_entry; do
  widget_read_log "${widget_entry#*$'\t'}"
  widget_count=$((widget_count+1))
  if test "$widget_count" -ge 6; then break; fi
done
}
'''
    return body


def read_log_records(output: str) -> list[tuple[str, int, str]]:
    records = output.split("\0")
    return [(records[i], int(records[i+1]), records[i+2])
            for i in range(0, len(records)-2, 3) if records[i+1].isdigit()]
