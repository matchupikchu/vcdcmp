-- Saturating MAC: acc <= sat(acc + a*b). The saturation branch is not exercised by the tb.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity mac is
  port (
    clk, rst   : in  std_logic;
    in_valid   : in  std_logic;
    in_a, in_b : in  signed(7 downto 0);
    out_valid  : out std_logic;
    out_acc    : out signed(15 downto 0)
  );
end entity;

architecture rtl of mac is
  constant C_MAX : signed(16 downto 0) := to_signed(32767, 17);
  constant C_MIN : signed(16 downto 0) := to_signed(-32768, 17);
  signal acc : signed(15 downto 0) := (others => '0');
begin
  process (clk)
    variable sum : signed(16 downto 0);
  begin
    if rising_edge(clk) then
      out_valid <= '0';
      if rst = '1' then
        acc <= (others => '0');
      elsif in_valid = '1' then
        sum := resize(acc, 17) + resize(in_a * in_b, 17);
        if sum > C_MAX then
          acc <= C_MAX(15 downto 0);
        elsif sum < C_MIN then
          acc <= C_MIN(15 downto 0);
        else
          acc <= sum(15 downto 0);
        end if;
        out_valid <= '1';
      end if;
    end if;
  end process;
  out_acc <= acc;
end architecture;
