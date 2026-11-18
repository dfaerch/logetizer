import os
import io
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from logetizer import LivePreview, PercentageHistory


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
        self.assertIn('25 more categories', text)
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


class PercentageHistoryTests(unittest.TestCase):
    def test_warmup_and_constant_history(self):
        history = PercentageHistory()
        history.add({'errors': 10, 'ok': 90})
        self.assertIsNone(history.score('errors', 13))
        history.add({'errors': 10, 'ok': 90})
        self.assertEqual(history.score('errors', 13), 3)
        self.assertEqual(history.deviation('errors', 13), (3, 3))
        self.assertEqual(history.score('errors', 8), -2)
        self.assertEqual(history.score('errors', 10.5), 0.5)

    def test_similar_startup_allows_third_bucket_detection(self):
        history = PercentageHistory()
        history.add({'errors': 9, 'ok': 91})
        history.add({'errors': 11, 'ok': 89})
        self.assertEqual(history.score('errors', 13), 3)
        self.assertEqual(history.score('errors', 7), -3)

    def test_variable_startup_waits_for_five_completed_buckets(self):
        history = PercentageHistory()
        for count in [10, 20, 10, 10]:
            history.add({'errors': count, 'ok': 100 - count})
            self.assertIsNone(history.score('errors', 30))
        history.add({'errors': 10, 'ok': 90})
        self.assertIsNotNone(history.score('errors', 30))

    def test_outliers_do_not_hide_later_spikes(self):
        history = PercentageHistory()
        for count in [9, 10, 11, 10, 10, 90]:
            history.add({'errors': count, 'ok': 100 - count})
        self.assertEqual(history.score('errors', 13), 3)
        before = list(history.buckets)
        history.score('errors', 90)
        self.assertEqual(list(history.buckets), before)

    def test_missing_categories_and_bounded_window(self):
        history = PercentageHistory(window=5)
        history.add({'new': 100})
        for _ in range(5):
            history.add({'ok': 100})
        history.add({})
        self.assertEqual(len(history.buckets), 5)
        self.assertEqual(history.score('new', 3), 3)


class AnomalyCliTests(unittest.TestCase):
    def test_colors_and_plain_output(self):
        with tempfile.TemporaryDirectory() as directory:
            rules = Path(directory) / 'test.rules'
            rules.write_text('errors: ERROR\nok: OK\n')
            lines = []
            for minute, errors in enumerate([10, 10, 10, 10, 10, 12, 15]):
                for index in range(100):
                    message = 'ERROR' if index < errors else 'OK'
                    lines.append(f'2026-11-18T12:{minute:02d}:00 {message}\n')
            command = [sys.executable, str(Path(__file__).with_name('logetizer.py')),
                       '--rules', str(rules), '--bucket-size', '1m', '--no-live']

            def run(*flags, env=None):
                return subprocess.run(command + list(flags), input=''.join(lines),
                                      text=True, capture_output=True, check=True, env=env).stdout

            colored = run('--anomalies', '--color', 'always')
            self.assertIn('(12% \x1b[33m+2pp\x1b[0m)', colored)
            self.assertIn('(15% \x1b[31m+5pp\x1b[0m)', colored)
            self.assertIn('(88% \x1b[33m-2pp\x1b[0m)', colored)
            self.assertIn('(85% \x1b[31m-5pp\x1b[0m)', colored)
            self.assertNotIn('\x1b', colored.split('0006')[0])
            plain = run('--anomalies')
            self.assertNotIn('\x1b', plain)
            self.assertIn('(15% +5pp)', plain)
            self.assertIn('(85% -5pp)', plain)
            self.assertEqual(re.sub(r' [+-][0-9.]+pp', '', plain), run('--percent'))
            self.assertEqual(run('--anomalies', '--color', 'never'), plain)
            self.assertEqual(run('--anomalies', env={**os.environ, 'NO_COLOR': ''}), plain)
            self.assertNotIn('%', run())


if __name__ == '__main__':
    unittest.main()
