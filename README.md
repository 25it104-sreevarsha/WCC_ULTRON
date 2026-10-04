# Agentic AI Smart City

A multi-agent simulation of a city run by five autonomous agents (**Traffic, Waste, Energy, Water, Emergency**). Each agent reads simulated camera/IoT data, detects problems, decides an action, dispatches the right city team, and tracks the job until it is resolved. Agents also pass signals to each other (e.g. Traffic detects an accident → Emergency dispatches Police; Emergency dispatches Fire → Traffic clears a corridor).

```
Cameras + IoT sensors  ->  5 agents (SENSE > DETECT > DECIDE > ACT > TRACK)  ->  City services
```

## Repository structure

```
.
├── README.md                      <- you are here
├── CONTRIBUTIONS.md               <- who built what (one section per team member)
├── smart_city_prototype_v2.py     <- extended simulation (latest)
├── test_smart_city.py             <- 21 unit/integration tests for v2
├── config.json                    <- optional settings (seed, cycles, crew sizes)
├── v1_hackathon/
│   └── smart_city_prototype.py    <- original hackathon submission (unchanged)
├── docs/
│   ├── index.html                 <- interactive explainer (GitHub Pages live page)
│   ├── smart-city-delivery-plan.html  <- workflow, work division, timeline
│   └── V2_ENHANCEMENTS.md         <- detailed notes + evidence for v2
└── sample_output/                 <- example run results (log, JSON metrics, CSV)
    ├── scripted/
    └── stress_24_cycles/
```

## Run

Requires Python 3.9+ and no external packages.

```bash
python3 v1_hackathon/smart_city_prototype.py          # original hackathon version
python3 smart_city_prototype_v2.py                    # v2, scripted demo (10 cycles)
python3 smart_city_prototype_v2.py --random-events --event-rate 0.25 --ticks 24 --quiet
python3 smart_city_prototype_v2.py --config config.json --out-dir my_run
python3 -m unittest -v test_smart_city                # run the tests
```

Each v2 run writes `smart_city_run_output.txt` (full log), `run_metrics.json` (summary) and `records.csv` (every job) to `--out-dir` (default `run_output/`).

## The five agents

| Agent | Watches | Acts by |
|---|---|---|
| Traffic | congestion, accidents | rerouting, signal changes, corridors for responders |
| Waste | bin fill levels | forecasting overflow, optimised collection routes |
| Energy | power use per zone | spotting spikes, dimming/load-shifting |
| Water | pressure and flow | locating leaks, raising repair orders |
| Emergency | fires, citizen reports, Traffic alerts | dispatching Police / Fire service |

## What v2 adds

Adaptive anomaly detection, forecast-driven waste routing with a real route optimiser, finite crews with priority dispatching and pre-emption, a closed-loop world model, de-duplicated incidents, two-way agent coordination, random-event scenarios, and JSON/CSV metrics export. Details and evidence: [docs/V2_ENHANCEMENTS.md](docs/V2_ENHANCEMENTS.md).

## Notes and limitations

- Sensor data is **simulated** (seeded, so runs are reproducible); nothing is validated on real city data.
- The agents are rule-based and statistical, not LLM-driven; there are no prompts.
- Who built which part: see [CONTRIBUTIONS.md](CONTRIBUTIONS.md).
