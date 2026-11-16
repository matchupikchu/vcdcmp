# vcdcmp

Vendor-independent tools for verifying FPGA modules (e.g. arithmetic blocks):

- **`vcdcmp.py`** – compares **transactions on module interfaces** recorded in
  simulation and in hardware by an **embedded logic analyzer** from any vendor.
  It checks that the device performed the same operations (operands, results) as the simulation.
- **`hdlcov.py`** – measures **RTL code coverage** of your tests (VHDL and Verilog, open-source
  tools) and reports untested lines.

Requires Python ≥ 3.8; the scripts have no dependencies beyond the standard library.

> Note: the command-line output and help messages are currently in Polish.

---

## vcdcmp – simulation vs hardware

### Supported sources

The tool is vendor independent. It reads the formats that embedded logic analyzers export to:

| Format | Example sources |
|---|---|
| **VCD** (Value Change Dump) | Intel SignalTap, AMD Vivado ILA (`write_hw_ila_data -vcd_file`), Lattice Reveal, Microchip Identify, simulators (Questa, Riviera, Xcelium, VCS, Icarus, Verilator, GHDL, NVC) |
| **CSV** (one row = one sample) | AMD Vivado ILA (`write_hw_ila_data -csv_file`), Intel SignalTap, other analyzers with a tabular export |

Naming conventions of different tools are normalized, so all of these refer to the same signal:
`tb.dut.u_mul.in_a` (simulation), `top:u0|mul:u_mul|in_a` (Intel), `u_top/u_mul/in_a[7:0]` (AMD).
Bus ranges may be written as `[7:0]`, `[7..0]` or `[3]`. Buses split into individual bits during
export (`p[0]` … `p[15]`) are reassembled.

### Concept

A transaction is a cycle in which `valid == 1` (and `ready == 1`, if `ready` is given).
The field values (e.g. `a`, `b` on the input, `p` on the output) are recorded for every transaction.
Transaction sequences from the simulation and from the hardware (**HW**) are aligned and compared, so:

- it does not matter where in the simulation the analyzer captured its window,
- signal names do not have to be identical (a common path suffix is enough),
- numeric values are compared (signed and fixed-point numbers are supported),
  so different widths or notations (HEX/BIN/DEC) are not a problem.

The report lists these kinds of findings:

| Kind | Meaning |
|---|---|
| `różnica` (difference) | the same transaction has different values in HW and SIM |
| `tylko HW` / `tylko SIM` (only in HW / SIM) | a transaction is lost or extra |
| `pakiet` (packet) | the end-of-packet marker (`tlast`, `endofpacket`) is on a different word in HW than in SIM – too early or too late, and by how many words |
| `protokół` (protocol) | a protocol rule is broken in HW or in SIM, e.g. `wlast`/`rlast` not on the last beat of a burst, `startofpacket` before the previous `endofpacket` |

The summary lists interfaces ordered by the time of their first mismatch. In a chain of modules,
the earliest one points to the suspect module (inputs match, output does not → bug in that module).

### Usage

```bash
# list signals in a file (helps write the configuration)
python3 vcdcmp.py --list capture.vcd capture.csv

# compare: configuration + simulation + analyzer captures
python3 vcdcmp.py config.json sim.vcd hw.vcd
python3 vcdcmp.py config.json sim.vcd captures_dir/ --merge-out merged.vcd --csv diffs.csv

# without a configuration file – interfaces given by prefix
python3 vcdcmp.py sim.vcd hw/ --clk tb.clk --signed --itf mul_in=u_mul.in_ --itf mul_out=u_mul.out_
```

| Option | Description |
|---|---|
| `--list` | list signals in VCD/CSV files |
| `--itf NAME[:PROTOCOL]=PREFIX[,HW_PREFIX]` | interface defined by a prefix, optionally with a bus protocol, e.g. `--itf m0:axi4=u_dut.m_axi_` (repeatable) |
| `--clk NAME` | simulation clock (instead of `sim_clock`) |
| `--signed` | fields of `--itf` interfaces are signed numbers |
| `--edge rise\|fall` | simulation clock edge |
| `--hw-period N` | sampling period of VCD captures (in their timescale units); auto-detected by default |
| `--radix HEX\|BIN\|DEC\|SIGNED\|OCT` | value format in CSV files without a `Radix` row (default HEX) |
| `--merge-out FILE` | write all HW captures into one VCD file |
| `--csv FILE` | write all mismatches to a CSV file |
| `--max N` | how many mismatches to print per interface (default 20) |

Exit code: `0` – everything matches, `1` – mismatches found, `2` – error (e.g. signal not found).
This makes it easy to use in scripts and CI.

### Multiple captures

Instead of a single file you can pass several files and/or directories. All `*.vcd` and `*.csv`
files are used, in natural sort order (`capture_2` before `capture_10`), and formats can be mixed.
Each capture is aligned to the simulation **separately**, because it may come from a different
part of the run or from a different run altogether. Results are combined into one report.

`--merge-out` writes the captures one after another into one VCD file with an extra
`vcdcmp_segment` signal (capture number). Use it to browse captures in any waveform viewer
(e.g. GTKWave, Surfer). CSV captures have no time axis and are written as 1 sample = 1 ns.

### CSV format

- first row: signal names (paths separated by `/`, `|` or `.`, optionally with a bit range),
- optional second row `Radix - HEX,BINARY,UNSIGNED,SIGNED,...` (as in AMD Vivado),
- index/time columns (`Sample in Buffer`, `Sample in Window`, `Time`, `TRIGGER`, …) are skipped,
- values: according to the `Radix` row or `--radix`. The notations `0x..`, `0b..`, `..h`
  and Verilog style (`16'h00ff`) are also recognized. `X`/`Z` are allowed,
- the delimiter (`,`, `;` or tab) is detected automatically.

### Configuration

Names are matched **by path suffix**. If a suffix matches several signals, the tool lists
the candidates; give a longer path in that case.

**Interface by prefix** (simplest). The tool finds `valid` by itself
(`valid`, `vld`, `tvalid`, `stb`, `en`, `start`, `done`…), optionally `ready`
(`ready`, `rdy`, `tready`), and treats the remaining signals with the prefix as fields.
Clocks and resets (`clk*`, `rst*`, …) are skipped. Only signals present both in the simulation
and in all HW captures are used.

```json
{
  "sim_clock": "tb.clk",
  "interfaces": [
    { "name": "mul_in",  "prefix": "u_mul.in_",  "signed": true },
    { "name": "mul_out", "prefix": "u_mul.out_", "signed": true },
    { "name": "acc_out", "prefix": "u_acc.", "valid": "u_mul.out_valid" }
  ]
}
```

| Key | Description |
|---|---|
| `prefix` | name prefix (`"u_mul.in_"`), or `{"sim": "...", "hw": "..."}` when they differ |
| `valid`, `ready` | suffix after the prefix (`"vld"`) or a full name from another scope (`"u_mul.out_valid"`) |
| `fields` | optional: list of suffixes (`["a", "b"]`) or `{"a": {"signed": true, "frac": 4}}` |
| `exclude` | glob patterns of suffixes to skip, e.g. `["dbg_*"]` |
| `signed`, `frac` | defaults for all fields; `frac` = number of fractional bits (Q format) |

**Explicit interface** – every signal given by hand:

```json
{
  "name": "mul_out",
  "valid": "u_mul.out_valid",
  "ready": "u_mul.out_ready",
  "fields": {
    "p": { "signal": "u_mul.out_p", "signed": true, "frac": 8 },
    "q": { "sim": "u_mul.q_reg", "hw": "u_mul.q" }
  }
}
```

More keys for an interface (explicit or by prefix):

| Key | Description |
|---|---|
| `ready_latency` | data is transferred when `valid == 1` and `ready` was 1 *N* cycles earlier (Avalon-ST `readyLatency`); default 0 |
| `last`, `sop` | end / start of packet signal – enables packet boundary checks (with `sop`: also start/end pairing) |
| `by_id` | name of a field (e.g. `"tid"`) – transactions are compared separately for each value of it |

More keys for a field:

| Key | Description |
|---|---|
| `byteenable` | signal (or list of signals) with one bit per byte lane – disabled bytes are not compared |
| `empty` | `{"signal": ..., "eop": ..., "symbol_bits": 8, "high_first": true}` – Avalon-ST style: on the end-of-packet word the last `empty` symbols are not compared |
| `when` | signal – the field is compared only in words where it is 1 |

**Inverted signals:** prefix any signal name with `!` (or `~`), e.g. `"ready": "!u_dut.avs_waitrequest"`.

Global keys: `sim_clock` (required, or `--clk`), `hw_clock` (if the capture contains a clock –
usually it does not), `hw_period`. The legacy names `stp`, `stp_clock` and `stp_period` are still accepted.

### Bus protocols

With `"protocol"` and `"prefix"` a single entry describes a whole standard interface. Signal
names are `<prefix><standard name>` (any case); optional signals that are missing on either side
are skipped and listed in the output.

```json
{
  "sim_clock": "tb.clk",
  "interfaces": [
    { "name": "axis", "protocol": "axi4-stream", "prefix": "u_dut.s_axis_" },
    { "name": "avst", "protocol": "avalon-st",   "prefix": "u_dut.asi_", "ready_latency": 1 },
    { "name": "axi",  "protocol": "axi4",        "prefix": "u_dut.s_axi_" },
    { "name": "avmm", "protocol": "avalon-mm",   "prefix": "u_dut.avs_" }
  ]
}
```

| Protocol | Transaction | Signals (required in bold) | Notes |
|---|---|---|---|
| `axi4-stream` | one word | **tvalid**, tready, **tdata**, tkeep, tstrb, tlast, tid, tdest, tuser | `tdata` compared only in bytes with `tkeep` (and `tstrb`) = 1; `tlast` checked for packet boundaries; `"by_id": true` compares per `tid` |
| `avalon-st` | one word | **valid**, ready, **data**, startofpacket, endofpacket, empty, channel, error | `ready_latency`; on the `endofpacket` word the `empty` symbols are not compared (`symbol_bits`, default 8; `first_symbol_in_high_order_bits`, default true); `startofpacket`/`endofpacket` pairing checked; `"by_id": true` compares per `channel` |
| `axi4` (`axi4-lite`) | whole write: AW + all W beats + B; whole read: AR + all R beats | **awvalid, awaddr, wvalid, wdata** / **arvalid, araddr, rvalid, rdata**; awready, awid, awlen, awsize, awburst, wready, wstrb, wlast, bvalid, bready, bid, bresp, arready, arid, arlen, arsize, arburst, rready, rid, rresp, rlast | creates two interfaces, `<name>.write` and `<name>.read`; write data compared only in bytes with `wstrb` = 1; read data assigned to requests by `rid`, so responses of different IDs may come in any order; `wlast`/`rlast` checked against `awlen`/`arlen`; `"by_id": true` compares each ID separately |
| `avalon-mm` | whole write or read burst | **address**, read, write, waitrequest, readdata, readdatavalid, writedata, byteenable, burstcount, response, writeresponsevalid, beginbursttransfer | command accepted when `waitrequest` = 0; bursts by `burstcount`; write data compared only in bytes with `byteenable` = 1, read data in bytes enabled by the command; without `readdatavalid` set `read_latency` |

How a capture window that starts in the middle of traffic is handled:

- AXI4: the data stream is cut into bursts at `wlast`/`rlast` and lined up with the
  `awlen`/`arlen` of the requests in the capture, so data of requests from before the window
  is skipped. Without `wlast`, W beats before the first AW in the capture are skipped (write data
  is then assumed not to precede its address). Responses (B, R) without a request in the capture
  are skipped.
- Avalon-MM: read data without a command in the capture is skipped. A write burst that is
  already in progress when the capture starts cannot always be recognized: the first burst is
  dropped when `write` was held high since the start of the capture. With `beginbursttransfer`
  bursts are always recognized.
- Streams: packet numbers in the report are counted from the start of each capture.

### Notes

- The simulation is sampled just before the clock edge (the value the flip-flops see),
  HW captures at their sampling points (VCD) or row by row (CSV).
- Values containing X/Z are compared as raw bits.
- With periodic data (e.g. all zeros) the alignment may be ambiguous – the report flags this.
- Native analyzer formats (e.g. binary project files) are not read – export them to VCD or CSV
  in the vendor's tool.

---

## hdlcov – RTL code coverage

Uses open-source tools that do not depend on the FPGA vendor:

| Language | Simulation | Coverage |
|---|---|---|
| VHDL | [NVC](https://github.com/nickg/nvc) | NVC (statements, branches, expressions, toggle) |
| Verilog | [Icarus Verilog](https://steveicarus.github.io/iverilog/) or an existing VCD from any simulator | [Covered](https://covered.sourceforge.net/) (lines, toggle, combinational logic, FSM) |

```bash
# VHDL: simulate the testbench, collecting coverage only for the DUT instance
python3 hdlcov.py vhdl -t tb_mac --dut tb_mac.dut rtl/mac.vhd tb/tb_mac.vhd

# Verilog (Icarus Verilog + Covered)
python3 hdlcov.py verilog -t tb_mac --dut tb_mac.dut --dut-module mac rtl/mac.v tb/tb_mac.v

# Verilog with an existing VCD (e.g. from another simulator)
python3 hdlcov.py verilog -t tb_mac --dut tb_mac.dut --dut-module mac --vcd sim.vcd rtl/mac.v

# report from an existing NVC database (e.g. from a cocotb run with SIM=nvc and --cover)
python3 hdlcov.py nvc-db sim_build/cov.ncdb
```

Common options:

- `-o DIR` – output directory (default `hdlcov_out/`),
- `--rtl PATTERN` – which files are RTL; by default everything except `tb_*`, `*_tb.*`, `test_*`
  and the `tb/`, `test/`, `sim/` directories,
- `--min PERCENT` – exit code 1 when RTL line coverage is below this value (for CI).

VHDL options: `--std`, `-g NAME=VALUE`, `--stop-time`. Verilog options: `-D MACRO`, `--vcd`.

Output (in the `-o` directory):

- `coverage.md` – summary, per-file coverage and a table of untested lines with their source,
- `coverage.json` – the same data for further processing,
- NVC: `html/index.html` (detailed report) and `cobertura.xml` (e.g. for GitLab/Jenkins),
- Covered: `summary.txt` and `details.txt` (full reports) plus `cov.cdd`,
- `hdlcov.log` – every command that was run, with its output.

Entry types in the untested-lines list:

- `not executed` – the statement never ran,
- `branch N%` – the condition did not take all of its values,
- `toggle` – (VHDL) not all bits of a port or signal toggled 0→1→0.

### Installing the tools

Debian/Ubuntu:

```bash
sudo apt install covered iverilog
# NVC: package from https://github.com/nickg/nvc/releases (Windows and macOS builds are also available)
sudo apt install ./nvc_*_amd64_ubuntu-24.04.deb
```

Without administrator rights you can unpack the packages locally (`apt-get download …`,
`dpkg -x package.deb ~/tools/…`) and point to the executables with the environment variables
`HDLCOV_NVC`, `HDLCOV_COVERED`, `HDLCOV_IVERILOG` and `HDLCOV_VVP`. `hdlcov.py` looks for the tools
in these variables first, then in `PATH`, then in `~/tools/bin`.

Covered cannot handle `$date`/`$version`/`$comment` blocks in VCD files, so `hdlcov.py`
strips them automatically before scoring.

---

## Example

```bash
cd example
python3 gen_test.py .            # sim.vcd + hw/capture_1.vcd, capture_2.vcd (Intel style), capture_3.csv (AMD style)
python3 ../vcdcmp.py config.json sim.vcd hw/          # finds the 1 injected bug in mul_out
python3 ../vcdcmp.py config_prefix.json sim.vcd hw/   # same, interfaces defined by prefix

cd bus                           # AXI4-Stream, Avalon-ST (readyLatency 1), AXI4, Avalon-MM
python3 gen_bus.py .             # HW run: other stalls, reordered AXI read responses, garbage in invalid bytes
python3 ../../vcdcmp.py config.json sim.vcd hw/          # finds exactly the 6 injected bugs
python3 ../../vcdcmp.py config_generic.json sim.vcd hw/  # the same without protocol templates ("!waitrequest", byteenable)
cd ..

cd coverage
python3 ../../hdlcov.py vhdl -t tb_mac --dut tb_mac.dut vhdl/mac.vhd vhdl/tb_mac.vhd
python3 ../../hdlcov.py verilog -t tb_mac --dut tb_mac.dut --dut-module mac verilog/mac.v verilog/tb_mac.v
```

In the coverage example the testbench deliberately does not exercise accumulator saturation,
so the report shows those lines as untested.

---

## License

[MIT](LICENSE) © 2026 matchupikchu <matchupikchu99@gmail.com>

The external tools used by `hdlcov.py` (NVC, Covered, Icarus Verilog) are separate programs
under their own licenses; they are not part of this repository.
