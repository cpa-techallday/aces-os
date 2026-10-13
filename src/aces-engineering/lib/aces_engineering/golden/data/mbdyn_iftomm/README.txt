# iftomm spatial rigid slider-crank benchmark — published reference trajectories.

Source: Exudyn repository (jgerstmayr/EXUDYN), directory
docs/verification/Slidercrank3DiftommBenchmark/, fetched 2026-10-14.
These are the reference solutions Exudyn's own documentation verifies against:
  - Masarati  (MBDyn; dense 4 ms grid; t, slider_x, crank_angle, comma-separated)
  - Masoudi   (irregular grid; tab-separated)
  - Chaojie   (20 ms grid; comma-separated; slider column carries a 15 mm datum-offset
               convention — starts at 0.215 m against the model's x_D = 0.2 m)

License: EXUDYN General License 1.0 (BSD-3-clause-equivalent; binary/source
redistribution permitted with this notice). Full text:
https://github.com/jgerstmayr/EXUDYN/blob/master/LICENSE.txt
Benchmark: https://www.iftomm-multibody.org/benchmark/
