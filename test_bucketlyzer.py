import json
from pathlib import Path
import subprocess
import sys
import unittest

from bucketlyzer import Analyzer, PercentageHistory, validate_bucket


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


def bucket(counts, number=1):
    return dict(version=1, bucket=number, start='2026-11-18T00:00:00',
                bucket_size=600, total=sum(counts.values()), counts=counts)


class AnalyzerTests(unittest.TestCase):
    def test_new_category_threshold_and_no_first_bucket_findings(self):
        analyzer = Analyzer(methods=['new'])
        self.assertEqual(analyzer.analyze(bucket({'ok': 1000})), [])
        self.assertEqual(analyzer.analyze(bucket({'ok': 1000, 'tiny': 3})), [])
        findings = analyzer.analyze(bucket({'ok': 1000, 'new': 100}))
        self.assertEqual([(f['name'], f['method']) for f in findings], [('new', 'new')])
        self.assertEqual(analyzer.analyze(bucket({'ok': 1000, 'new': 100})), [])

    def test_uniform_traffic_growth_is_not_a_share_change(self):
        analyzer = Analyzer()
        analyzer.analyze(bucket({'ok': 990, 'error': 10}))
        self.assertEqual(analyzer.analyze(bucket({'ok': 9900, 'error': 100})), [])

    def test_rare_category_spike_and_disappearance(self):
        analyzer = Analyzer(methods=['share'])
        analyzer.analyze(bucket({'ok': 9990, 'error': 10}))
        findings = analyzer.analyze(bucket({'ok': 9900, 'error': 100}))
        error = next(f for f in findings if f['name'] == 'error')
        self.assertAlmostEqual(error['excess'], 90)
        self.assertAlmostEqual(error['relative_change'], 900)
        findings = analyzer.analyze(bucket({'ok': 10000}))
        self.assertTrue(any(f['name'] == 'error' and f['excess'] < 0 for f in findings))

    def test_zero_to_five_is_below_default_thresholds(self):
        analyzer = Analyzer()
        analyzer.analyze(bucket({'ok': 10000}))
        self.assertEqual(analyzer.analyze(bucket({'ok': 9995, 'error': 5})), [])

    def test_robust_method_remains_available(self):
        analyzer = Analyzer(methods=['robust'])
        for _ in range(2):
            analyzer.analyze(bucket({'ok': 90, 'error': 10}))
        findings = analyzer.analyze(bucket({'ok': 85, 'error': 15}))
        self.assertEqual({f['method'] for f in findings}, {'robust'})
        self.assertEqual(next(f['score'] for f in findings if f['name'] == 'error'), 5)

    def test_robust_finding_counts_use_the_filtered_baseline(self):
        analyzer = Analyzer(methods=['robust'])
        for errors in [10, 10, 10, 10, 90]:
            analyzer.analyze(bucket({'ok': 100 - errors, 'error': errors}))
        finding = next(f for f in analyzer.analyze(bucket({'ok': 85, 'error': 15}))
                       if f['name'] == 'error')
        self.assertEqual(finding['baseline'], 10)
        self.assertEqual(finding['expected'], 10)
        self.assertEqual(finding['excess'], 5)

    def test_cli_text_colors_and_json_findings(self):
        command = [sys.executable, '-B', str(Path(__file__).with_name('bucketlyzer.py'))]
        data = '\n'.join(json.dumps(record) for record in [
            bucket({'ok': 1000}), bucket({'ok': 900, 'new': 100}, 2)])
        result = subprocess.run(command + ['--color', 'always'], input=data,
                                text=True, capture_output=True, check=True)
        self.assertIn('new: 100 (10.00% \x1b[31m+10.00pp\x1b[0m)', result.stdout)
        self.assertIn('[new;', result.stdout)
        result = subprocess.run(command + ['--json', '--color', 'always'], input=data,
                                text=True, capture_output=True, check=True)
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(records[0]['findings'], [])
        self.assertEqual(records[1]['findings'][0]['method'], 'new')
        self.assertNotIn('\x1b', result.stdout)

    def test_invalid_json_and_schema_report_input_line(self):
        command = [sys.executable, '-B', str(Path(__file__).with_name('bucketlyzer.py'))]
        for bad in ['not json', '[]', json.dumps(bucket({'ok': -1})),
                    json.dumps(dict(bucket({'ok': 1}), total=2))]:
            result = subprocess.run(command, input=json.dumps(bucket({'ok': 1})) + '\n' + bad,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('line 2:', result.stderr)
            self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
