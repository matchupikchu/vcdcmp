# vcdcmp

Narzędzia do weryfikacji modułów FPGA (np. bloków arytmetycznych):

- **`vcdcmp.py`** – porównuje **transakcje na interfejsach modułów** zapisane w VCD
  z symulacji i z **SignalTap** (Intel Quartus). Sprawdza, czy w sprzęcie zaszły
  te same operacje (operandy, wyniki) co w symulacji.
- **`hdlcov.py`** – mierzy **pokrycie kodu RTL** w testach: VHDL przez
  [NVC](https://github.com/nickg/nvc), Verilog przez
  [Covered](https://covered.sourceforge.net/). Raportuje podsumowanie
  i listę nieprzetestowanych linii RTL.

Wymagany Python ≥ 3.8; skrypty nie mają zależności poza biblioteką standardową.

---

## vcdcmp – symulacja vs SignalTap

### Idea

Transakcja to cykl, w którym `valid == 1` (oraz `ready == 1`, jeśli podano `ready`).
Z każdej transakcji zapisywane są wartości pól (np. `a`, `b` na wejściu, `p` na wyjściu).
Sekwencje transakcji z symulacji i z SignalTap są wyrównywane i porównywane, więc:

- nie ma znaczenia, w którym miejscu symulacji SignalTap złapał okno,
- nazwy sygnałów nie muszą być identyczne (`tb.dut.u_mul.in_a` ↔ `top:u0|mul:u_mul|in_a`),
- szyny rozbite przez SignalTap na bity (`p[0]` … `p[15]`) są składane z powrotem,
- porównywane są wartości liczbowe (z obsługą liczb ze znakiem i stałoprzecinkowych).

Raport rozróżnia transakcje o **różnych wartościach** oraz transakcje występujące
**tylko w STP** / **tylko w SIM** (zgubione / nadmiarowe). Podsumowanie podaje interfejsy
uporządkowane wg czasu pierwszej rozbieżności – w łańcuchu modułów najwcześniejsza
wskazuje podejrzany moduł (wejścia zgodne, wyjście nie → błąd w module).

### Użycie

```bash
# lista sygnałów w pliku (pomaga napisać konfigurację)
python3 vcdcmp.py --list stp.vcd

# porównanie: konfiguracja + VCD z symulacji + nagrania SignalTap
python3 vcdcmp.py config.json sim.vcd stp.vcd
python3 vcdcmp.py config.json sim.vcd katalog_z_nagraniami/ --merge-out scalony.vcd --csv roznice.csv

# bez pliku konfiguracji – interfejsy podane prefiksem
python3 vcdcmp.py sim.vcd stp/ --clk tb.clk --signed --itf mul_in=u_mul.in_ --itf mul_out=u_mul.out_
```

| Opcja | Opis |
|---|---|
| `--list` | wypisz sygnały z plików VCD |
| `--itf NAZWA=PREFIKS[,PREFIKS_STP]` | interfejs z prefiksu (można powtarzać) |
| `--clk NAZWA` | zegar w symulacji (zamiast `sim_clock`) |
| `--signed` | pola interfejsów z `--itf` są liczbami ze znakiem |
| `--edge rise\|fall` | zbocze zegara symulacji |
| `--stp-period N` | okres próbkowania STP (jednostki timescale pliku); domyślnie wykrywany |
| `--merge-out PLIK` | zapisz wszystkie nagrania STP w jednym VCD |
| `--csv PLIK` | wszystkie rozbieżności do CSV |
| `--max N` | ile rozbieżności wypisać na interfejs (domyślnie 20) |

Kod wyjścia: `0` – wszystko zgodne, `1` – są rozbieżności, `2` – błąd (np. brak sygnału).

### Wiele nagrań SignalTap

Zamiast jednego pliku można podać kilka plików i/lub katalogów (brane są wszystkie `*.vcd`,
sortowane naturalnie: `capture_2` przed `capture_10`). Każde nagranie jest wyrównywane do
symulacji **osobno** (może pochodzić z innego miejsca przebiegu albo innego uruchomienia),
a wyniki są łączone w jeden raport. `--merge-out` zapisuje nagrania jedno za drugim w jednym
pliku VCD z dodatkowym sygnałem `vcdcmp_segment` = numer nagrania (wygodne w GTKWave).

### Konfiguracja

Nazwy sygnałów dopasowywane są **po końcówce ścieżki** – `u_mul.in_a` pasuje do
`tb.dut.u_mul.in_a` i do `top_inst:u0|mul:u_mul|in_a`. Gdy końcówka pasuje do kilku
sygnałów, narzędzie wypisze kandydatów – wtedy podaj dłuższą ścieżkę.

**Interfejs z prefiksem** (najprościej) – narzędzie samo znajdzie `valid`
(`valid`, `vld`, `tvalid`, `stb`, `en`, `start`, `done`…), opcjonalnie `ready`
(`ready`, `rdy`, `tready`), a pozostałe sygnały z prefiksem potraktuje jako pola.
Zegary i resety (`clk*`, `rst*`, …) są pomijane. Brane są tylko sygnały obecne
zarówno w symulacji, jak i we wszystkich nagraniach STP.

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

Klucze w trybie prefiksu:

| Klucz | Opis |
|---|---|
| `prefix` | prefiks nazw (`"u_mul.in_"`), albo `{"sim": "...", "stp": "..."}` gdy różne |
| `valid`, `ready` | końcówka po prefiksie (`"vld"`) albo pełna nazwa z innego zakresu (`"u_mul.out_valid"`) |
| `fields` | opcjonalnie: lista końcówek (`["a", "b"]`) albo `{"a": {"signed": true, "frac": 4}}` |
| `exclude` | wzorce glob końcówek do pominięcia, np. `["dbg_*"]` |
| `signed`, `frac` | domyślne dla wszystkich pól; `frac` = bity ułamkowe (Q-format) |

**Interfejs jawny** – każdy sygnał podany ręcznie:

```json
{
  "name": "mul_out",
  "valid": "u_mul.out_valid",
  "ready": "u_mul.out_ready",
  "fields": {
    "p": { "signal": "u_mul.out_p", "signed": true, "frac": 8 },
    "q": { "sim": "u_mul.q_reg", "stp": "u_mul.q" }
  }
}
```

Klucze globalne: `sim_clock` (wymagany, albo `--clk`), `stp_clock` (jeśli nagranie
zawiera zegar – zwykle nie), `stp_period`.

### Uwagi

- Symulacja próbkowana jest tuż przed zboczem zegara (wartość widziana przez przerzutniki),
  SignalTap – w swoich chwilach próbkowania.
- Wartości z X/Z w symulacji porównywane są jako surowe bity.
- Przy danych okresowych (np. same zera) wyrównanie może być niejednoznaczne – raport to zaznacza.

---

## hdlcov – pokrycie kodu RTL

```bash
# VHDL (NVC): symulacja testbencha z pomiarem pokrycia tylko dla instancji DUT
python3 hdlcov.py vhdl -t tb_mac --dut tb_mac.dut rtl/mac.vhd tb/tb_mac.vhd

# Verilog (Icarus Verilog + Covered)
python3 hdlcov.py verilog -t tb_mac --dut tb_mac.dut --dut-module mac rtl/mac.v tb/tb_mac.v

# Verilog z gotowym VCD (np. z innego symulatora)
python3 hdlcov.py verilog -t tb_mac --dut tb_mac.dut --dut-module mac --vcd sim.vcd rtl/mac.v

# raport z istniejącej bazy NVC (np. z przebiegu cocotb z SIM=nvc i --cover)
python3 hdlcov.py nvc-db sim_build/cov.ncdb
```

Wspólne opcje: `-o KATALOG` (domyślnie `hdlcov_out/`), `--rtl WZORZEC` (które pliki to RTL;
domyślnie wszystko poza `tb_*`, `*_tb.*`, `test_*`, katalogami `tb/`, `test/`, `sim/`),
`--min PROCENT` (kod wyjścia 1, gdy pokrycie linii RTL jest niższe – do CI).
VHDL: `--std`, `-g NAZWA=WARTOŚĆ`, `--stop-time`. Verilog: `-D MAKRO`, `--vcd`.

Wynik (w katalogu `-o`):

- `coverage.md` – podsumowanie, pokrycie per plik, tabela nieprzetestowanych linii z kodem,
- `coverage.json` – to samo do dalszego przetwarzania,
- NVC: `html/index.html` (szczegółowy raport), `cobertura.xml` (np. do GitLab/Jenkins),
- Covered: `summary.txt`, `details.txt` (pełne raporty), `cov.cdd`,
- `hdlcov.log` – wszystkie wywołane polecenia i ich wyjście.

Rodzaje wpisów w liście nieprzetestowanych linii: `nie wykonana` (instrukcja nigdy nie
wykonana), `gałąź N%` (warunek nie przyjął wszystkich wartości), `toggle` (VHDL: nie
wszystkie bity portu/sygnału zmieniły stan 0→1→0).

### Instalacja narzędzi

Ubuntu/Debian:

```bash
sudo apt install covered iverilog
# NVC: pakiet .deb z https://github.com/nickg/nvc/releases
sudo apt install ./nvc_*_amd64_ubuntu-24.04.deb
```

Bez uprawnień administratora można rozpakować pakiety lokalnie (`apt-get download …`,
`dpkg -x pakiet.deb ~/tools/…`) i wskazać pliki wykonywalne zmiennymi
`HDLCOV_NVC`, `HDLCOV_COVERED`, `HDLCOV_IVERILOG`, `HDLCOV_VVP`. `hdlcov.py` szuka narzędzi
kolejno: w tych zmiennych, w `PATH` i w `~/tools/bin`.

Covered nie parsuje bloków `$date`/`$version`/`$comment` w VCD – `hdlcov.py` usuwa je
automatycznie przed analizą.

---

## Przykład

```bash
cd example
python3 gen_test.py .                                      # sim.vcd + stp/capture_{1,2,3}.vcd
python3 ../vcdcmp.py config.json sim.vcd stp/              # wykryje 1 wstrzyknięty błąd w mul_out
python3 ../vcdcmp.py config_prefix.json sim.vcd stp/       # to samo, interfejsy z prefiksów

cd coverage
python3 ../../hdlcov.py vhdl -t tb_mac --dut tb_mac.dut vhdl/mac.vhd vhdl/tb_mac.vhd
python3 ../../hdlcov.py verilog -t tb_mac --dut tb_mac.dut --dut-module mac verilog/mac.v verilog/tb_mac.v
```

W przykładzie pokrycia testbench celowo nie sprawdza nasycenia akumulatora – raport
pokazuje te linie jako nieprzetestowane.
