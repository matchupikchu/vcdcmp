library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity tb_mac is
end entity;

architecture sim of tb_mac is
  signal clk, rst, in_valid, out_valid : std_logic := '0';
  signal in_a, in_b : signed(7 downto 0) := (others => '0');
  signal out_acc    : signed(15 downto 0);
begin
  clk <= not clk after 5 ns;
  dut : entity work.mac port map (clk, rst, in_valid, in_a, in_b, out_valid, out_acc);

  process
  begin
    rst <= '1';
    wait until rising_edge(clk);
    rst <= '0';
    for i in 1 to 10 loop
      in_valid <= '1';
      in_a <= to_signed(i, 8);
      in_b <= to_signed(-3, 8);
      wait until rising_edge(clk);
    end loop;
    in_valid <= '0';
    wait until rising_edge(clk);
    assert out_acc = to_signed(-165, 16) report "zly wynik" severity error;
    std.env.finish;
  end process;
end architecture;
