"""Run fixed Verification inputs against staged FDS changes on one machine."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import re
import shutil
import statistics
import subprocess
import time

VARIANTS = ('original', 'timers', 'angles', 'optimized')
CASES = (('NS_Analytical_Solution/ns2d_16.fds', 1),
         ('Pressure_Solver/dancing_eddies_default.fds', 2),
         ('Fires/box_burn_away1.fds', 2))


def physical_csv(directory):
    return {p.name: p for p in directory.glob('*.csv')
            if not p.name.endswith(('_cpu.csv', '_steps.csv'))}


def compare(reference, candidate):
    left, right = physical_csv(reference), physical_csv(candidate)
    if not left or left.keys() != right.keys():
        raise AssertionError(f'Physical CSV file sets differ: {left.keys()} / {right.keys()}')
    fields = 0
    for name in left:
        with left[name].open(newline='') as a, right[name].open(newline='') as b:
            rows_a, rows_b = list(csv.reader(a)), list(csv.reader(b))
        if len(rows_a) != len(rows_b):
            raise AssertionError(f'{name}: row counts differ')
        for row_index, (row_a, row_b) in enumerate(zip(rows_a, rows_b), 1):
            if len(row_a) != len(row_b):
                raise AssertionError(f'{name}:{row_index}: column counts differ')
            for column, (value_a, value_b) in enumerate(zip(row_a, row_b), 1):
                try:
                    x, y = float(value_a), float(value_b)
                except ValueError:
                    if value_a.strip() != value_b.strip():
                        raise AssertionError(f'{name}:{row_index}:{column}: headers differ')
                else:
                    if x != y and not (math.isnan(x) and math.isnan(y)):
                        raise AssertionError(f'{name}:{row_index}:{column}: {x} != {y}')
                    fields += 1
    # Runtime/date/version text differs; compare convergence diagnostics separately.
    pattern = re.compile(r'Maximum.*Error|Time Step Size|Pressure Iterations', re.I)
    def diagnostics(directory):
        return {p.name: [line.strip() for line in p.read_text(errors='replace').splitlines()
                         if pattern.search(line)] for p in directory.glob('*.out')}
    a, b = diagnostics(reference), diagnostics(candidate)
    if not a or a != b:
        raise AssertionError('Convergence/time-step diagnostics differ')
    # Binary field data provide an additional check beyond device CSV values.
    binary_count = 0
    for extension in ('*.sf', '*.bf', '*.s3d'):
        a = {p.name: p for p in reference.glob(extension)}
        b = {p.name: p for p in candidate.glob(extension)}
        if a.keys() != b.keys():
            raise AssertionError(f'{extension}: field file sets differ')
        for name, path in a.items():
            if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(b[name].read_bytes()).digest():
                raise AssertionError(f'{name}: binary field data differ')
            binary_count += 1
    return {'numeric_csv_fields': fields, 'binary_fields': binary_count,
            'diagnostic_lines': sum(map(len, diagnostics(reference).values()))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    workspace, results = args.workspace.resolve(), args.results.resolve()
    results.mkdir(parents=True, exist_ok=True)
    executables = {v: workspace / ('fds' if v == 'optimized' else v) /
                   'Build/ompi_gnu_linux/fds_ompi_gnu_linux' for v in VARIANTS}
    for exe in executables.values():
        if not exe.is_file():
            raise FileNotFoundError(exe)
    measurements, checks = [], []
    try:
        with (results / 'timings.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=('case', 'ranks', 'repeat', 'variant',
                                                       'seconds', 'peak_rss_kib', 'exit_code'))
            writer.writeheader()
            for input_path, ranks in CASES:
                input_file = workspace / 'fds/Verification' / input_path
                name = input_file.stem
                for repeat in range(args.repeats + 1):
                    directories = {}
                    order = VARIANTS if repeat % 2 == 0 else tuple(reversed(VARIANTS))
                    for variant in order:
                        directory = results / f'{name}_p{ranks}_r{repeat}_{variant}'
                        directory.mkdir()
                        shutil.copy2(input_file, directory / input_file.name)
                        directories[variant] = directory
                        print(f'START {name} ranks={ranks} repeat={repeat} variant={variant}', flush=True)
                        started = time.monotonic()
                        with (directory / 'console.log').open('w') as log:
                            code = subprocess.run(
                                ['/usr/bin/time', '-f', '%e,%M', '-o', 'resources.txt',
                                 'timeout', '-k', '30s', '180s', 'mpirun', '--oversubscribe',
                                 '-np', str(ranks), str(executables[variant]), input_file.name],
                                cwd=directory, stdout=log, stderr=subprocess.STDOUT,
                                timeout=240,
                            ).returncode
                        elapsed = time.monotonic() - started
                        resource_lines = (directory / 'resources.txt').read_text().strip().splitlines()
                        seconds, rss = resource_lines[-1].split(',')
                        item = dict(case=name, ranks=ranks, repeat=repeat, variant=variant,
                                    seconds=float(seconds), peak_rss_kib=int(rss), exit_code=code)
                        writer.writerow(item)
                        stream.flush()
                        measurements.append(item)
                        print(f'END {name} {variant}: code={code}, seconds={elapsed:.3f}', flush=True)
                        console = (directory / 'console.log').read_text(errors='replace')
                        if code or 'STOP: FDS completed successfully' not in console:
                            raise RuntimeError(f'{name}/{variant}: calculation failed, exit={code}')
                    for variant in VARIANTS[1:]:
                        check = compare(directories['original'], directories[variant])
                        checks.append(dict(case=name, repeat=repeat, variant=variant, **check))
                        print(f'PHYSICS MATCH {name} {variant}: {check}', flush=True)
        summary = []
        rng = random.Random(0)
        for input_path, ranks in CASES:
            name = Path(input_path).stem
            by_variant = {v: [r['seconds'] for r in measurements if r['case'] == name and
                             r['variant'] == v and r['repeat'] > 0] for v in VARIANTS}
            for variant in VARIANTS:
                times = by_variant[variant]
                ratios = [a / b for a, b in zip(by_variant['timers'], times)]
                bootstrap = sorted(statistics.median(rng.choices(ratios, k=len(ratios)))
                                   for _ in range(2000))
                summary.append(dict(case=name, ranks=ranks, variant=variant,
                                    median_seconds=statistics.median(times),
                                    min_seconds=min(times), max_seconds=max(times),
                                    paired_speedup_vs_timers=statistics.median(ratios),
                                    bootstrap_95_interval=[bootstrap[50], bootstrap[1949]]))
        (results / 'summary.json').write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary, indent=2), flush=True)
    except Exception as error:
        (results / 'failure.txt').write_text(str(error))
        raise
    finally:
        (results / 'physics-checks.json').write_text(json.dumps(checks, indent=2))


if __name__ == '__main__':
    main()
