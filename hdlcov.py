#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hdlcov - pokrycie kodu RTL w testach: VHDL przez NVC, Verilog przez Covered.

Uruchamia symulację z pomiarem pokrycia i tworzy raport: podsumowanie
(linie/instrukcje, gałęzie, toggle, wyrażenia) oraz listę nieprzetestowanych
linii RTL z ich treścią.

Użycie:
  python3 hdlcov.py vhdl    -t tb_mac --dut tb_mac.dut  rtl/mac.vhd tb/tb_mac.vhd
  python3 hdlcov.py verilog -t tb_mac --dut tb_mac.dut --dut-module mac  rtl/mac.v tb/tb_mac.v
  python3 hdlcov.py nvc-db  work/cov.ncdb            # raport z istniejącej bazy NVC (np. z cocotb)

Wyniki w katalogu -o (domyślnie hdlcov_out/): coverage.md, coverage.json,
raport HTML z NVC albo pełne raporty tekstowe z Covered.
"""
import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

TOOLS = {'nvc': 'HDLCOV_NVC', 'covered': 'HDLCOV_COVERED', 'iverilog': 'HDLCOV_IVERILOG', 'vvp': 'HDLCOV_VVP'}
TB_PATTERNS = ('tb_*', '*_tb.*', '*_tb_*', 'test_*', '*/tb/*', '*/test/*', '*/tests/*', '*/sim/*')


class ToolError(Exception):
    pass


def tool(name):
    """Ścieżka do narzędzia: zmienna HDLCOV_<NAZWA>, PATH, albo ~/tools/bin."""
    p = os.environ.get(TOOLS[name]) or shutil.which(name)
    if not p:
        cand = os.path.expanduser('~/tools/bin/' + name)
        p = cand if os.access(cand, os.X_OK) else None
    if not p:
        raise ToolError('nie znaleziono "%s" - zainstaluj albo ustaw %s (patrz README)' % (name, TOOLS[name]))
    return p


def run(cmd, cwd, log):
    with open(log, 'a', encoding='utf-8') as f:
        f.write('$ %s\n' % ' '.join(cmd))
        r = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
        f.write(r.stdout)
    if r.returncode != 0:
        tail = '\n'.join(r.stdout.strip().splitlines()[-15:])
        raise ToolError('polecenie zakończone błędem (%d): %s\n%s\n(pełny log: %s)'
                        % (r.returncode, ' '.join(cmd), tail, log))
    return r.stdout


def is_rtl(path, rtl_patterns):
    p = path.replace('\\', '/')
    if rtl_patterns:
        return any(fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(os.path.basename(p), pat) for pat in rtl_patterns)
    return not any(fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(os.path.basename(p), pat) for pat in TB_PATTERNS)


def source_line(path, n, cache={}):
    if path not in cache:
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                cache[path] = f.read().splitlines()
        except OSError:
            cache[path] = []
    lines = cache[path]
    return lines[n - 1].strip() if 0 < n <= len(lines) else ''


# ---------------------------------------------------------------- VHDL / NVC

DECL_RE = re.compile(r'^\s*(signal\s+|variable\s+|shared\s+variable\s+)?[\w\s,]+:\s*'
                     r'(in|out|inout|buffer|linkage)?\s*[\w.]+\s*(\(.*\))?\s*(:=.*)?[;)]?\s*(--.*)?$', re.I)
NOTE_RE = re.compile(r'\*\* Note:\s+(statement|branch|toggle|expression|FSM state|functional|average):\s+'
                     r'(?:([\d.]+) % \((\d+)/(\d+)\)|N\.A\.)')


def nvc_report(ncdb, out, rtl, log):
    """Raport HTML + eksport Cobertura z bazy NVC -> (podsumowanie, niepokryte)."""
    nvc = tool('nvc')
    txt = run([nvc, '--cover-report', '-o', os.path.join(out, 'html'), ncdb], out, log)
    summary = {}
    for m in NOTE_RE.finditer(txt):
        if m.group(2) is not None:
            summary[m.group(1)] = (int(m.group(3)), int(m.group(4)))
    xml = os.path.join(out, 'cobertura.xml')
    run([nvc, '--cover-export', '--format=cobertura', '-o', xml, ncdb], out, log)
    root = ET.parse(xml).getroot()
    base = (root.findtext('sources/source') or '.').strip()
    files, missed = {}, []
    for cls in root.iter('class'):
        fn = cls.get('filename')
        path = os.path.normpath(os.path.join(out, base, fn)) if not os.path.isabs(fn) else fn
        if not is_rtl(path, rtl):
            continue
        hit = tot = 0
        for ln in cls.iter('line'):
            n, hits = int(ln.get('number')), int(ln.get('hits'))
            cond = (ln.get('condition-coverage') or '').replace(' ', '')
            code = source_line(path, n)
            if DECL_RE.match(code):
                # deklaracja portu/sygnału: NVC raportuje tu toggle, a nie wykonanie
                if hits == 0:
                    missed.append((path, n, 'toggle', code))
                continue
            tot += 1
            if hits > 0 or cond == '100%':
                hit += 1
                if cond and cond != '100%':
                    missed.append((path, n, 'gałąź %s' % cond, code))
            else:
                missed.append((path, n, 'gałąź %s' % cond if cond else 'nie wykonana', code))
        h, t = files.get(path, (0, 0))
        files[path] = (h + hit, t + tot)
    return summary, files, sorted(set(missed))


def cmd_vhdl(a, out, log):
    nvc = tool('nvc')
    std = ['--std=' + a.std]
    work = ['--work=work:' + os.path.join(out, 'work')]
    srcs = [os.path.abspath(s) for s in a.sources]
    run([nvc] + std + work + ['-a'] + srcs, out, log)
    elab = [nvc] + std + work + ['-e', '--cover=statement,branch,expression,toggle',
                                 '--cover-file=' + os.path.join(out, 'cov.ncdb')]
    if a.dut:
        spec = os.path.join(out, 'cover_spec.txt')
        h = 'WORK.' + a.dut.replace(':', '.').upper()
        with open(spec, 'w') as f:
            f.write('+hierarchy %s\n+hierarchy %s.*\n' % (h, h))
        elab.append('--cover-spec=' + spec)
    elab += ['-g%s' % g for g in a.generic] + [a.top]
    run(elab, out, log)
    r = [nvc] + std + work + ['-r', a.top]
    if a.stop_time:
        r.append('--stop-time=' + a.stop_time)
    run(r, out, log)
    return nvc_report(os.path.join(out, 'cov.ncdb'), out, a.rtl, log)


def cmd_nvc_db(a, out, log):
    return nvc_report(os.path.abspath(a.db), out, a.rtl, log)


# ---------------------------------------------------------------- Verilog / Covered

COV_SECTIONS = (('LINE COVERAGE', 'linie'), ('TOGGLE COVERAGE', 'toggle'),
                ('COMBINATIONAL LOGIC COVERAGE', 'wyrażenia'), ('FINITE STATE MACHINE COVERAGE', 'FSM'))


def clean_vcd(src, dst):
    """Covered nie parsuje bloków $date/$version/$comment (np. z Icarusa) - usuwamy je."""
    with open(src, encoding='latin-1') as f:
        t = f.read()
    t = re.sub(r'\$(date|version|comment)\b.*?\$end\s*', '', t, flags=re.S)
    with open(dst, 'w', encoding='latin-1') as f:
        f.write(t)


def cmd_verilog(a, out, log):
    srcs = [os.path.abspath(s) for s in a.sources]
    rtl_srcs = [s for s in srcs if is_rtl(s, a.rtl)]
    if not rtl_srcs:
        raise ToolError('brak plików RTL (wszystkie wyglądają na testbench - użyj --rtl)')
    if a.vcd:
        vcd = os.path.abspath(a.vcd)
    else:
        iverilog, vvp = tool('iverilog'), tool('vvp')
        dump = os.path.join(out, 'hdlcov_dump.v')
        with open(dump, 'w') as f:
            f.write('module hdlcov_dump;\n  initial begin\n    $dumpfile("sim.vcd");\n'
                    '    $dumpvars(0, %s);\n  end\nendmodule\n' % a.dut.split('.')[0])
        defs = ['-D' + d for d in a.define]
        run([iverilog, '-g2012', '-o', 'sim.vvp', '-s', a.top, '-s', 'hdlcov_dump'] + defs + srcs + [dump], out, log)
        run([vvp, '-n', 'sim.vvp'], out, log)
        vcd = os.path.join(out, 'sim.vcd')
    clean = os.path.join(out, 'sim_covered.vcd')
    clean_vcd(vcd, clean)
    covered = tool('covered')
    cmd = [covered, 'score', '-t', a.dut_module, '-i', a.dut, '-vcd', clean, '-o', 'cov.cdd']
    for s in rtl_srcs:
        cmd += ['-v', s]
    for d in a.define:
        cmd += ['-D', d]
    run(cmd, out, log)
    run([covered, 'report', '-m', 'ltcf', '-o', 'summary.txt', 'cov.cdd'], out, log)
    run([covered, 'report', '-d', 'v', '-m', 'ltcf', '-o', 'details.txt', 'cov.cdd'], out, log)

    with open(os.path.join(out, 'summary.txt'), encoding='utf-8', errors='replace') as f:
        summ = f.read()
    summary = {}
    for title, key in COV_SECTIONS:
        k = summ.find(title)
        m = re.search(r'Accumulated\s+(\d+)/\s*(\d+)/\s*(\d+)', summ[k:]) if k >= 0 else None
        if m and int(m.group(3)):
            summary[key] = (int(m.group(1)), int(m.group(3)))

    with open(os.path.join(out, 'details.txt'), encoding='utf-8', errors='replace') as f:
        det = f.read()
    files, missed = {}, []
    sec = det[det.find('LINE COVERAGE RESULTS'):det.find('TOGGLE COVERAGE RESULTS')]
    for m in re.finditer(r'^\s+(\S+)\s+(\S+)\s+(\d+)/\s*(\d+)/\s*(\d+)\s+\d+%', sec, re.M):
        if m.group(1) not in ('$root', 'Accumulated'):
            path = next((s for s in rtl_srcs if os.path.basename(s) == m.group(2)), m.group(2))
            files[path] = (int(m.group(3)), int(m.group(5)))
    for blk in re.finditer(r'Module: \S+, File: (\S+)\s+-+\s+Missed Lines\n(.*?)(?=\n\s*\n|\n\s*-{5}|\Z)', sec, re.S):
        path = os.path.normpath(os.path.join(out, blk.group(1)))
        for lm in re.finditer(r'^\s*(\d+):', blk.group(2), re.M):
            n = int(lm.group(1))
            missed.append((path, n, 'nie wykonana', source_line(path, n)))
    return summary, files, sorted(set(missed))


# ---------------------------------------------------------------- raport

def pct(h, t):
    return 100.0 * h / t if t else 100.0


def report(kind, summary, files, missed, out, min_pct):
    rel = lambda p: os.path.relpath(p)
    lines = ['# Pokrycie kodu RTL (%s)' % kind, '', '## Podsumowanie', '', '| Metryka | Pokryte | Razem | % |',
             '|---|---:|---:|---:|']
    for k, (h, t) in summary.items():
        lines.append('| %s | %d | %d | %.1f |' % (k, h, t, pct(h, t)))
    lines += ['', '## Pliki RTL (linie)', '', '| Plik | Pokryte | Razem | % |', '|---|---:|---:|---:|']
    for p, (h, t) in sorted(files.items()):
        lines.append('| `%s` | %d | %d | %.1f |' % (rel(p), h, t, pct(h, t)))
    lines += ['', '## Nieprzetestowane linie RTL (%d)' % len(missed), '']
    if missed:
        lines += ['| Plik:linia | Rodzaj | Kod |', '|---|---|---|']
        for p, n, why, code in missed:
            lines.append('| `%s:%d` | %s | `%s` |' % (rel(p), n, why, code.replace('|', '\\|')))
    else:
        lines.append('Brak - wszystkie linie RTL zostały wykonane.')
    with open(os.path.join(out, 'coverage.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    with open(os.path.join(out, 'coverage.json'), 'w', encoding='utf-8') as f:
        json.dump({'tool': kind, 'summary': {k: {'hit': h, 'total': t} for k, (h, t) in summary.items()},
                   'files': {rel(p): {'hit': h, 'total': t} for p, (h, t) in files.items()},
                   'missed': [{'file': rel(p), 'line': n, 'kind': w, 'code': c} for p, n, w, c in missed]},
                  f, indent=2, ensure_ascii=False)

    print('== Pokrycie (%s) ==' % kind)
    for k, (h, t) in summary.items():
        print('   %-12s %6.1f %%  (%d/%d)' % (k, pct(h, t), h, t))
    print('\n== Pliki RTL ==')
    for p, (h, t) in sorted(files.items()):
        print('   %-50s %6.1f %%  (%d/%d linii)' % (rel(p), pct(h, t), h, t))
    print('\n== Nieprzetestowane linie RTL (%d) ==' % len(missed))
    for p, n, why, code in missed:
        print('   %s:%d  [%s]  %s' % (rel(p), n, why, code))
    print('\nRaport: %s' % os.path.join(out, 'coverage.md'))
    if kind == 'NVC':
        print('HTML:   %s' % os.path.join(out, 'html', 'index.html'))
    h = sum(x for x, _ in files.values())
    t = sum(x for _, x in files.values())
    if min_pct is not None and pct(h, t) < min_pct:
        print('POKRYCIE LINII %.1f %% < wymagane %.1f %%' % (pct(h, t), min_pct))
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description='Pokrycie kodu RTL: VHDL (NVC) i Verilog (Covered).')
    sub = ap.add_subparsers(dest='cmd', required=True)

    def common(p):
        p.add_argument('-o', '--out', default='hdlcov_out', help='katalog wyników (domyślnie hdlcov_out)')
        p.add_argument('--rtl', action='append', default=[], metavar='WZORZEC',
                       help='które pliki są RTL (glob, można powtarzać); domyślnie wszystko poza tb_*/test_*/tb/')
        p.add_argument('--min', type=float, help='minimalne pokrycie linii RTL w %% - poniżej kod wyjścia 1')

    v = sub.add_parser('vhdl', help='symulacja w NVC z pomiarem pokrycia')
    v.add_argument('sources', nargs='+', help='pliki VHDL w kolejności analizy')
    v.add_argument('-t', '--top', required=True, help='encja testbencha')
    v.add_argument('--dut', help='ścieżka instancji DUT, np. tb_mac.dut - pokrycie tylko dla niej')
    v.add_argument('--std', default='2008', help='standard VHDL (domyślnie 2008)')
    v.add_argument('-g', '--generic', action='append', default=[], metavar='NAZWA=WARTOŚĆ')
    v.add_argument('--stop-time', help='np. 10ms, gdy testbench sam się nie kończy')
    common(v)

    g = sub.add_parser('verilog', help='symulacja w Icarus Verilog + pokrycie w Covered')
    g.add_argument('sources', nargs='+', help='pliki Verilog (RTL + testbench)')
    g.add_argument('-t', '--top', required=True, help='moduł testbencha')
    g.add_argument('--dut', required=True, help='ścieżka instancji DUT w symulacji, np. tb_mac.dut')
    g.add_argument('--dut-module', required=True, help='nazwa modułu DUT, np. mac')
    g.add_argument('--vcd', help='gotowy VCD z symulacji (pomija uruchamianie Icarusa)')
    g.add_argument('-D', '--define', action='append', default=[], metavar='MAKRO[=WARTOŚĆ]')
    common(g)

    d = sub.add_parser('nvc-db', help='raport z istniejącej bazy pokrycia NVC (.ncdb)')
    d.add_argument('db')
    common(d)

    a = ap.parse_args()
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    log = os.path.join(out, 'hdlcov.log')
    open(log, 'w').close()
    try:
        if a.cmd == 'vhdl':
            res, kind = cmd_vhdl(a, out, log), 'NVC'
        elif a.cmd == 'nvc-db':
            res, kind = cmd_nvc_db(a, out, log), 'NVC'
        else:
            res, kind = cmd_verilog(a, out, log), 'Covered'
    except (ToolError, OSError, ET.ParseError) as e:
        print('BŁĄD: %s' % e, file=sys.stderr)
        return 2
    return report(kind, *res, out, a.min)


if __name__ == '__main__':
    sys.exit(main())
