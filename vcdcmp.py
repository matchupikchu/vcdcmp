#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vcdcmp - compares transactions on FPGA module interfaces recorded in
simulation and in hardware by an embedded logic analyzer (vendor independent:
Intel SignalTap, AMD Vivado ILA, Lattice Reveal, Microchip Identify and
others - any tool that exports VCD or CSV).

Transaction = a cycle in which valid==1 (and ready==1, if ready is given).
Field values (operands / result) are taken from every transaction. The
transaction sequences from both sources are aligned and compared, regardless
of where in the simulation the analyzer captured its window.

Usage:
  python3 vcdcmp.py --list file.vcd|file.csv
  python3 vcdcmp.py config.json sim.vcd hw.vcd [--csv diffs.csv]
  python3 vcdcmp.py config.json sim.vcd hw_dir/ [--merge-out merged.vcd]

HW captures: .vcd/.csv files and/or directories containing them. Each file is
a separate capture (analyzer window) - it is aligned to the simulation
independently and the results are combined into one report. --merge-out writes
all captures one after another into a single VCD file (e.g. for GTKWave).
"""
import argparse
import csv
import difflib
import fnmatch
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from functools import reduce

UNITS = {'s': 10**15, 'ms': 10**12, 'us': 10**9, 'ns': 10**6, 'ps': 10**3, 'fs': 1}
SPLIT_RE = re.compile(r'[|./\\]')
RANGE_RE = re.compile(r'^(.*?)\s*\[(\d+)(?:(?::|\.\.)(\d+))?\]$')    # [7:0], [7..0], [3]
NORM = str.maketrans('uwlh-UWLHXZ', 'xx01xxx01xz')
WILD = set('xz?')


# ---------------------------------------------------------------- names

def split_parts(s):
    """'top:u0|mul:u_mul|p' / 'u_top/u_mul/p' / 'tb.dut.u_mul.p' -> [..., 'u_mul', 'p']"""
    out = []
    for p in SPLIT_RE.split(s):
        if ':' in p:                      # e.g. Intel: entity:instance
            p = p.rsplit(':', 1)[1]
        p = p.strip().lower()
        if p:
            out.append(p)
    return out


def split_range(name):
    m = RANGE_RE.match(name.strip())
    if not m:
        return name.strip(), None
    a = int(m.group(2))
    b = int(m.group(3)) if m.group(3) is not None else a
    return m.group(1), (max(a, b), min(a, b))


# ---------------------------------------------------------------- VCD

class Var:
    __slots__ = ('path', 'hi', 'lo', 'width', 'ranged', 'times', 'values')

    def __init__(self, path, hi, lo, width, ranged):
        self.path, self.hi, self.lo, self.width, self.ranged = path, hi, lo, width, ranged
        self.times, self.values = [], []

    @property
    def name(self):
        r = '' if not self.ranged else (
            '[%d]' % self.hi if self.hi == self.lo else '[%d:%d]' % (self.hi, self.lo))
        return '.'.join(self.path) + r


def pad(v, w):
    n = len(v)
    if n == w:
        return v
    if n < w:
        return (v[0] if v[0] in 'xz' else '0') * (w - n) + v
    return v[-w:]


class VCD:
    untimed = False      # True: time = sample number (CSV without a time axis)

    def __init__(self, fname):
        self.fname = fname
        self.ts = 1          # fs per file time unit
        self.vars = []
        self.tmax = 0
        self._parse()

    def _parse(self):
        with open(self.fname, 'r', encoding='latin-1') as f:
            text = f.read()
        k = text.find('$enddefinitions')
        if k < 0:
            raise ValueError('%s: brak $enddefinitions - to nie jest plik VCD?' % self.fname)
        toks = text[:k].split()
        byid = defaultdict(list)
        scope = []
        i = 0
        while i < len(toks):
            t = toks[i]
            if t == '$timescale':
                j = toks.index('$end', i)
                m = re.match(r'(\d+)\s*([munpf]?s)$', ''.join(toks[i + 1:j]))
                if m:
                    self.ts = int(m.group(1)) * UNITS[m.group(2)]
                i = j + 1
            elif t == '$scope':
                scope.append(toks[i + 2])
                i = toks.index('$end', i) + 1
            elif t == '$upscope':
                if scope:
                    scope.pop()
                i = toks.index('$end', i) + 1
            elif t == '$var':
                j = toks.index('$end', i)
                vtype, width, code = toks[i + 1], int(toks[i + 2]), toks[i + 3]
                if vtype != 'real':
                    base, rng = split_range(''.join(toks[i + 4:j]))
                    hi, lo = rng if rng else (width - 1, 0)
                    if hi - lo + 1 != width:
                        hi = lo + width - 1
                    path = tuple(p for s in scope for p in split_parts(s)) + tuple(split_parts(base))
                    v = Var(path, hi, lo, width, rng is not None or width > 1)
                    byid[code].append(v)
                    self.vars.append(v)
                i = j + 1
            elif t in ('$comment', '$date', '$version'):
                i = toks.index('$end', i) + 1
            else:
                i += 1

        body = text[k:].split()
        n, i, t = len(body), 1, 0
        while i < n:
            tok = body[i]
            c = tok[0]
            if c == '#':
                t = int(tok[1:])
                if t > self.tmax:
                    self.tmax = t
                i += 1
                continue
            if c in 'bB':
                val, code = tok[1:].translate(NORM), body[i + 1]
                i += 2
            elif c in 'rR':
                i += 2
                continue
            elif c == '$':
                if tok == '$comment':
                    while i < n and body[i] != '$end':
                        i += 1
                i += 1
                continue
            else:
                val, code = c.translate(NORM), tok[1:]
                i += 1
            for v in byid.get(code, ()):
                pv = pad(val, v.width)
                if v.times and v.times[-1] == t:
                    v.values[-1] = pv
                else:
                    v.times.append(t)
                    v.values.append(pv)

    # -- signal lookup by path suffix
    def find(self, name):
        base, rng = split_range(name)
        q = tuple(split_parts(base))
        if not q:
            raise LookupError('pusta nazwa sygnału')
        hits = [v for v in self.vars if v.path[-len(q):] == q]
        if not hits:
            raise LookupError('%s: nie znaleziono sygnału "%s" (użyj --list)' % (self.fname, name))
        paths = sorted({v.path for v in hits})
        if len(paths) > 1:
            raise LookupError('%s: nazwa "%s" jest niejednoznaczna, pasuje do:\n    %s' % (
                self.fname, name, '\n    '.join('.'.join(p) for p in paths[:10])))
        return SigRef(name, hits, rng)

    def find_prefix(self, prefix):
        """'u_mul.in_' -> ['valid', 'a', 'b'] (signal name suffixes within one scope).
        A prefix ending with a separator ('u_mul.') means all signals in that scope."""
        p = prefix.strip()
        parts = tuple(split_parts(p))
        if not p or p[-1] in './|\\':
            scope, lead = parts, ''
        else:
            scope, lead = parts[:-1], parts[-1]
        hits = {}
        for v in self.vars:
            leaf = v.path[-1]
            if (len(leaf) > len(lead) and leaf.startswith(lead) and len(v.path) > len(scope)
                    and v.path[len(v.path) - 1 - len(scope):-1] == scope):
                hits.setdefault(leaf[len(lead):], set()).add(v.path[:-1])
        if not hits:
            raise LookupError('%s: brak sygnałów z prefiksem "%s" (użyj --list)' % (self.fname, prefix))
        scopes = {sc for s in hits.values() for sc in s}
        if len(scopes) > 1:
            raise LookupError('%s: prefiks "%s" pasuje do kilku zakresów - podaj dłuższy:\n    %s' % (
                self.fname, prefix, '\n    '.join('.'.join(sc) for sc in sorted(scopes)[:10])))
        return list(hits)

    def clock_edges(self, name, edge='rise'):
        ref = self.find(name)
        if len(ref.vars) != 1 or ref.vars[0].width != 1:
            raise LookupError('zegar "%s" musi być sygnałem 1-bitowym' % name)
        v = ref.vars[0]
        a, b = ('0', '1') if edge == 'rise' else ('1', '0')
        out, prev = [], None
        for t, val in zip(v.times, v.values):
            if prev == a and val == b:
                out.append(t)
            prev = val
        return out

    def sample_grid(self, period=None):
        """Sample grid for a file without a clock (typical logic analyzer export)."""
        times = sorted({t for v in self.vars for t in v.times} | {self.tmax})
        if not times:
            return []
        if period is None:
            deltas = [b - a for a, b in zip(times, times[1:]) if b > a]
            period = reduce(math.gcd, deltas) if deltas else 1
        return list(range(times[0], times[-1] + 1, period))


INDEX_COL_RE = re.compile(r'^(sample.*|time.*|trigger|index|#|nr|lp\.?)$', re.I)
RADIX = {'HEX': 'hex', 'HEXADECIMAL': 'hex', 'BIN': 'bin', 'BINARY': 'bin', 'UNSIGNED': 'dec',
         'DEC': 'dec', 'DECIMAL': 'dec', 'SIGNED': 'sdec', 'OCT': 'oct', 'OCTAL': 'oct'}


def csv_value(s, radix):
    """CSV cell value -> bits ('0101', may contain x/z) or an integer."""
    s = s.strip().replace('_', '')
    if not s:
        return 'x'
    low = s.lower()
    m = re.match(r"^\d*'s?([hbdo])(.*)$", low)            # Verilog style: 16'h00ff
    if m:
        radix, low = {'h': 'hex', 'b': 'bin', 'd': 'dec', 'o': 'oct'}[m.group(1)], m.group(2)
    elif radix is None and low.startswith('0x'):         # prefixes only when radix is unknown
        radix, low = 'hex', low[2:]                       # (HEX '0B80' is not binary '80')
    elif radix is None and low.startswith('0b'):
        radix, low = 'bin', low[2:]
    elif radix is None and low.endswith('h'):
        radix, low = 'hex', low[:-1]
    radix = radix or 'hex'
    if radix == 'bin':
        return low.translate(NORM)
    if radix in ('hex', 'oct'):
        n = 4 if radix == 'hex' else 3
        return ''.join(c * n if c in 'xz' else format(int(c, 16 if n == 4 else 8), '0%db' % n)
                       for c in low.translate(NORM))
    if 'x' in low or 'z' in low:
        return 'x'
    return int(low)


class CSVWave(VCD):
    """CSV export from a logic analyzer (e.g. Vivado ILA, SignalTap): one row = one sample.
    Index columns (Sample in Buffer, Time, TRIGGER...) are skipped; an optional
    'Radix - HEX,...' row (Vivado) sets the value format, otherwise the default radix is used."""
    untimed = True

    def __init__(self, fname, radix=None):
        self.fname, self.ts, self.vars, self.tmax = fname, UNITS['ns'], [], 0
        with open(fname, newline='', encoding='utf-8-sig', errors='replace') as f:
            text = f.read()
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel
        rows = [r for r in csv.reader(text.splitlines(), dialect) if any(c.strip() for c in r)]
        if len(rows) < 2:
            raise ValueError('%s: plik CSV bez danych' % fname)
        header = [h.strip() for h in rows[0]]
        data = rows[1:]
        radixes = [radix] * len(header)
        if data[0] and data[0][0].strip().lower().startswith('radix'):
            for i, c in enumerate(data[0][:len(header)]):
                r = re.sub(r'(?i)^radix\s*-\s*', '', c.strip()).upper()
                radixes[i] = RADIX.get(r, radix)
            data = data[1:]
        cols = [i for i, h in enumerate(header) if h and not INDEX_COL_RE.match(h)]
        if not cols:
            raise ValueError('%s: brak kolumn z sygnałami' % fname)
        self.tmax = len(data) - 1
        for i in cols:
            base, rng = split_range(header[i])
            try:
                vals = [csv_value(r[i] if i < len(r) else '', radixes[i]) for r in data]
            except ValueError as e:
                raise ValueError('%s: kolumna "%s": niepoprawna wartość (%s) - sprawdź --radix'
                                 % (fname, header[i], e))
            if rng:
                w = rng[0] - rng[1] + 1
            else:
                bits = [len(v) for v in vals if isinstance(v, str)]
                nums = [v for v in vals if not isinstance(v, str)]
                w = max(bits + [max(abs(v) for v in nums).bit_length() + any(v < 0 for v in nums)] if nums else bits + [1])
            hi, lo = rng if rng else (w - 1, 0)
            v = Var(tuple(split_parts(base)), hi, lo, w, rng is not None or w > 1)
            prev = None
            for t, x in enumerate(vals):
                x = pad(x, w) if isinstance(x, str) else format(x & ((1 << w) - 1), '0%db' % w)
                if x != prev:
                    v.times.append(t)
                    v.values.append(x)
                    prev = x
            self.vars.append(v)


def load_wave(path, radix=None):
    return CSVWave(path, radix) if path.lower().endswith('.csv') else VCD(path)


def sample_var(v, times, pre):
    out, j, T, V, n = [], 0, v.times, v.values, len(v.times)
    cur = 'x' * v.width
    for t in times:
        if pre:
            while j < n and T[j] < t:
                cur = V[j]
                j += 1
        else:
            while j < n and T[j] <= t:
                cur = V[j]
                j += 1
        out.append(cur)
    return out


class SigRef:
    """A signal (may consist of split bits, e.g. p[0]..p[15] from an analyzer export)."""

    def __init__(self, name, vars_, rng):
        self.name, self.vars = name, vars_
        self.hi = max(v.hi for v in vars_)
        self.lo = min(v.lo for v in vars_)
        self.rng = rng

    def sample(self, times, pre):
        if len(self.vars) == 1:
            vals = sample_var(self.vars[0], times, pre)
        else:
            w = self.hi - self.lo + 1
            cols = [(self.hi - v.hi, sample_var(v, times, pre)) for v in self.vars]
            vals = []
            for i in range(len(times)):
                ch = ['?'] * w
                for off, col in cols:
                    s = col[i]
                    ch[off:off + len(s)] = s
                vals.append(''.join(ch))
        if self.rng:
            s, e = self.hi - self.rng[0], self.hi - self.rng[1] + 1
            vals = [x[s:e] for x in vals]
        return vals


# ---------------------------------------------------------------- multiple HW files

def natural_key(s):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r'(\d+)', s)]


def expand_hw(paths):
    """Files and directories -> naturally sorted list of .vcd/.csv files."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            found = sorted((os.path.join(p, f) for f in os.listdir(p) if f.lower().endswith(('.vcd', '.csv'))),
                           key=lambda f: natural_key(os.path.basename(f)))
            if not found:
                raise ValueError('katalog %s nie zawiera plików .vcd ani .csv' % p)
            out += found
        elif os.path.isfile(p):
            out.append(p)
        else:
            raise ValueError('nie ma takiego pliku ani katalogu: %s' % p)
    return out


def vcd_id(n):
    chars = [chr(c) for c in range(33, 127)]
    s = ''
    while True:
        s = chars[n % len(chars)] + s
        n = n // len(chars) - 1
        if n < 0:
            return s


def write_merged(vcds, out, gap_samples=10):
    """Captures one after another in a single VCD. Signal 'vcdcmp_segment' = capture number."""
    ts = min(v.ts for v in vcds)
    names = {}
    for v in vcds:
        for var in v.vars:
            names.setdefault(var.name, var)
    ids = {n: vcd_id(i + 1) for i, n in enumerate(names)}
    seg_id = vcd_id(0)
    with open(out, 'w', encoding='latin-1') as f:
        f.write('$comment scalone przez vcdcmp: %s $end\n' % ', '.join(os.path.basename(v.fname) for v in vcds))
        unit = next(u for u in ('s', 'ms', 'us', 'ns', 'ps', 'fs') if ts % UNITS[u] == 0)
        f.write('$timescale %d%s $end\n' % (ts // UNITS[unit], unit))
        f.write('$var integer 32 %s vcdcmp_segment $end\n' % seg_id)
        tree = {}
        for n, var in names.items():
            node = tree
            for p in var.path[:-1]:
                node = node.setdefault(p, {})
            node.setdefault(None, []).append(var)

        def emit(node):
            for var in node.get(None, []):
                rng = (' [%d]' % var.hi if var.hi == var.lo else ' [%d:%d]' % (var.hi, var.lo)) if var.ranged else ''
                f.write('$var wire %d %s %s%s $end\n' % (var.width, ids[var.name], var.path[-1], rng))
            for k, sub in node.items():
                if k is not None:
                    f.write('$scope module %s $end\n' % k)
                    emit(sub)
                    f.write('$upscope $end\n')
        emit(tree)
        f.write('$enddefinitions $end\n')

        base = 0
        for si, v in enumerate(vcds):
            mul = v.ts // ts
            grid = v.sample_grid()
            period = (grid[1] - grid[0]) if len(grid) > 1 else 1
            ev = defaultdict(list)
            for var in v.vars:
                for t, val in zip(var.times, var.values):
                    ev[t].append((ids[var.name], val, var.width))
            t0 = min(ev) if ev else 0
            f.write('#%d\nb%s %s\n' % (base, format(si, 'b'), seg_id))
            for t in sorted(ev):
                tt = base + (t - t0) * mul
                if tt != base:
                    f.write('#%d\n' % tt)
                for code, val, w in ev[t]:
                    f.write('%s%s\n' % (val, code) if w == 1 else 'b%s %s\n' % (val, code))
            base += (v.tmax - t0 + period * gap_samples) * mul
        f.write('#%d\n' % base)


# ---------------------------------------------------------------- configuration

class Field:
    def __init__(self, name, spec, defaults):
        if isinstance(spec, str):
            spec = {'signal': spec}
        self.name = name
        self.sim = spec.get('sim', spec.get('signal'))
        self.hw = spec.get('hw', spec.get('stp', spec.get('signal')))   # "stp" - legacy name
        self.signed = spec.get('signed', defaults.get('signed', False))
        self.frac = spec.get('frac', defaults.get('frac', 0))

    def key(self, bits):
        """Compared value: a number (width independent) or raw bits when X/Z are present."""
        if any(c in WILD for c in bits):
            return bits
        v = int(bits, 2)
        if self.signed and bits[0] == '1':
            v -= 1 << len(bits)
        return v

    def fmt(self, k):
        if isinstance(k, str):
            return "b'" + k
        if self.frac:
            return '%d (%g)' % (k, k / float(1 << self.frac))
        return str(k)


def side_name(spec, side):
    if spec is None or isinstance(spec, str):
        return spec
    if side == 'hw' and 'hw' not in spec and 'stp' in spec:      # "stp" - legacy name
        side = 'stp'
    return spec.get(side, spec.get('signal'))


VALID_NAMES = ('valid', 'vld', 'tvalid', 'val', 'stb', 'strobe', 'en', 'start', 'done')
READY_NAMES = ('ready', 'rdy', 'tready')
SKIP_NAMES = ('clk', 'clock', 'clk_*', '*_clk', 'rst*', 'reset*', 'arst*', 'aresetn*', '*_rst', '*_rst_n')


class Interface:
    def __init__(self, d, glob):
        self.d = d
        self.name = d['name']
        self.prefix = d.get('prefix')
        self.valid = d.get('valid')
        self.ready = d.get('ready')
        self.clock = d.get('clock', glob.get('sim_clock'))
        self.hw_clock = d.get('hw_clock', d.get('stp_clock', glob.get('hw_clock', glob.get('stp_clock'))))
        if self.prefix is None:
            if self.valid is None or 'fields' not in d:
                raise KeyError('interfejs %s: podaj "prefix" albo "valid" i "fields"' % self.name)
            self.fields = [Field(n, s, d) for n, s in d['fields'].items()]

    def resolve(self, sim_vcd, hw_vcds):
        """Prefix mode: finds valid/ready/fields among signals starting with the prefix."""
        if self.prefix is None:
            return
        d = self.d
        ps, pt = side_name(self.prefix, 'sim'), side_name(self.prefix, 'hw')
        in_sim = sim_vcd.find_prefix(ps)
        in_hw = [v.find_prefix(pt) for v in hw_vcds]
        common_hw = set.intersection(*map(set, in_hw))
        skip = list(d.get('exclude', [])) + list(SKIP_NAMES)
        names = [n for n in in_sim if n in common_hw and not any(fnmatch.fnmatch(n, p) for p in skip)]
        given = [side_name(d.get(k), 'sim') for k in ('valid', 'ready')]

        def full(n):          # full name (with a dot/bar) instead of a suffix after the prefix
            return isinstance(n, dict) or any(c in n for c in '.|/')

        def pick(given, defaults, what, required):
            if given and full(given):
                return given
            if given:
                if given not in names:
                    raise LookupError('interfejs %s: brak sygnału "%s%s" w obu plikach (znalezione: %s)'
                                      % (self.name, ps, given, ', '.join(names) or '-'))
                return given
            hit = next((n for n in defaults if n in names), None)
            if required and hit is None:
                raise LookupError('interfejs %s: nie znaleziono sygnału %s dla prefiksu "%s" '
                                  '(znalezione: %s) - podaj "valid"' % (self.name, what, ps, ', '.join(names) or '-'))
            return hit

        valid = pick(d.get('valid'), VALID_NAMES, 'valid', True)
        ready = pick(d.get('ready'), READY_NAMES, 'ready', False)
        self.valid = valid if full(valid) else {'sim': ps + valid, 'hw': pt + valid}
        self.ready = (ready if full(ready) else {'sim': ps + ready, 'hw': pt + ready}) if ready else None

        spec = d.get('fields')
        if spec is None:
            spec = {n: {} for n in names if n not in (valid, ready) and n not in given}
        elif isinstance(spec, list):
            spec = {n: {} for n in spec}
        self.fields = []
        for n, opts in spec.items():
            if n not in names:
                raise LookupError('interfejs %s: pole "%s%s" nie występuje w obu plikach' % (self.name, ps, n))
            opts = {'signed': opts, 'frac': 0} if isinstance(opts, bool) else dict(opts)
            opts.update(sim=ps + n, hw=pt + n)
            self.fields.append(Field(n, opts, d))
        if not self.fields:
            raise LookupError('interfejs %s: brak pól danych dla prefiksu "%s"' % (self.name, ps))

        only_sim = [n for n in in_sim if n not in common_hw and not any(fnmatch.fnmatch(n, p) for p in skip)]
        print('   %-12s prefiks %s -> valid=%s%s, pola: %s%s' % (
            self.name, ps, side_name(valid, 'sim'), ', ready=%s' % side_name(ready, 'sim') if ready else '',
            ', '.join(f.name for f in self.fields),
            '  (pominięte, brak w HW: %s)' % ', '.join(only_sim) if only_sim else ''))


# ---------------------------------------------------------------- extraction

class Side:
    """One waveform file + sampling method (clock or grid)."""

    def __init__(self, vcd, side, edge, period):
        self.vcd, self.side, self.edge, self.period = vcd, side, edge, period
        self.seg = os.path.basename(vcd.fname) if side == 'hw' else None
        self._times, self._cache = {}, {}

    def times(self, clock):
        if clock not in self._times:
            if clock:
                self._times[clock] = (self.vcd.clock_edges(clock, self.edge), True)
            else:
                self._times[clock] = (self.vcd.sample_grid(self.period), False)
        return self._times[clock]

    def sample(self, name, clock):
        k = (name, clock)
        if k not in self._cache:
            times, pre = self.times(clock)
            self._cache[k] = self.vcd.find(name).sample(times, pre)
        return self._cache[k]

    def transactions(self, itf):
        clock = itf.clock if self.side == 'sim' else itf.hw_clock
        if self.side == 'sim' and not clock:
            raise LookupError('interfejs %s: podaj "clock" lub globalne "sim_clock"' % itf.name)
        times, _ = self.times(clock)
        valid = self.sample(side_name(itf.valid, self.side), clock)
        rname = side_name(itf.ready, self.side)
        ready = self.sample(rname, clock) if rname else None
        cols = [self.sample(getattr(f, self.side), clock) for f in itf.fields]
        out = []
        for i, vl in enumerate(valid):
            if vl == '1' and (ready is None or ready[i] == '1'):
                out.append(Txn(i, times[i], tuple(f.key(c[i]) for f, c in zip(itf.fields, cols)), self.seg))
        return out


class Txn:
    __slots__ = ('idx', 'time', 'key', 'seg')

    def __init__(self, idx, time, key, seg=None):
        self.idx, self.time, self.key, self.seg = idx, time, key, seg


# ---------------------------------------------------------------- comparison

def align(T, S):
    """Best offset k (HW[i] <-> SIM[i+k]) - voting over equal transactions."""
    index = defaultdict(list)
    for j, s in enumerate(S):
        index[s].append(j)
    votes = Counter()
    for i, t in enumerate(T):
        js = index.get(t, ())
        if len(js) > 2000:          # very frequent value (e.g. all zeros) - little information
            continue
        for j in js:
            votes[j - i] += 1
    if not votes:
        return 0, 0, 0
    best = votes.most_common(2)
    tie = len(best) > 1 and best[1][1] == best[0][1]
    return best[0][0], best[0][1], tie


def compare(itf, T, S):
    res = {'name': itf.name, 'n_hw': len(T), 'n_sim': len(S), 'ok': 0, 'issues': []}
    if not T:
        res['note'] = 'brak transakcji w nagraniu HW'
        return res
    if not S:
        res['note'] = 'brak transakcji w symulacji'
        res['issues'] = [('tylko HW', t, None) for t in T]
        return res
    k, votes, tie = align([t.key for t in T], [s.key for s in S])
    res['offset'], res['votes'], res['tie'] = k, votes, tie
    slack = len(T) // 10 + 4
    s0 = max(0, k)
    Ss = S[s0:k + len(T) + slack]
    sm = difflib.SequenceMatcher(None, [t.key for t in T], [s.key for s in Ss], autojunk=False)
    ops = sm.get_opcodes()
    last_t = max((i2 for tag, i1, i2, j1, j2 in ops if tag == 'equal'), default=0)
    issues = res['issues']
    for tag, i1, i2, j1, j2 in ops:
        if tag == 'equal':
            res['ok'] += i2 - i1
            continue
        tt, ss = T[i1:i2], Ss[j1:j2]
        n = min(len(tt), len(ss)) if tag == 'replace' else 0
        for a, b in zip(tt[:n], ss[:n]):
            issues.append(('różnica', a, b))
        for a in tt[n:]:
            issues.append(('tylko HW', a, None))
        for b in ss[n:]:
            if tag == 'insert' and i1 >= len(T) and i1 >= last_t:
                continue                # surplus of the simulation window past the end of the HW capture
            issues.append(('tylko SIM', None, b))
    return res


# ---------------------------------------------------------------- report

def fmt_time(t, ts):
    fs = t * ts
    for u in ('s', 'ms', 'us', 'ns', 'ps'):
        if fs >= UNITS[u]:
            return '%g %s' % (fs / UNITS[u], u)
    return '%d fs' % fs


def describe(itf, kind, a, b, sim_ts, hw_ts):
    loc = []
    if a is not None:
        t = ' (%s)' % fmt_time(a.time, hw_ts[a.seg]) if hw_ts[a.seg] else ''
        loc.append('HW %s próbka %d%s' % (a.seg, a.idx, t))
    if b is not None:
        loc.append('SIM cykl %d (%s)' % (b.idx, fmt_time(b.time, sim_ts)))
    head = '[%s] %s' % (kind, ' <-> '.join(loc))
    if kind == 'różnica':
        diffs = ['%s: HW=%s SIM=%s' % (f.name, f.fmt(x), f.fmt(y))
                 for f, x, y in zip(itf.fields, a.key, b.key) if x != y]
        return head + '\n        ' + '; '.join(diffs)
    t = a or b
    return head + '\n        ' + ', '.join('%s=%s' % (f.name, f.fmt(x)) for f, x in zip(itf.fields, t.key))


def cmd_list(fname, radix):
    v = load_wave(fname, radix)
    print('%s  (%s, %d sygnałów)' % (fname, 'CSV, %d próbek' % (v.tmax + 1) if v.untimed
                                     else 'timescale ' + fmt_time(1, v.ts), len(v.vars)))
    for var in v.vars:
        print('  %-60s  %3d bit  %d zmian' % (var.name, var.width, len(var.times)))


def main():
    ap = argparse.ArgumentParser(description='Porównanie transakcji na interfejsach: symulacja vs nagranie z analizatora logicznego w FPGA.')
    ap.add_argument('files', nargs='+',
                    help='[config.json] sim.vcd HW [HW ...]  - HW to nagranie z analizatora logicznego '
                         '(.vcd/.csv) lub katalog z nagraniami (z --list: dowolne pliki)')
    ap.add_argument('--itf', action='append', default=[], metavar='NAZWA=PREFIKS[,PREFIKS_HW]',
                    help='interfejs zdefiniowany prefiksem, np. --itf mul_in=u_mul.in_ (można powtarzać)')
    ap.add_argument('--clk', help='zegar w symulacji (zamiast "sim_clock" w configu)')
    ap.add_argument('--signed', action='store_true', help='pola interfejsów z --itf są liczbami ze znakiem')
    ap.add_argument('--list', action='store_true', help='wypisz sygnały z pliku VCD i zakończ')
    ap.add_argument('--edge', choices=('rise', 'fall'), default='rise', help='zbocze zegara (domyślnie rise)')
    ap.add_argument('--hw-period', '--stp-period', dest='hw_period', type=int,
                    help='okres próbkowania nagrań HW (VCD) w jednostkach ich timescale '
                                                        '(domyślnie wykrywany automatycznie)')
    ap.add_argument('--radix', type=str.upper, choices=sorted(RADIX),
                    help='format wartości w wejściowych CSV bez wiersza "Radix" (domyślnie HEX)')
    ap.add_argument('--merge-out', help='zapisz scalone nagrania HW do jednego pliku VCD')
    ap.add_argument('--max', type=int, default=20, help='ile rozbieżności wypisać na interfejs (domyślnie 20)')
    ap.add_argument('--csv', help='zapisz wszystkie rozbieżności do pliku CSV')
    a = ap.parse_args()

    try:
        if a.list:
            for f in expand_hw(a.files):
                cmd_list(f, a.radix)
            return 0
        files, cfg = list(a.files), {}
        if files[0].lower().endswith('.json'):
            with open(files.pop(0), encoding='utf-8') as f:
                cfg = json.load(f)
        if len(files) < 2:
            ap.error('podaj: [config.json] sim.vcd HW [HW ...]')
        if a.clk:
            cfg['sim_clock'] = a.clk
        defs = list(cfg.get('interfaces', []))
        for spec in a.itf:
            name, _, pref = spec.partition('=')
            if not pref:
                ap.error('--itf: oczekiwano NAZWA=PREFIKS, jest "%s"' % spec)
            sp, _, tp = pref.partition(',')
            defs.append({'name': name, 'prefix': {'sim': sp, 'hw': tp or sp}, 'signed': a.signed})
        if not defs:
            ap.error('brak interfejsów - podaj config.json albo --itf NAZWA=PREFIKS')
        itfs = [Interface(d, cfg) for d in defs]

        # 1. merge HW captures
        hw_files = expand_hw(files[1:])
        hw_vcds = [load_wave(p, a.radix) for p in hw_files]
        print('== Nagrania HW: %d plik(ów) ==' % len(hw_vcds))
        for v in hw_vcds:
            print('   %-40s %d próbek' % (os.path.basename(v.fname), len(v.sample_grid(a.hw_period))))
        if len({os.path.basename(v.fname) for v in hw_vcds}) != len(hw_vcds):
            raise ValueError('pliki HW z różnych katalogów mają te same nazwy - zmień nazwy')
        if a.merge_out:
            write_merged(hw_vcds, a.merge_out)
            print('   scalony plik: %s' % a.merge_out)

        # 2. compare - each capture is aligned to the simulation separately
        sim = Side(load_wave(files[0], a.radix), 'sim', a.edge, None)
        if any(i.prefix is not None for i in itfs):
            print('\n== Interfejsy z prefiksów ==')
        for itf in itfs:
            itf.resolve(sim.vcd, hw_vcds)
        hws = [Side(v, 'hw', a.edge, a.hw_period or cfg.get('hw_period', cfg.get('stp_period'))) for v in hw_vcds]
        results = []
        for itf in itfs:
            S = sim.transactions(itf)
            results.append((itf, len(S), [(st.seg, compare(itf, st.transactions(itf), S)) for st in hws]))
    except (LookupError, ValueError, KeyError, OSError) as e:
        sys.stdout.flush()
        print('BŁĄD: %s' % e, file=sys.stderr)
        return 2

    sts = sim.vcd.ts
    pts = {st.seg: None if st.vcd.untimed else st.vcd.ts for st in hws}
    order = {st.seg: i for i, st in enumerate(hws)}
    rows, first, bad = [], [], False
    for itf, n_sim, segs in results:
        issues = [x for _, r in segs for x in r['issues']]
        ok = sum(r['ok'] for _, r in segs)
        n_hw = sum(r['n_hw'] for _, r in segs)
        cnt = Counter(k for k, _, _ in issues)
        print('\n== %s ==  SIM: %d transakcji, HW: %d transakcji w %d nagraniach'
              % (itf.name, n_sim, n_hw, len(segs)))
        print('   zgodne: %d   różne: %d   tylko HW: %d   tylko SIM: %d'
              % (ok, cnt['różnica'], cnt['tylko HW'], cnt['tylko SIM']))
        for seg, r in segs:
            line = '   %-28s ' % seg
            if 'offset' in r:
                line += 'HW#0 <-> SIM#%-6d %d/%d zgodnych' % (r['offset'], r['ok'], r['n_hw'])
                if r['tie']:
                    line += '  UWAGA: kilka równie dobrych przesunięć - dane okresowe?'
            if 'note' in r:
                line += 'UWAGA: ' + r['note']
            print(line)
            bad |= bool(r['issues']) or 'note' in r
        for kind, x, y in issues[:a.max]:
            print('   ' + describe(itf, kind, x, y, sts, pts))
        if len(issues) > a.max:
            print('   ... i %d więcej (--max / --csv)' % (len(issues) - a.max))
        hw_hits = sorted((x for _, x, _ in issues if x is not None), key=lambda t: (order[t.seg], t.time))
        if hw_hits:
            h = hw_hits[0]
            first.append(((order[h.seg], h.time), itf.name, h))
        for kind, x, y in issues:
            rows.append([itf.name, kind, x.seg if x else '',
                         x.idx if x else '', fmt_time(x.time, pts[x.seg]) if x and pts[x.seg] else '',
                         y.idx if y else '', fmt_time(y.time, sts) if y else ''] +
                        ['%s|%s' % (f.fmt(x.key[n]) if x else '', f.fmt(y.key[n]) if y else '')
                         for n, f in enumerate(itf.fields)])

    print('\n== PODSUMOWANIE ==')
    if not bad:
        print('   Wszystkie transakcje zgodne.')
    elif first:
        print('   Pierwsze rozbieżności wg czasu nagrań HW (najwcześniejsza wskazuje podejrzany moduł):')
        for _, name, h in sorted(first, key=lambda e: e[0]):
            print('     %-24s %s próbka %d%s' % (name, h.seg, h.idx,
                                                   ' (%s)' % fmt_time(h.time, pts[h.seg]) if pts[h.seg] else ''))

    if a.csv:
        with open(a.csv, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['interfejs', 'typ', 'hw_plik', 'hw_probka', 'hw_czas', 'sim_cykl', 'sim_czas',
                        'pola (HW|SIM)...'])
            w.writerows(rows)
        print('   CSV: %s' % a.csv)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
