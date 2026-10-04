"""
Agentic AI Smart City - working prototype
=========================================

A self-contained simulation of the five city agents described in the design:

    Cameras + IoT Sensors  ->  Agentic AI (5 agents)  ->  City Services

Each agent follows the shared rhythm:  SENSE -> DETECT -> DECIDE -> ACT -> TRACK.

This is a demonstration model: sensors are simulated with a seeded random
generator so a run is fully reproducible. Run it with:

    python3 smart_city_prototype.py

Author: prototype for the Agentic AI Smart City concept.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from statistics import mean

SEED = 42
random.seed(SEED)

ZONES = ["Central", "North", "East", "West", "South", "Riverside"]
TICKS = 10  # each tick = 30 minutes of city time
START_HOUR = 7.0


# ---------------------------------------------------------------------------
# Shared infrastructure: the message bus and city services
# ---------------------------------------------------------------------------

@dataclass
class Record:
    """A single action / work order / incident raised by an agent."""
    tick: int
    agent: str
    kind: str          # action | recommendation | work_order | incident | alert
    target: str        # zone or team
    detail: str
    status: str = "open"
    opened_tick: int = 0
    closed_tick: int | None = None


class CityServices:
    """The outside world: authorities, crews and responders the agents act on."""

    def __init__(self):
        self.inbox: dict[str, list[str]] = {
            "Traffic authority": [],
            "Waste-collection team": [],
            "Facilities / energy desk": [],
            "Maintenance crew": [],
            "Emergency responders": [],
        }

    def dispatch(self, team: str, message: str):
        self.inbox[team].append(message)


class MessageBus:
    """Shared memory so agents can coordinate (e.g. traffic feeds emergency)."""

    def __init__(self):
        self.records: list[Record] = []
        self.signals: list[dict] = []   # cross-agent signals for the same tick

    def emit(self, rec: Record):
        self.records.append(rec)

    def publish_signal(self, signal: dict):
        self.signals.append(signal)

    def drain_signals(self) -> list[dict]:
        out = self.signals[:]
        self.signals.clear()
        return out


# ---------------------------------------------------------------------------
# Simulated sensing layer
# ---------------------------------------------------------------------------

class SensorGrid:
    """Produces simulated camera / IoT readings for the current tick."""

    def __init__(self):
        self.tick = 0
        self.bin_fill = {z: random.uniform(25, 55) for z in ZONES}
        self.bin_rate = {z: random.uniform(5.0, 9.0) for z in ZONES}  # %/tick
        self.base_energy = {z: random.uniform(80, 140) for z in ZONES}  # kWh
        self.pressure = {z: random.uniform(48, 55) for z in ZONES}  # psi
        self.flow = {z: random.uniform(95, 110) for z in ZONES}  # L/s
        # pre-scripted events so the demo always shows every agent firing
        self.scripted = {
            2: {"accident": "North"},
            3: {"leak": "Riverside"},
            5: {"fire": "Central"},
            6: {"citizen_report": ("East", "smoke near market")},
            7: {"energy_spike": "West"},
        }

    def advance(self, tick: int):
        self.tick = tick
        # bins fill gradually
        for z in ZONES:
            self.bin_fill[z] = min(100, self.bin_fill[z] + self.bin_rate[z])
        # energy: gentle daily curve + noise
        for z in ZONES:
            shape = 1.0 + 0.35 * (tick / TICKS)
            self.base_energy[z] = max(30, self.base_energy[z] * 0.6 + random.uniform(70, 150) * shape * 0.4)
        # water pressure / flow drift
        for z in ZONES:
            self.pressure[z] += random.uniform(-0.8, 0.8)
            self.flow[z] += random.uniform(-3, 3)

    # ---- readings -----------------------------------------------------
    def traffic_reading(self, zone: str) -> dict:
        base = random.uniform(0.2, 0.5)
        if random.random() < 0.16:
            base = random.uniform(0.7, 0.95)  # congestion builds
        reading = {"zone": zone, "congestion": round(base, 2), "accident": False}
        ev = self.scripted.get(self.tick, {})
        if ev.get("accident") == zone:
            reading["accident"] = True
            reading["congestion"] = 0.9
        return reading

    def bin_reading(self, zone: str) -> dict:
        return {"zone": zone, "fill": round(self.bin_fill[zone], 1),
                "rate": round(self.bin_rate[zone], 2)}

    def energy_reading(self, zone: str) -> dict:
        val = self.base_energy[zone]
        ev = self.scripted.get(self.tick, {})
        if ev.get("energy_spike") == zone:
            val *= 2.4  # abnormal spike
        return {"zone": zone, "kwh": round(val, 1)}

    def water_reading(self, zone: str) -> dict:
        p, f = self.pressure[zone], self.flow[zone]
        ev = self.scripted.get(self.tick, {})
        if ev.get("leak") == zone:
            p -= 9.0     # pressure drop
            f += 28.0    # unexplained flow
        return {"zone": zone, "pressure": round(p, 1), "flow": round(f, 1)}

    def emergency_reading(self) -> dict:
        ev = self.scripted.get(self.tick, {})
        out = {"fire_zone": None, "citizen_report": None}
        if ev.get("fire"):
            out["fire_zone"] = ev["fire"]
        if ev.get("citizen_report"):
            out["citizen_report"] = ev["citizen_report"]
        return out


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

class Agent:
    name = "Agent"
    team = "City services"

    def __init__(self, sensors: SensorGrid, bus: MessageBus, services: CityServices):
        self.sensors = sensors
        self.bus = bus
        self.services = services
        self.detected = 0
        self.actions = 0

    def step(self, tick: int, log):
        raise NotImplementedError

    # helpers -----------------------------------------------------------
    def _open(self, tick, kind, target, detail) -> Record:
        rec = Record(tick=tick, agent=self.name, kind=kind, target=target,
                     detail=detail, opened_tick=tick)
        self.bus.emit(rec)
        return rec

    def resolve_due(self, tick, log, after_ticks: int, kinds=("work_order", "incident")):
        """TRACK: close items this agent owns once enough ticks have passed."""
        for rec in self.bus.records:
            if (rec.agent == self.name and rec.status == "open"
                    and rec.kind in kinds and tick - rec.opened_tick >= after_ticks):
                rec.status = "closed"
                rec.closed_tick = tick
                log(f"{self.name:<9} TRACK    resolved: {rec.detail}  (opened T{rec.opened_tick}, closed T{tick})")


class TrafficAgent(Agent):
    name = "TRAFFIC"
    team = "Traffic authority"

    def step(self, tick, log):
        for z in ZONES:
            r = self.sensors.traffic_reading(z)
            if r["accident"]:
                self.detected += 1
                log(f"{self.name:<9} DETECT   accident in {z} (congestion {r['congestion']:.2f})")
                log(f"{self.name:<9} DECIDE   reroute + clear corridor; escalate to Emergency")
                self._open(tick, "incident", z, f"accident in {z}")
                self.bus.publish_signal({"type": "traffic_accident", "zone": z})
                self.services.dispatch(self.team, f"Accident in {z}: corridor cleared, signals overridden")
                self.actions += 1
            elif r["congestion"] >= 0.7:
                self.detected += 1
                log(f"{self.name:<9} DETECT   congestion in {z} ({r['congestion']:.2f})")
                log(f"{self.name:<9} DECIDE   alternative route via arterial; adjust signals")
                self._open(tick, "action", z, f"signal timing +12s, reroute traffic in {z}")
                self.services.dispatch(self.team, f"Congestion in {z}: signals adjusted, reroute advised")
                self.actions += 1
            else:
                log(f"{self.name:<9} SENSE    {z} flowing normally ({r['congestion']:.2f})")
        self.resolve_due(tick, log, after_ticks=2, kinds=("incident", "action"))


class WasteAgent(Agent):
    name = "WASTE"
    team = "Waste-collection team"

    def step(self, tick, log):
        readings = [self.sensors.bin_reading(z) for z in ZONES]
        full = [r for r in readings if r["fill"] >= 80]
        soon = [r for r in readings if 60 <= r["fill"] < 80]
        for r in readings:
            flag = "FULL" if r["fill"] >= 80 else ("filling" if r["fill"] >= 60 else "ok")
            log(f"{self.name:<9} SENSE    {r['zone']:<9} bin {r['fill']:>5.1f}%  {flag}")
        if full:
            self.detected += len(full)
            route = self._optimise_route([r["zone"] for r in full] + [r["zone"] for r in soon[:1]])
            log(f"{self.name:<9} DETECT   {len(full)} bin(s) near capacity: {', '.join(r['zone'] for r in full)}")
            log(f"{self.name:<9} PREDICT  {len(soon)} more bin(s) filling soon")
            log(f"{self.name:<9} OPTIMISE route: {' -> '.join(route)}")
            self._open(tick, "work_order", "route", f"collect: {' -> '.join(route)}")
            self.services.dispatch(self.team, f"Optimised route: {' -> '.join(route)}")
            self.actions += 1
        else:
            log(f"{self.name:<9} DECIDE   no collection needed this cycle")

    @staticmethod
    def _optimise_route(zones: list[str]) -> list[str]:
        # greedy nearest-neighbour over zone order (deterministic demo)
        remaining = [z for z in ZONES if z in set(zones)]
        return remaining if remaining else zones


class EnergyAgent(Agent):
    name = "ENERGY"
    team = "Facilities / energy desk"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.history: list[float] = []

    def step(self, tick, log):
        readings = [self.sensors.energy_reading(z) for z in ZONES]
        vals = [r["kwh"] for r in readings]
        self.history.extend(vals)
        baseline = mean(self.history) if self.history else mean(vals)
        for r in readings:
            spike = r["kwh"] > baseline * 1.6
            tag = "SPIKE" if spike else "normal"
            log(f"{self.name:<9} SENSE    {r['zone']:<9} {r['kwh']:>6.1f} kWh  {tag}")
            if spike:
                self.detected += 1
                saved = round(r["kwh"] * 0.22, 1)
                log(f"{self.name:<9} DETECT   abnormal consumption in {r['zone']} ({r['kwh']:.1f} vs {baseline:.1f} kWh)")
                log(f"{self.name:<9} PREDICT  demand will stay high; load-shift advised")
                log(f"{self.name:<9} ACT      dim non-critical lighting + shift load (~{saved} kWh saved)")
                self._open(tick, "action", r["zone"], f"energy-saving applied in {r['zone']} (~{saved} kWh)")
                self.services.dispatch(self.team, f"Anomaly in {r['zone']}: saving action applied")
                self.actions += 1
        self.resolve_due(tick, log, after_ticks=3, kinds=("action",))


class WaterAgent(Agent):
    name = "WATER"
    team = "Maintenance crew"

    def step(self, tick, log):
        for z in ZONES:
            r = self.sensors.water_reading(z)
            leak = r["flow"] > 120 or r["pressure"] < 45
            if leak:
                self.detected += 1
                log(f"{self.name:<9} DETECT   possible leak in {z} (pressure {r['pressure']} psi, flow {r['flow']} L/s)")
                log(f"{self.name:<9} LOCATE   segment isolated to {z} distribution line")
                self._open(tick, "work_order", z, f"leak repair in {z}")
                self.services.dispatch(self.team, f"Leak suspected in {z}: work order raised")
                self.actions += 1
            else:
                log(f"{self.name:<9} SENSE    {z:<9} pressure {r['pressure']} psi, flow {r['flow']} L/s  ok")
        self.resolve_due(tick, log, after_ticks=3, kinds=("work_order",))


class EmergencyAgent(Agent):
    name = "EMERGENCY"
    team = "Emergency responders"

    def step(self, tick, log):
        # INGEST from cameras/sensors AND from other agents via the bus
        signals = self.bus.drain_signals()
        incidents = []
        for s in signals:
            if s["type"] == "traffic_accident":
                incidents.append((s["zone"], "accident", "Police"))
        r = self.sensors.emergency_reading()
        if r["fire_zone"]:
            incidents.append((r["fire_zone"], "fire", "Fire service"))
        if r["citizen_report"]:
            zone, desc = r["citizen_report"]
            incidents.append((zone, desc, "Fire service"))

        if not incidents:
            log(f"{self.name:<9} SENSE    no active incidents")
        for zone, kind, service in incidents:
            self.detected += 1
            unit = f"{service.split()[0][:2].upper()}-{random.randint(10, 99)}"
            log(f"{self.name:<9} DETECT   {kind} reported in {zone}")
            log(f"{self.name:<9} ROUTE    relevant service: {service}")
            log(f"{self.name:<9} DISPATCH unit {unit} to {zone} with location + incident data")
            self._open(tick, "incident", zone, f"{kind} in {zone} -> {service} ({unit})")
            self.services.dispatch(self.team, f"{service}: {kind} at {zone}, unit {unit} dispatched")
            self.actions += 1
        self.resolve_due(tick, log, after_ticks=2, kinds=("incident",))


# ---------------------------------------------------------------------------
# Simulation runner
# ---------------------------------------------------------------------------

def run():
    sensors = SensorGrid()
    bus = MessageBus()
    services = CityServices()
    agents = [
        TrafficAgent(sensors, bus, services),
        WasteAgent(sensors, bus, services),
        EnergyAgent(sensors, bus, services),
        WaterAgent(sensors, bus, services),
        EmergencyAgent(sensors, bus, services),
    ]

    lines: list[str] = []

    def log(msg: str):
        lines.append(msg)
        print(msg)

    log("=" * 78)
    log("AGENTIC AI SMART CITY  -  simulation run")
    log(f"zones: {', '.join(ZONES)}")
    log(f"{TICKS} cycles of 30 min, starting {int(START_HOUR):02d}:00")
    log("=" * 78)

    for tick in range(1, TICKS + 1):
        sensors.advance(tick)
        hour = START_HOUR + tick * 0.5
        hh, mm = int(hour), int((hour % 1) * 60)
        log("")
        log(f"--- CYCLE {tick:02d}  {hh:02d}:{mm:02d} " + "-" * 52)
        for agent in agents:
            agent.step(tick, log)

    # ---- final report -------------------------------------------------
    log("")
    log("=" * 78)
    log("RUN SUMMARY")
    log("=" * 78)
    total_detected = sum(a.detected for a in agents)
    total_actions = sum(a.actions for a in agents)
    for a in agents:
        log(f"  {a.name:<10} detected {a.detected:>2}  |  actions {a.actions:>2}")
    log(f"  {'TOTAL':<10} detected {total_detected:>2}  |  actions {total_actions:>2}")

    closed = [r for r in bus.records if r.status == "closed"]
    open_ = [r for r in bus.records if r.status == "open"]
    res_times = [r.closed_tick - r.opened_tick for r in closed if r.closed_tick is not None]
    log("")
    log(f"  records raised : {len(bus.records)}")
    log(f"  resolved       : {len(closed)}")
    log(f"  still open     : {len(open_)}")
    if res_times:
        log(f"  avg resolution : {mean(res_times):.1f} cycles (~{mean(res_times)*30:.0f} min)")
    log("")
    log("  city-services inbox (what teams received):")
    for team, msgs in services.inbox.items():
        log(f"    - {team:<26} {len(msgs)} message(s)")
    log("=" * 78)

    return "\n".join(lines)


if __name__ == "__main__":
    output = run()
    with open("smart_city_run_output.txt", "w") as fh:
        fh.write(output + "\n")
    print("\n[output also written to smart_city_run_output.txt]")
