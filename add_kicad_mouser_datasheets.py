#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

BUILTIN_BY_VALUE = {
    'ESP32-WROOM-32': 'https://eu.mouser.com/datasheet/3/1574/1/esp32-wroom-32d_esp32-wroom-32u_datasheet_en.pdf',
    'LM1117-3.3': 'https://www.ti.com/lit/gpn/lm1117',
    'TSR_1-2450': 'https://www.tracopower.com/tsr1-datasheet',
}
BUILTIN_BY_LIB_ID = {
    'RF_Module:ESP32-WROOM-32': BUILTIN_BY_VALUE['ESP32-WROOM-32'],
    'Regulator_Linear:LM1117-3.3': BUILTIN_BY_VALUE['LM1117-3.3'],
    'Regulator_Switching:TSR_1-2450': BUILTIN_BY_VALUE['TSR_1-2450'],
}
SKIP_LIB_PREFIXES = (
    'power:', 'Mechanical:', 'Device:R', 'Device:C', 'Device:L', 'Device:D', 'Device:LED', 'Connector_Generic:'
)
SKIP_REFS = ('#PWR',)


def load_overrides(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {'by_reference': {}, 'by_value': {}, 'by_lib_id': {}}
    data = json.loads(path.read_text(encoding='utf-8'))
    return {
        'by_reference': dict(data.get('by_reference', {})),
        'by_value': dict(data.get('by_value', {})),
        'by_lib_id': dict(data.get('by_lib_id', {})),
    }


def find_symbol_blocks(text: str) -> list[tuple[int, int, str]]:
    out = []
    pos = 0
    token = '(symbol\n'
    while True:
        start = text.find(token, pos)
        if start < 0:
            break
        depth = 0
        i = start
        in_str = False
        esc = False
        while i < len(text):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
            i += 1
        out.append((start, i, text[start:i]))
        pos = i
    return out


def first_match(pattern: str, text: str) -> str:
    m = re.search(pattern, text)
    return m.group(1) if m else ''


def should_skip(reference: str, lib_id: str) -> tuple[bool, str]:
    if any(reference.startswith(p) for p in SKIP_REFS):
        return True, 'power symbol'
    if any(lib_id.startswith(p) for p in SKIP_LIB_PREFIXES):
        return True, 'generic/mechanical symbol'
    return False, ''


def resolve(reference: str, value: str, lib_id: str, datasheet: str, overrides: dict[str, dict[str, str]], replace_existing: bool) -> tuple[str | None, str]:
    if (not replace_existing) and datasheet and datasheet not in {'', '~'}:
        return None, 'already had datasheet'
    if reference in overrides['by_reference']:
        return overrides['by_reference'][reference], 'override: reference'
    if lib_id in overrides['by_lib_id']:
        return overrides['by_lib_id'][lib_id], 'override: lib_id'
    if value in overrides['by_value']:
        return overrides['by_value'][value], 'override: value'
    if lib_id in BUILTIN_BY_LIB_ID:
        return BUILTIN_BY_LIB_ID[lib_id], 'builtin: lib_id'
    if value in BUILTIN_BY_VALUE:
        return BUILTIN_BY_VALUE[value], 'builtin: value'
    return None, 'no confident Mouser match'


def replace_datasheet_in_block(block: str, new_url: str) -> tuple[str, str]:
    pat = re.compile(r'(\(property\s+"Datasheet"\s+")((?:[^"\\]|\\.)*)(")', re.S)
    m = pat.search(block)
    if not m:
        raise ValueError('Datasheet property not found in symbol block')
    old = m.group(2)
    safe = new_url.replace('\\', '\\\\').replace('"', '\\"')
    new_block = block[:m.start(2)] + safe + block[m.end(2):]
    return new_block, old


def main() -> int:
    ap = argparse.ArgumentParser(description='Safely add Mouser-oriented datasheet links to a KiCad schematic without reformatting the file.')
    ap.add_argument('schematic', type=Path)
    ap.add_argument('--output', type=Path)
    ap.add_argument('--report', type=Path)
    ap.add_argument('--overrides', type=Path)
    ap.add_argument('--in-place', action='store_true')
    ap.add_argument('--replace-existing', action='store_true')
    args = ap.parse_args()

    in_path = args.schematic
    if args.in_place and args.output:
        raise SystemExit('Use either --in-place or --output, not both')
    out_path = in_path if args.in_place else (args.output or in_path.with_name(in_path.stem + '_mouser_datasheets.kicad_sch'))
    report_path = args.report or in_path.with_name(in_path.stem + '_mouser_datasheets_report.csv')

    text = in_path.read_text(encoding='utf-8')
    blocks = find_symbol_blocks(text)
    overrides = load_overrides(args.overrides)

    rows = []
    pieces = []
    last = 0
    updated = kept = skipped = unresolved = 0

    for start, end, block in blocks:
        pieces.append(text[last:start])
        reference = first_match(r'\(property\s+"Reference"\s+"((?:[^"\\]|\\.)*)"', block)
        value = first_match(r'\(property\s+"Value"\s+"((?:[^"\\]|\\.)*)"', block)
        lib_id = first_match(r'\(lib_id\s+"((?:[^"\\]|\\.)*)"', block)
        datasheet = first_match(r'\(property\s+"Datasheet"\s+"((?:[^"\\]|\\.)*)"', block)

        skip, reason = should_skip(reference, lib_id)
        if skip:
            skipped += 1
            rows.append([reference, value, lib_id, datasheet, '', 'skipped', reason])
            pieces.append(block)
            last = end
            continue

        url, why = resolve(reference, value, lib_id, datasheet, overrides, args.replace_existing)
        if why == 'already had datasheet':
            kept += 1
            rows.append([reference, value, lib_id, datasheet, datasheet, 'kept', why])
            pieces.append(block)
            last = end
            continue

        if url:
            new_block, old = replace_datasheet_in_block(block, url)
            updated += 1
            rows.append([reference, value, lib_id, old, url, 'updated', why])
            pieces.append(new_block)
        else:
            unresolved += 1
            rows.append([reference, value, lib_id, datasheet, '', 'unresolved', why])
            pieces.append(block)
        last = end

    pieces.append(text[last:])
    out_text = ''.join(pieces)
    out_path.write_text(out_text, encoding='utf-8')

    with report_path.open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['reference', 'value', 'lib_id', 'old_datasheet', 'new_datasheet', 'status', 'reason'])
        w.writerows(rows)

    print(f'Input:      {in_path}')
    print(f'Output:     {out_path}')
    print(f'Report:     {report_path}')
    print(f'Symbols:    {len(blocks)}')
    print(f'Updated:    {updated}')
    print(f'Kept:       {kept}')
    print(f'Skipped:    {skipped}')
    print(f'Unresolved: {unresolved}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
