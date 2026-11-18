import os
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from logetizer import LivePreview


class LivePreviewTests(unittest.TestCase):
    def test_tall_bucket_is_bounded_and_cleared_before_final_output(self):
        output = io.StringIO()
        preview = LivePreview(output)
        with patch('logetizer.shutil.get_terminal_size', return_value=os.terminal_size((80, 8))):
            for count in range(10):
                preview.draw([f'category {index}: {count}' for index in range(30)])
                self.assertEqual(preview.lines, 6)
            preview.clear()
        text = output.getvalue()
        self.assertEqual(preview.lines, 0)
        self.assertIn('...and 25 more.', text)
        self.assertNotIn('category 5:', text)
        self.assertEqual(text.count('\x1b[6A\r\x1b[J'), 10)
        # No redraw tries to reach into scrollback, even for a 30-row bucket.
        self.assertNotIn('\x1b[30A', text)

    def test_long_rows_cannot_wrap_and_clear_resets_cursor_accounting(self):
        output = io.StringIO()
        preview = LivePreview(output)
        with patch('logetizer.shutil.get_terminal_size', return_value=os.terminal_size((20, 8))):
            preview.draw(['long category ' * 20, 'second category'])
            preview.clear()
            preview.clear()
        self.assertEqual(output.getvalue().count('\x1b[2A'), 1)
        self.assertIn('\x1b[?7l\r\x1b[2K', output.getvalue())
        self.assertIn('\r\n\x1b[?7h', output.getvalue())


class ChangeCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.rules = Path(self.directory.name) / 'test.rules'
        self.rules.write_text('errors: ERROR\nok: OK\nnew: NEW\n!ignored: IGNORE\n')
        self.command = [sys.executable, '-B', str(Path(__file__).with_name('logetizer.py')),
                        '--rules', str(self.rules), '--bucket-size', '1m', '--no-live']
        self.logs = ''
        for minute, errors in enumerate([10, 12, 15, 0]):
            for index in range(100):
                message = 'ERROR' if index < errors else 'OK'
                self.logs += f'2026-11-18T12:{minute:02d}:00 {message}\n'
        self.logs += '2026-11-18T12:04:00 NEW\n'

    def run_cli(self, *flags, env=None):
        command = [arg for arg in self.command if not (arg == '--no-live' and '--live' in flags)]
        return subprocess.run(command + list(flags), input=self.logs,
                              text=True, capture_output=True, check=True, env=env).stdout

    def test_changes_compare_previous_bucket_and_keep_percentage_plain(self):
        colored = self.run_cli('--color-change', '--color', 'always')
        self.assertIn('(12% \x1b[31m+2pp\x1b[0m)', colored)
        self.assertIn('(15% \x1b[31m+3pp\x1b[0m)', colored)
        self.assertIn('errors: 0 (0% \x1b[32m-15pp\x1b[0m)', colored)
        self.assertIn('new: 1 (100% \x1b[31m+100pp\x1b[0m)', colored)
        self.assertNotIn('pp', colored.split('0002')[0])
        plain = self.run_cli('--color-change')
        self.assertNotIn('\x1b', plain)
        self.assertEqual(self.run_cli('--color-change', '--color', 'never'), plain)
        self.assertEqual(self.run_cli('--color-change', env={**os.environ, 'NO_COLOR': ''}), plain)
        self.assertNotIn('%', self.run_cli())
        self.assertNotIn('pp', self.run_cli('--percent'))

    def test_threshold_suppresses_small_changes(self):
        text = self.run_cli('--color-change', '--min-change', '3')
        self.assertNotIn('+2pp', text)
        self.assertIn('+3pp', text)

    def test_json_is_complete_uncolored_bucket_data(self):
        text = self.run_cli('--json', '--color-change', '--color', 'always', '--live')
        records = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(len(records), 5)
        self.assertEqual(records[0], dict(version=1, bucket=1, start='2026-11-18T12:00:00',
                                          bucket_size=60, total=100, counts={'errors': 10, 'ok': 90}))
        self.assertNotIn('errors', records[3]['counts'])
        self.assertEqual(records[4]['counts'], {'new': 1})
        self.assertNotIn('\x1b', text)
        analyzer = subprocess.run([sys.executable, '-B', str(Path(__file__).with_name('bucketlyzer.py')),
                                   '--json', '--min-count', '1'], input=text, text=True,
                                  capture_output=True, check=True)
        findings = [json.loads(line) for line in analyzer.stdout.splitlines()]
        self.assertEqual(len(findings), 5)
        self.assertTrue(any(f['method'] == 'new' for f in findings[-1]['findings']))

    def test_json_respects_silent_rules_misses_and_recount(self):
        self.logs = ('2026-11-18T12:00:00 ERROR OK\n'
                     '2026-11-18T12:00:00 IGNORE\n'
                     '2026-11-18T12:00:00 unmatched\n')
        record = json.loads(self.run_cli('--json', '--recount', '--misses'))
        self.assertEqual(record['counts'], {'errors': 1, 'ok': 1, 'Misses': 1})
        self.assertEqual(record['total'], 3)

    def test_invalid_options_fail(self):
        for flags in [('--min-change', 'nan'), ('--min-change', '-1'),
                      ('--json', '--debug-print-miss'), ('--anomalies',)]:
            result = subprocess.run(self.command + list(flags), input='', text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
