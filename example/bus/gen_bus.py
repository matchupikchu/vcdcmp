#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 matchupikchu <matchupikchu99@gmail.com>
"""Generates sim.vcd and hw/capture_{1,2}.vcd with four bus interfaces of one DUT:
AXI4-Stream, Avalon-ST (readyLatency = 1), AXI4 (memory mapped) and Avalon-MM.

The "hardware" run carries the same transactions with different handshake timing
(other stalls, AXI read responses of different IDs returned in another order) and
random garbage in byte lanes that are not valid (tkeep, empty, wstrb, byteenable).
None of that may be reported. It also carries deliberate bugs that must be reported:
  - AXI4-Stream: tlast one word too early in one packet, one wrong data byte,
  - Avalon-ST:   endofpacket one word too late in one packet,
  - AXI4:        wlast one word too early in one write burst, one wrong read data word,
  - Avalon-MM:   one wrong read data word.
"""
import os
import random
import sys
from collections import deque

N = 900                       # simulated cycles
GAP = (430, 480)              # no new transfers start here (HW capture 2 starts in this gap)
WINDOWS = [(5, 420), (440, 890)]

rnd = random.Random(7)        # transaction contents - identical in SIM and HW


def allowed(c):
    return not GAP[0] <= c < GAP[1]


AXIS_PKTS = []
for _ in range(110):
    n = rnd.randint(1, 6)
    AXIS_PKTS.append([(rnd.getrandbits(32), 0xF if k < n - 1 else rnd.choice((0x1, 0x3, 0x7, 0xF)))
                      for k in range(n)])
AVST_PKTS = [[rnd.getrandbits(32) for _ in range(rnd.randint(1, 5))] for _ in range(95)]
AVST_EMPTY = [rnd.randint(0, 3) for _ in AVST_PKTS]
AXI_WR = [dict(id=rnd.randint(0, 1), addr=rnd.randrange(0, 1 << 16, 4), len=rnd.randint(0, 3)) for _ in range(110)]
for w in AXI_WR:
    w['data'] = [rnd.getrandbits(32) for _ in range(w['len'] + 1)]
    w['strb'] = [rnd.choice((0xF, 0xF, 0x3, 0xC, 0x1)) for _ in range(w['len'] + 1)]
AXI_RD = [dict(id=rnd.randint(0, 1), addr=rnd.randrange(0, 1 << 16, 4), len=rnd.randint(0, 3)) for _ in range(110)]
AVMM_OPS = []
for _ in range(150):
    if rnd.random() < 0.5:
        n = rnd.randint(1, 4)
        AVMM_OPS.append(dict(kind='W', addr=rnd.randrange(0, 1 << 16, 4), n=n,
                             data=[rnd.getrandbits(32) for _ in range(n)],
                             be=[rnd.choice((0xF, 0xF, 0x3, 0xC, 0x1)) for _ in range(n)]))
    else:
        AVMM_OPS.append(dict(kind='R', addr=rnd.randrange(0, 1 << 16, 4), n=rnd.randint(1, 2)))

# deliberate HW bugs
AXIS_EARLY = next(i for i, p in enumerate(AXIS_PKTS) if i >= 7 and len(p) >= 3)
AXIS_BAD = 80
AVST_LATE = next(i for i, p in enumerate(AVST_PKTS) if i >= 10 and len(p) >= 2)
AXI_WLAST = next(i for i, w in enumerate(AXI_WR) if i >= 5 and w['len'] >= 2)
AXI_RBAD = 100
AVMM_RBAD = next(i for i, o in enumerate(AVMM_OPS) if i >= 110 and o['kind'] == 'R')


def mem(addr, beat):
    return (addr * 2654435761 + beat * 97) & 0xFFFFFFFF


def garbage(value, enable, g):
    """Random bytes in the lanes that are not enabled (bit k of enable = byte k)."""
    for k in range(4):
        if not enable >> k & 1:
            value = value & ~(0xFF << 8 * k) | g.getrandbits(8) << 8 * k
    return value


def run(hw):
    t = random.Random(2 if hw else 1)       # handshake timing
    g = random.Random(20 if hw else 10)     # garbage in invalid byte lanes
    cyc = []
    # AXI4-Stream
    ax_p = ax_b = 0
    ax_cur = None
    # Avalon-ST, readyLatency = 1
    av_p = av_b = 0
    av_ready = [1 if t.random() < 0.75 else 0 for _ in range(N)]
    # AXI4
    wr_i, aw_cur, w_q, w_cur, b_q = 0, None, deque(), None, deque()
    rd_i, ar_cur, outst, r_cur = 0, None, [], None
    # Avalon-MM
    mm_i, mm_beat, mm_active, rdq = 0, 0, False, deque()
    mm_garb = {}
    for c in range(N):
        s = {}
        # ---- AXI4-Stream
        s['s_axis_tready'] = tready = 1 if t.random() < 0.8 else 0
        if ax_cur is None and ax_p < len(AXIS_PKTS) and (ax_b > 0 or allowed(c)) and t.random() < 0.7:
            pkt = AXIS_PKTS[ax_p]
            data, keep = pkt[ax_b]
            last = ax_b == len(pkt) - (2 if hw and ax_p == AXIS_EARLY else 1)
            if hw and ax_p == AXIS_BAD and ax_b == 0:
                data ^= 0xFF
            ax_cur = (garbage(data, keep, g), keep, int(last))
        if ax_cur:
            s.update(s_axis_tvalid=1, s_axis_tdata=ax_cur[0], s_axis_tkeep=ax_cur[1], s_axis_tlast=ax_cur[2])
            if tready:
                ax_cur, ax_b = None, ax_b + 1
                if ax_b == len(AXIS_PKTS[ax_p]):
                    ax_p, ax_b = ax_p + 1, 0
        else:
            s.update(s_axis_tvalid=0, s_axis_tdata=0, s_axis_tkeep=0, s_axis_tlast=0)
        # ---- Avalon-ST (valid only when ready was 1 one cycle earlier)
        s['asi_ready'] = av_ready[c]
        if (c > 0 and av_ready[c - 1] and av_p < len(AVST_PKTS) and (av_b > 0 or allowed(c))
                and t.random() < 0.8):
            pkt = AVST_PKTS[av_p]
            eop = av_b == len(pkt) - 1
            if hw and av_p == AVST_LATE and eop:
                eop = False
            if hw and av_p == AVST_LATE + 1 and av_b == 0:
                eop = True                                   # end of the previous packet, one word late
            empty = AVST_EMPTY[av_p] if av_b == len(pkt) - 1 else 0
            data = garbage(pkt[av_b], 0xF << empty & 0xF, g) if av_b == len(pkt) - 1 else pkt[av_b]
            s.update(asi_valid=1, asi_data=data, asi_startofpacket=int(av_b == 0),
                     asi_endofpacket=int(eop), asi_empty=empty if eop else 0)
            av_b += 1
            if av_b == len(pkt):
                av_p, av_b = av_p + 1, 0
        else:
            s.update(asi_valid=0, asi_data=0, asi_startofpacket=0, asi_endofpacket=0, asi_empty=0)
        # ---- AXI4 write: AW, then W beats, then B
        if aw_cur is None and wr_i < len(AXI_WR) and allowed(c) and t.random() < 0.25:
            aw_cur = AXI_WR[wr_i]
        s['s_axi_awready'] = awready = 1 if t.random() < 0.7 else 0
        if aw_cur:
            s.update(s_axi_awvalid=1, s_axi_awid=aw_cur['id'], s_axi_awaddr=aw_cur['addr'],
                     s_axi_awlen=aw_cur['len'], s_axi_awsize=2, s_axi_awburst=1)
            if awready:
                w_q.append([wr_i, 0])
                aw_cur, wr_i = None, wr_i + 1
        else:
            s.update(s_axi_awvalid=0, s_axi_awid=0, s_axi_awaddr=0, s_axi_awlen=0, s_axi_awsize=0, s_axi_awburst=0)
        if w_cur is None and w_q and t.random() < 0.8:
            k, beat = w_q[0]
            w = AXI_WR[k]
            last = beat == w['len'] - (1 if hw and k == AXI_WLAST else 0)
            w_cur = (garbage(w['data'][beat], w['strb'][beat], g), w['strb'][beat], int(last))
        s['s_axi_wready'] = wready = 1 if t.random() < 0.8 else 0
        if w_cur:
            s.update(s_axi_wvalid=1, s_axi_wdata=w_cur[0], s_axi_wstrb=w_cur[1], s_axi_wlast=w_cur[2])
            if wready:
                w_cur = None
                w_q[0][1] += 1
                k, beat = w_q[0]
                if beat == AXI_WR[k]['len'] + 1:
                    w_q.popleft()
                    b_q.append((c + 2, AXI_WR[k]['id']))
        else:
            s.update(s_axi_wvalid=0, s_axi_wdata=0, s_axi_wstrb=0, s_axi_wlast=0)
        s['s_axi_bready'] = 1
        if b_q and b_q[0][0] <= c:
            s.update(s_axi_bvalid=1, s_axi_bid=b_q[0][1], s_axi_bresp=0)
            b_q.popleft()
        else:
            s.update(s_axi_bvalid=0, s_axi_bid=0, s_axi_bresp=0)
        # ---- AXI4 read: AR, R bursts (HW returns ID 1 before ID 0 when both are pending)
        if ar_cur is None and rd_i < len(AXI_RD) and allowed(c) and t.random() < 0.25:
            ar_cur = AXI_RD[rd_i]
        s['s_axi_arready'] = arready = 1 if t.random() < 0.7 else 0
        if ar_cur:
            s.update(s_axi_arvalid=1, s_axi_arid=ar_cur['id'], s_axi_araddr=ar_cur['addr'],
                     s_axi_arlen=ar_cur['len'], s_axi_arsize=2, s_axi_arburst=1)
            if arready:
                outst.append([rd_i, 0, c + 3])
                ar_cur, rd_i = None, rd_i + 1
        else:
            s.update(s_axi_arvalid=0, s_axi_arid=0, s_axi_araddr=0, s_axi_arlen=0, s_axi_arsize=0, s_axi_arburst=0)
        if r_cur is None:
            ready_ = [o for o in outst if o[2] <= c]
            if hw:
                ready_ = [o for o in ready_ if AXI_RD[o[0]]['id'] == 1] or ready_
            r_cur = ready_[0] if ready_ else None
        s['s_axi_rready'] = rready = 1 if t.random() < 0.85 else 0
        if r_cur:
            rd = AXI_RD[r_cur[0]]
            data = mem(rd['addr'], r_cur[1]) ^ (1 if hw and r_cur[0] == AXI_RBAD and r_cur[1] == 0 else 0)
            s.update(s_axi_rvalid=1, s_axi_rid=rd['id'], s_axi_rdata=data, s_axi_rresp=0,
                     s_axi_rlast=int(r_cur[1] == rd['len']))
            if rready:
                r_cur[1] += 1
                if r_cur[1] == rd['len'] + 1:
                    outst.remove(r_cur)
                    r_cur = None
        else:
            s.update(s_axi_rvalid=0, s_axi_rid=0, s_axi_rdata=0, s_axi_rresp=0, s_axi_rlast=0)
        # ---- Avalon-MM (slave stalls with waitrequest, read data 2+ cycles later, in order)
        s['avs_waitrequest'] = wait = 1 if t.random() < 0.3 else 0
        s.update(avs_read=0, avs_write=0, avs_address=0, avs_burstcount=0, avs_writedata=0, avs_byteenable=0)
        if not mm_active and mm_i < len(AVMM_OPS):     # a started operation is held until accepted
            mm_active = allowed(c) and t.random() < 0.4
        if mm_active:
            o = AVMM_OPS[mm_i]
            s.update(avs_address=o['addr'], avs_burstcount=o['n'])
            if o['kind'] == 'W':
                gk = (mm_i, mm_beat)
                if gk not in mm_garb:
                    mm_garb[gk] = garbage(o['data'][mm_beat], o['be'][mm_beat], g)
                s.update(avs_write=1, avs_writedata=mm_garb[gk], avs_byteenable=o['be'][mm_beat])
                if not wait:
                    mm_beat += 1
                    if mm_beat == o['n']:
                        mm_i, mm_beat, mm_active = mm_i + 1, 0, False
            else:
                s.update(avs_read=1, avs_byteenable=0xF)
                if not wait:
                    for k in range(o['n']):
                        rdq.append((c + 2, mem(o['addr'], k) ^ (0x100 if hw and mm_i == AVMM_RBAD and k == 0 else 0)))
                    mm_i, mm_active = mm_i + 1, False
        if rdq and rdq[0][0] <= c:
            s.update(avs_readdatavalid=1, avs_readdata=rdq.popleft()[1])
        else:
            s.update(avs_readdatavalid=0, avs_readdata=0)
        cyc.append(s)
    return cyc


WIDTHS = dict(s_axis_tvalid=1, s_axis_tready=1, s_axis_tdata=32, s_axis_tkeep=4, s_axis_tlast=1,
              asi_valid=1, asi_ready=1, asi_data=32, asi_startofpacket=1, asi_endofpacket=1, asi_empty=2,
              s_axi_awvalid=1, s_axi_awready=1, s_axi_awid=2, s_axi_awaddr=16, s_axi_awlen=8, s_axi_awsize=3,
              s_axi_awburst=2, s_axi_wvalid=1, s_axi_wready=1, s_axi_wdata=32, s_axi_wstrb=4, s_axi_wlast=1,
              s_axi_bvalid=1, s_axi_bready=1, s_axi_bid=2, s_axi_bresp=2,
              s_axi_arvalid=1, s_axi_arready=1, s_axi_arid=2, s_axi_araddr=16, s_axi_arlen=8, s_axi_arsize=3,
              s_axi_arburst=2, s_axi_rvalid=1, s_axi_rready=1, s_axi_rid=2, s_axi_rdata=32, s_axi_rresp=2,
              s_axi_rlast=1,
              avs_address=16, avs_read=1, avs_write=1, avs_waitrequest=1, avs_readdata=32,
              avs_readdatavalid=1, avs_writedata=32, avs_byteenable=4, avs_burstcount=3)


def ident(n):
    chars = [chr(c) for c in range(33, 127)]
    s = ''
    while True:
        s, n = chars[n % 94] + s, n // 94 - 1
        if n < 0:
            return s


def val(v, w):
    return '%d' % v if w == 1 else 'b' + format(v, 'b') + ' '


def write_sim(path, cyc):
    ids = {n: ident(i + 1) for i, n in enumerate(WIDTHS)}
    with open(path, 'w') as f:
        f.write('$timescale 1ps $end\n$scope module tb $end\n$var wire 1 ! clk $end\n$scope module u_dut $end\n')
        for n, w in WIDTHS.items():
            f.write('$var wire %d %s %s%s $end\n' % (w, ids[n], n, ' [%d:0]' % (w - 1) if w > 1 else ''))
        f.write('$upscope $end\n$upscope $end\n$enddefinitions $end\n#0\n0!\n')
        prev = {}
        for c, s in enumerate(cyc):
            t = c * 10000
            f.write('#%d\n1!\n#%d\n' % (t, t + 100))
            for n, v in s.items():
                if prev.get(n) != v:
                    f.write('%s%s\n' % (val(v, WIDTHS[n]), ids[n]))
                    prev[n] = v
            f.write('#%d\n0!\n' % (t + 5000))
        f.write('#%d\n' % (N * 10000))


def write_hw(path, cyc, start, end):
    """Logic analyzer export: no clock, sample k = state seen at the clock edge."""
    ids = {n: ident(i) for i, n in enumerate(WIDTHS)}
    with open(path, 'w') as f:
        f.write('$timescale 1ps $end\n$scope module top $end\n')
        for n, w in WIDTHS.items():
            f.write('$var wire %d %s top_inst:u0|dut:u_dut|%s%s $end\n'
                    % (w, ids[n], n, ' [%d:0]' % (w - 1) if w > 1 else ''))
        f.write('$upscope $end\n$enddefinitions $end\n')
        prev = {}
        for k, c in enumerate(range(start, end)):
            f.write('#%d\n' % (k * 20000))
            for n, v in cyc[c - 1].items():
                if prev.get(n) != v:
                    f.write('%s%s\n' % (val(v, WIDTHS[n]), ids[n]))
                    prev[n] = v
        f.write('#%d\n' % ((end - start - 1) * 20000))


if __name__ == '__main__':
    out = sys.argv[1] if len(sys.argv) > 1 else '.'
    os.makedirs(os.path.join(out, 'hw'), exist_ok=True)
    write_sim(os.path.join(out, 'sim.vcd'), run(False))
    hw = run(True)
    for n, (a, b) in enumerate(WINDOWS, 1):
        write_hw(os.path.join(out, 'hw', 'capture_%d.vcd' % n), hw, a, b)
    print('wstrzyknięte błędy: AXIS tlast za wcześnie w pakiecie %d, zły bajt w pakiecie %d; '
          'Avalon-ST eop za późno w pakiecie %d; AXI wlast za wcześnie w zapisie %d, zły odczyt %d; '
          'Avalon-MM zły odczyt (operacja %d)' % (AXIS_EARLY, AXIS_BAD, AVST_LATE, AXI_WLAST, AXI_RBAD, AVMM_RBAD))
