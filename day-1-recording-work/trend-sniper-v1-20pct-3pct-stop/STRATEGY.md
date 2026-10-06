# Trend Sniper

```
┌───────────────────────────────────────────────────────────────────┐
│  TREND SNIPER                                                     │
│  Part-Time Quant Academy · Day 3 spec · v1                        │
├───────────────────────────────────────────────────────────────────┤
│  INSTRUMENT   SPY (etf, USD, daily bars)                          │
│  HISTORY      2000-01-01 to today, via auto                       │
│  DIRECTION    long only                                           │
├───────────────────────────────────────────────────────────────────┤
│  ENTRY        all of these true, fill next bar open               │
│               1. close > SMA(50), 2 bars in a row                 │
├───────────────────────────────────────────────────────────────────┤
│  EXIT         first one to fire, fill next bar open               │
│               1. close < SMA(50)                                  │
├───────────────────────────────────────────────────────────────────┤
│  STOP         3.0% below entry, fixed, checked intrabar           │
│  SIZING       20.00% of account into each position                │
│               max 1 position(s) open, max 1.00% risk on at once   │
│  COSTS        6.0 bps per side                                    │
├───────────────────────────────────────────────────────────────────┤
│  TERMS PINNED 5                                                   │
│  UNRESOLVED   0                                                   │
│  STATUS       CHECKED, not yet backtested                         │
└───────────────────────────────────────────────────────────────────┘
```

Source rule, day 2, unedited:

> Not decided yet
