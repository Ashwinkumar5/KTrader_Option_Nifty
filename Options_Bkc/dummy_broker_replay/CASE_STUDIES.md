# Strategy Case Studies

## Case 1 — 07 August failed-auction PUT move

- **Window:** 07 August 2026, approximately 09:44–11:05 IST
- **Primary strategy:** `DERIVATIVES_QUANT`
- **Status:** Research plan approved; not implemented in production
- **Observed failure:** An early 24,650 CE trade stopped for −5.5659% net. Its
  900-second cooldown overlapped the correct qualified 24,650 PE signal at
  10:02:09 by approximately 11 seconds.
- **Missed opportunity:** 24,650 PE was approximately ₹139.70 at the qualified
  signal, reached the configured 10% target near 10:18, and later traded near
  ₹168.75 without touching the 5% stop first.
- **Missing evidence:** Failed-auction/lower-high state, multi-strike transition
  from put writing to put short covering, call writing, and cumulative futures
  short buildup.
- **Proposed research:** Add an early-CALL failed-auction veto and writer-
  transition evidence to `DERIVATIVES_QUANT`, plus a strictly confirmed
  opposite-side cooldown exception in the signal gate/execution layer.
- **Out of scope:** Do not change `SMC/Liquidity_Sweep_Reclaim` for this case;
  do not create a new strategy unless the pattern later proves independently
  profitable across a larger sample.
- **Validation limit:** One causal five-date ablation cycle, then stop and
  present results before any active-profile change.

