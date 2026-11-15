# vcdcmp

Niezależne od producenta narzędzia do weryfikacji modułów FPGA (np. bloków arytmetycznych):

- **`vcdcmp.py`** – porównuje **transakcje na interfejsach modułów** zarejestrowane
  w symulacji i w sprzęcie przez **wbudowany analizator logiczny** dowolnego producenta.
  Sprawdza, czy w układzie zaszły te same operacje (operandy, wyniki) co w symulacji.
- **`hdlcov.py`** – mierzy **pokrycie kodu RTL** w testach (VHDL i Verilog, narzędzia
  open source) i raportuje nieprzetestowane linie.

Wymagany Python ≥ 3.8; skrypty nie mają zależności poza biblioteką standardową.

---

## vcdcmp – symulacja vs sprzęt

### Obsługiwane źródła

Narzędzie nie zależy od producenta – czyta formaty, do których eksportują wbudowane
analizatory logiczne:

| Format | Przykładowe źródła |
|---|---|
| **VCD** (Value Change Dump) | Intel SignalTap, AMD Vivado ILA (`write_hw_ila_data -vcd_file`), Lattice Reveal, Microchip Identify, symulatory (Questa, Riviera, Xcelium, VCS, Icarus, Verilator, GHDL, NVC) |
| **CSV** (wiersz = próbka) | AMD Vivado ILA (`write_hw_ila_data -csv_file`), Intel SignalTap, inne analizatory z eksportem tabelarycznym |

Konwencje nazw różnych narzędzi są normalizowane, więc te nazwy wskazują ten sam sygnał:
`tb.dut.u_mul.in_a` (symulacja), `top:u0|mul:u_mul|in_a` (Intel), `u_top/u_mul/in_a[7:0]` (AMD).
Zakresy szyn mogą mieć postać `[7:0]`, `[7..0]` lub `[3]`. Szyny rozbite przy eksporcie na
pojedyncze bity (`p[0]` … `p[15]`) są składane z powrotem.

### Idea

Transakcja to cykl, w którym `valid == 1` (oraz `ready == 1`, jeśli podano `ready`).
Z każdej transakcji zapisywane są wartości pól (np. `a`, `b` na wejściu, `p` na wyjściu).
Sekwencje transakcji z symulacji i ze sprzętu (**HW**) są wyrównywane i porównywane, więc:

- nie ma znaczenia, w którym miejscu symulacji analizator złapał swoje okno,
- nazwy sygnałów nie muszą być identyczne (wystarczy wspólna końcówka ścieżki),
- porównywane są wartości liczbowe (z obsługą liczb ze znakiem i stałoprzecinkowych),
  więc różne szerokości lub formaty zapisu (HEX/BIN/DEC) nie przeszkadzają.

Raport rozróżnia transakcje o **różnych wartościach** oraz transakcje występujące
**tylko w HW** / **tylko w SIM** (zgubione lub nadmiarowe). Podsumowanie podaje interfejsy
uporządkowane według czasu pierwszej rozbieżności – w łańcuchu modułów najwcześniejsza
wskazuje podejrzany moduł (wejścia zgodne, wyjście nie → błąd w module).

### Użycie

```bash
# lista sygnałów w pliku (pomaga napisać konfigurację)
python3 vcdcmp.py --list nagranie.vcd nagranie.csv

# porównanie: konfiguracja + symulacja + nagrania z analizatora
python3 vcdcmp.py config.json sim.vcd hw.vcd
python3 vcdcmp.py config.json sim.vcd katalog_z_nagraniami/ --merge-out scalony.vcd --csv roznice.csv

# bez pliku konfiguracji – interfejsy podane prefiksem
python3 vcdcmp.py sim.vcd hw/ --clk tb.clk --signed --itf mul_in=u_mul.in_ --itf mul_out=u_mul.out_
```

| Opcja | Opis |
|---|---|
| `--list` | wypisz sygnały z plików VCD/CSV |
| `--itf NAZWA=PREFIKS[,PREFIKS_HW]` | interfejs z prefiksu (można powtarzać) |
| `--clk NAZWA` | zegar w symulacji (zamiast `sim_clock`) |
| `--signed` | pola interfejsów z `--itf` są liczbami ze znakiem |
| `--edge rise\|fall` | zbocze zegara symulacji |
| `--hw-period N` | okres próbkowania nagrań VCD (w jednostkach ich timescale); domyślnie wykrywany |
| `--radix HEX\|BIN\|DEC\|SIGNED\|OCT` | format wartości w CSV bez wiersza `Radix` (domyślnie HEX) |
| `--merge-out PLIK` | zapisz wszystkie nagrania HW w jednym pliku VCD |
| `--csv PLIK` | zapisz wszystkie rozbieżności do pliku CSV |
| `--max N` | ile rozbieżności wypisać na interfejs (domyślnie 20) |

Kod wyjścia: `0` – wszystko zgodne, `1` – są rozbieżności, `2` – błąd (np. brak sygnału).
Dzięki temu narzędzie można wpiąć w skrypty i CI.

### Wiele nagrań

Zamiast jednego pliku można podać kilka plików i/lub katalogów. Brane są wszystkie
`*.vcd` i `*.csv`, sortowane naturalnie (`capture_2` przed `capture_10`); formaty można
mieszać. Każde nagranie jest wyrównywane do symulacji **osobno**, bo może pochodzić
z innego miejsca przebiegu albo z innego uruchomienia. Wyniki są łączone w jeden raport.

`--merge-out` zapisuje nagrania jedno za drugim w jednym pliku VCD z dodatkowym sygnałem
`vcdcmp_segment` (numer nagrania). Plik przydaje się do przeglądania w dowolnej przeglądarce
przebiegów (np. GTKWave, Surfer); nagrania CSV, które nie mają osi czasu, są w nim
zapisywane jako 1 próbka = 1 ns.

### Format CSV

- pierwszy wiersz: nazwy sygnałów (ścieżki z `/`, `|` lub `.`, opcjonalnie z zakresem bitów),
- opcjonalny drugi wiersz `Radix - HEX,BINARY,UNSIGNED,SIGNED,...` (jak w AMD Vivado),
- kolumny indeksu/czasu (`Sample in Buffer`, `Sample in Window`, `Time`, `TRIGGER`, …) są pomijane,
- wartości: zgodnie z wierszem `Radix` albo `--radix`; rozpoznawane są też zapisy
  `0x..`, `0b..`, `..h` i w stylu Verilog (`16'h00ff`); `X`/`Z` są dozwolone,
- separator `,`, `;` lub tabulator jest wykrywany automatycznie.

### Konfiguracja

Nazwy dopasowywane są **po końcówce ścieżki**. Gdy końcówka pasuje do kilku sygnałów,
narzędzie wypisze kandydatów i trzeba wtedy podać dłuższą ścieżkę.

**Interfejs z prefiksem** (najprościej). Narzędzie samo znajdzie `valid`
(`valid`, `vld`, `tvalid`, `stb`, `en`, `start`, `done`…), opcjonalnie `ready`
(`ready`, `rdy`, `tready`), a pozostałe sygnały z prefiksem potraktuje jako pola.
Zegary i resety (`clk*`, `rst*`, …) są pomijane. Brane są tylko sygnały obecne
zarówno w symulacji, jak i we wszystkich nagraniach HW.

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

| Klucz | Opis |
|---|---|
| `prefix` | prefiks nazw (`"u_mul.in_"`) albo `{"sim": "...", "hw": "..."}`, gdy różne |
| `valid`, `ready` | końcówka po prefiksie (`"vld"`) albo pełna nazwa z innego zakresu (`"u_mul.out_valid"`) |
| `fields` | opcjonalnie: lista końcówek (`["a", "b"]`) albo `{"a": {"signed": true, "frac": 4}}` |
| `exclude` | wzorce glob końcówek do pominięcia, np. `["dbg_*"]` |
| `signed`, `frac` | ustawienia domyślne dla wszystkich pól; `frac` = liczba bitów ułamkowych (format Q) |

**Interfejs jawny** – każdy sygnał podany ręcznie:

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

Klucze globalne: `sim_clock` (wymagany, albo `--clk`), `hw_clock` (jeśli nagranie
zawiera zegar – zwykle nie zawiera), `hw_period`. Starsze nazwy `stp`, `stp_clock` i `stp_period`
są nadal akceptowane.

### Uwagi

- Symulacja jest próbkowana tuż przed zboczem zegara (wartość widziana przez przerzutniki),
  nagranie HW – w jego chwilach próbkowania (VCD) albo co wiersz (CSV).
- Wartości z X/Z porównywane są jako surowe bity.
- Przy danych okresowych (np. same zera) wyrównanie może być niejednoznaczne – raport to zaznacza.
- Formaty natywne analizatorów (np. binarne pliki projektów) nie są czytane – trzeba je
  wyeksportować do VCD lub CSV w narzędziu producenta.

---

## hdlcov – pokrycie kodu RTL

Używa narzędzi open source, niezależnych od producenta FPGA:

| Język | Symulacja | Pokrycie |
|---|---|---|
| VHDL | [NVC](https://github.com/nickg/nvc) | NVC (instrukcje, gałęzie, wyrażenia, toggle) |
| Verilog | [Icarus Verilog](https://steveicarus.github.io/iverilog/) albo gotowy VCD z dowolnego symulatora | [Covered](https://covered.sourceforge.net/) (linie, toggle, logika kombinacyjna, FSM) |

```bash
# VHDL: symulacja testbencha z pomiarem pokrycia tylko dla instancji DUT
python3 hdlcov.py vhdl -t tb_mac --dut tb_mac.dut rtl/mac.vhd tb/tb_mac.vhd

# Verilog (Icarus Verilog + Covered)
python3 hdlcov.py verilog -t tb_mac --dut tb_mac.dut --dut-module mac rtl/mac.v tb/tb_mac.v

# Verilog z gotowym VCD (np. z innego symulatora)
python3 hdlcov.py verilog -t tb_mac --dut tb_mac.dut --dut-module mac --vcd sim.vcd rtl/mac.v

# raport z istniejącej bazy NVC (np. z przebiegu cocotb z SIM=nvc i --cover)
python3 hdlcov.py nvc-db sim_build/cov.ncdb
```

Wspólne opcje:

- `-o KATALOG` – katalog wyników (domyślnie `hdlcov_out/`),
- `--rtl WZORZEC` – które pliki to RTL; domyślnie wszystko poza `tb_*`, `*_tb.*`, `test_*`
  i katalogami `tb/`, `test/`, `sim/`,
- `--min PROCENT` – kod wyjścia 1, gdy pokrycie linii RTL jest niższe (do CI).

Opcje dla VHDL: `--std`, `-g NAZWA=WARTOŚĆ`, `--stop-time`. Dla Veriloga: `-D MAKRO`, `--vcd`.

Wyniki (w katalogu `-o`):

- `coverage.md` – podsumowanie, pokrycie poszczególnych plików i tabela nieprzetestowanych linii z kodem,
- `coverage.json` – te same dane do dalszego przetwarzania,
- NVC: `html/index.html` (szczegółowy raport) i `cobertura.xml` (np. dla GitLab/Jenkins),
- Covered: `summary.txt` i `details.txt` (pełne raporty) oraz `cov.cdd`,
- `hdlcov.log` – wszystkie wywołane polecenia i ich wyjście.

Rodzaje wpisów na liście nieprzetestowanych linii:

- `nie wykonana` – instrukcja ani razu nie została wykonana,
- `gałąź N%` – warunek nie przyjął wszystkich wartości,
- `toggle` – (VHDL) nie wszystkie bity portu lub sygnału przełączyły się 0→1→0.

### Instalacja narzędzi

Debian/Ubuntu:

```bash
sudo apt install covered iverilog
# NVC: pakiet z https://github.com/nickg/nvc/releases (są też wersje dla Windows i macOS)
sudo apt install ./nvc_*_amd64_ubuntu-24.04.deb
```

Bez uprawnień administratora można rozpakować pakiety lokalnie (`apt-get download …`,
`dpkg -x pakiet.deb ~/tools/…`) i wskazać pliki wykonywalne zmiennymi środowiskowymi
`HDLCOV_NVC`, `HDLCOV_COVERED`, `HDLCOV_IVERILOG` i `HDLCOV_VVP`. `hdlcov.py` szuka narzędzi
kolejno: w tych zmiennych, w `PATH` i w `~/tools/bin`.

Covered nie radzi sobie z blokami `$date`/`$version`/`$comment` w VCD, dlatego `hdlcov.py`
usuwa je automatycznie przed analizą.

---

## Przykład

```bash
cd example
python3 gen_test.py .            # sim.vcd + hw/capture_1.vcd, capture_2.vcd (styl Intel), capture_3.csv (styl AMD)
python3 ../vcdcmp.py config.json sim.vcd hw/          # wykrywa 1 wstrzyknięty błąd w mul_out
python3 ../vcdcmp.py config_prefix.json sim.vcd hw/   # to samo, interfejsy z prefiksów

cd coverage
python3 ../../hdlcov.py vhdl -t tb_mac --dut tb_mac.dut vhdl/mac.vhd vhdl/tb_mac.vhd
python3 ../../hdlcov.py verilog -t tb_mac --dut tb_mac.dut --dut-module mac verilog/mac.v verilog/tb_mac.v
```

W przykładzie pokrycia testbench celowo nie sprawdza nasycenia akumulatora, więc raport
pokazuje te linie jako nieprzetestowane.
