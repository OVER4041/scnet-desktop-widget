"""Render only this app's window and check stale-data and compact-mode behavior."""
import json
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from scnet_widget.config import Settings
from scnet_widget.monitor import SlurmMonitor, Snapshot
from scnet_widget.widget import Options, Widget, enable_dpi_awareness, memory_label

from PIL import ImageGrab


def main():
    snapshot = Snapshot.from_dict(json.loads((ROOT / '.state/widget-live-verification.json').read_text('utf8')))
    options = Options.load(ROOT / 'widget.local.json')
    settings = Settings.load(options.config)
    temporary = tempfile.TemporaryDirectory(dir=ROOT / '.state')
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        app = Widget(root, SlurmMonitor(settings, options.cluster), options, Path(temporary.name), autostart=False)
        app.snapshot = snapshot
        app.online = True
        if app.compact:
            app.toggle_compact()
        app.paint()
        root.update()
        full_height = root.winfo_height()
        assert app.counter_labels['running'].cget('text') == str(snapshot.counts()['running'])
        selected = app.focus_job()
        assert selected is not None and app.focus_id.cget('text').endswith(selected.job_id)
        assert memory_label(selected.memory) in app.resources.cget('text')
        assert app.tree.get_children()
        if snapshot.progress.get('eta_state') == 'estimated':
            assert app.eta_value.cget('text').startswith('本阶段剩余约 ')
            assert app.compact_eta.cget('text') == app.eta_value.cget('text')
            assert '点/分' in app.eta_note.cget('text')
        app.toggle_compact()
        root.update()
        compact_height = root.winfo_height()
        assert compact_height < full_height
        root.attributes('-alpha', 1.0)
        root.update()
        left, top = root.winfo_rootx(), root.winfo_rooty()
        ImageGrab.grab(bbox=(left, top, left+root.winfo_width(), top+root.winfo_height())).save(ROOT / '.state/widget-preview-compact.png')
        app.toggle_compact()
        root.update()
        app.online = False
        app.error = 'Simulated connection loss for offline UI verification'
        app.paint()
        frozen = app.timer.cget('text')
        time.sleep(1.1)
        app.paint()
        assert app.timer.cget('text') == frozen
        assert app.counter_labels['running'].cget('text') == str(snapshot.counts()['running'])
        assert '上次记录' in app.connection.cget('text')
        if selected.in_queue and selected.state == 'RUNNING':
            assert '等待同步' in app.eta_value.cget('text')
            assert '点/分' not in app.eta_note.cget('text')
        app.online = True
        app.error = ''
        app.paint()
        root.update()
        left, top = root.winfo_rootx(), root.winfo_rooty()
        width, height = root.winfo_width(), root.winfo_height()
        root.lift()
        root.attributes('-alpha', 1.0)
        root.update()
        image = ImageGrab.grab(bbox=(left, top, left+width, top+height))
        image.save(ROOT / '.state/widget-preview.png')
        report = {'rendered': True, 'using_actual_cluster_snapshot': True,
            'job_id': app.focus, 'counts': snapshot.counts(), 'resources': app.resources.cget('text'),
            'progress': snapshot.progress, 'full_height': full_height, 'compact_height': compact_height,
            'eta_label': app.eta_value.cget('text'), 'eta_detail': app.eta_note.cget('text'),
            'offline_data_preserved': True, 'offline_timer_frozen': True,
            'window_within_screen': top+height <= root.winfo_screenheight() and left+width <= root.winfo_screenwidth(),
            'screenshot': '.state/widget-preview.png'}
        assert report['window_within_screen']
        (ROOT / '.state/widget-ui-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf8')
        print(json.dumps(report, ensure_ascii=False))
        app.close()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
        temporary.cleanup()


if __name__ == '__main__':
    main()
