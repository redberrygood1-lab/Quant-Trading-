# The three checks, in plain words

Every formula in `scripts/verdict.py` is ported from the PTQ engine's validation
module (`validation.py`). It was checked column-for-column against the engine on
random matrices and matches to twelve decimal places — PBO, the probabilistic
and deflated Sharpe, and every field of the walk-forward. Nothing here is
approximated or invented.

The trial grid itself is ported from the engine's `signals.param_grid` and
`grid_with_base`, and is simulated by day 4's backtester, which is itself a port
of the engine's `backtest.simulate`.

There is no scipy. The normal CDF comes from `math.erf`; the inverse normal CDF
is Acklam's approximation. Same as the engine.

---

## 1. PBO — probability of backtest overfitting

**The question:** you tried a set of settings and kept the best one. If you had
had a different slice of history, would you have kept that same one, and would
it still have been any good?

**How it's computed (CSCV):**

1. Take the T×N matrix — T bars down, one column per configuration you tried.
2. Cut the rows into S = 8 equal blocks along time.
3. Take every way of choosing 4 of those 8 blocks as in-sample. That is
   C(8,4) = **70 splits**. Not a sample of them. All of them.
4. On each split: find the column with the best in-sample Sharpe. Look up where
   that same column ranks, out-of-sample, among all N columns. Turn its relative
   rank *w* into a logit, `log(w / (1-w))`.
5. PBO is the share of the 70 splits where that logit came out at or below zero
   — that is, the share where the in-sample winner finished in the **bottom
   half** out-of-sample.

**Reading it:** PBO of 65% means that two times in three, the setting that
looked best on one part of your history was below-median on the rest. Your
selection process is picking noise.

PBO of 5% does not mean your rule works. It means your *selection* was stable.
That is a different and smaller claim.

**Passes when:** PBO < 50%. That threshold is the engine's, not invented here.

**Paper:** Bailey, Borwein, López de Prado & Zhu, *The Probability of Backtest
Overfitting* (2016).

---

## 2. Deflated Sharpe ratio

**The question:** search enough settings and one of them will show a good Sharpe
by chance alone. Is yours better than the best you'd expect from pure luck,
given how hard you searched?

**How it's computed:**

First, the expected maximum Sharpe from N random trials:

```
SR* = sd(trial Sharpes) × [ (1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) ]
```

where γ is the Euler–Mascheroni constant, 0.5772…, and Φ⁻¹ is the inverse
normal CDF. The more configurations you tried, and the more their Sharpes
varied, the higher SR* climbs — **your own search raises the bar you have to
clear.** The card prints SR* so you can see how far the search moved it.

Then the probabilistic Sharpe ratio of your configuration against that
benchmark, which corrects for skew, fat tails and sample length:

```
PSR = Φ( (SR − SR*) · √(n−1) / √(1 − skew·SR + ((kurt−1)/4)·SR²) )
```

**Reading it:** it is a probability, not a ratio. 0.98 means a 98% chance the
true Sharpe beats what the search alone would have produced. 0.60 means you
cannot tell your result apart from the best of that many random tries.

**Passes when:** > 0.95.

**If you only tried one configuration** there is no spread to deflate against.
The number degrades to the plain probabilistic Sharpe against zero, and the card
relabels the gate so you are not told you passed something you did not run.

**Papers:** Bailey & López de Prado, *The Deflated Sharpe Ratio* (2014), and
*The Sharpe Ratio Efficient Frontier* (2012) for PSR.

---

## 3. Walk-forward out-of-sample

**The question:** does the way you *pick* settings generalise, on data the
picking never saw?

**How it's computed:** anchored (expanding) K-fold, 5 folds by default.

For each fold:
- In-sample is every bar before the fold.
- Choose the configuration with the best in-sample Sharpe.
- Score **that** configuration on the fold's bars, which the choice never saw.
- Keep the fold's out-of-sample returns.

Then the out-of-sample segments are stitched into one continuous return stream,
and the annualised Sharpe of that stream is the gate.

**Why not just split the data 60/40?** Because a fixed configuration split in
two tests the rule. It does not test the process that produced the rule. Here
the settings are re-chosen from scratch on every fold, so what gets measured is
your whole method — including the part where you picked a lookback because it
looked good.

The in-sample block always ends before the out-of-sample block starts, so
selection cannot see its own test window. No look-ahead.

**Also reported:** walk-forward efficiency, the out-of-sample Sharpe divided by
the in-sample one. 1.0 means the edge carried in full. 0.3 means seventy per
cent of what you saw was in-sample flattery. At or below zero, none of it
survived. It is a diagnostic here, not a gate.

**Passes when:** stitched out-of-sample Sharpe > 0. A low bar, deliberately.
Most rules do not clear it.

---

## The verdict

Three gates. All three must hold. One failure is a FAIL.

`verdict.json` also carries the engine's three-state word, on the same rule the
engine uses: 0 failures `approve`, 1 failure `reduce`, 2 or more `block`.

A gate that could not run counts as not held. A check you did not manage to run
is not a check you passed.

---

## What this manual version does not do

The PTQ engine additionally **purges** the splits by trade label: any trade that
straddles an in-sample / out-of-sample boundary is dropped from the training
side, with an embargo after each test window. That closes a small leak where a
trade's outcome is partly known on both sides of the cut.

The version here does not purge. It builds the trial matrix from per-bar
returns and does not carry the per-trade label intervals needed to spot a
straddle. The effect is that in-sample scores can flatter slightly. The card
states this in its methodology block rather than leaving it unsaid.

---

## Costs

Transaction costs are day 4's job, and day 5 re-runs day 4's simulator, so the
same per-side cost as day 4 (6 bps, or 1 on FX) is charged on every column of the grid. Day 5 refuses to
judge a run with `costBpsPerSide` of zero or missing, because a frictionless equity curve is not a curve anyone
could have traded. The academy default is 6 bps per side (1 on FX), covering spread,
commission and slippage — the same figure the engine uses.
