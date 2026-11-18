# Logetizer

A tool for grouping syslog-compatible log entries into time buckets.

Logetizer searches logs using regular expressions and counts matches in time buckets. Use it to answer questions such as:

- How many successful and failed logins occur every 10 minutes?
- What percentage of matches represent errors throughout the day?
- Which types of errors occur most often, and does that change throughout the day?

## Running

Logetizer requires Python 3 and uses only the standard library. It reads logs from standard input. Select one or more rule files with `--rules` and optionally set the bucket size:

```
$ tail -n 120000 /var/log/maillog | ./logetizer.py --rules test.rules --bucket-size 30m
Time Bucket size = 30m
0001 20261118 12:00:00 errors: 112
0001 20261118 12:00:00 logins: 5563

0002 20261118 12:30:00 errors: 296
0002 20261118 12:30:00 logins: 21398

0003 20261118 13:00:00 errors: 660
0003 20261118 13:00:00 logins: 20962
```

Repeat `--rules` to load multiple files. Files are loaded in command-line order, and rules within each file keep their original order. By default, the first matching rule across all files wins:

```sh
./logetizer.py --rules r.file.1 --rules some-other-file < /var/log/maillog
```

To process a complete file, use input redirection:

```sh
./logetizer.py --rules test.rules < /var/log/maillog
```

To follow new log entries:

```sh
tail -f /var/log/maillog | ./logetizer.py --rules test.rules
```

On a terminal, Logetizer automatically starts updating the current bucket in place if input continues for more than a second. Use `--live` to enable this immediately or `--no-live` to print completed buckets without redrawing. Redirected output always uses ordinary text lines. Live previews are limited to the terminal height, with an overflow summary for additional categories. Long preview rows do not wrap. When a bucket closes, its preview is cleared and every final row is printed once, so scrollback contains complete results rather than intermediate counts. The last bucket is printed when input ends or you press Ctrl+C.

### Bucket size and alignment

The default bucket size is 10 minutes. Use `--bucket-size` with a positive integer followed by `s`, `m`, `h`, or `d` for seconds, minutes, hours, or days:

```sh
./logetizer.py --rules test.rules --bucket-size 30s < /var/log/maillog
```

By default, buckets align to clock boundaries measured from midnight. With `--bucket-size 30m`, a line at 12:17 belongs to the bucket starting at 12:00. Use `--align first` to start the first bucket at the timestamp of the first eligible log line instead.

### Selecting a time range

Use `--from` and `--to` to count only lines within a time range. Both limits are inclusive:

```sh
./logetizer.py --rules test.rules \
  --from '2026-11-18 12:00:00' --to '2026-11-18 12:59:59' \
  < /var/log/maillog
```

Accepted limit formats are `YYYY-MM-DD HH:MM:SS`, `YYYYMMDD HH:MM:SS`, and classic syslog timestamps such as `Nov 18 12:00:00`.

Classic syslog timestamps have no year, so Logetizer assumes the current year. Use `--year 2025` when processing older logs. A December-to-January transition advances the inferred year by one. The `--year` option also supplies the year for classic syslog timestamps in `--from` and `--to`.

Input lines must begin with a classic syslog timestamp or an ISO timestamp such as `2026-11-18T12:00:00Z`. ISO timezone offsets are accepted, but times are grouped as written, without conversion to a common timezone. Lines without a supported, valid timestamp are skipped and reported on stderr.

Process logs in chronological order. Once a bucket closes, later input belonging to that bucket or an earlier one is skipped and reported on stderr.

### Counting options

By default, only the first matching rule adds a count for each log line, even if its regex occurs several times in that line. Use `--recount` to let a line count under several rules. Regexes search the entire line, including the timestamp and hostname.

| Option | Effect |
| --- | --- |
| `--exclusive` | Stop at the first matching rule for each line, in file and rule order (default). |
| `--recount` | Count every matching rule for each line. |
| `--misses` | Count lines that match no rule in a `Misses` category. |
| `--percent` | Append each category's share of the bucket's visible counts, rounded to a whole percentage. |
| `--skip N` | Skip the first N input lines before parsing timestamps or matching rules. |

For example, to show the share of each category while counting each line at most once:

```sh
./logetizer.py --rules test.rules --misses --percent < /var/log/maillog
```

With `--recount`, a line can contribute more than one count, so percentages describe the share of counts rather than the share of input lines.

### Changes from the previous bucket

Use `--color-change` to show each category's percentage and its change from the previous completed bucket:

```sh
tail -f /var/log/maillog | ./logetizer.py --rules test.rules --color-change
```

For example, `(9% +3pp)` means the share rose from 6% to 9%. Only the change is colored: red for increases and green for decreases, indicating direction rather than severity. The first bucket has no comparison. An absent category has a 0% share; categories disappearing since the previous bucket get a zero-count row so their decrease is visible. Empty time intervals are omitted, so comparisons can span gaps in filtered input.

`--min-change PP` controls the minimum absolute change displayed, in percentage points (default `0.1`). Changes are computed before rounding and displayed to one decimal place, with trailing zeros omitted; changes that round to zero are omitted. Percentages retain whole-number rounding. Live comparisons always use the previous completed bucket, so redraws don't affect the baseline.

Colors are automatic on terminals unless `NO_COLOR` is set. Use `--color always` to retain colors through pipes, or `--color never` to disable them. Signed changes remain visible without colors. `--percent` alone displays percentages without changes.

### JSON Lines output

Use `--json` to emit one JSON object per finalized, nonempty bucket. There is no header, blank separator, ANSI coloring or live preview, even with `--live`. Diagnostics remain on stderr. The last bucket is emitted on EOF or Ctrl+C and may represent only part of the configured time interval. `--json` cannot be combined with `--debug-print-miss`.

```sh
./logetizer.py --rules test.rules --bucket-size 6h --misses --json < /var/log/maillog > buckets.jsonl
```

Each record has this schema:

```json
{"version": 1, "bucket": 1, "start": "2026-11-18T00:00:00", "bucket_size": 21600, "total": 1000, "counts": {"errors": 10, "logins": 990}}
```

`bucket_size` is in seconds. `total` is the sum of visible category counts, including `Misses` when requested and excluding silent matches. With `--recount`, it can exceed the number of input lines. Counts retain their full precision; consumers calculate shares from counts and total. `start` uses the same unconverted local timestamp as text output. Text display options do not alter the exported counts.

## Bucketlyzer

Note: Bucketlyzer is highly experimental and may produce misleading findings.

Bucketlyzer analyzes Logetizer JSON Lines from stdin. It uses only the Python standard library.

```sh
./logetizer.py --rules test.rules --bucket-size 6h --misses --json < /var/log/maillog |
  ./bucketlyzer.py --min-count 20 --min-excess 50
```

By default it combines `new` and `share` methods. Select methods explicitly with repeated `--method` options:

| Method | Behavior |
| --- | --- |
| `new` | Report a category never previously observed with a positive count, when its current count reaches `--min-count` (default 10). The first bucket establishes known categories without findings. A category previously seen with a small count is not new later. |
| `share` | Compare the current share with the mean share across previous buckets. Require both an absolute excess-event count of at least `--min-excess` (default 10) and a relative change in share of at least `--min-change` percent (default 50). This method requires a nonzero baseline; new categories have their own method. |
| `robust` | Use median/MAD clipping followed by a mean and population standard deviation; report absolute scores at least `--sigma` (default 2). |

For `share`, expected count is `historical mean share × current total`, and excess is `actual count − expected count`. For example, a 0.1% baseline in a bucket of 100,000 predicts 100 events; 500 events means +400 excess and a +400% relative change in share. Uniform traffic growth does not trigger a finding. Decreases, including disappearance, can trigger findings too. This threshold is different from Logetizer's `--min-change`, which measures percentage points.

History uses the previous `--window` nonempty buckets (default 100), with missing categories treated as 0%. Findings are calculated before adding the current bucket. Gaps in timestamps do not reset history. New-category tracking covers all prior input, beyond the rolling window. A count of 0–5 does not meet either default event threshold. These are configurable comparison heuristics, not proof of an incident.

The optional `robust` method excludes values more than `max(2 × 1.4826 × MAD, 1 percentage point)` from the historical median, then calculates the mean and standard deviation of retained values. Standard deviation has a one-point floor. Scoring starts after two completed buckets if their shares span at most two points; otherwise it waits for five. `--window` must be at least five for this method. The one-point floor limits sensitivity to changes in rare categories; use `new` or `share` to detect those changes.

```sh
./bucketlyzer.py --method robust --sigma 3 < buckets.jsonl
./bucketlyzer.py --method new --method share --window 20 --json < buckets.jsonl
```

Text output contains only findings, labeled by method, with the current share, signed change in percentage points, and excess count (or robust sigma score). Colors indicate direction using the same red/green scheme and `--color`/`NO_COLOR` behavior as Logetizer. `--json` emits every input bucket with an added `findings` array, including empty arrays, without colors. Invalid input stops processing with a line-numbered error on stderr and exit status 2.

Use `./logetizer.py --help` to see all options.

## Output format

```
<bucket number> <YYYYMMDD HH:MM:SS> <name>: <count> [(<percentage>%)]
```

- **Bucket number:** A sequential number starting at `0001`, padded to at least four digits. Only buckets with visible counts receive a number; empty time intervals are omitted.
- **Time:** The start of the bucket, rather than the timestamp of an individual matching line.
- **Name:** The category from the matching rule, with any capture-group references expanded.
- **Count:** The total for that category within the bucket.
- **Percentage:** Included with `--percent` or `--color-change`, for example `errors: 112 (2%)`.

Categories are sorted alphabetically, ignoring case, with `Misses` last. Categories with no counts are omitted, except for disappearance rows in `--color-change` mode. A blank line separates completed buckets; use `--no-newline` to omit it.

The first output line reports the bucket size. Warnings and debug information go to stderr. With `--debug-print-miss`, stdout contains only unmatched raw log lines, without the header or bucket summaries.

## Writing rules

### Rule file format

A rule file contains one rule per line. Blank lines and lines starting with `#` (after optional whitespace) are ignored. End-of-line comments are not supported.

The format is:

```
name:regex
```

The name is the category under which matching log lines are counted. For example:

Rule:

```
all my errors:[Ee]rror
```

Log:

```
Nov 11 14:11:47 myhost service[1191]: Error: something bad happened
Nov 11 14:12:47 myhost service[1191]: Error: something CATASTROPHIC happened
Nov 11 14:15:47 myhost service[1191]: Something failed due to an error.
```

The output includes the following count (assuming the year is 2026 and the default 10-minute bucket size):

```
0001 20261111 14:10:00 all my errors: 3
```

All three log lines fall within the same time bucket and match the regex.

#### Regex capture groups

You can reference regex capture groups in the category name using `$1`, `$2`, and so on.

Rules:

```
my $1 errors:Error: something (\S+)
other errors:[Ee]rror
```

Applied to the same log with `--recount`, these rules produce:

```
0001 20261111 14:10:00 my bad errors: 1
0001 20261111 14:10:00 my CATASTROPHIC errors: 1
0001 20261111 14:10:00 other errors: 3
```

The second rule counts all three lines, including the two already counted by the first rule. By default (`--exclusive`), each line counts only under the first matching rule. Rules are checked in command-line file order, then in the order they appear within each file. The result is:

```
0001 20261111 14:10:00 my bad errors: 1
0001 20261111 14:10:00 my CATASTROPHIC errors: 1
0001 20261111 14:10:00 other errors: 1
```

#### Silent rules

Prefix a category name with `!` to mark a rule as silent. It matches lines without adding a visible count:

```
!health checks:health check succeeded
errors:[Ee]rror
```

Silent matches are excluded from `Misses` and `--debug-print-miss` output. By default, a silent match also stops later rules from being checked, so place these rules before broader rules you want them to override. With `--recount`, later rules can still count the same line.

### Tips and tricks

To find gaps in your rules, use `--debug-print-miss` to print only log lines that do not match any existing rule. Add rules for those lines, then run the command again to see what remains unmatched.

Example:

```
tail -f /var/log/maillog | ./logetizer.py --rules private_rules/mymail.rules --debug-print-miss
```

To inspect which rules match each eligible line, add `--debug`. It prints the line, matching rule numbers, category names, regexes, and captured groups to stderr while retaining the usual bucket output on stdout:

```sh
./logetizer.py --rules test.rules --debug < /var/log/maillog
```
