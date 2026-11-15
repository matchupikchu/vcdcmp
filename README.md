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

The report distinguishes transactions with **different values** from transactions that appear
**only in HW** / **only in SIM** (lost or extra). The summary lists interfaces ordered by the time
of their first mismatch. In a chain of modules, the earliest one points to the suspect module
(inputs match, output does not → bug in that module).

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
| `--itf NAME=PREFIX[,HW_PREFIX]` | interface defined by a prefix (repeatable) |
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

Global keys: `sim_clock` (required, or `--clk`), `hw_clock` (if the capture contains a clock –
usually it does not), `hw_period`. The legacy names `stp`, `stp_clock` and `stp_period` are still accepted.

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

- `nie wykonana` (not executed) – the statement never ran,
- `gałąź N%` (branch N%) – the condition did not take all of its values,
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

cd coverage
python3 ../../hdlcov.py vhdl -t tb_mac --dut tb_mac.dut vhdl/mac.vhd vhdl/tb_mac.vhd
python3 ../../hdlcov.py verilog -t tb_mac --dut tb_mac.dut --dut-module mac verilog/mac.v verilog/tb_mac.v
```

In the coverage example the testbench deliberately does not exercise accumulator saturation,
so the report shows those lines as untested.
