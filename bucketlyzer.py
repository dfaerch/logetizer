#!/usr/bin/env python3
"""Analyze Logetizer JSON Lines buckets for new categories and changes in share."""
import argparse
from collections import deque
from datetime import datetime
import json
import math
import os
from statistics import mean, median, pstdev
import sys


class PercentageHistory:
    """Bounded history for the robust percentage-share method."""

    def __init__(self, window=100):
        self.buckets = deque(maxlen=window)

    def score(self, name, percentage):
        deviation = self.deviation(name, percentage)
        return deviation[1] if deviation is not None else None

    def deviation(self, name, percentage):
        """Return (percentage-point change, sigma score), or None during warmup."""
        if len(self.buckets) < 2:
            return None
        values = [bucket.get(name, 0.0) for bucket in self.buckets]
        # Two similar completed buckets are enough for an early baseline.
        # With a variable startup, wait for more evidence before clipping.
        if len(values) < 5 and max(values) - min(values) > 2.0:
            return None
        center = median(values)
        # Median absolute deviation seeds clipping without letting a spike
        # inflate the standard deviation and hide subsequent spikes.
        spread = 1.4826 * median(abs(value - center) for value in values)
        typical = [value for value in values if abs(value - center) <= max(2 * spread, 1.0)]
        # A one percentage-point floor handles constant histories and avoids
        # highlighting negligible changes when the measured variance is tiny.
        change = percentage - mean(typical)
        return change, change / max(pstdev(typical), 1.0)

    def add(self, counts):
        total = sum(counts.values())
        if total:
            self.buckets.append({name: count * 100 / total for name, count in counts.items()})


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def nonnegative_number(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError('must be finite and nonnegative')
    return number


def validate_bucket(record):
    if not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] != 1:
        raise ValueError('expected a version 1 bucket object')
    for key in ('bucket', 'bucket_size', 'total'):
        value = record.get(key)
        if type(value) is not int or value < 1:
            raise ValueError(f'{key} must be a positive integer')
    if not isinstance(record.get('start'), str):
        raise ValueError('start must be an ISO timestamp')
    try:
        datetime.fromisoformat(record['start'])
    except ValueError:
        raise ValueError('start must be an ISO timestamp') from None
    counts = record.get('counts')
    if not isinstance(counts, dict) or any(
        not isinstance(name, str) or type(count) is not int or count < 0
        for name, count in counts.items()
    ):
        raise ValueError('counts must map category names to nonnegative integers')
    if sum(counts.values()) != record['total']:
        raise ValueError('total must equal the sum of counts')
    return record


class Analyzer:
    def __init__(self, methods=('new', 'share'), window=100, min_count=10,
                 min_excess=10, min_change=50, sigma=2):
        self.methods = methods
        self.history = PercentageHistory(window)
        self.seen = set()
        self.min_count = min_count
        self.min_excess = min_excess
        self.min_change = min_change
        self.sigma = sigma

    def analyze(self, record):
        counts, total = record['counts'], record['total']
        findings = []
        names = set(counts)
        for bucket in self.history.buckets:
            names.update(bucket)
        for name in sorted(names, key=lambda name: (name == 'Misses', name.casefold())):
            count = counts.get(name, 0)
            percentage = count * 100 / total
            if not self.history.buckets:
                continue  # The first bucket establishes the baseline.
            baseline = mean(bucket.get(name, 0.0) for bucket in self.history.buckets)
            expected = baseline * total / 100
            excess = count - expected
            common = dict(name=name, count=count, percentage=percentage,
                          baseline=baseline, change_pp=percentage - baseline,
                          expected=expected, excess=excess)
            if 'new' in self.methods and name not in self.seen and count >= self.min_count:
                findings.append(dict(common, method='new'))
            # New categories have their own signal; no infinite relative change.
            if 'share' in self.methods and baseline > 0:
                relative = (percentage - baseline) / baseline * 100
                if abs(excess) >= self.min_excess and abs(relative) >= self.min_change:
                    findings.append(dict(common, method='share', relative_change=relative))
            if 'robust' in self.methods:
                deviation = self.history.deviation(name, percentage)
                if deviation is not None and abs(deviation[1]) >= self.sigma:
                    change, score = deviation
                    findings.append(dict(common, method='robust', change_pp=change,
                                         baseline=percentage - change, score=score,
                                         expected=(percentage - change) * total / 100,
                                         excess=change * total / 100))
        self.history.add(counts)
        self.seen.update(name for name, count in counts.items() if count > 0)
        return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--method', action='append', choices=('new', 'share', 'robust'),
                        help='repeat to combine methods; default: new and share')
    parser.add_argument('--window', type=positive_int, default=100, help='previous buckets used as history; robust requires at least 5')
    parser.add_argument('--min-count', type=positive_int, default=10, help='minimum occurrences for a new category')
    parser.add_argument('--min-excess', type=positive_int, default=10, help='minimum absolute excess events for share findings')
    parser.add_argument('--min-change', type=nonnegative_number, default=50, help='minimum relative change in share, in percent')
    parser.add_argument('--sigma', type=nonnegative_number, default=2, help='minimum absolute robust score')
    parser.add_argument('--color', choices=('auto', 'always', 'never'), default='auto')
    parser.add_argument('--json', action='store_true', help='emit one analysis object per input bucket, including empty findings')
    args = parser.parse_args()
    if 'robust' in (args.method or ()) and args.window < 5:
        parser.error('--window must be at least 5 for the robust method')
    analyzer = Analyzer(args.method or ('new', 'share'), args.window, args.min_count,
                        args.min_excess, args.min_change, args.sigma)
    colors = args.color == 'always' or (args.color == 'auto' and sys.stdout.isatty() and 'NO_COLOR' not in os.environ)
    for lineno, line in enumerate(sys.stdin, 1):
        if not line.strip():
            continue
        try:
            record = validate_bucket(json.loads(line))
        except (ValueError, TypeError) as exc:
            print(f'bucketlyzer: line {lineno}: {exc}', file=sys.stderr)
            return 2
        findings = analyzer.analyze(record)
        if args.json:
            print(json.dumps(dict(record, findings=findings), ensure_ascii=False), flush=True)
            continue
        for finding in findings:
            delta = f"{finding['change_pp']:+.2f}pp"
            if colors:
                color = 31 if finding['change_pp'] >= 0 else 32
                delta = f'\x1b[{color}m{delta}\x1b[0m'
            detail = f"excess {finding['excess']:+.1f}"
            if finding['method'] == 'robust':
                detail = f"score {finding['score']:+.2f} sigma"
            print(f"{record['bucket']:04d} {record['start']} {finding['name']}: "
                  f"{finding['count']} ({finding['percentage']:.2f}% {delta}) "
                  f"[{finding['method']}; {detail}]", flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
