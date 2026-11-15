#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vcdcmp - compares transactions on FPGA module interfaces recorded in
simulation and in hardware by an embedded logic analyzer (vendor independent:
Intel SignalTap, AMD Vivado ILA, Lattice Reveal, Microchip Identify and
others - any tool that exports VCD or CSV).

Transaction = a cycle in which valid==1 (and ready==1, if ready is given), or -
with a bus protocol template (AXI4, Avalon-MM) - a whole burst with its address,
data and response. Invalid bytes (tkeep, empty, wstrb, byteenable) are not compared,
packet boundaries (tlast / endofpacket) and burst ends (wlast / rlast) are checked.
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

class Missing(LookupError):
    """Signal not found (unlike an ambiguous name, which is a plain LookupError)."""


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
            raise Missing('%s: nie znaleziono sygnału "%s" (użyj --list)' % (self.fname, name))
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


# ---------------------------------------------------------------- values

INV = str.maketrans('01', '10')


def bits_key(bits, signed=False):
    """Bits -> int (width independent), or the raw bits when X/Z/? are present."""
    if any(c in WILD for c in bits):
        return bits
    v = int(bits, 2)
    if signed and bits[0] == '1':
        v -= 1 << len(bits)
    return v


def masked(bits, valid, unit=8):
    """Bits -> '0x...' hex string, unit by unit (MSB first). Units with valid[k] == False
    (k = 0 is the least significant unit) become '..' and therefore are not compared."""
    n = -(-len(bits) // unit)
    bits = pad(bits, n * unit)
    hexw = -(-unit // 4)
    out = []
    for k in range(n - 1, -1, -1):
        if k < len(valid) and not valid[k]:
            out.append('.' * hexw)
            continue
        u = bits[(n - 1 - k) * unit:(n - k) * unit]
        out.append('x' * hexw if any(c in WILD for c in u) else format(int(u, 2), '0%dx' % hexw))
    return '0x' + ''.join(out)


def lanes(bits):
    """Enable vector (tkeep, wstrb, byteenable) -> list of bools, index 0 = byte lane 0 (LSB)."""
    return [c == '1' for c in reversed(bits)]


def burst_len(bits, plus_one=True):
    """AXI len (beats - 1) or Avalon burstcount (beats) -> number of beats."""
    if bits is None or any(c in WILD for c in bits):
        return 1
    return int(bits, 2) + 1 if plus_one else max(1, int(bits, 2))


def sync_bursts(beats, lens, has_last):
    """Index of the first data beat that belongs to the first request (AW/AR) of the capture.
    The capture may start in the middle of the data of earlier requests: the data stream is
    cut into bursts at wlast/rlast and the burst lengths are matched against the request
    lengths (first few bursts only, so a later wrong last flag does not move the start)."""
    if not has_last or not beats or not lens:
        return 0
    segs, start = [], 0
    for k, x in enumerate(beats):
        if x.get('last') == '1':
            segs.append((start, k + 1 - start))
            start = k + 1
    best = (-1, 0)
    for d in range(min(len(segs), 8)):
        m = min(len(segs) - d, len(lens), 8)
        score = sum(segs[d + k][1] == lens[k] for k in range(m))
        if score > best[0]:
            best = (score, d)
    return segs[best[1]][0] if segs else 0


def eq_masked(x, y):
    """Equality where masked bytes ('..') and values not compared in this beat (None) match anything."""
    if x == y or x is None or y is None:
        return True
    if isinstance(x, tuple) and isinstance(y, tuple):
        return len(x) == len(y) and all(eq_masked(a, b) for a, b in zip(x, y))
    if isinstance(x, str) and isinstance(y, str) and x.startswith('0x') and len(x) == len(y):
        return all(a == b or a == '.' or b == '.' for a, b in zip(x, y))
    return False


def fmt_value(k, frac=0):
    if k is None:
        return '-'
    if isinstance(k, tuple):
        s = ', '.join(fmt_value(x, frac) for x in k[:8])
        return '[%s%s]' % (s, ', ...' if len(k) > 8 else '')
    if isinstance(k, str):
        return k if k.startswith('0x') else "b'" + k
    if frac:
        return '%d (%g)' % (k, k / float(1 << frac))
    return str(k)


def as_list(x):
    return [] if x is None else list(x) if isinstance(x, list) else [x]


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
        self.when = spec.get('when')                       # compared only in beats where this is 1
        self.byteenable = as_list(spec.get('byteenable'))  # one bit per byte lane (tkeep, wstrb...)
        self.empty = spec.get('empty')                     # {'signal', 'eop', 'symbol_bits', 'high_first'}

    def fmt(self, k):
        return fmt_value(k, self.frac)


class Col:
    """Column of a composite (memory-mapped) transaction."""

    def __init__(self, name):
        self.name = name

    def fmt(self, k):
        return fmt_value(k)


def side_name(spec, side):
    if spec is None or isinstance(spec, str):
        return spec
    if side == 'hw' and 'hw' not in spec and 'stp' in spec:      # "stp" - legacy name
        side = 'stp'
    return spec.get(side, spec.get('signal'))


def exists(wave, name):
    try:
        wave.find(name.lstrip('!~ '))
        return True
    except Missing:
        return False


class SigSet(dict):
    """Signals <prefix><suffix> that exist in the simulation and in every HW capture."""

    def __init__(self, itf, sim_wave, hw_waves, suffixes):
        super().__init__()
        self.itf = itf
        self.ps, self.ph = side_name(itf.prefix, 'sim'), side_name(itf.prefix, 'hw')
        self.partial = []
        for s in suffixes:
            in_sim = exists(sim_wave, self.ps + s)
            in_hw = [exists(w, self.ph + s) for w in hw_waves]
            if in_sim and all(in_hw):
                self[s] = {'sim': self.ps + s, 'hw': self.ph + s}
            elif in_sim or any(in_hw):
                self.partial.append(s)

    def need(self, *names):
        miss = [n for n in names if n not in self]
        if miss:
            raise LookupError('interfejs %s: brak sygnałów %s (prefiks "%s") w symulacji lub w nagraniach HW'
                              % (self.itf.name, ', '.join(self.ps + n for n in miss), self.ps))

    def describe(self, extra=''):
        print('   %-14s %-12s prefiks %s -> %s%s%s' % (
            self.itf.name, self.itf.d.get('protocol', ''), self.ps, ', '.join(self), extra,
            '  (pominięte, brak po jednej ze stron: %s)' % ', '.join(self.partial) if self.partial else ''))


VALID_NAMES = ('valid', 'vld', 'tvalid', 'val', 'stb', 'strobe', 'en', 'start', 'done')
READY_NAMES = ('ready', 'rdy', 'tready')
SKIP_NAMES = ('clk', 'clock', 'clk_*', '*_clk', 'rst*', 'reset*', 'arst*', 'aresetn*', '*_rst', '*_rst_n')


class Interface:
    """Base class: a named source of transactions (resolve() names, extract() per side)."""
    last = None
    disabled = None

    def __init__(self, d, glob):
        self.d = d
        self.name = d['name']
        self.prefix = d.get('prefix')
        self.clock = d.get('clock', glob.get('sim_clock'))
        self.hw_clock = d.get('hw_clock', d.get('stp_clock', glob.get('hw_clock', glob.get('stp_clock'))))
        self.id_index = None          # index of the ID column in the key -> compare per ID

    def clock_for(self, side):
        return self.clock if side.side == 'sim' else self.hw_clock

    def resolve(self, sim_wave, hw_waves):
        pass


class BeatInterface(Interface):
    """Streaming-style interface: one transaction per beat (valid [&& ready N cycles earlier])."""

    def __init__(self, d, glob):
        super().__init__(d, glob)
        self.valid = d.get('valid')
        self.ready = d.get('ready')
        self.ready_latency = int(d.get('ready_latency', 0))
        self.last = d.get('last')
        self.sop = d.get('sop')
        self.last_name = 'last'
        self.fields = None
        if self.prefix is None:
            if self.valid is None or 'fields' not in d:
                raise KeyError('interfejs %s: podaj "prefix" albo "valid" i "fields"' % self.name)
            self.fields = [Field(n, s, d) for n, s in d['fields'].items()]
            self.set_id(d.get('by_id'))

    def set_id(self, name):
        if isinstance(name, str):
            idx = [f.name for f in self.fields].index(name) if name in [f.name for f in self.fields] else None
            if idx is None:
                raise KeyError('interfejs %s: "by_id" wskazuje nieznane pole "%s"' % (self.name, name))
            self.id_index = idx

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
        used = set()

        def full(n):          # full name (with a dot/bar) instead of a suffix after the prefix
            return isinstance(n, dict) or any(c in n for c in '.|/')

        def pick(given, defaults, what, required):
            if given and full(given):
                return given
            if given:
                inv, base = given[:len(given) - len(given.lstrip('!~'))], given.lstrip('!~')
                if base not in names:
                    raise LookupError('interfejs %s: brak sygnału "%s%s" w obu plikach (znalezione: %s)'
                                      % (self.name, ps, base, ', '.join(names) or '-'))
                used.add(base)
                return {'sim': inv + ps + base, 'hw': inv + pt + base}
            hit = next((n for n in defaults if n in names), None)
            if required and hit is None:
                raise LookupError('interfejs %s: nie znaleziono sygnału %s dla prefiksu "%s" '
                                  '(znalezione: %s) - podaj "valid"' % (self.name, what, ps, ', '.join(names) or '-'))
            used.add(hit)
            return {'sim': ps + hit, 'hw': pt + hit} if hit else None

        self.valid = pick(d.get('valid'), VALID_NAMES, 'valid', True)
        self.ready = pick(d.get('ready'), READY_NAMES, 'ready', False)
        self.last = pick(d.get('last'), (), 'last', False)
        self.sop = pick(d.get('sop'), (), 'sop', False)

        spec = d.get('fields')
        if spec is None:
            spec = {n: {} for n in names if n not in used}
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
        self.set_id(d.get('by_id'))

        only_sim = [n for n in in_sim if n not in common_hw and not any(fnmatch.fnmatch(n, p) for p in skip)]
        print('   %-14s prefiks %s -> valid=%s%s, pola: %s%s' % (
            self.name, ps, side_name(self.valid, 'sim'),
            ', ready=%s' % side_name(self.ready, 'sim') if self.ready else '',
            ', '.join(f.name for f in self.fields),
            '  (pominięte, brak w HW: %s)' % ', '.join(only_sim) if only_sim else ''))

    def extract(self, side):
        sn = lambda spec: side_name(spec, side.side)
        sigs = {}
        for n, f in enumerate(self.fields):
            sigs[('f', n)] = getattr(f, side.side)
            for m, be in enumerate(f.byteenable):
                sigs[('be', n, m)] = sn(be)
            if f.empty:
                sigs[('em', n)] = sn(f.empty['signal'])
                sigs[('eop', n)] = sn(f.empty['eop'])
            if f.when:
                sigs[('w', n)] = sn(f.when)
        if self.last:
            sigs['last'] = sn(self.last)
        if self.sop:
            sigs['sop'] = sn(self.sop)
        beats = side.handshakes(sn(self.valid), sn(self.ready) if self.ready else None,
                                self.ready_latency, sigs, self.clock_for(side))
        txns = []
        for i, t, v in beats:
            key = []
            for n, f in enumerate(self.fields):
                bits = v[('f', n)]
                if ('w', n) in v and v[('w', n)] != '1':
                    key.append(None)
                elif ('em', n) in v and v[('eop', n)] == '1':
                    e = v[('em', n)]
                    empty = 0 if any(c in WILD for c in e) else int(e, 2)
                    unit = int(f.empty.get('symbol_bits', 8))
                    nsym = -(-len(bits) // unit)
                    if f.empty.get('high_first', True):     # symbol 0 in the high-order bits (Avalon default)
                        ok = [k >= empty for k in range(nsym)]
                    else:
                        ok = [k < nsym - empty for k in range(nsym)]
                    key.append(masked(bits, ok, unit))
                elif f.byteenable or ('em', n) in v:
                    ok = None
                    for m in range(len(f.byteenable)):
                        ln = lanes(v[('be', n, m)])
                        ok = ln if ok is None else [a and b for a, b in zip(ok, ln)]
                    key.append(masked(bits, ok or []))
                else:
                    key.append(bits_key(bits, f.signed))
            tx = Txn(i, t, tuple(key), side.seg)
            tx.last, tx.sop = v.get('last'), v.get('sop')
            txns.append(tx)
        return txns, self.packet_checks(txns, side)

    def packet_checks(self, txns, side):
        """Numbers packets/words; with sop also checks sop/eop pairing (protocol errors)."""
        issues = []
        if self.last is None and self.sop is None:
            return issues
        pkt = pos = 0
        inside, seen_sop = False, False
        for tx in txns:
            if self.sop is not None:
                if tx.sop == '1':
                    if inside and seen_sop:
                        issues.append(side.proto(tx, 'startofpacket przed endofpacket poprzedniego pakietu'))
                        pkt += 1
                    pos, inside, seen_sop = 0, True, True
                elif not inside and seen_sop:
                    issues.append(side.proto(tx, 'słowo poza pakietem (brak startofpacket)'))
            tx.pkt, tx.pos = pkt, pos
            pos += 1
            if tx.last == '1':
                pkt, pos, inside = pkt + 1, 0, False
        return issues


class AxiStream(BeatInterface):
    SIGNALS = ('tvalid', 'tready', 'tdata', 'tkeep', 'tstrb', 'tlast', 'tid', 'tdest', 'tuser')

    def resolve(self, sim_wave, hw_waves):
        S = SigSet(self, sim_wave, hw_waves, self.SIGNALS)
        S.need('tvalid', 'tdata')
        self.valid, self.ready, self.last = S['tvalid'], S.get('tready'), S.get('tlast')
        self.last_name = 'tlast'
        keep = [S[s] for s in ('tkeep', 'tstrb') if s in S]
        self.fields = [Field('tdata', dict(S['tdata'], byteenable=keep), {})]
        self.fields += [Field(s, S[s], self.d) for s in ('tkeep', 'tstrb', 'tid', 'tdest', 'tuser') if s in S]
        if self.d.get('by_id') and 'tid' in S:
            self.set_id('tid')
        S.describe('  (tdata maskowane: %s)' % '&'.join(s for s in ('tkeep', 'tstrb') if s in S) if keep else '')


class AvalonSt(BeatInterface):
    SIGNALS = ('valid', 'ready', 'data', 'startofpacket', 'endofpacket', 'empty', 'channel', 'error')

    def resolve(self, sim_wave, hw_waves):
        S = SigSet(self, sim_wave, hw_waves, self.SIGNALS)
        S.need('valid', 'data')
        self.valid, self.ready = S['valid'], S.get('ready')
        self.last, self.sop = S.get('endofpacket'), S.get('startofpacket')
        self.last_name = 'endofpacket'
        data = dict(S['data'])
        if 'empty' in S and 'endofpacket' in S:
            data['empty'] = {'signal': S['empty'], 'eop': S['endofpacket'],
                             'symbol_bits': self.d.get('symbol_bits', 8),
                             'high_first': self.d.get('first_symbol_in_high_order_bits', True)}
        self.fields = [Field('data', data, {})]
        if 'empty' in S:
            self.fields.append(Field('empty', dict(S['empty'], when=S.get('endofpacket')), {}))
        self.fields += [Field(s, S[s], self.d) for s in ('channel', 'error') if s in S]
        if self.d.get('by_id') and 'channel' in S:
            self.set_id('channel')
        S.describe('  (ready_latency=%d%s)' % (self.ready_latency, ', data maskowane: empty' if 'empty' in data else ''))


AXI_ADDR = ('id', 'addr', 'len', 'size', 'burst')


class Axi4Write(Interface):
    SIGNALS = tuple('aw' + s for s in ('valid', 'ready') + AXI_ADDR) + \
        ('wvalid', 'wready', 'wdata', 'wstrb', 'wlast', 'bvalid', 'bready', 'bid', 'bresp')

    def __init__(self, d, glob):
        super().__init__(d, glob)
        self.name = d['name'] + '.write'

    def resolve(self, sim_wave, hw_waves):
        S = self.S = SigSet(self, sim_wave, hw_waves, self.SIGNALS)
        if 'awvalid' not in S and 'wvalid' not in S:
            self.disabled = 'brak kanałów AW/W'
            return
        S.need('awvalid', 'awaddr', 'wvalid', 'wdata')
        self.cols = [c for c in AXI_ADDR if 'aw' + c in S] + ['data'] + (['resp'] if 'bresp' in S else [])
        self.fields = [Col(c) for c in self.cols]
        if self.d.get('by_id') and 'awid' in S:
            self.id_index = 0
        S.describe()

    def extract(self, side):
        S, clk = self.S, self.clock_for(side)
        g = lambda k: side_name(S[k], side.side) if k in S else None
        aw = side.handshakes(g('awvalid'), g('awready'), 0,
                             {c: g('aw' + c) for c in AXI_ADDR if 'aw' + c in S}, clk)
        w = side.handshakes(g('wvalid'), g('wready'), 0,
                            {k: g('w' + k) for k in ('data', 'strb', 'last') if 'w' + k in S}, clk)
        b = side.handshakes(g('bvalid'), g('bready'), 0,
                            {k: g('b' + k) for k in ('id', 'resp') if 'b' + k in S}, clk) if 'bvalid' in S else []
        if aw and 'wlast' in S:     # skip W data of AWs from before the capture window
            w = w[sync_bursts([x[2] for x in w], [burst_len(a.get('len')) for _, _, a in aw], True):]
        elif aw:                    # without wlast: W beats before the first AW belong to earlier bursts
            w = [x for x in w if x[0] >= aw[0][0]]
        issues, txns, wi = [], [], 0
        pend = defaultdict(list)
        for i, t, a in aw:
            n = burst_len(a.get('len'))
            beats = w[wi:wi + n]
            if len(beats) < n:
                break                       # write data not complete inside the window
            wi += n
            tx = Txn(i, t, None, side.seg)
            if 'wlast' in S:
                at = [k for k, x in enumerate(beats) if x[2]['last'] == '1']
                if at != [n - 1]:
                    where = 'brak wlast' if not at else 'wlast na słowie %d (%s)' % (
                        at[0], 'za wcześnie' if at[0] < n - 1 else 'kilka razy')
                    if not at and wi < len(w) and w[wi][2]['last'] == '1':
                        where = 'wlast za późno (na słowie %d)' % n
                    issues.append(side.proto(tx, '%s, oczekiwane na słowie %d (awlen=%d)' % (where, n - 1, n - 1)))
            data = tuple(masked(x[2]['data'], lanes(x[2]['strb']) if 'strb' in x[2] else []) for x in beats)
            tx.parts = [bits_key(a[c]) for c in AXI_ADDR if c in a] + [data]
            tx.wend = beats[-1][0]
            txns.append(tx)
            pend[a.get('id')].append(tx)
        if 'bvalid' in S:
            for i, t, r in b:
                q = pend.get(r.get('id'))
                if q and q[0].wend <= i:    # otherwise: response to a write from before the window
                    q.pop(0).parts.append(bits_key(r['resp']) if 'resp' in r else None)
        txns = [x for x in txns if len(x.parts) == len(self.cols)]
        for x in txns:
            x.key = tuple(x.parts)
        return txns, issues


class Axi4Read(Interface):
    SIGNALS = tuple('ar' + s for s in ('valid', 'ready') + AXI_ADDR) + \
        ('rvalid', 'rready', 'rid', 'rdata', 'rresp', 'rlast')

    def __init__(self, d, glob):
        super().__init__(d, glob)
        self.name = d['name'] + '.read'

    def resolve(self, sim_wave, hw_waves):
        S = self.S = SigSet(self, sim_wave, hw_waves, self.SIGNALS)
        if 'arvalid' not in S and 'rvalid' not in S:
            self.disabled = 'brak kanałów AR/R'
            return
        S.need('arvalid', 'araddr', 'rvalid', 'rdata')
        self.cols = [c for c in AXI_ADDR if 'ar' + c in S] + ['data'] + (['resp'] if 'rresp' in S else [])
        self.fields = [Col(c) for c in self.cols]
        if self.d.get('by_id') and 'arid' in S:
            self.id_index = 0
        S.describe('  (odpowiedzi przypisywane do żądań wg rid)' if 'rid' in S else '')

    def extract(self, side):
        S, clk = self.S, self.clock_for(side)
        g = lambda k: side_name(S[k], side.side) if k in S else None
        ar = side.handshakes(g('arvalid'), g('arready'), 0,
                             {c: g('ar' + c) for c in AXI_ADDR if 'ar' + c in S}, clk)
        r = side.handshakes(g('rvalid'), g('rready'), 0,
                            {k: g('r' + k) for k in ('id', 'data', 'resp', 'last') if 'r' + k in S}, clk)
        issues, txns = [], []
        ars, rs = defaultdict(list), defaultdict(list)
        for i, t, v in ar:
            tx = Txn(i, t, None, side.seg)
            tx.parts = [bits_key(v[c]) for c in AXI_ADDR if c in v]
            tx.n, tx.beats, tx.resps = burst_len(v.get('len')), [], []
            txns.append(tx)
            ars[v.get('id')].append(tx)
        for x in r:
            rs[x[2].get('id')].append(x)
        # responses of one ID come in request order; IDs may be interleaved/reordered freely
        for rid, reqs in ars.items():
            beats = [x for x in rs.get(rid, []) if x[0] > reqs[0].idx]
            k = sync_bursts([x[2] for x in beats], [tx.n for tx in reqs], 'rlast' in S)
            for tx in reqs:
                chunk = beats[k:k + tx.n]
                if len(chunk) < tx.n:
                    break                   # read data not complete inside the window
                k += tx.n
                tx.beats = [masked(x[2]['data'], []) for x in chunk]
                tx.resps = [bits_key(x[2]['resp']) if 'resp' in x[2] else None for x in chunk]
                at = [m for m, x in enumerate(chunk) if x[2].get('last') == '1']
                if 'rlast' in S and at != [tx.n - 1]:
                    where = 'brak rlast' if not at else 'rlast na słowie %d (%s)' % (
                        at[0], 'za wcześnie' if at[0] < tx.n - 1 else 'kilka razy')
                    if not at and k < len(beats) and beats[k][2].get('last') == '1':
                        where = 'rlast za późno (na słowie %d)' % tx.n
                    issues.append(side.proto(tx, '%s, oczekiwane na słowie %d (arlen=%d)' % (where, tx.n - 1, tx.n - 1)))
        done = [x for x in txns if len(x.beats) == x.n]
        for x in done:
            x.key = tuple(x.parts + [tuple(x.beats)] + ([tuple(x.resps)] if 'rresp' in S else []))
        return done, issues


class AvalonMm(Interface):
    SIGNALS = ('address', 'read', 'write', 'waitrequest', 'readdata', 'readdatavalid', 'writedata',
               'byteenable', 'burstcount', 'response', 'writeresponsevalid', 'beginbursttransfer')

    def resolve(self, sim_wave, hw_waves):
        S = self.S = SigSet(self, sim_wave, hw_waves, self.SIGNALS)
        S.need('address')
        if 'read' not in S and 'write' not in S:
            raise LookupError('interfejs %s: brak sygnałów read/write (prefiks "%s")' % (self.name, S.ps))
        self.read_latency = self.d.get('read_latency')
        if 'read' in S and 'readdatavalid' not in S and self.read_latency is None:
            raise KeyError('interfejs %s: brak readdatavalid - podaj "read_latency"' % self.name)
        self.cols = ['type', 'addr'] + (['burstcount'] if 'burstcount' in S else []) + ['data'] + \
            (['resp'] if 'response' in S else [])
        self.fields = [Col(c) for c in self.cols]
        S.describe()

    def extract(self, side):
        S, clk = self.S, self.clock_for(side)
        times, _ = side.times(clk)
        v = {k: side.sample(side_name(S[k], side.side), clk) for k in S}
        has = lambda k, i: k in v and v[k][i] == '1'
        resp = 'response' in S
        txns, rq, wq, wopen = [], [], [], None
        write_idle = False                  # was `write` low at some point since the capture start?
        for i in range(len(times)):
            if has('readdatavalid', i) and rq:              # data first: belongs to earlier commands
                tx = rq[0]
                tx.beats.append(masked(v['readdata'][i], tx.be))
                tx.resps.append(bits_key(v['response'][i]) if resp else None)
                if len(tx.beats) == tx.n:
                    rq.pop(0)
            if has('writeresponsevalid', i) and wq:
                wq.pop(0).resps.append(bits_key(v['response'][i]))
            accepted = not has('waitrequest', i)
            if has('write', i) and accepted:
                if wopen is not None and has('beginbursttransfer', i):
                    wopen = None                            # new burst: the open one stays incomplete
                if wopen is None:
                    wopen = Txn(i, times[i], None, side.seg)
                    wopen.kind, wopen.n = 'W', burst_len(v['burstcount'][i] if 'burstcount' in v else None, False)
                    wopen.addr, wopen.beats, wopen.resps = bits_key(v['address'][i]), [], []
                    # write held high since the capture start: maybe the rest of a burst
                    # from before the window - cannot be told apart, so it is dropped
                    wopen.partial = (side.side == 'hw' and not write_idle and wopen.n > 1
                                     and 'beginbursttransfer' not in S)
                    txns.append(wopen)
                be = lanes(v['byteenable'][i]) if 'byteenable' in v else []
                wopen.beats.append(masked(v['writedata'][i], be) if 'writedata' in v else None)
                if len(wopen.beats) == wopen.n:
                    if 'writeresponsevalid' in v:
                        wq.append(wopen)
                    wopen = None
            if not has('write', i):
                write_idle = True
            if has('read', i) and accepted:
                tx = Txn(i, times[i], None, side.seg)
                tx.kind, tx.n = 'R', burst_len(v['burstcount'][i] if 'burstcount' in v else None, False)
                tx.addr, tx.beats, tx.resps = bits_key(v['address'][i]), [], []
                tx.be = lanes(v['byteenable'][i]) if 'byteenable' in v else []
                txns.append(tx)
                if 'readdatavalid' in v:
                    rq.append(tx)
                elif i + int(self.read_latency) < len(times):
                    j = i + int(self.read_latency)
                    tx.beats.append(masked(v['readdata'][j], tx.be))
                    tx.resps.append(bits_key(v['response'][j]) if resp else None)
        out = []
        for tx in txns:
            if getattr(tx, 'partial', False) or len(tx.beats) != tx.n:
                continue
            if tx.kind == 'W' and 'writeresponsevalid' in v and not tx.resps:
                continue
            key = ['W' if tx.kind == 'W' else 'R', tx.addr] + ([tx.n] if 'burstcount' in S else []) + [tuple(tx.beats)]
            if resp:
                key.append(tuple(tx.resps) if tx.resps else None)
            tx.key = tuple(key)
            out.append(tx)
        return out, []


PROTOCOLS = {'axi4-stream': [AxiStream], 'axis': [AxiStream], 'avalon-st': [AvalonSt],
             'axi4': [Axi4Write, Axi4Read], 'axi4-lite': [Axi4Write, Axi4Read], 'axi': [Axi4Write, Axi4Read],
             'avalon-mm': [AvalonMm]}


def make_interfaces(d, glob):
    p = d.get('protocol')
    if p is None:
        return [BeatInterface(d, glob)]
    if p.lower() not in PROTOCOLS:
        raise KeyError('interfejs %s: nieznany protokół "%s" (dostępne: %s)' % (d['name'], p, ', '.join(PROTOCOLS)))
    if d.get('prefix') is None:
        raise KeyError('interfejs %s: protokół %s wymaga "prefix"' % (d['name'], p))
    return [cls(d, glob) for cls in PROTOCOLS[p.lower()]]


# ---------------------------------------------------------------- extraction

class Side:
    """One waveform file + sampling method (clock or grid)."""

    def __init__(self, vcd, side, edge, period):
        self.vcd, self.side, self.edge, self.period = vcd, side, edge, period
        self.seg = os.path.basename(vcd.fname) if side == 'hw' else None
        self._times, self._cache = {}, {}

    def times(self, clock):
        if self.side == 'sim' and not clock:
            raise LookupError('podaj zegar symulacji: "sim_clock" w konfiguracji albo --clk')
        if clock not in self._times:
            if clock:
                self._times[clock] = (self.vcd.clock_edges(clock, self.edge), True)
            else:
                self._times[clock] = (self.vcd.sample_grid(self.period), False)
        return self._times[clock]

    def sample(self, name, clock):
        """Values of a signal at the sampling points; '!name' / '~name' = inverted signal."""
        k = (name, clock)
        if k not in self._cache:
            base = name.lstrip('!~ ')
            times, pre = self.times(clock)
            vals = self.vcd.find(base).sample(times, pre)
            if base != name.strip():
                vals = [x.translate(INV) for x in vals]
            self._cache[k] = vals
        return self._cache[k]

    def handshakes(self, valid, ready, latency, sigs, clock):
        """Beats where valid == 1 and ready == 1 `latency` cycles earlier -> [(i, time, {key: bits})]."""
        times, _ = self.times(clock)
        vl = self.sample(valid, clock)
        rd = self.sample(ready, clock) if ready else None
        cols = {k: self.sample(n, clock) for k, n in sigs.items() if n}
        out = []
        for i, x in enumerate(vl):
            if x != '1':
                continue
            if rd is not None and (i < latency or rd[i - latency] != '1'):
                continue
            out.append((i, times[i], {k: c[i] for k, c in cols.items()}))
        return out

    def proto(self, tx, msg):
        return ('protokół', tx, None, msg) if self.side == 'hw' else ('protokół', None, tx, msg)


class Txn:
    def __init__(self, idx, time, key, seg=None):
        self.idx, self.time, self.key, self.seg = idx, time, key, seg
        self.last = self.sop = self.pkt = self.pos = None


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


def boundary_issues(itf, pairs, T, S):
    """Packet end marker (tlast / endofpacket) on a different word in HW than in SIM."""
    out, skip = [], set()
    pt = {id(t): k for k, t in enumerate(T)}
    ps = {id(s): k for k, s in enumerate(S)}
    words = lambda n: '%d słowo' % n if n == 1 else '%d słowa' % n if n < 5 else '%d słów' % n
    for a, b in pairs:
        if a.last == b.last or id(a) in skip or id(b) in skip:
            continue
        if a.last == '1':           # HW ends the packet, SIM does not (yet)
            k = ps[id(b)]
            n = next((m for m in range(k, len(S)) if S[m].last == '1'), None)
            if n is None:
                continue
            out.append(('pakiet', a, b, '%s w HW za wcześnie o %s' % (itf.last_name, words(n - k))))
            skip.add(id(S[n]))
        else:                       # SIM ends the packet, HW does not
            k = pt[id(a)]
            n = next((m for m in range(k, len(T)) if T[m].last == '1'), None)
            if n is None:
                out.append(('pakiet', a, b, 'brak %s w HW do końca nagrania' % itf.last_name))
                continue
            out.append(('pakiet', a, b, '%s w HW za późno o %s' % (itf.last_name, words(n - k))))
            skip.add(id(T[n]))
    return out


def compare_seq(itf, T, S):
    res = {'n_hw': len(T), 'n_sim': len(S), 'ok': 0, 'issues': []}
    if not T:
        res['note'] = 'brak transakcji w nagraniu HW'
        return res
    if not S:
        res['note'] = 'brak transakcji w symulacji'
        res['issues'] = [('tylko HW', t, None, None) for t in T]
        return res
    k, votes, tie = align([t.key for t in T], [s.key for s in S])
    res['offset'], res['votes'], res['tie'] = k, votes, tie
    slack = len(T) // 10 + 4
    s0 = max(0, k)
    Ss = S[s0:k + len(T) + slack]
    sm = difflib.SequenceMatcher(None, [t.key for t in T], [s.key for s in Ss], autojunk=False)
    ops = sm.get_opcodes()
    last_t = max((i2 for tag, i1, i2, j1, j2 in ops if tag == 'equal'), default=0)
    issues, pairs = res['issues'], []
    for tag, i1, i2, j1, j2 in ops:
        if tag == 'equal':
            res['ok'] += i2 - i1
            pairs += zip(T[i1:i2], Ss[j1:j2])
            continue
        tt, ss = T[i1:i2], Ss[j1:j2]
        n = min(len(tt), len(ss)) if tag == 'replace' else 0
        for a, b in zip(tt[:n], ss[:n]):
            if eq_masked(a.key, b.key):
                res['ok'] += 1          # differ only in bytes that are not valid on one of the sides
            else:
                issues.append(('różnica', a, b, None))
            pairs.append((a, b))
        for a in tt[n:]:
            issues.append(('tylko HW', a, None, None))
        for b in ss[n:]:
            if tag == 'insert' and i1 >= len(T) and i1 >= last_t:
                continue                # surplus of the simulation window past the end of the HW capture
            issues.append(('tylko SIM', None, b, None))
    if itf.last is not None:
        issues += boundary_issues(itf, pairs, T, Ss)
    return res


def compare(itf, T, S):
    """Compares HW and SIM transaction sequences; with an ID column - separately per ID."""
    if itf.id_index is None:
        return compare_seq(itf, T, S)
    idx = itf.id_index
    groups = sorted({t.key[idx] for t in T} | {s.key[idx] for s in S}, key=str)
    res = {'n_hw': len(T), 'n_sim': len(S), 'ok': 0, 'issues': [], 'ids': len(groups)}
    for g in groups:
        r = compare_seq(itf, [t for t in T if t.key[idx] == g], [s for s in S if s.key[idx] == g])
        res['ok'] += r['ok']
        res['issues'] += r['issues']
    res['issues'].sort(key=lambda x: (x[1] or x[2]).idx)
    if not T:
        res['note'] = 'brak transakcji w nagraniu HW'
    return res


# ---------------------------------------------------------------- report

def fmt_time(t, ts):
    fs = t * ts
    for u in ('s', 'ms', 'us', 'ns', 'ps'):
        if fs >= UNITS[u]:
            return '%g %s' % (fs / UNITS[u], u)
    return '%d fs' % fs


def where(tx, ts, label):
    pkt = ' pakiet %d słowo %d' % (tx.pkt, tx.pos) if tx.pkt is not None else ''
    t = ' (%s)' % fmt_time(tx.time, ts) if ts else ''
    if label == 'HW':
        return 'HW %s próbka %d%s%s' % (tx.seg, tx.idx, t, pkt)
    return 'SIM cykl %d%s%s' % (tx.idx, t, pkt)


def diff_text(f, x, y):
    if isinstance(x, tuple) and isinstance(y, tuple):
        if len(x) != len(y):
            return '%s: liczba słów HW=%d SIM=%d' % (f.name, len(x), len(y))
        d = [k for k in range(len(x)) if x[k] != y[k]]
        return '; '.join('%s[%d]: HW=%s SIM=%s' % (f.name, k, fmt_value(x[k]), fmt_value(y[k]))
                         for k in d[:4]) + (' ...' if len(d) > 4 else '')
    return '%s: HW=%s SIM=%s' % (f.name, f.fmt(x), f.fmt(y))


def describe(itf, issue, sim_ts, hw_ts):
    kind, a, b, msg = issue
    loc = []
    if a is not None:
        loc.append(where(a, hw_ts[a.seg], 'HW'))
    if b is not None:
        loc.append(where(b, sim_ts, 'SIM'))
    head = '[%s] %s' % (kind, ' <-> '.join(loc))
    if msg:
        return head + '\n        ' + msg
    if kind == 'różnica':
        return head + '\n        ' + '; '.join(diff_text(f, x, y) for f, x, y in zip(itf.fields, a.key, b.key) if x != y)
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
    ap.add_argument('--itf', action='append', default=[], metavar='NAZWA[:PROTOKÓŁ]=PREFIKS[,PREFIKS_HW]',
                    help='interfejs zdefiniowany prefiksem, np. --itf mul_in=u_mul.in_ albo '
                         '--itf m0:axi4=u_dut.m_axi_ (można powtarzać); protokoły: %s' % ', '.join(PROTOCOLS))
    ap.add_argument('--clk', help='zegar w symulacji (zamiast "sim_clock" w configu)')
    ap.add_argument('--signed', action='store_true', help='pola interfejsów z --itf są liczbami ze znakiem')
    ap.add_argument('--list', action='store_true', help='wypisz sygnały z pliku VCD/CSV i zakończ')
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
            name, _, proto = name.partition(':')
            sp, _, tp = pref.partition(',')
            d = {'name': name, 'prefix': {'sim': sp, 'hw': tp or sp}, 'signed': a.signed}
            if proto:
                d['protocol'] = proto
            defs.append(d)
        if not defs:
            ap.error('brak interfejsów - podaj config.json albo --itf NAZWA=PREFIKS')
        itfs = [x for d in defs for x in make_interfaces(d, cfg)]

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
        print('\n== Interfejsy ==')
        for itf in itfs:
            itf.resolve(sim.vcd, hw_vcds)
            if itf.disabled:
                print('   %-14s pominięty: %s' % (itf.name, itf.disabled))
        itfs = [i for i in itfs if not i.disabled]
        hws = [Side(v, 'hw', a.edge, a.hw_period or cfg.get('hw_period', cfg.get('stp_period'))) for v in hw_vcds]
        results = []
        for itf in itfs:
            S, sim_issues = itf.extract(sim)
            segs = []
            for st in hws:
                T, hw_issues = itf.extract(st)
                r = compare(itf, T, S)
                r['issues'] = hw_issues + r['issues']
                segs.append((st.seg, r))
            results.append((itf, len(S), sim_issues, segs))
    except (LookupError, ValueError, KeyError, OSError) as e:
        sys.stdout.flush()
        print('BŁĄD: %s' % e, file=sys.stderr)
        return 2

    sts = sim.vcd.ts
    pts = {st.seg: None if st.vcd.untimed else st.vcd.ts for st in hws}
    order = {st.seg: i for i, st in enumerate(hws)}
    rows, first, bad = [], [], False
    for itf, n_sim, sim_issues, segs in results:
        issues = sim_issues + [x for _, r in segs for x in r['issues']]
        ok = sum(r['ok'] for _, r in segs)
        n_hw = sum(r['n_hw'] for _, r in segs)
        cnt = Counter(x[0] for x in issues)
        print('\n== %s ==  SIM: %d transakcji, HW: %d transakcji w %d nagraniach'
              % (itf.name, n_sim, n_hw, len(segs)))
        print('   zgodne: %d   różne: %d   tylko HW: %d   tylko SIM: %d%s%s'
              % (ok, cnt['różnica'], cnt['tylko HW'], cnt['tylko SIM'],
                 '   granice pakietów: %d' % cnt['pakiet'] if itf.last is not None else '',
                 '   protokół: %d' % cnt['protokół'] if cnt['protokół'] else ''))
        for seg, r in segs:
            line = '   %-28s ' % seg
            if 'note' in r:
                line += 'UWAGA: ' + r['note']
            elif 'ids' in r:
                line += 'dopasowanie wg ID (%d ID)  %d/%d zgodnych' % (r['ids'], r['ok'], r['n_hw'])
            elif 'offset' in r:
                line += 'HW#0 <-> SIM#%-6d %d/%d zgodnych' % (r['offset'], r['ok'], r['n_hw'])
                if r['tie']:
                    line += '  UWAGA: kilka równie dobrych przesunięć - dane okresowe?'
            print(line)
            bad |= bool(r['issues']) or 'note' in r
        bad |= bool(sim_issues)
        for x in issues[:a.max]:
            print('   ' + describe(itf, x, sts, pts))
        if len(issues) > a.max:
            print('   ... i %d więcej (--max / --csv)' % (len(issues) - a.max))
        hw_hits = sorted((x[1] for x in issues if x[1] is not None), key=lambda t: (order[t.seg], t.time))
        if hw_hits:
            h = hw_hits[0]
            first.append(((order[h.seg], h.time), itf.name, h))
        for kind, x, y, msg in issues:
            rows.append([itf.name, kind, x.seg if x else '',
                         x.idx if x else '', fmt_time(x.time, pts[x.seg]) if x and pts[x.seg] else '',
                         y.idx if y else '', fmt_time(y.time, sts) if y else '', msg or ''] +
                        ['%s|%s' % (f.fmt(x.key[n]) if x and x.key else '', f.fmt(y.key[n]) if y and y.key else '')
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
            w.writerow(['interfejs', 'typ', 'hw_plik', 'hw_probka', 'hw_czas', 'sim_cykl', 'sim_czas', 'opis',
                        'pola (HW|SIM)...'])
            w.writerows(rows)
        print('   CSV: %s' % a.csv)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
