# Prototype v2: Detailed Enhancement Notes

**Base project:** the team's hackathon prototype (`v1_hackathon/smart_city_prototype.py`, unchanged for comparison): five city agents (Traffic, Waste, Energy, Water, Emergency) sharing a message bus, following SENSE → DETECT → DECIDE → ACT → TRACK.

**v2 work:** a post-hackathon extension, `smart_city_prototype_v2.py`, plus tests, configuration, and metrics export. The original architecture is kept; the changes are in how the agents decide, how work gets resourced, and how outcomes feed back into the simulated city.

---

## 1. What was identified as weak in v1

| Limitation in v1 | Why it matters |
|---|---|
| Fixed thresholds (`flow > 120`, `pressure < 45`) and a global running-mean energy baseline | Brittle: a slow drift triggers false alarms, a real fault in a high-baseline zone can be missed |
| "Route optimiser" sorted zones in a fixed order | Not an optimiser; no distance model at all |
| Tickets closed on a timer | Closure wasn't tied to any work being done |
| Unlimited crews and responders | No resource contention, so prioritisation was meaningless |
| A persistent fault was re-reported every cycle | Duplicate work orders |
| Only Traffic → Emergency coordination | One-way agent cooperation |
| Fully scripted events, no metrics output | Impossible to evaluate or stress-test |

## 2. What was built

| # | Enhancement | Where in the code |
|---|---|---|
| 1 | **Adaptive anomaly detection.** Per-zone EWMA baseline + deviation score replaces fixed limits (Energy, Water). Anomalies don't update their own baseline. | `EwmaDetector`, `EnergyAgent`, `WaterAgent` |
| 2 | **Predictive waste logistics.** Fill-rate is estimated from recent readings to forecast time-to-overflow; bins due soon are batched into trips already going out. | `WasteAgent._forecast`, `WasteAgent.step` |
| 3 | **Real route optimisation.** Zone coordinates + depot; exact search up to 8 stops, nearest-neighbour + 2-opt beyond. Reports km vs the fixed-order baseline. | `optimise_route`, `route_distance` |
| 4 | **Finite crews + priority dispatching with pre-emption.** Each team has N units; jobs are queued by priority (P1 critical … P4 low); a P1 job can pull a unit off a P3+ job, which later resumes with only its remaining work. | `Dispatcher` |
| 5 | **Closed-loop world model.** Completing a job changes the simulation: bins are emptied, leaks stop, fires end, accidents clear, energy spikes end. Jobs close when crews finish, not on a timer. | `on_close` callbacks, `SensorGrid.end_event / empty_bins` |
| 6 | **Incident de-duplication.** A persistent fault yields one work order; later cycles log "still active". | `Agent.is_open`, `_open` |
| 7 | **Two-way agent coordination.** Added Emergency → Traffic green-wave corridors; message bus signals are now topic-filtered. | `MessageBus.drain_signals`, `TrafficAgent.step` |
| 8 | **Scenario engine.** Original scripted demo kept; added seeded random events (`--random-events`) for stress tests. | `SensorGrid._inject_events` |
| 9 | **Engineering:** JSON config (`config.json`), CLI, per-run `run_metrics.json` + `records.csv`, 21 unit tests, isolated seeded RNGs for reproducibility. | `Config`, `main`, `export`, `test_smart_city.py` |

## 3. Evidence (all reproducible with the commands below)

- **Tests:** 21 unit/integration tests pass (`python3 -m unittest -v test_smart_city`). They cover detector behaviour, routing optimality, pre-emption and resume, de-duplication, closed-loop effects, determinism, config validation and export.
- **Detector comparison, found while testing:** on the simulated sensors, the fixed limits kept from v1 raised **97 false alarms in 60 quiet 30-cycle runs**, while the adaptive detector raised **0**. The cause was the simulation itself: v1's pressure/flow were unbounded random walks that eventually wandered past the fixed limits. the simulated network was made mean-reverting (a regulated water network), after which there were **0 false alarms in 200 quiet 30-cycle runs** (36,000 zone-readings), and injected faults are still detected.
- **Routing:** in the demo, a 3-stop trip is reordered to 15.3 km vs 16.4 km fixed-order. Across 50 stress runs (24 cycles, event rate 0.25) the optimiser cut total truck distance by 3.5% on average (0% to 17.8% depending on how many multi-stop trips occurred). Trips with 1–2 stops cannot be improved, so the gain is modest by design; it is an honest number, not a headline one.
- **Resource what-if (stress scenario: 24 cycles, event rate 0.25, 30 seeds):** average wait of CRITICAL incidents before a unit starts, by emergency-unit count:

  | Emergency units | Avg CRITICAL wait (cycles) | Avg peak queue |
  |---|---|---|
  | 1 | 1.35 | 5.0 |
  | 2 | 0.08 | 1.6 |
  | 3 | 0.01 | 0.5 |

  The simulation can now answer "how many units does the city need?", which v1 couldn't.
- **Default demo (scripted, seed 42):** 16 records raised, 14 resolved, 2 still in progress at cycle 10, 1 pre-emption, 1.1 km saved, ~63 kWh estimated saved. See `sample_output/`.

## 4. How to run

```bash
python3 smart_city_prototype_v2.py                                  # scripted demo
python3 smart_city_prototype_v2.py --random-events --event-rate 0.25 --ticks 24 --quiet
python3 smart_city_prototype_v2.py --config config.json --out-dir my_run
python3 -m unittest -v test_smart_city                              # 21 tests
```

Outputs per run: `smart_city_run_output.txt` (full log), `run_metrics.json` (summary), `records.csv` (every job).

## 5. Changes to existing behaviour (so nothing is hidden)

- The scripted citizen smoke report moved from cycle 6 to cycle 4 and is now **normal** priority (an unverified report), so the demo can show a confirmed fire pre-empting it.
- Pressure and flow are now mean-reverting rather than unbounded random walks (see evidence above).
- Because randomness is now per-component (`random.Random`), the same seed gives different numbers than v1.
- The default crew capacities (Emergency = 1, Maintenance = 1, Waste = 1) are deliberately tight to make contention visible; they are configurable.

## 6. Honest limitations

- All sensor data is still **simulated**; nothing here has been validated on real city data.
- The "~22% kWh saved" figure is v1's modelling assumption, not a measurement.
- The EWMA detector is a classical statistical method, not machine learning. It's adaptive and explainable, which is the point, but it is not a trained model.
- Priority levels and job durations are hand-chosen constants (`DURATION`, `PRIORITY_NAME`).
- Emergency → Traffic corridor signals take effect one cycle later (agents run in a fixed order each cycle).
- Possible future work: a live dashboard, multi-objective routing (time windows, truck capacity), and replaying real open-data traffic/energy datasets.

## 7. Summary line

> Extended the team's multi-agent smart-city simulation: designed adaptive (EWMA) anomaly detection, forecast-driven waste routing with exact/2-opt route optimisation, and a priority-based dispatcher with finite crews and pre-emption; built a closed-loop world model, JSON/CSV metrics export and a 21-test suite.

