"""
Agentic AI Smart City - prototype v2 (extended)
================================================

Extends the original hackathon prototype (smart_city_prototype.py) while keeping
its architecture: five agents sharing a message bus, each following
SENSE -> DETECT -> DECIDE -> ACT -> TRACK.

What is new in v2 (full list with rationale in CONTRIBUTIONS.md):

  1. Adaptive anomaly detection   - per-zone EWMA baselines replace fixed
                                    thresholds / a drifting global mean.
  2. Predictive waste logistics   - fill-rate forecasting + an exact route
                                    optimiser (brute force <= 8 stops,
                                    nearest-neighbour + 2-opt above that).
  3. Limited city resources       - every team has a finite number of crews.
  4. Priority dispatching         - priority queue with pre-emption, so a
                                    critical incident can pull a crew off a
                                    low-priority job.
  5. Closed-loop world model      - jobs really change the world: bins get
                                    emptied, leaks get fixed, fires go out.
                                    Nothing "resolves" on a timer any more.
  6. Incident de-duplication      - a persistent fault produces ONE work order.
  7. Richer agent coordination    - Emergency -> Traffic green-wave corridors
                                    (in addition to Traffic -> Emergency).
  8. Scenario engine              - scripted demo OR seeded random events.
  9. Config + CLI + metrics export (JSON / CSV) and a unittest suite.

Run:
    python3 smart_city_prototype_v2.py                      # scripted demo
    python3 smart_city_prototype_v2.py --random-events --event-rate 0.25 --ticks 24
    python3 smart_city_prototype_v2.py --config config.json --quiet
Test:
    python3 -m unittest -v test_smart_city
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import os
import random
from dataclasses import dataclass, field
from statistics import mean
from typing import Callable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ZONES = ["Central", "North", "East", "West", "South", "Riverside"]
# Zone centres in km on a simple city grid (used by the route optimiser).
ZONE_XY = {
    "Central": (0.0, 0.0), "North": (0.0, 5.0), "East": (6.0, 0.0),
    "West": (-6.0, 0.0), "South": (0.0, -5.0), "Riverside": (4.0, -4.0),
}
DEPOT = (-1.0, -1.0)          # waste-collection depot
KM_PER_CYCLE = 10.0           # distance a truck covers in one 30-minute cycle

TEAMS = [
    "Traffic authority", "Waste-collection team", "Facilities / energy desk",
    "Maintenance crew", "Emergency responders",
]
TEAM_CODE = {
    "Traffic authority": "TR", "Waste-collection team": "WS",
    "Facilities / energy desk": "EN", "Maintenance crew": "MT",
    "Emergency responders": "EM",
}
# Concurrent jobs each team can handle (units / crews / trucks). Deliberately
# small so contention - and therefore prioritisation - actually shows up.
DEFAULT_CAPACITY = {
    "Traffic authority": 2, "Waste-collection team": 1,
    "Facilities / energy desk": 1, "Maintenance crew": 1,
    "Emergency responders": 1,
}
PRIORITY_NAME = {1: "CRITICAL", 2: "HIGH", 3: "NORMAL", 4: "LOW"}
# How many 30-minute cycles each kind of job occupies a crew.
DURATION = {"traffic_action": 1, "accident_clear": 2, "waste_min": 1,
            "energy_action": 2, "leak_repair": 3, "emergency": 2}


@dataclass
class Config:
    seed: int = 42
    ticks: int = 10                 # each tick = 30 minutes of city time
    start_hour: float = 7.0
    random_events: bool = False     # False = scripted demo scenario
    event_rate: float = 0.10        # per-tick, per-event-kind probability
    capacity: dict = field(default_factory=lambda: dict(DEFAULT_CAPACITY))

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path) as fh:
            data = json.load(fh)
        cfg = cls()
        for key, value in data.items():
            if key.startswith("_"):          # allow "_comment" keys
                continue
            if not hasattr(cfg, key):
                raise ValueError(f"unknown config key: {key!r}")
            if key == "capacity":
                for team, n in value.items():
                    if team not in TEAMS:
                        raise ValueError(f"unknown team in capacity: {team!r}")
                    if int(n) < 1:
                        raise ValueError(f"capacity for {team!r} must be >= 1")
                    cfg.capacity[team] = int(n)
            else:
                setattr(cfg, key, value)
        return cfg


# ---------------------------------------------------------------------------
# Shared infrastructure: records, message bus, city services
# ---------------------------------------------------------------------------

@dataclass
class Record:
    """A job / incident raised by an agent and worked by a city team."""
    tick: int
    agent: str
    kind: str            # action | work_order | incident
    target: str
    detail: str
    team: str
    priority: int = 3    # 1 critical .. 4 low
    duration: int = 1    # cycles of crew time needed
    remaining: int = 1   # cycles still needed (shrinks if pre-empted)
    status: str = "open"           # queued | active | closed
    opened_tick: int = 0
    first_started_tick: int | None = None
    started_tick: int | None = None
    closed_tick: int | None = None
    unit: str = ""
    preemptions: int = 0
    uid: int = -1
    meta: dict = field(default_factory=dict)
    on_close: Callable | None = field(default=None, repr=False, compare=False)


class CityServices:
    """The outside world: the inboxes of the teams the agents talk to."""

    def __init__(self):
        self.inbox: dict[str, list[str]] = {t: [] for t in TEAMS}

    def dispatch(self, team: str, message: str):
        self.inbox[team].append(message)


class MessageBus:
    """Shared memory so agents can coordinate. Signals are topic-filtered."""

    def __init__(self):
        self.records: list[Record] = []
        self.signals: list[dict] = []

    def emit(self, rec: Record):
        rec.uid = len(self.records)
        self.records.append(rec)

    def publish_signal(self, signal: dict):
        self.signals.append(signal)

    def drain_signals(self, types: set[str]) -> list[dict]:
        """Remove and return only the signals whose type is in `types`."""
        taken = [s for s in self.signals if s["type"] in types]
        self.signals = [s for s in self.signals if s["type"] not in types]
        return taken


def fmt(name: str, stage: str, msg: str) -> str:
    return f"{name:<9} {stage:<8} {msg}"


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

class EwmaDetector:
    """Adaptive anomaly detector.

    Keeps an exponentially-weighted mean and mean-absolute-deviation of a
    signal. `score()` says how many "typical deviations" a new value is from
    the baseline, so thresholds adapt per zone and follow slow drift. The
    caller should only `update()` with readings it believes are normal, so an
    anomaly can't poison its own baseline.
    """

    def __init__(self, floor: float, alpha: float = 0.3, k: float = 4.0):
        self.floor, self.alpha, self.k = floor, alpha, k
        self.mean: float | None = None
        self.dev = floor

    def score(self, x: float) -> float:
        if self.mean is None:
            return 0.0                       # no baseline yet
        return (x - self.mean) / max(1.25 * self.dev, self.floor)

    def update(self, x: float):
        if self.mean is None:
            self.mean = x
            return
        self.dev = (1 - self.alpha) * self.dev + self.alpha * abs(x - self.mean)
        self.mean = (1 - self.alpha) * self.mean + self.alpha * x


def route_distance(stops, depot=DEPOT, coords=None) -> float:
    coords = coords or ZONE_XY
    pts = [depot] + [coords[s] for s in stops] + [depot]
    return sum(math.dist(p, q) for p, q in zip(pts, pts[1:]))


def optimise_route(stops, depot=DEPOT, coords=None):
    """Shortest depot -> stops -> depot tour. Returns (route, km).

    Exact (brute force) for up to 8 stops; nearest-neighbour followed by 2-opt
    improvement beyond that.
    """
    coords = coords or ZONE_XY
    stops = list(dict.fromkeys(stops))
    dist = lambda r: route_distance(r, depot, coords)
    if len(stops) <= 1:
        return stops, round(dist(stops), 1)
    if len(stops) <= 8:
        best = list(min(itertools.permutations(stops), key=dist))
        return best, round(dist(best), 1)
    route, left, here = [], stops[:], depot
    while left:                                       # nearest neighbour
        nxt = min(left, key=lambda s: math.dist(here, coords[s]))
        route.append(nxt)
        left.remove(nxt)
        here = coords[nxt]
    improved = True
    while improved:                                   # 2-opt
        improved = False
        for i in range(len(route) - 1):
            for j in range(i + 1, len(route)):
                cand = route[:i] + route[i:j + 1][::-1] + route[j + 1:]
                if dist(cand) < dist(route) - 1e-9:
                    route, improved = cand, True
    return route, round(dist(route), 1)


# ---------------------------------------------------------------------------
# Dispatcher: finite crews, priority queue, pre-emption, outcome tracking
# ---------------------------------------------------------------------------

class Dispatcher:
    """Allocates limited crews to jobs. Jobs close when crews finish them."""

    NAME = "CITY-OPS"

    def __init__(self, capacity: dict[str, int]):
        self.capacity = capacity
        self.slots: dict[str, list[Record | None]] = {
            t: [None] * n for t, n in capacity.items()}
        self.queues: dict[str, list[Record]] = {t: [] for t in capacity}
        self.busy_slot_cycles = {t: 0 for t in capacity}
        self.max_queue = {t: 0 for t in capacity}
        self.cycles = 0

    def submit(self, rec: Record):
        rec.status = "queued"
        rec.remaining = rec.duration
        self.queues[rec.team].append(rec)

    # -- start of cycle: crews that finished their job report back ----------
    def complete(self, tick: int, log):
        for team, slots in self.slots.items():
            for i, rec in enumerate(slots):
                if rec and tick - rec.started_tick >= rec.remaining:
                    rec.status, rec.closed_tick = "closed", tick
                    slots[i] = None
                    log(fmt(self.NAME, "TRACK",
                            f"resolved: {rec.detail}  ({rec.unit}, P{rec.priority}, "
                            f"opened T{rec.opened_tick}, closed T{tick})"))
                    if rec.on_close:
                        rec.on_close(rec)

    # -- end of cycle: hand free (or pre-empted) crews the best waiting job -
    def allocate(self, tick: int, log):
        self.cycles += 1
        for team, slots in self.slots.items():
            queue = self.queues[team]
            queue.sort(key=lambda r: (r.priority, r.opened_tick, r.uid))
            while queue:
                free = next((i for i, s in enumerate(slots) if s is None), None)
                if free is None:
                    worst_i = max(range(len(slots)),
                                  key=lambda i: (slots[i].priority, slots[i].started_tick))
                    worst = slots[worst_i]
                    # pre-empt only when the gap is big (>= 2 levels)
                    if queue[0].priority <= worst.priority - 2:
                        worst.remaining -= tick - worst.started_tick
                        worst.status = "queued"
                        worst.preemptions += 1
                        worst.meta.pop("queued_logged", None)
                        queue.append(worst)
                        log(fmt(self.NAME, "PREEMPT",
                                f"{worst.unit} pulled off '{worst.detail}' (P{worst.priority}) "
                                f"for '{queue[0].detail}' (P{queue[0].priority})"))
                        slots[worst_i] = None
                        free = worst_i
                        queue.sort(key=lambda r: (r.priority, r.opened_tick, r.uid))
                    else:
                        break
                rec = queue.pop(0)
                rec.status, rec.started_tick = "active", tick
                if rec.first_started_tick is None:
                    rec.first_started_tick = tick
                rec.unit = f"{TEAM_CODE[team]}-{free + 1}"
                slots[free] = rec
                log(fmt(self.NAME, "DISPATCH",
                        f"{rec.unit} -> {rec.detail}  (P{rec.priority}, "
                        f"waited {tick - rec.opened_tick} cycle(s), needs {rec.remaining})"))
            for pos, rec in enumerate(queue, 1):
                if not rec.meta.get("queued_logged"):
                    rec.meta["queued_logged"] = True
                    log(fmt(self.NAME, "QUEUED",
                            f"{rec.detail} (P{rec.priority}) waiting for {team}, "
                            f"position {pos}, all {len(slots)} unit(s) busy"))
            self.busy_slot_cycles[team] += sum(1 for s in slots if s)
            self.max_queue[team] = max(self.max_queue[team], len(queue))


# ---------------------------------------------------------------------------
# Simulated sensing layer + world state
# ---------------------------------------------------------------------------

class SensorGrid:
    """Simulated cameras / IoT sensors over a world that agents can change."""

    SCRIPTED = {
        2: [("accident", "North")],
        3: [("leak", "Riverside")],
        4: [("citizen_report", ("East", "smoke near market"))],
        5: [("fire", "Central")],
        7: [("energy_spike", "West")],
    }

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)              # sensor noise
        self.event_rng = random.Random(cfg.seed + 1)    # random scenarios
        r = self.rng
        self.tick = 0
        self.bin_fill = {z: r.uniform(25, 55) for z in ZONES}
        self.bin_rate = {z: r.uniform(5.0, 9.0) for z in ZONES}    # %/tick
        self.base_energy = {z: r.uniform(80, 140) for z in ZONES}  # kWh
        self.pressure = {z: r.uniform(48, 55) for z in ZONES}      # psi
        self.flow = {z: r.uniform(95, 110) for z in ZONES}         # L/s
        self.p_nominal, self.f_nominal = dict(self.pressure), dict(self.flow)
        self.active: set[tuple[str, str]] = set()   # (kind, zone) still happening
        self.reports: list[tuple[str, str]] = []    # one-shot citizen reports

    # -- world dynamics --------------------------------------------------
    def advance(self, tick: int):
        self.tick = tick
        r = self.rng
        for z in ZONES:
            self.bin_fill[z] = min(100, self.bin_fill[z] + self.bin_rate[z])
        for z in ZONES:
            shape = 1.0 + 0.35 * (tick / self.cfg.ticks)
            self.base_energy[z] = max(30, self.base_energy[z] * 0.6 + r.uniform(70, 150) * shape * 0.4)
        for z in ZONES:
            # regulated network: noise pulls back towards the nominal value
            # (the original unbounded random walk drifted past fixed limits)
            self.pressure[z] += 0.25 * (self.p_nominal[z] - self.pressure[z]) + r.uniform(-0.8, 0.8)
            self.flow[z] += 0.25 * (self.f_nominal[z] - self.flow[z]) + r.uniform(-3, 3)
        self._inject_events(tick)

    def _inject_events(self, tick: int):
        if not self.cfg.random_events:
            for kind, arg in self.SCRIPTED.get(tick, []):
                self._start(kind, arg)
            return
        er = self.event_rng
        for kind in ("accident", "leak", "fire", "energy_spike"):
            if er.random() < self.cfg.event_rate:
                self._start(kind, er.choice(ZONES))
        if er.random() < self.cfg.event_rate:
            self._start("citizen_report", (er.choice(ZONES), "smoke reported"))

    def _start(self, kind: str, arg):
        if kind == "citizen_report":
            self.reports.append(arg)
        else:
            self.active.add((kind, arg))

    # -- things agents' work changes ------------------------------------
    def end_event(self, kind: str, zone: str):
        self.active.discard((kind, zone))

    def empty_bins(self, zones):
        for z in zones:
            self.bin_fill[z] = 5.0

    # -- readings ---------------------------------------------------------
    def traffic_reading(self, zone: str) -> dict:
        base = self.rng.uniform(0.2, 0.5)
        if self.rng.random() < 0.16:
            base = self.rng.uniform(0.7, 0.95)
        reading = {"zone": zone, "congestion": round(base, 2), "accident": False}
        if ("accident", zone) in self.active:
            reading.update(accident=True, congestion=0.9)
        return reading

    def bin_reading(self, zone: str) -> dict:
        return {"zone": zone, "fill": round(self.bin_fill[zone], 1),
                "rate": round(self.bin_rate[zone], 2)}

    def energy_reading(self, zone: str) -> dict:
        val = self.base_energy[zone]
        if ("energy_spike", zone) in self.active:
            val *= 2.4
        return {"zone": zone, "kwh": round(val, 1)}

    def water_reading(self, zone: str) -> dict:
        p, f = self.pressure[zone], self.flow[zone]
        if ("leak", zone) in self.active:
            p -= 9.0
            f += 28.0
        return {"zone": zone, "pressure": round(p, 1), "flow": round(f, 1)}

    def emergency_reading(self) -> dict:
        out = {"fires": sorted(z for k, z in self.active if k == "fire"),
               "reports": self.reports[:]}
        self.reports.clear()                 # a citizen report is a one-off
        return out


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

class Agent:
    name = "Agent"
    team = "City services"

    def __init__(self, sensors: SensorGrid, bus: MessageBus,
                 services: CityServices, dispatcher: Dispatcher):
        self.sensors, self.bus = sensors, bus
        self.services, self.dispatcher = services, dispatcher
        self.detected = 0
        self.actions = 0
        self._open_keys: set = set()

    def step(self, tick: int, log):
        raise NotImplementedError

    def is_open(self, key) -> bool:
        """True while a job for this fault is still unresolved (de-dup)."""
        return key in self._open_keys

    def _open(self, tick, kind, target, detail, *, key, priority, duration,
              on_close=None, meta=None, team=None) -> Record:
        rec = Record(tick=tick, agent=self.name, kind=kind, target=target,
                     detail=detail, team=team or self.team, priority=priority,
                     duration=duration, remaining=duration, opened_tick=tick,
                     meta=meta or {})
        self._open_keys.add(key)

        def _done(r, _key=key, _cb=on_close):
            self._open_keys.discard(_key)
            if _cb:
                _cb(r)

        rec.on_close = _done
        self.bus.emit(rec)
        self.dispatcher.submit(rec)
        return rec


class TrafficAgent(Agent):
    name = "TRAFFIC"
    team = "Traffic authority"

    def step(self, tick, log):
        # Coordination: Emergency asks for a cleared corridor to an incident.
        for s in self.bus.drain_signals({"emergency_dispatch"}):
            key = ("corridor", s["zone"])
            if self.is_open(key):
                continue
            log(fmt(self.name, "DECIDE", f"green-wave corridor to {s['zone']} for "
                    f"{s['service']} (signal from EMERGENCY)"))
            self._open(tick, "action", s["zone"], f"emergency corridor to {s['zone']}",
                       key=key, priority=2, duration=DURATION["traffic_action"])
            self.services.dispatch(self.team, f"Green-wave corridor to {s['zone']} for {s['service']}")
            self.actions += 1

        for z in ZONES:
            r = self.sensors.traffic_reading(z)
            if r["accident"]:
                key = ("accident", z)
                if self.is_open(key):
                    log(fmt(self.name, "SENSE", f"accident in {z} still being cleared"))
                    continue
                self.detected += 1
                log(fmt(self.name, "DETECT", f"accident in {z} (congestion {r['congestion']:.2f})"))
                log(fmt(self.name, "DECIDE", "reroute + clear corridor; escalate to Emergency"))
                self._open(tick, "incident", z, f"clear accident in {z}", key=key,
                           priority=2, duration=DURATION["accident_clear"],
                           on_close=lambda rec, z=z: self.sensors.end_event("accident", z))
                self.bus.publish_signal({"type": "traffic_accident", "zone": z})
                self.services.dispatch(self.team, f"Accident in {z}: corridor cleared, signals overridden")
                self.actions += 1
            elif r["congestion"] >= 0.7:
                key = ("congestion", z)
                if self.is_open(key):
                    continue
                self.detected += 1
                log(fmt(self.name, "DETECT", f"congestion in {z} ({r['congestion']:.2f})"))
                log(fmt(self.name, "DECIDE", "alternative route via arterial; adjust signals"))
                self._open(tick, "action", z, f"signal timing +12s, reroute in {z}", key=key,
                           priority=3, duration=DURATION["traffic_action"])
                self.services.dispatch(self.team, f"Congestion in {z}: signals adjusted, reroute advised")
                self.actions += 1
            else:
                log(fmt(self.name, "SENSE", f"{z} flowing normally ({r['congestion']:.2f})"))


class WasteAgent(Agent):
    name = "WASTE"
    team = "Waste-collection team"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.history: dict[str, list[float]] = {z: [] for z in ZONES}
        self.assigned: set[str] = set()      # zones already on a live route

    def _forecast(self, zone: str, reading: dict):
        """Estimate fill-rate from recent readings and time-to-overflow."""
        h = self.history[zone]
        if h and reading["fill"] < h[-1] - 1:      # bin was emptied: restart
            h.clear()
        h.append(reading["fill"])
        recent = h[-4:]
        deltas = [b - a for a, b in zip(recent, recent[1:])]
        rate = max(mean(deltas) if deltas else reading["rate"], 0.1)
        return rate, (100 - reading["fill"]) / rate

    def step(self, tick, log):
        rows = []
        for z in ZONES:
            r = self.sensors.bin_reading(z)
            rate, eta = self._forecast(z, r)
            rows.append((z, r["fill"], rate, eta))
            flag = ("FULL" if r["fill"] >= 80 else
                    "overflow risk" if eta <= 2 else
                    "filling" if r["fill"] >= 60 else "ok")
            log(fmt(self.name, "SENSE",
                    f"{z:<9} bin {r['fill']:>5.1f}%  +{rate:.1f}%/cycle  "
                    f"full in ~{eta * 30:.0f} min  {flag}"))
        due = [z for z, fill, _, eta in rows
               if (fill >= 80 or eta <= 2) and z not in self.assigned]
        if not due:
            msg = ("collection in progress, nothing new to add" if self.assigned
                   else "no collection needed this cycle")
            log(fmt(self.name, "DECIDE", msg))
            return
        self.detected += len(due)
        # Forecast-driven batching: while a truck is going out anyway, also
        # pick up bins predicted to overflow within ~2 hours.
        extra = [(z, eta) for z, fill, _, eta in rows
                 if z not in due and z not in self.assigned and eta <= 4]
        for z, eta in extra:
            log(fmt(self.name, "PREDICT", f"{z} will overflow in ~{eta * 30:.0f} min: adding to this trip"))
        due = due + [z for z, _ in extra]
        route, km = optimise_route(due)
        naive_km = round(route_distance([z for z in ZONES if z in due]), 1)
        urgent = any(fill >= 90 for z, fill, _, _ in rows if z in due)
        duration = max(DURATION["waste_min"], math.ceil(km / KM_PER_CYCLE))
        log(fmt(self.name, "DETECT", f"{len(due)} bin(s) full or about to overflow: {', '.join(due)}"))
        log(fmt(self.name, "OPTIMISE",
                f"route {' -> '.join(route)}  {km} km (fixed order would be {naive_km} km)"))
        self.assigned |= set(route)

        def done(rec, zones=tuple(route)):
            self.sensors.empty_bins(zones)
            self.assigned -= set(zones)

        self._open(tick, "work_order", "route", f"collect: {' -> '.join(route)}",
                   key=("collect", tuple(sorted(route))), priority=3 if urgent else 4,
                   duration=duration, on_close=done,
                   meta={"km": km, "naive_km": naive_km})
        self.services.dispatch(self.team, f"Optimised route: {' -> '.join(route)} ({km} km)")
        self.actions += 1


class EnergyAgent(Agent):
    name = "ENERGY"
    team = "Facilities / energy desk"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.det = {z: EwmaDetector(floor=12.0) for z in ZONES}

    def step(self, tick, log):
        for z in ZONES:
            kwh = self.sensors.energy_reading(z)["kwh"]
            det = self.det[z]
            score = det.score(kwh)
            spike = score >= det.k
            log(fmt(self.name, "SENSE",
                    f"{z:<9} {kwh:>6.1f} kWh  {'SPIKE' if spike else 'normal'}  (score {score:+.1f})"))
            if not spike:
                det.update(kwh)
                continue
            key = ("energy", z)
            if self.is_open(key):
                continue
            self.detected += 1
            saved = round(kwh * 0.22, 1)       # modelled estimate, not measured
            log(fmt(self.name, "DETECT",
                    f"abnormal consumption in {z} ({kwh:.1f} vs baseline {det.mean:.1f} kWh)"))
            log(fmt(self.name, "PREDICT", "demand will stay high; load-shift advised"))
            log(fmt(self.name, "ACT",
                    f"dim non-critical lighting + shift load (~{saved} kWh saved)"))
            self._open(tick, "action", z, f"energy-saving in {z} (~{saved} kWh)", key=key,
                       priority=3, duration=DURATION["energy_action"],
                       on_close=lambda rec, z=z: self.sensors.end_event("energy_spike", z),
                       meta={"kwh_saved": saved})
            self.services.dispatch(self.team, f"Anomaly in {z}: saving action applied")
            self.actions += 1


class WaterAgent(Agent):
    name = "WATER"
    team = "Maintenance crew"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.p_det = {z: EwmaDetector(floor=1.0) for z in ZONES}
        self.f_det = {z: EwmaDetector(floor=3.0) for z in ZONES}

    def step(self, tick, log):
        for z in ZONES:
            r = self.sensors.water_reading(z)
            pd, fd = self.p_det[z], self.f_det[z]
            leak = (pd.score(r["pressure"]) <= -pd.k or fd.score(r["flow"]) >= fd.k
                    or r["flow"] > 120 or r["pressure"] < 45)    # safety-net limits
            if not leak:
                pd.update(r["pressure"])
                fd.update(r["flow"])
                log(fmt(self.name, "SENSE",
                        f"{z:<9} pressure {r['pressure']} psi, flow {r['flow']} L/s  ok"))
                continue
            key = ("leak", z)
            if self.is_open(key):
                log(fmt(self.name, "SENSE", f"{z} leak still active, repair already ordered"))
                continue
            self.detected += 1
            log(fmt(self.name, "DETECT",
                    f"possible leak in {z} (pressure {r['pressure']} psi, flow {r['flow']} L/s)"))
            log(fmt(self.name, "LOCATE", f"segment isolated to {z} distribution line"))
            self._open(tick, "work_order", z, f"leak repair in {z}", key=key,
                       priority=2, duration=DURATION["leak_repair"],
                       on_close=lambda rec, z=z: self.sensors.end_event("leak", z))
            self.services.dispatch(self.team, f"Leak suspected in {z}: work order raised")
            self.actions += 1


class EmergencyAgent(Agent):
    name = "EMERGENCY"
    team = "Emergency responders"

    def step(self, tick, log):
        incidents = []                       # (zone, kind, service, priority, key)
        for s in self.bus.drain_signals({"traffic_accident"}):
            incidents.append((s["zone"], "accident", "Police", 1, ("accident", s["zone"])))
        r = self.sensors.emergency_reading()
        for z in r["fires"]:
            incidents.append((z, "fire", "Fire service", 1, ("fire", z)))
        for z, desc in r["reports"]:
            incidents.append((z, desc, "Fire service", 3, ("report", z, desc)))   # unverified

        new = [i for i in incidents if not self.is_open(i[4])]
        if not incidents:
            log(fmt(self.name, "SENSE", "no active incidents"))
        elif not new:
            log(fmt(self.name, "SENSE", "active incidents already being handled"))
        for zone, kind, service, prio, key in new:
            self.detected += 1
            log(fmt(self.name, "DETECT", f"{kind} reported in {zone}  [{PRIORITY_NAME[prio]}]"))
            log(fmt(self.name, "ROUTE", f"relevant service: {service}"))
            on_close = None
            if kind == "fire":
                on_close = lambda rec, z=zone: self.sensors.end_event("fire", z)
            self._open(tick, "incident", zone, f"{kind} in {zone} -> {service}", key=key,
                       priority=prio, duration=DURATION["emergency"], on_close=on_close)
            self.services.dispatch(self.team, f"[P{prio}] {service}: {kind} at {zone}")
            if kind != "accident":           # Traffic already owns accident zones
                self.bus.publish_signal({"type": "emergency_dispatch",
                                         "zone": zone, "service": service})
            self.actions += 1


# ---------------------------------------------------------------------------
# Simulation runner + metrics
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    lines: list[str]
    summary: dict
    summary_start: int
    bus: MessageBus
    agents: list[Agent]
    dispatcher: Dispatcher
    services: CityServices
    sensors: SensorGrid


def build_summary(cfg, agents, bus, dispatcher, services) -> dict:
    recs = bus.records
    by_priority = {}
    for p in sorted({r.priority for r in recs}):
        rs = [r for r in recs if r.priority == p]
        cl = [r for r in rs if r.status == "closed"]
        waits = [r.first_started_tick - r.opened_tick for r in rs if r.first_started_tick is not None]
        by_priority[PRIORITY_NAME[p]] = {
            "raised": len(rs), "resolved": len(cl),
            "avg_wait_cycles": round(mean(waits), 2) if waits else None,
            "avg_resolution_cycles": round(mean(r.closed_tick - r.opened_tick for r in cl), 2) if cl else None,
        }
    teams = {}
    for t in TEAMS:
        cap, cyc = dispatcher.capacity[t], max(dispatcher.cycles, 1)
        teams[t] = {"units": cap, "messages": len(services.inbox[t]),
                    "utilisation_pct": round(100 * dispatcher.busy_slot_cycles[t] / (cap * cyc), 1),
                    "max_queue": dispatcher.max_queue[t]}
    closed = [r for r in recs if r.status == "closed"]
    waste = [r for r in recs if "km" in r.meta]
    energy = [r for r in closed if "kwh_saved" in r.meta]
    return {
        "config": {"seed": cfg.seed, "ticks": cfg.ticks, "random_events": cfg.random_events,
                   "event_rate": cfg.event_rate, "capacity": cfg.capacity},
        "agents": {a.name: {"detected": a.detected, "actions": a.actions} for a in agents},
        "totals": {
            "detected": sum(a.detected for a in agents), "actions": sum(a.actions for a in agents),
            "records": len(recs), "resolved": len(closed),
            "still_open": sum(1 for r in recs if r.status != "closed"),
            "preemptions": sum(r.preemptions for r in recs),
            "avg_resolution_cycles": round(mean(r.closed_tick - r.opened_tick for r in closed), 2) if closed else None,
        },
        "by_priority": by_priority,
        "teams": teams,
        "waste_routing": {"routes": len(waste),
                          "km_planned": round(sum(r.meta["km"] for r in waste), 1),
                          "km_if_fixed_order": round(sum(r.meta["naive_km"] for r in waste), 1),
                          "km_saved": round(sum(r.meta["naive_km"] - r.meta["km"] for r in waste), 1)},
        "energy": {"est_kwh_saved": round(sum(r.meta["kwh_saved"] for r in energy), 1)},
    }


def run(cfg: Config | None = None, echo: bool = True) -> RunResult:
    cfg = cfg or Config()
    sensors = SensorGrid(cfg)
    bus, services = MessageBus(), CityServices()
    dispatcher = Dispatcher(cfg.capacity)
    agents = [cls(sensors, bus, services, dispatcher) for cls in
              (TrafficAgent, WasteAgent, EnergyAgent, WaterAgent, EmergencyAgent)]

    lines: list[str] = []

    def log(msg: str):
        lines.append(msg)
        if echo:
            print(msg)

    log("=" * 82)
    log("AGENTIC AI SMART CITY v2  -  simulation run")
    log(f"zones: {', '.join(ZONES)}")
    log(f"{cfg.ticks} cycles of 30 min from {int(cfg.start_hour):02d}:00 | seed {cfg.seed} | "
        f"{'random' if cfg.random_events else 'scripted'} events")
    log("crews: " + ", ".join(f"{TEAM_CODE[t]}x{n}" for t, n in cfg.capacity.items()))
    log("=" * 82)

    for tick in range(1, cfg.ticks + 1):
        sensors.advance(tick)
        hour = cfg.start_hour + tick * 0.5
        log("")
        log(f"--- CYCLE {tick:02d}  {int(hour):02d}:{int((hour % 1) * 60):02d} " + "-" * 56)
        dispatcher.complete(tick, log)      # finished jobs change the world first
        for agent in agents:
            agent.step(tick, log)
        dispatcher.allocate(tick, log)

    summary = build_summary(cfg, agents, bus, dispatcher, services)
    out = log
    start = len(lines)
    out("")
    out("=" * 82)
    out("RUN SUMMARY")
    out("=" * 82)
    for name, a in summary["agents"].items():
        out(f"  {name:<10} detected {a['detected']:>2}  |  actions {a['actions']:>2}")
    t = summary["totals"]
    out(f"  {'TOTAL':<10} detected {t['detected']:>2}  |  actions {t['actions']:>2}")
    out("")
    out(f"  records {t['records']} | resolved {t['resolved']} | still open {t['still_open']} "
        f"| pre-emptions {t['preemptions']}")
    if t["avg_resolution_cycles"] is not None:
        out(f"  avg resolution : {t['avg_resolution_cycles']} cycles (~{t['avg_resolution_cycles'] * 30:.0f} min)")
    out("")
    out("  by priority (wait = cycles until a crew started):")
    for name, p in summary["by_priority"].items():
        out(f"    {name:<9} raised {p['raised']:>2}  resolved {p['resolved']:>2}  "
            f"avg wait {p['avg_wait_cycles']}  avg resolution {p['avg_resolution_cycles']}")
    out("")
    out("  team load:")
    for team, v in summary["teams"].items():
        out(f"    {team:<26} {v['units']} unit(s)  util {v['utilisation_pct']:>5}%  "
            f"max queue {v['max_queue']}  msgs {v['messages']}")
    w = summary["waste_routing"]
    out("")
    out(f"  waste routing  : {w['routes']} route(s), {w['km_planned']} km planned vs "
        f"{w['km_if_fixed_order']} km fixed-order -> {w['km_saved']} km saved")
    out(f"  energy         : ~{summary['energy']['est_kwh_saved']} kWh saved (modelled estimate)")
    out("=" * 82)
    return RunResult(lines, summary, start, bus, agents, dispatcher, services, sensors)


def export(result: RunResult, out_dir: str) -> list[str]:
    """Write the log, a metrics JSON and a per-record CSV. Returns the paths."""
    os.makedirs(out_dir, exist_ok=True)
    paths = {
        "log": os.path.join(out_dir, "smart_city_run_output.txt"),
        "json": os.path.join(out_dir, "run_metrics.json"),
        "csv": os.path.join(out_dir, "records.csv"),
    }
    with open(paths["log"], "w") as fh:
        fh.write("\n".join(result.lines) + "\n")
    with open(paths["json"], "w") as fh:
        json.dump(result.summary, fh, indent=2)
    cols = ["uid", "agent", "kind", "team", "target", "detail", "priority", "status", "unit",
            "opened_tick", "first_started_tick", "closed_tick", "duration", "preemptions"]
    with open(paths["csv"], "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in result.bus.records:
            w.writerow([getattr(r, c) for c in cols])
    return list(paths.values())


def main(argv=None):
    ap = argparse.ArgumentParser(description="Agentic AI Smart City simulation v2")
    ap.add_argument("--config", help="JSON file overriding defaults")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--ticks", type=int)
    ap.add_argument("--random-events", action="store_true", help="seeded random events instead of the scripted demo")
    ap.add_argument("--event-rate", type=float)
    ap.add_argument("--out-dir", default="run_output")
    ap.add_argument("--quiet", action="store_true", help="print only the summary")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config) if args.config else Config()
    if args.seed is not None:
        cfg.seed = args.seed
    if args.ticks is not None:
        cfg.ticks = args.ticks
    if args.random_events:
        cfg.random_events = True
    if args.event_rate is not None:
        cfg.event_rate = args.event_rate

    result = run(cfg, echo=not args.quiet)
    if args.quiet:
        print("\n".join(result.lines[result.summary_start:]))
    paths = export(result, args.out_dir)
    print("\n[wrote] " + ", ".join(paths))


if __name__ == "__main__":
    main()
