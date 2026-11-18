#!/usr/bin/env python3
"""Count syslog regex matches in fixed time buckets."""
import argparse
import codecs
import os
import select
import time
from collections import defaultdict
from datetime import datetime, timedelta
import re
import sys

SYSLOG_TS = re.compile(r'^\s*(?P<stamp>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})(?=\s)')
ISO_TS = re.compile(r'^\s*(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)(?=\s)')
DURATION = re.compile(r'^(\d+)([smhd])$', re.I)
MONTHS = {month: i for i, month in enumerate(('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}


def parse_duration(value):
    match = DURATION.fullmatch(value)
    if not match or int(match[1]) < 1:
        raise argparse.ArgumentTypeError('bucket size must be positive, e.g. 30s, 10m, 2h, 1d')
    return int(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2].lower()]


def parse_time(value, year):
    value = ' '.join(value.split())
    for fmt in ('%Y%m%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    try:
        return datetime.strptime(f'{year} {value}', '%Y %b %d %H:%M:%S')
    except ValueError:
        raise ValueError(f'bad date {value!r}; expected YYYYMMDD HH:MM:SS or "Dec 24 13:39:01"') from None


GROUP_REF = re.compile(r'\$(\d+|\$)')


def expand_name(template, match):
    """Expand $1, $2, ... and $$ (literal dollar) in a category name."""
    def replace(ref):
        key = ref.group(1)
        if key == '$':
            return '$'
        return match.group(int(key)) or ''
    return GROUP_REF.sub(replace, template)


def load_patterns(path):
    rules = []
    with open(path, encoding='utf-8') as file:
        for lineno, line in enumerate(file, 1):
            line = line.rstrip('\n')
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            if ':' not in line:
                raise ValueError(f'{path}:{lineno}: expected "Name: regex"')
            name, pattern = line.split(':', 1)
            name, pattern = name.strip(), pattern.strip()
            if not name or not pattern:
                raise ValueError(f'{path}:{lineno}: missing name or regex')
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                raise ValueError(f'{path}:{lineno}: invalid regex: {exc}') from exc
            for ref in GROUP_REF.finditer(name):
                if ref.group(1) != '$' and int(ref.group(1)) > regex.groups:
                    raise ValueError(f'{path}:{lineno}: ${ref.group(1)} refers to missing capture group')
            silent = name.startswith("!")
            if silent:
                name = name[1:].strip()
                if not name:
                    raise ValueError(f"{path}:{lineno}: missing name after !")
            rules.append((name, regex, silent))
    if not rules:
        raise ValueError(f'{path}: no patterns found')
    return rules


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--rules', action='append', required=True, metavar='FILE', help='one "Name: regex" per line; repeat to load rule files in order')
    parser.add_argument('--bucket-size', type=parse_duration, default=600, metavar='SIZE', help='e.g. 30s, 10m, 1h')
    parser.add_argument('--from', dest='from_time', metavar='TIME', help='inclusive, quoted timestamp')
    parser.add_argument('--to', dest='to_time', metavar='TIME', help='inclusive, quoted timestamp')
    matching_group = parser.add_mutually_exclusive_group()
    matching_group.add_argument('--exclusive', action='store_true', default=True, help='count only first matching regex rule per line')
    matching_group.add_argument('--recount', dest='exclusive', action='store_false', help='count every matching regex rule per line')
    parser.add_argument('--misses', action='store_true', help='count unmatched lines in a Misses category')
    parser.add_argument('--percent', action='store_true', help='append percentage of visible counts within each bucket')
    parser.add_argument('--no-newline', action='store_true', help='omit the blank line after each completed bucket')
    parser.add_argument('--debug', action='store_true', help='show each eligible log line and rule matches on stderr')
    parser.add_argument('--debug-print-miss', action='store_true', help='print only raw lines matching no regex rule (including silent rules); suppress all other output')
    parser.add_argument('--skip', type=int, default=0, metavar='N', help='skip N initial input lines without parsing them')
    live_group = parser.add_mutually_exclusive_group()
    live_group.add_argument('--live', dest='live_mode', action='store_const', const=True, help='force live redraw on a terminal')
    live_group.add_argument('--no-live', dest='live_mode', action='store_const', const=False, help='disable automatic live redraw')
    parser.set_defaults(live_mode=None)
    parser.add_argument('--year', type=int, default=datetime.now().year, help='year for classic syslog timestamps')
    parser.add_argument('--align', choices=('clock', 'first'), default='clock', help='clock: bucket on fixed wall-clock boundaries; first: anchor at first eligible log line')
    args = parser.parse_args()

    try:
        rules = []
        for path in args.rules:
            rules.extend(load_patterns(path))
        lower = parse_time(args.from_time, args.year) if args.from_time else None
        upper = parse_time(args.to_time, args.year) if args.to_time else None
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.skip < 0:
        parser.error('--skip must be nonnegative')
    if lower and upper and lower > upper:
        parser.error('--from must not be later than --to')

    seconds = args.bucket_size
    # Keep only the currently open bucket in memory. Later timestamps close it.
    active_bucket = None
    active_counts = defaultdict(int)
    name_order = {}
    bucket_index = 0
    late_lines = 0
    last_emitted = None
    terminal = sys.stdout.isatty()
    live = args.live_mode is True and terminal and not args.debug_print_miss
    if args.live_mode is True and not terminal and not args.debug_print_miss:
        print('Warning: --live requires a terminal on stdout; using normal streaming output', file=sys.stderr)
    auto_live = terminal and args.live_mode is None and not args.debug_print_miss
    rendered_lines = 0
    dirty = False
    last_redraw = 0.0
    redraw_interval = 0.2
    input_started_at = time.monotonic()
    scanning_dt = None
    scanning_dirty = False
    progress_visible = False
    last_progress_redraw = 0.0

    def clear_progress():
        """Remove the temporary pre---from status without leaving terminal artifacts."""
        nonlocal progress_visible
        if progress_visible:
            sys.stdout.write('\x1b[1A\r\x1b[2K')
            sys.stdout.flush()
            progress_visible = False

    def display_progress():
        nonlocal scanning_dirty, progress_visible, last_progress_redraw
        if not live or scanning_dt is None or active_bucket is not None:
            return
        text = f'Scanning: {scanning_dt:%Y%m%d %H:%M:%S} / {lower:%Y%m%d %H:%M:%S}'
        if progress_visible:
            sys.stdout.write('\x1b[1A')
        sys.stdout.write('\r\x1b[2K' + text + '\n')
        sys.stdout.flush()
        progress_visible = True
        scanning_dirty = False
        last_progress_redraw = time.monotonic()

    def bucket_rows():
        """Produce the identical output lines for final and live rendering."""
        total = sum(active_counts.values())
        for name in sorted(active_counts, key=lambda name: (name == 'Misses', name.casefold())):
            count = active_counts[name]
            percent = f' ({count * 100 / total:.0f}%)' if args.percent and total else ''
            yield f'{bucket_index + 1:04d} {active_bucket:%Y%m%d %H:%M:%S} {name}: {count}{percent}'

    def display_live():
        """Rewrite only the current bucket, leaving completed rows untouched."""
        nonlocal rendered_lines, dirty, last_redraw
        if not live or active_bucket is None:
            return
        rows = list(bucket_rows())
        if rendered_lines:
            # Cursor is positioned immediately below the previously drawn rows.
            sys.stdout.write(f'\x1b[{rendered_lines}A')
        for row in rows:
            sys.stdout.write('\r\x1b[2K' + row + '\n')
        for _ in range(rendered_lines - len(rows)):
            sys.stdout.write('\r\x1b[2K\n')
        # Counts only increase, so rows should never shrink, but handle it anyway.
        if rendered_lines > len(rows):
            sys.stdout.write(f'\x1b[{rendered_lines - len(rows)}A')
        sys.stdout.flush()
        rendered_lines = len(rows)
        dirty = False
        last_redraw = time.monotonic()

    def emit_bucket():
        nonlocal active_bucket, active_counts, bucket_index, last_emitted, name_order, rendered_lines, dirty
        if active_bucket is None:
            return
        if active_counts:
            if live:
                display_live()  # Final redraw, if required. Rows already remain on screen.
            else:
                for row in bucket_rows():
                    print(row, flush=True)
            if not args.no_newline:
                print(flush=True)  # Separate completed buckets, including in live TTY mode.
            bucket_index += 1
        rendered_lines = 0
        dirty = False
        last_emitted = active_bucket
        active_bucket = None
        active_counts = defaultdict(int)
        name_order = {}

    def maybe_flush_live():
        # Do not let the clock flush arbitrary historical/backfill input.
        # A bucket is live if its end is within one bucket duration of now.
        if active_bucket is None:
            return
        end = active_bucket + timedelta(seconds=seconds)
        now = datetime.now()
        if end <= now and now - end <= timedelta(seconds=seconds):
            emit_bucket()

    def input_lines():
        """Read incrementally without buffering ahead of select().

        Wake up for redraws and for auto-live detection even when stdin is idle.
        """
        nonlocal live
        fd = sys.stdin.fileno()
        decoder = codecs.getincrementaldecoder(sys.stdin.encoding or 'utf-8')('replace')
        pending = ''
        to_skip = args.skip
        while True:
            now_mono = time.monotonic()
            if auto_live and not live and now_mono - input_started_at >= 1.0:
                live = True
                if active_bucket is not None:
                    display_live()
                elif scanning_dirty:
                    display_progress()

            timeouts = []
            if auto_live and not live:
                timeouts.append(max(0.0, 1.0 - (now_mono - input_started_at)))
            if live and dirty and active_bucket is not None:
                timeouts.append(max(0.0, redraw_interval - (now_mono - last_redraw)))
            if live and scanning_dirty and active_bucket is None:
                timeouts.append(max(0.0, redraw_interval - (now_mono - last_progress_redraw)))
            if active_bucket is not None:
                end = active_bucket + timedelta(seconds=seconds)
                now = datetime.now()
                if timedelta(0) <= end - now <= timedelta(seconds=seconds):
                    timeouts.append(max(0.0, (end - now).total_seconds()))
            timeout = min(timeouts) if timeouts else None
            readable, _, _ = select.select([fd], [], [], timeout)
            if not readable:
                if live and dirty and active_bucket is not None and time.monotonic() - last_redraw >= redraw_interval:
                    display_live()
                if live and scanning_dirty and active_bucket is None and time.monotonic() - last_progress_redraw >= redraw_interval:
                    display_progress()
                maybe_flush_live()
                continue
            data = os.read(fd, 65536)
            if to_skip and data:
                # Skip entire lines at the byte level: no timestamp parsing,
                # regex matching or text decoding during fast-forward.
                newlines = data.count(b'\n')
                if newlines < to_skip:
                    to_skip -= newlines
                    continue
                offset = -1
                for _ in range(to_skip):
                    offset = data.find(b'\n', offset + 1)
                data = data[offset + 1:]
                to_skip = 0
            if not data:
                pending += decoder.decode(b'', final=True)
                if pending:
                    yield pending
                return
            pending += decoder.decode(data)
            while '\n' in pending:
                line, pending = pending.split('\n', 1)
                yield line + '\n'
    year = args.year
    last_month = None
    anchor = None
    bad_lines = 0
    first_after_skip = args.skip > 0
    first_in_range = lower is not None
    cached_stamp = None
    cached_dt = None
    cached_kind = None
    if not args.debug_print_miss:
        print(f'Time Bucket size = {seconds // 60}m' if seconds % 60 == 0 else f'Time Bucket size = {seconds}s', flush=True)
    interrupted = False
    try:
        for line in input_lines():
            if first_after_skip and not args.debug_print_miss:
                # Reference the exact first raw line after fast-forwarding.
                print(f'After --skip {args.skip}: {line.rstrip()}', file=sys.stderr, flush=True)
                first_after_skip = False
            if auto_live and not live and time.monotonic() - input_started_at >= 1.0:
                live = True
                if active_bucket is not None:
                    display_live()
                elif scanning_dirty:
                    display_progress()
            match = SYSLOG_TS.match(line)
            if match:
                stamp = match['stamp']
                if cached_kind == 'syslog' and stamp == cached_stamp:
                    dt = cached_dt
                else:
                    # Reuse the parsed datetime for consecutive lines sharing a timestamp.
                    # Year rollover still works because a changed stamp always gets parsed.
                    month_name, day, clock = stamp.split()
                    month = MONTHS.get(month_name)
                    if month is None:
                        bad_lines += 1
                        continue
                    if last_month == 12 and month == 1:
                        year += 1
                    last_month = month
                    try:
                        hour, minute, second = map(int, clock.split(':'))
                        dt = datetime(year, month, int(day), hour, minute, second)
                    except ValueError:
                        bad_lines += 1
                        continue
                    cached_kind, cached_stamp, cached_dt = 'syslog', stamp, dt
            else:
                match = ISO_TS.match(line)
                if not match:
                    bad_lines += 1
                    continue
                stamp = match['stamp']
                if cached_kind == 'iso' and stamp == cached_stamp:
                    dt = cached_dt
                else:
                    try:
                        dt = datetime.fromisoformat(stamp.replace('Z', '+00:00')).replace(tzinfo=None)
                    except ValueError:
                        bad_lines += 1
                        continue
                    cached_kind, cached_stamp, cached_dt = 'iso', stamp, dt

            if lower is not None and dt < lower:
                scanning_dt = dt
                scanning_dirty = True
                if live and time.monotonic() - last_progress_redraw >= redraw_interval:
                    display_progress()
                continue
            if upper is not None and dt > upper:
                continue
            if args.debug_print_miss:
                # Standalone diagnostic output: no headers, bucket output or debug logs.
                # Silent (!) rules still count as matches, so they are not printed.
                if not any(regex.search(line) for _, regex, _ in rules):
                    sys.stdout.write(line)
                    sys.stdout.flush()
                continue
            if progress_visible:
                clear_progress()
            scanning_dirty = False
            if first_in_range and not args.debug_print_miss:
                print(f'Reached --from: {line.rstrip()}', file=sys.stderr, flush=True)
                first_in_range = False
            if anchor is None:
                anchor = dt
            if args.align == 'first':
                bucket = anchor + timedelta(seconds=((dt - anchor).total_seconds() // seconds) * seconds)
            else:
                midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
                bucket = midnight + timedelta(seconds=((dt - midnight).total_seconds() // seconds) * seconds)
            if active_bucket is not None and bucket > active_bucket:
                emit_bucket()
            if active_bucket is None:
                # A previously emitted bucket cannot be reopened.
                if last_emitted is not None and bucket <= last_emitted:
                    late_lines += 1
                    if args.debug:
                        print(f'  LATE line skipped: {line.rstrip()}', file=sys.stderr)
                    continue
                active_bucket = bucket
            elif bucket < active_bucket:
                late_lines += 1
                if args.debug:
                    print(f'  LATE line skipped: {line.rstrip()}', file=sys.stderr)
                continue
            matched_any = False
            if args.debug:
                print(f'LINE {dt:%Y%m%d %H:%M:%S} [{bucket:%Y%m%d %H:%M:%S}] {line.rstrip()}', file=sys.stderr)
            for rule_number, (template, regex, silent) in enumerate(rules, 1):
                found = regex.search(line)
                if found is None:
                    continue
                matched_any = True
                name = expand_name(template, found)
                if not silent:
                    active_counts[name] += 1
                    if name not in name_order:
                        name_order[name] = len(name_order)
                if args.debug:
                    kind = 'IGNORE' if silent else 'MATCH'
                    print(f'  {kind} rule={rule_number} category={name!r} regex={regex.pattern!r} groups={found.groups()!r}', file=sys.stderr)
                if args.exclusive:
                    break
            if not matched_any:
                if args.misses:
                    active_counts['Misses'] += 1
                    if 'Misses' not in name_order:
                        name_order['Misses'] = len(name_order)
                if args.debug:
                    print('  MISS (counted)' if args.misses else '  MISS', file=sys.stderr)
            dirty = True
            if live and time.monotonic() - last_redraw >= redraw_interval:
                display_live()

    except KeyboardInterrupt:
        # Preserve the current bucket as final plain text and exit without traceback.
        interrupted = True

    clear_progress()
    emit_bucket()
    if late_lines and not args.debug_print_miss:
        print(f'Warning: skipped {late_lines} late lines for already closed buckets', file=sys.stderr)
    if bad_lines and not args.debug_print_miss:
        print(f'Warning: skipped {bad_lines} lines without valid supported timestamps', file=sys.stderr)
    if interrupted:
        return 130


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Covers an interrupt before the streaming loop starts.
        sys.exit(130)

