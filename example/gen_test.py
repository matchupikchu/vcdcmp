#!/usr/bin/env python3
"""Generates an example sim.vcd and an hw/ directory with several captures from
an embedded FPGA logic analyzer: multiplier in_a*in_b -> out_p (latency 2) and
an accumulator. Captures come in different formats (Intel-style VCD, AMD
Vivado ILA-style CSV), have no clock, use different names than the simulation
and contain one deliberately wrong result."""
import os
import random
import sys

random.seed(1)
N, LAT = 1000, 2
WINDOWS = [(50, 64), (300, 128), (700, 100)]   # (start cycle, number of samples) - separate HW captures
BAD = 357


def b(v, w):
    return format(v & ((1 << w) - 1), '0%db' % w)


# state in consecutive cycles (values after the clock edge)
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

# ---- simulation: 10 ns clock, 1 ps timescale
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
        f.write('#%d\n1!\n' % t)                      # rising edge
        f.write('#%d\n%d"\nb%s #\nb%s $\n%d%%\nb%s &\nb%s \'\n' % (
            t + 100, s['in_valid'], b(s['a'], 8), b(s['b'], 8), s['out_valid'],
            b(s['p'], 16), b(s['acc'], 20)))
        f.write('#%d\n0!\n' % (t + 5000))
    f.write('#%d\n' % (N * 10000))

# ---- logic analyzer captures (HW): sample k = value seen at the clock edge,
#      i.e. the state from the previous cycle. Two formats, as with different vendors:
#      VCD with Intel-style names (entity:instance|signal, out_p bus split into bits)
#      and AMD Vivado ILA-style CSV (paths with '/', "Radix" row, HEX values).
os.makedirs(out + '/hw', exist_ok=True)


def hw_samples(start, length):
    for k in range(length):
        s = dict(cyc[start + k - 1])
        if start + k - 1 == BAD and s['out_valid']:
            s['p'] ^= 0x40                                   # "hardware" bug
        yield k, s


def write_vcd(path, start, length):
    with open(path, 'w') as f:
        f.write('$date today $end\n$version logic analyzer export $end\n$timescale 1ps $end\n'
                '$scope module top $end\n'
                '$var wire 1 A top_inst:u0|mul:u_mul|in_valid $end\n'
                '$var wire 8 B top_inst:u0|mul:u_mul|in_a [7:0] $end\n'
                '$var wire 8 C top_inst:u0|mul:u_mul|in_b [7:0] $end\n'
                '$var wire 1 D top_inst:u0|mul:u_mul|out_valid $end\n')
        for i in range(16):
            f.write('$var wire 1 P%d top_inst:u0|mul:u_mul|out_p[%d] $end\n' % (i, i))
        f.write('$var wire 20 E top_inst:u0|acc:u_acc|acc [19:0] $end\n$upscope $end\n$enddefinitions $end\n')
        for k, s in hw_samples(start, length):
            f.write('#%d\n%dA\nb%s B\nb%s C\n%dD\n' % (k * 20000, s['in_valid'], b(s['a'], 8), b(s['b'], 8),
                                                     s['out_valid']))
            for i, ch in enumerate(reversed(b(s['p'], 16))):
                f.write('%sP%d\n' % (ch, i))
            f.write('b%s E\n' % b(s['acc'], 20))


def write_csv(path, start, length):
    cols = ['u_top/u_mul/in_valid', 'u_top/u_mul/in_a[7:0]', 'u_top/u_mul/in_b[7:0]',
            'u_top/u_mul/out_valid', 'u_top/u_mul/out_p[15:0]', 'u_top/u_acc/acc[19:0]']
    with open(path, 'w') as f:
        f.write('Sample in Buffer,Sample in Window,TRIGGER,' + ','.join(cols) + '\n')
        f.write('Radix - UNSIGNED,UNSIGNED,UNSIGNED,BINARY,HEX,HEX,BINARY,HEX,HEX\n')
        for k, s in hw_samples(start, length):
            f.write('%d,%d,%d,%d,%02X,%02X,%d,%04X,%05X\n' % (
                k, k, int(k == 0), s['in_valid'], s['a'] & 0xFF, s['b'] & 0xFF, s['out_valid'],
                s['p'] & 0xFFFF, s['acc']))


for n, (start, length) in enumerate(WINDOWS, 1):
    if n == len(WINDOWS):
        write_csv(out + '/hw/capture_%d.csv' % n, start, length)
    else:
        write_vcd(out + '/hw/capture_%d.vcd' % n, start, length)
print('cykl z błędem:', BAD, 'out_valid =', cyc[BAD]['out_valid'])
