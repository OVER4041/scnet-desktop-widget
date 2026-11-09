import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest

from scnet_widget.calculation import calculation_labels, parse_sidecar
from scnet_widget.monitor import parse_progress
from scnet_widget.eta import estimate_progress, eta_labels
from scnet_widget.log_discovery import discovery_window, log_read_script, read_log_records

FIXTURE = Path(__file__).parent/'fixtures/shengbte_synthetic.log'


class CalculationTests(unittest.TestCase):
    def test_shengbte_log_final_stage_is_not_old_matrix_percent(self):
        result = parse_progress(FIXTURE.read_text('utf8'))
        self.assertEqual(result['program'], 'ShengBTE')
        self.assertEqual(result['phase'], 'finished')
        self.assertEqual(result['iteration'], 6)
        self.assertEqual(result['temperature_k'], 250)
        self.assertAlmostEqual(result['relative_change'], 8.5e-7)
        self.assertTrue(result['solver_converged'])
        self.assertTrue(result['native_footer'])
        self.assertNotIn('work_total', result)

    def test_matrix_elements_have_bounded_percentage_and_generic_eta(self):
        text = FIXTURE.read_text('utf8')
        before = parse_progress(text[:text.index(' Info: matrix elements:  30%')])
        later = parse_progress(text[:text.index(' Info: matrix elements:  50%')])
        a = estimate_progress({**before, 'job_id': '101', 'log_path': 'a.log'}, now=1000)
        b = estimate_progress({**later, 'job_id': '101', 'log_path': 'a.log'}, now=1060, previous=a)
        self.assertEqual(before['work_current'], 20)
        self.assertEqual(later['work_current'], 40)
        self.assertEqual(b['eta_state'], 'estimated')
        self.assertAlmostEqual(b['eta_seconds'], 180)
        self.assertIn('%/分', eta_labels(b, recent=True, running=True)[1])

    def test_bte_iteration_has_error_measure_but_no_fixed_total_or_eta(self):
        text = 'Info: Temperature= 300\nInfo: Iteration : 19 Timer : 20 seconds\nInfo: Relative change = .12D-04'
        result = parse_progress(text)
        self.assertEqual(result['phase'], 'bte_iteration')
        self.assertAlmostEqual(result['relative_change'], 1.2e-5)
        self.assertNotIn('work_total', result)
        self.assertEqual(estimate_progress(result, now=1000)['eta_state'], 'iteration')
        self.assertIn('相对变化', calculation_labels(result)[2])

    def test_temperature_change_resets_old_iteration_and_error(self):
        result = parse_progress('ShengBTE 2.1.0\nTemperature=300\nInfo: Iteration : 10\nRelative change = 1e-7\nTemperature=400')
        self.assertEqual(result['temperature_k'], 400)
        self.assertNotIn('iteration', result)
        self.assertNotIn('relative_change', result)
        self.assertNotIn('work_total', result)

    def test_phonon_dfpt_has_representation_and_inner_iteration_not_fake_percent(self):
        result = parse_progress('Program PHONON v.7.3.1\nCalculation of q = 0.25 0.43 0.25\nRepresentation # 5 mode # 5\niter # 7 total cpu time : 6.7 secs')
        self.assertEqual(result['program'], 'QE PH')
        self.assertEqual(result['representation'], 5)
        self.assertEqual(result['scf_iteration'], 7)
        self.assertNotIn('q_total', result)
        self.assertNotIn('work_total', result)
        self.assertIn('q =', calculation_labels(result)[2])

    def test_force_scf_then_forces_then_job_done(self):
        text = 'Program PWSCF v.7.3.1\niteration # 8\nestimated scf accuracy < 2.3E-8 Ry'
        result = parse_progress(text, 'logs/04_force_SCF_101.out')
        self.assertEqual(result['phase'], 'force_scf')
        self.assertEqual(result['scf_iteration'], 8)
        self.assertAlmostEqual(result['scf_accuracy_ry'], 2.3e-8)
        result = parse_progress(text+'\nconvergence has been achieved\nForces acting on atoms', 'force.out')
        self.assertEqual(result['phase'], 'forces')
        self.assertTrue(result['scf_converged'])
        result = parse_progress(text+'\nJOB DONE.', 'force.out')
        self.assertTrue(result['native_footer'])
        self.assertEqual(estimate_progress(result, now=1)['eta_state'], 'program_complete')

    def test_phonopy_stages_do_not_imply_force_calculations_done(self):
        result = parse_progress('phonopy\nForce constants are calculated\nMesh sampling')
        self.assertEqual(result['program'], 'Phonopy')
        self.assertEqual(result['phase'], 'mesh')
        self.assertNotIn('work_total', result)
        self.assertNotIn('native_footer', result)

    def test_explicit_unconverged_and_driver_error_are_shown(self):
        result = parse_progress('Program PWSCF v7\nconvergence NOT achieved after 100 iterations\nJOB DONE.')
        self.assertTrue(result['iteration_not_converged'])
        self.assertEqual(estimate_progress(result, now=1)['eta_state'], 'not_converged')
        error = parse_progress('Traceback (most recent call last):\nRuntimeError: failed')
        self.assertTrue(error['native_error'])

    def test_epw_progress_is_unchanged_and_not_misidentified_as_phonopy(self):
        result = parse_progress('Program EPW v6\nPhonopy force constants\nProgression iq (fine) = 10/100')
        self.assertEqual(result, {'q_current': 10, 'q_total': 100})

    def test_shengbte_mention_in_build_or_json_is_not_runtime_progress(self):
        self.assertEqual(parse_progress('Building ShengBTE\n{"program": "ShengBTE"}'), {})

    def test_force_batch_contract_validates_job_and_finished_counts(self):
        data = {'job_id': '101', 'program': 'force_batch', 'phase': 'ifc3', 'completed': 12, 'total': 128}
        result = parse_sidecar(json.dumps(data), '101')
        self.assertEqual(result['work_current'], 12)
        self.assertEqual(result['work_total'], 128)
        self.assertEqual(parse_sidecar(json.dumps(data), '102'), {})
        self.assertEqual(parse_sidecar('{broken', '101'), {})
        self.assertEqual(parse_sidecar(json.dumps({**data, 'program': []}), '101'), {})
        data['completed'] = 129
        self.assertNotIn('work_total', parse_sidecar(json.dumps(data), '101'))

    def test_batch_eta_resets_when_ifc_phase_or_temperature_changes(self):
        common = {'job_id': '101', 'log_path': 'monitor-progress.json', 'program': 'force_batch',
                  'phase': 'ifc2', 'work_current': 10, 'work_total': 128}
        a = estimate_progress(common, now=1000)
        b = estimate_progress({**common, 'work_current': 20}, now=1060, previous=a)
        self.assertEqual(b['eta_state'], 'estimated')
        for change in ({'phase': 'ifc3'}, {'temperature_k': 400}, {'phase_cycle': 2}):
            c = estimate_progress({**common, 'work_current': 30, **change}, now=1120, previous=b)
            self.assertEqual(c['eta_state'], 'collecting')

    def test_fixed_logs_require_job_window_and_no_shared_directory(self):
        text = '/home/user\nJobId=101 WorkDir=/home/user/calc StartTime=2026-11-09T12:00:00 EndTime=2026-11-09T13:00:00\n'
        self.assertEqual(discovery_window(text, '101'), ('2026-11-09T12:00:00', '2026-11-09T13:00:00'))
        self.assertIsNone(discovery_window(text+'__WIDGET_DIR__102|/home/user/calc\n', '101'))
        self.assertIsNone(discovery_window('WorkDir=/home/user/calc StartTime=Unknown', '101'))
        body = log_read_script('101', 'calc', [], window=discovery_window(text, '101'))
        self.assertIn('widget_since', body)
        self.assertIn('widget_mtime', body)
        self.assertIn('monitor-progress.json', body)
        self.assertNotIn('widget_since', log_read_script('101', 'calc', []))

    @unittest.skipUnless(os.name == 'posix', 'Generated SSH shell test runs on Linux')
    def test_generated_shell_filters_old_other_job_and_symlink_logs_and_bounds_reads(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            root = Path(folder)
            logs = root/'calc/logs'
            logs.mkdir(parents=True)
            now = int(time.time())
            fresh = logs/'09_official_QE_example.log'
            fresh.write_text(FIXTURE.read_text('utf8'))
            old = logs/'old.log'
            old.write_text('ShengBTE 2.1.0\nold content')
            os.utime(old, (now-3600, now-3600))
            other = logs/'force_999999.out'
            other.write_text('other job')
            secret = Path(outside)/'private.log'
            secret.write_text('outside-root-secret')
            (logs/'escape.log').symlink_to(secret)
            body = log_read_script('101', 'calc', ['calc/logs/escape.log'],
                                   window=(time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(now-60)), ''))
            command = 'set -eu\nexport LC_ALL=C\nroot='+shlex.quote(folder)+'\ncd "$root"\n'+body
            result = subprocess.run(['bash'], input=command, text=True, capture_output=True, timeout=10, check=True)
            records = read_log_records(result.stdout)
            self.assertEqual([entry[0] for entry in records], ['calc/logs/09_official_QE_example.log'])
            self.assertNotIn('outside-root-secret', result.stdout)
            for index in range(8):
                (logs/f'extra-{index}.log').write_text('x'*10000)
            result = subprocess.run(['bash'], input=command, text=True, capture_output=True, timeout=10, check=True)
            self.assertLessEqual(len(read_log_records(result.stdout)), 6)
            self.assertLess(len(result.stdout.encode()), 65536)


if __name__ == '__main__':
    unittest.main()
