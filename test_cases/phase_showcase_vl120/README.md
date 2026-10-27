# VL=120 V: predicted angle and AC RMS

This 22-case unit uses the Tektronix MSO58B and records **CH1**, the current between the HV supply and HV bus capacitor. The instrument already reports CH1 in amperes; its unit configuration therefore has no `probe_V_per_A` conversion. The prior CH2 configuration would analyze an AC voltage as though it were source current.

The **primary performance metric is broadband CH1 AC RMS after removing its DC mean**. The 10/20 kHz groups and FFT peaks are diagnostic evidence about the spectrum; their rankings must not override the broadband AC RMS ranking.

At Xi=0.500, the commanded angles are 90, 135, 150, **158** (the online complex-model prediction from the 2026-10-09 sweep), 165, and 180 degrees. The 150, 158, 165, and 180 degree points repeat at the end of the batch to establish fault-free repeatability and drift. The corrected CH1 data had a two-harmonic carrier-energy fit minimum of about 159 degrees; the measured 15-degree grid minimum was 165 degrees. This unit tests the unmeasured predicted angle and the 165-degree neighbour in one run.

Xi=0.505, 0.510, 0.515, and 0.520 retain three-angle comparisons against 90 and 180 degrees. Their candidate angles (156, 158, 159, 160 degrees) are **provisional estimates from the earlier local interpolation**, not newly validated CH1 predictions. Record the online `phiOptDeg` at each case and compare it with the commanded candidate before describing any point as a successful real-time prediction.

Run `check.ps1` only with the DUT de-energized. Verify the CH1 probe position and its display in amperes, the 30 A probe range, the CH1 trace headroom, and `Enable1=0`. `run.ps1` controls the hardware and requires the normal bench approval and supervision. It has not been run as part of this update.

After a completed batch, `analyze_results.ps1 -BatchPath <batch directory>` writes `showcase_metrics.csv`, `showcase_summary.json`, and four PNG/SVG figures: `showcase_angle_comparison`, `xi0500_angle_curve`, `group_vs_acrms`, and `showcase_tradeoff`. The figures put broadband AC RMS first and use carrier groups for diagnosis. The analysis includes CH1 FFT peaks, group energy and RMS, broadband AC RMS, power/readback context, predicted-angle comparisons, and a descriptive association of group energy with AC energy.

The quality filter rejects records with low-frequency (0–500 Hz) fault energy over 0.01 A², or changes of more than 5% in HV current DC mean or 25% in AC2 power within the same Xi. The low-frequency cutoff is an empirical guard from this VL120 configuration, not a general physical threshold. Inspect exclusions rather than silently pooling unlike conditions. An AC-RMS improvement close to the repeated-point spread cannot support a strong optimum claim.
