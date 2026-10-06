# Performance

Docker stack (one uvicorn process, one worker, Postgres 16) after the demo: 8,000 encounters, 40,000 encounter events, 154 beds, one
approved recommendation. Reproduce with `make demo`, then `make load-test`.

## Reads: the capacity view, the arrivals forecast and the bed forecast, in rotation, closed loop, 10 s per level

| Concurrency | Requests | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|
| 1 | 1693 | 169.2 req/s | 4.5 ms | 10.2 ms | 11.5 ms | 0.00% |
| 8 | 3708 | 370.2 req/s | 20.4 ms | 29.2 ms | 32.5 ms | 0.00% |
| 32 | 2726 | 270.8 req/s | 87.6 ms | 100.9 ms | 138.5 ms | 0.00% |

Target from the blueprint: p95 under 500 ms for reads: met. The first run of this test had a p95 of 1.4 s at 32 concurrent, for two
reasons that are now fixed: the arrivals forecast wrote 24 forecast rows on every read (now stored once per clock), and the bed
forecast called the admission model once per patient in a bed (now one batched call) and is cached per state of knowledge. The live
state itself (the history plus the hours since, with what the generator was told) is computed once per clock and kept in memory.

## Compute (in-process, one core)

| Work | Time |
|---|---|
| Sixteen weeks of the department, 17,000 patients | 0.5 s |
| Fit the four models and their held-out reports | 0.7 s |
| Forward simulation, 12 hours × 30 runs | 2 s |
| Staffing: two simulations of 30 runs plus CP-SAT | 4 s |
| Evaluation: three departments, 18 mornings, surges | about a minute |

Not measured: a health system of several departments, or event rates of a real EHR feed; the simulator's events arrive in batches
when the clock moves, which is not how an HL7 feed behaves.
