// MAC z nasyceniem: acc <= sat(acc + a*b). Gałąź nasycenia nie jest testowana w tb.
module mac (
  input  wire               clk, rst,
  input  wire               in_valid,
  input  wire signed [7:0]  in_a, in_b,
  output reg                out_valid,
  output reg  signed [15:0] out_acc
);
  wire signed [16:0] sum = out_acc + in_a * in_b;

  always @(posedge clk) begin
    out_valid <= 1'b0;
    if (rst) begin
      out_acc <= 16'sd0;
    end else if (in_valid) begin
      if (sum > 17'sd32767)
        out_acc <= 16'sh7fff;
      else if (sum < -17'sd32768)
        out_acc <= 16'sh8000;
      else
        out_acc <= sum[15:0];
      out_valid <= 1'b1;
    end
  end
endmodule
