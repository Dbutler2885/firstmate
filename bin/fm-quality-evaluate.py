#!/usr/bin/env python3
"""Crapper adapter: score only functions intersecting the task's Git diff.

Run with the project's pinned Crapper interpreter and a JSON request file.
Crapper owns discovery, parsing, complexity, coverage interpretation, scoring,
and report formatting. This adapter only selects changed function spans and
merges explicitly configured LCOV reports for projects with multiple packages.
"""
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
import subprocess
from xml.etree import ElementTree

from crapper.coverage import CoverageBundle, _profile_segments, parse_go_profile, parse_jacoco_index, parse_lcov
from crapper.crap import make_entry, sort_entries
from crapper.discover import SKIP_DIRS, is_test_file, iter_source_files, language_of
from crapper.languages import functions_in_file
from crapper.report import format_report


def unit_body(unit, source):
    if unit.start_byte >= 0 and unit.end_byte >= unit.start_byte:
        return source[unit.start_byte:unit.end_byte]
    return b''.join(source.splitlines(keepends=True)[unit.start_line-1:unit.end_line])


def has_metrics(bundle, unit):
    if unit.language == 'go':
        segments = _profile_segments(bundle.go_profile, unit.path, bundle.source_parts) or []
        return any(start <= unit.end_line and end >= unit.start_line and statements > 0 for start, end, statements, _ in segments)
    if unit.language == 'java':
        names = [unit.jacoco_class] if unit.jacoco_class else []
        if unit.namespace not in names:
            names.append(unit.namespace)
        methods = [method for name in names for method in (bundle.jacoco or {}).get(f'{name}#{unit.name}', [])]
        method = min(methods, key=lambda method: abs(method.line - unit.start_line), default=None)
        return method is not None and unit.start_line <= method.line <= unit.end_line and method.missed + method.covered > 0
    record = bundle._lcov_lines(unit.path)
    return record is not None and any(unit.start_line <= line <= unit.end_line and total > 0 for metrics in (record, record.branches) for line, (_, total) in metrics.items())


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    root = Path(request['root'])
    bundle = CoverageBundle()
    # Consume only the declared fresh reports. Auto-discovery could silently
    # reuse an older report for another language or package.
    for report in request['reports']:
        path = root / report['path']
        text = path.read_text()
        if path.name == 'coverage.out':
            records = parse_go_profile(text)
            if not records:
                raise ValueError(f"No usable Go coverage in {report['path']}")
            bundle.go_profile = {**(bundle.go_profile or {}), **records}
        elif path.name == 'jacoco.xml':
            if any(counter.get('type') == 'INSTRUCTION' and (not counter.get('missed') or not counter.get('covered'))
                   for counter in ElementTree.fromstring(text).iter('counter')):
                raise ValueError(f"Missing JaCoCo instruction metrics in {report['path']}")
            records = parse_jacoco_index(text)
            if not records:
                raise ValueError(f"No usable JaCoCo coverage in {report['path']}")
            bundle.jacoco = {**(bundle.jacoco or {}), **records}
        else:
            records = parse_lcov(text)
            if not any(record or record.branches for record in records.values()):
                raise ValueError(f"No usable LCOV records in {report['path']}")
            for source, coverage in records.items():
                source_path = Path(source)
                source = str(source_path if source_path.is_absolute() else (root / report['source_root'] / source_path).resolve())
                bundle.lcov[source] = coverage
    units = []
    for name in request['files']:
        path = root / name
        language = language_of(path)
        if not language or is_test_file(Path(name)) or any(part in SKIP_DIRS for part in Path(name).parts):
            continue
        diff = subprocess.check_output(['git', '-C', str(root), 'diff', '--no-ext-diff', '--no-renames', '--unified=0', request['base'], '--', name], text=True)
        spans, deletions = [], []
        for line in diff.splitlines():
            match = re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
            if match:
                old_start, old_count = int(match[1]), int(match[2] or '1')
                new_start, new_count = int(match[3]), int(match[4] or '1')
                if new_count:
                    spans.append((new_start, new_start + new_count - 1))
                elif old_count:
                    deletions.append((old_start, old_start + old_count - 1))
        old_blob = subprocess.run(['git', '-C', str(root), 'show', request['base'] + ':' + name], capture_output=True, text=True)
        old_units = functions_in_file(language, old_blob.stdout, name, str(root)) if old_blob.returncode == 0 else []
        old_keys = {(unit.namespace, unit.name) for unit in old_units}
        old_bytes = old_blob.stdout.encode()
        old_bodies = {(unit.namespace, unit.name, unit_body(unit, old_bytes)) for unit in old_units}
        deleted_inside = {(unit.namespace, unit.name) for unit in old_units if any(start <= unit.end_line and end >= unit.start_line for start, end in deletions)}
        source = path.read_text()
        source_bytes = source.encode()
        for unit in functions_in_file(language, source, name, str(root)):
            key = (unit.namespace, unit.name)
            deletion_changed_body = key in deleted_inside and (*key, unit_body(unit, source_bytes)) not in old_bodies
            if key not in old_keys or deletion_changed_body or any(start <= unit.end_line and end >= unit.start_line for start, end in spans):
                units.append((name, unit))
    bundle.bind_sources([str(path.relative_to(root)) for path in iter_source_files([root])])
    measured = []
    for name, unit in units:
        percent = bundle.percent_for(unit) if has_metrics(bundle, unit) else None
        if percent is None:
            print(f'No coverage metrics for changed function {unit.name} in {name}; declare a fresh report that covers it', file=sys.stderr)
            return 1
        measured.append(make_entry(unit, percent))
    entries = sort_entries(measured)
    print(format_report(entries), end='')
    Path(request['output']).write_text(json.dumps([asdict(entry) for entry in entries], indent=2) + '\n')
    return 2 if any(entry.crap is None or entry.crap > request['threshold'] for entry in entries) else 0


if __name__ == '__main__':
    sys.exit(main())
