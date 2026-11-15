`timescale 1ns/1ps
module tb_mac;
  reg clk = 0, rst = 1, in_valid = 0;
  reg signed [7:0] in_a = 0, in_b = 0;
  wire out_valid;
  wire signed [15:0] out_acc;
  integer i;

  always #5 clk = ~clk;
  mac dut (.clk(clk), .rst(rst), .in_valid(in_valid), .in_a(in_a), .in_b(in_b),
           .out_valid(out_valid), .out_acc(out_acc));

  initial begin
    repeat (2) @(posedge clk);
    rst <= 0;
    for (i = 1; i <= 10; i = i + 1) begin
      in_valid <= 1; in_a <= i; in_b <= -3;
      @(posedge clk);
    end
    in_valid <= 0;
    @(posedge clk); #1;
    if (out_acc !== -16'sd165) $display("BLAD: %0d", out_acc);
    $finish;
  end
endmodule
