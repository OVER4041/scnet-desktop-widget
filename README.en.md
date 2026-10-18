# SCNet Desktop Widget

[中文](README.md)

A native Windows/Tk widget for your SSH-accessible Slurm cluster. It runs independently of Codex and needs no remote installation or third-party runtime Python packages. Its read-only SSH support is extracted from [mcp-scnet-hpc](https://github.com/OVER4041/mcp-scnet-hpc).

Shows running/pending counts, today's completions/failures, selected-job elapsed/wait time, requested CPUs/memory, optional EPW stage progress and optional ledger elapsed/core hours. Compact/expanded layouts support dragging, pinning, manual refresh and 30/60/120/300-second intervals.

Install Python 3.11+ with Tkinter and OpenSSH. Verify the SSH alias and host key yourself. Copy `config.example.toml` to `config.local.toml` and set `ssh_host`; copy `widget.example.json` to `widget.local.json`. Double-click `启动超算挂件.vbs` or run `pythonw launch-widget.pyw`. Keep the computer awake and connected. No login autostart is installed.

Configure watched IDs, remote-relative log paths and a project ledger as described in the Chinese README. After package installation, use `scnet-desktop-widget --widget-config widget.local.json`; `--once` prints one live snapshot without opening a window.

Lost connections retain the last snapshot and freeze interpolated timers. Missing queue jobs remain unconfirmed until accounting responds. Steps are excluded; arrays count tasks. Today's counts use the cluster date/end time. Cancellations do not count as failures. Ledger totals include failures/resumes; requested memory is not RSS. EPW progress is stage-specific, not an overall ETA. Scheduler completion does not establish scientific convergence.

The widget never submits/cancels jobs or automatically accepts unknown host keys. Closing it does not affect computations. Personal configuration, credentials and runtime state are excluded. It does not itself wake an AI conversation.

For offline tests set `PYTHONPATH=src` and run `python -m unittest discover -s tests -v`. See [CHANGELOG.md](CHANGELOG.md).

### Automatic log discovery

The widget follows active jobs without a job ID or log mapping. It reads scheduler paths and simple stdout redirects as text, and checks the job directory plus its `logs` directory for filenames containing the current job ID. This also finds EPW logs created by Python drivers. It reads at most six tails (160 lines / 8 KiB each), checks resolved paths against `remote_root`, and never executes the submitted script. Home directories are not scanned. `job_logs` remains an explicit override for unusual paths. Manual job selection stays selected for the current session. Log failures do not discard scheduler updates; progress refers to the current phase.

### Yield to fullscreen apps

On Windows, the widget checks the foreground app once a second and temporarily removes topmost status when its client area covers its display. Leaving fullscreen restores the existing preference; a manually disabled topmost preference stays disabled. Borderless fullscreen and secondary-monitor coordinates are supported. Ordinary maximized windows, the desktop and the widget itself are excluded. Settings dialogs follow the same policy.

Run `python scripts/verify_fullscreen.py` on an interactive Windows desktop for a short live test. It creates a separate test window, exercises fullscreen / borderless / maximized / minimized transitions, verifies the native topmost flag, and closes automatically without connecting to HPC.

### Estimated remaining phase time

Compact and expanded views show an approximate remaining q-point phase time, using `(total - current) / recent progress rate`. The rate uses at most 10 minutes and 32 observations, including idle intervals, and needs at least 30 seconds with positive progress. Samples stay in the local snapshot across short restarts. Job, log, total, counter or runtime changes reset sampling. Stalled output, insufficient data, offline/stale snapshots, unknown iteration convergence and inactive jobs suppress an estimate. This is the current phase only; it excludes subsequent stages. Estimates update with observations rather than counting down unobserved work.
