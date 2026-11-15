#!/usr/bin/env python3
"""Generuje przykładowe sim.vcd i katalog stp/ z kilkoma nagraniami SignalTap:
mnożarka in_a*in_b -> out_p (latencja 2) i akumulator. W nagraniach: nazwy w stylu
Quartus, brak zegara, szyna out_p rozbita na bity, jeden celowo błędny wynik."""
import os
import random
import sys

random.seed(1)
N, LAT = 1000, 2
WINDOWS = [(50, 64), (300, 128), (700, 100)]   # (od cyklu, liczba próbek) - osobne nagrania
BAD = 357


def b(v, w):
    return format(v & ((1 << w) - 1), '0%db' % w)


# stan w kolejnych cyklach (wartości po zboczu)
cyc = []
pipe = [(0, 0, 0)] * LAT
acc = 0
for c in range(N):
    iv = 1 if random.random() < 0.6 else 0
    a, bb = random.randint(-128, 127), random.randint(-128, 127)
    ov, _, p = pipe[0]
    pipe = pipe[1:] + [(iv, 0, a * bb)]
    if ov:
        acc = (acc + p) & 0xFFFFF
    cyc.append(dict(in_valid=iv, a=a, b=bb, out_valid=ov, p=p, acc=acc))

out = sys.argv[1] if len(sys.argv) > 1 else '.'

# ---- symulacja: zegar 10 ns, timescale 1 ps
with open(out + '/sim.vcd', 'w') as f:
    f.write('$timescale 1ps $end\n$scope module tb $end\n$var wire 1 ! clk $end\n'
            '$scope module dut $end\n$scope module u_mul $end\n'
            '$var wire 1 " in_valid $end\n$var wire 8 # in_a [7:0] $end\n$var wire 8 $ in_b [7:0] $end\n'
            '$var wire 1 % out_valid $end\n$var wire 16 & out_p [15:0] $end\n$upscope $end\n'
            '$scope module u_acc $end\n$var wire 20 \' acc [19:0] $end\n$upscope $end\n'
            '$upscope $end\n$upscope $end\n$enddefinitions $end\n')
    f.write('#0\n0!\n0"\nbx #\nbx $\n0%\nb0 &\nb0 \'\n')
    for c, s in enumerate(cyc):
        t = c * 10000
        f.write('#%d\n1!\n' % t)                      # zbocze narastające
        f.write('#%d\n%d"\nb%s #\nb%s $\n%d%%\nb%s &\nb%s \'\n' % (
            t + 100, s['in_valid'], b(s['a'], 8), b(s['b'], 8), s['out_valid'],
            b(s['p'], 16), b(s['acc'], 20)))
        f.write('#%d\n0!\n' % (t + 5000))
    f.write('#%d\n' % (N * 10000))

# ---- SignalTap: próbka k = wartość widziana na zboczu (czyli stan z cyklu poprzedniego)
os.makedirs(out + '/stp', exist_ok=True)
for n, (STP_FROM, STP_LEN) in enumerate(WINDOWS, 1):
    with open(out + '/stp/capture_%d.vcd' % n, 'w') as f:
        f.write('$date today $end\n$version Quartus SignalTap $end\n$timescale 1ps $end\n'
                '$scope module top $end\n'
                '$var wire 1 A top_inst:u0|mul:u_mul|in_valid $end\n'
                '$var wire 8 B top_inst:u0|mul:u_mul|in_a [7:0] $end\n'
                '$var wire 8 C top_inst:u0|mul:u_mul|in_b [7:0] $end\n'
                '$var wire 1 D top_inst:u0|mul:u_mul|out_valid $end\n')
        for i in range(16):
            f.write('$var wire 1 P%d top_inst:u0|mul:u_mul|out_p[%d] $end\n' % (i, i))
        f.write('$var wire 20 E top_inst:u0|acc:u_acc|acc [19:0] $end\n$upscope $end\n$enddefinitions $end\n')
        for k in range(STP_LEN):
            s = dict(cyc[STP_FROM + k - 1])
            if STP_FROM + k - 1 == BAD and s['out_valid']:
                s['p'] ^= 0x40                                   # błąd "sprzętowy"
            f.write('#%d\n%dA\nb%s B\nb%s C\n%dD\n' % (k * 20000, s['in_valid'], b(s['a'], 8), b(s['b'], 8), s['out_valid']))
            for i, ch in enumerate(reversed(b(s['p'], 16))):
                f.write('%sP%d\n' % (ch, i))
            f.write('b%s E\n' % b(s['acc'], 20))
print('cykl z błędem:', BAD, 'out_valid =', cyc[BAD]['out_valid'])
