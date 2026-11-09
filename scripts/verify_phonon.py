"""Replay program logs in the native widget without SSH or paid compute."""
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from scnet_widget.monitor import Job, Snapshot, parse_progress
from scnet_widget.eta import estimate_progress
from scnet_widget.widget import Options, Widget, enable_dpi_awareness
from PIL import ImageGrab


def main():
    enable_dpi_awareness()
    output = ROOT/'.state'
    output.mkdir(exist_ok=True)
    text = (ROOT/'tests/fixtures/shengbte_synthetic.log').read_text('utf8')
    cases = [
        ('matrix', text[:text.index(' Info: matrix elements:  70%')], '矩阵元'),
        ('bte', text[:text.index(' Info: Iteration : 6')], '迭代求解'),
        ('dfpt', 'Program PHONON v.7.3.1\nCalculation of q = 0.25 0.43 0.25\nRepresentation # 5 mode # 5\niter # 7 total cpu time : 6.7 secs', 'DFPT'),
        ('force', 'Program PWSCF v.7.3.1\niteration # 8\nestimated scf accuracy < 2.3E-8 Ry', 'SCF'),
    ]
    reports = []
    with tempfile.TemporaryDirectory() as folder:
        root = tk.Tk()
        app = Widget(root, SimpleNamespace(previous=None), Options(Path(folder)/'unused.toml', cluster='replay'), Path(folder), autostart=False)
        try:
            if app.compact:
                app.toggle_compact()
            for name, contents, expected in cases:
                progress = {**parse_progress(contents, 'force.out' if name == 'force' else name+'.log'), 'job_id': '101', 'log_path': name+'.log'}
                if name == 'matrix':
                    first = estimate_progress({**progress, 'work_current': 20}, now=time.time()-60)
                    progress = estimate_progress(progress, now=time.time(), previous=first)
                else:
                    progress = estimate_progress(progress, now=time.time())
                snapshot = Snapshot([Job('101', 'Archived log replay', 'RUNNING', 120, cpus=32, in_queue=True)], datetime.now().astimezone().strftime('%Y-%m-%dT%H:%M:%S%z'), progress=progress)
                app.snapshot, app.online = snapshot, True
                root.attributes('-alpha', 1.0)
                app.paint()
                root.update()
                assert expected in app.progress_caption.cget('text')
                assert expected in app.compact_phase.cget('text')
                if name == 'matrix':
                    assert '本阶段剩余约' in app.eta_value.cget('text')
                    assert app.progress_value.cget('text') == '60.0%'
                if name == 'bte':
                    assert '相对变化' in app.progress_note.cget('text')
                    assert '暂无法估算' in app.eta_value.cget('text')
                left, top = root.winfo_rootx(), root.winfo_rooty()
                ImageGrab.grab(bbox=(left, top, left+root.winfo_width(), top+root.winfo_height())).save(output/f'phonon-{name}-replay.png')
                reports.append({'case': name, 'title': app.progress_caption.cget('text'), 'value': app.progress_value.cget('text'), 'detail': app.progress_note.cget('text'), 'eta': app.eta_value.cget('text')})
            app.toggle_compact()
            root.update()
            assert root.winfo_height() < 500
            app.online = False
            app.paint()
            assert '等待同步' in app.compact_eta.cget('text')
        finally:
            app.close()
    (output/'phonon-ui-verification.json').write_text(json.dumps({'replay_only': True, 'verified': True, 'cases': reports}, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({'replay_only': True, 'verified': True, 'cases': reports}, ensure_ascii=True))


if __name__ == '__main__':
    main()
