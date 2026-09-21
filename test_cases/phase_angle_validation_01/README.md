# Phase-angle validation 01

Purpose: validate one stable operating point for which the predictor tends toward 180 degrees
while the measured optimum is expected below 180 degrees.

Fixed point: manual LV supply 120 V, `xi=0.50`, `ud_ref6=ud_ref7=60`. The sequence is
90, 105, 120, 135, 150, 165, 180 degrees, followed by 120 degrees again as a drift check.
`phi2_deg` is the only executed angle. The predictor remains active and its outputs are read only.

For every row the runner waits 6 s after enable before the MXO38 takes a 0.5 s CH2 record, then
reads `phiOptDeg`, `phiSearchDeg`, `K1`, `K2`, `Gamma`, `valid`, `status`, `diag`, DC voltages,
port powers, and AC P/Q. Power factor is derived as `P/sqrt(P^2+Q^2)` with the sign of P.
ControlDesk phase-current MF4 files and the scope HDF5 are retained for every case.

Before energizing:

1. Confirm CH2 is physically the HV source-current probe and its displayed unit is A.
2. Confirm the DUT is de-energized while checking wiring and scope setup.
3. In the ControlDesk desktop session, run `check.ps1`. It must resolve every observation path,
   report `Enable1=0`, and pass the read-only scope check.
4. Verify the manual supplies and thermal state. The scripts do not control power supplies.
5. Only after the operator has made the bench safe and energized it intentionally, run `run.ps1`.

Acceptance requires all eight cases, `valid != 0`, acceptable `status`, finite `diag(1:4)`, stable
HV/LV voltage and P/Q across the sweep, and agreement between the two 120-degree measurements
within the experiment's declared drift tolerance. Do not infer the optimum from predictor output;
use the measured CH2 band RMS/peaks versus the applied `Phi` stored in each `case.json`.
