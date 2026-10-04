"""Unit tests for smart_city_prototype_v2.  Run:  python3 -m unittest -v test_smart_city"""
import json
import os
import random
import tempfile
import unittest

import smart_city_prototype_v2 as sc


def make_record(team="Maintenance crew", priority=3, duration=2, opened=1, detail="job"):
    return sc.Record(tick=opened, agent="T", kind="work_order", target="X", detail=detail,
                     team=team, priority=priority, duration=duration, remaining=duration,
                     opened_tick=opened)


class TestDetector(unittest.TestCase):
    def test_flags_spike_but_not_noise(self):
        rng = random.Random(1)
        det = sc.EwmaDetector(floor=5.0)
        false_alarms = 0
        for _ in range(300):
            x = 100 + rng.uniform(-6, 6)
            if abs(det.score(x)) >= det.k:
                false_alarms += 1
            else:
                det.update(x)
        self.assertEqual(false_alarms, 0)
        self.assertGreaterEqual(det.score(180), det.k)

    def test_follows_slow_drift(self):
        det = sc.EwmaDetector(floor=2.0)
        for i in range(200):
            x = 50 + i * 0.5                    # slow upward drift
            self.assertLess(abs(det.score(x)), det.k, f"drift flagged at step {i}")
            det.update(x)

    def test_no_baseline_means_no_alarm(self):
        self.assertEqual(sc.EwmaDetector(floor=1.0).score(1e9), 0.0)


class TestRouting(unittest.TestCase):
    def test_exact_route_is_never_worse_than_any_order(self):
        import itertools
        stops = ["North", "East", "South", "West", "Riverside"]
        route, km = sc.optimise_route(stops)
        best = min(sc.route_distance(p) for p in itertools.permutations(stops))
        self.assertAlmostEqual(km, round(best, 1), places=1)
        self.assertEqual(sorted(route), sorted(stops))

    def test_heuristic_branch_beats_naive_on_many_stops(self):
        rng = random.Random(3)
        coords = {f"S{i}": (rng.uniform(-10, 10), rng.uniform(-10, 10)) for i in range(12)}
        stops = list(coords)
        route, km = sc.optimise_route(stops, coords=coords)
        naive = sc.route_distance(stops, coords=coords)
        self.assertEqual(sorted(route), sorted(stops))
        self.assertLess(km, round(naive, 1))

    def test_single_and_empty(self):
        self.assertEqual(sc.optimise_route([])[0], [])
        self.assertEqual(sc.optimise_route(["East"])[0], ["East"])


class TestDispatcher(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.log = self.logs.append

    def test_priority_order_and_wait(self):
        d = sc.Dispatcher({t: 1 for t in sc.TEAMS})
        low, high = make_record(priority=4, opened=1), make_record(priority=2, opened=1)
        for i, r in enumerate((low, high)):
            r.uid = i
            d.submit(r)
        d.allocate(1, self.log)
        self.assertEqual(high.status, "active")      # higher priority goes first
        self.assertEqual(low.status, "queued")

    def test_job_closes_only_after_duration(self):
        d = sc.Dispatcher({t: 1 for t in sc.TEAMS})
        r = make_record(duration=3)
        d.submit(r)
        d.allocate(1, self.log)
        for t in (2, 3):
            d.complete(t, self.log)
            self.assertEqual(r.status, "active")
        d.complete(4, self.log)
        self.assertEqual((r.status, r.closed_tick), ("closed", 4))

    def test_preemption_and_resume(self):
        d = sc.Dispatcher({t: 1 for t in sc.TEAMS})
        low = make_record(priority=3, duration=3, opened=1)
        low.uid = 0
        d.submit(low)
        d.allocate(1, self.log)
        crit = make_record(priority=1, duration=1, opened=2)
        crit.uid = 1
        d.submit(crit)
        d.complete(2, self.log)
        d.allocate(2, self.log)
        self.assertEqual(crit.status, "active")
        self.assertEqual((low.status, low.preemptions, low.remaining), ("queued", 1, 2))
        d.complete(3, self.log)                      # crit done
        d.allocate(3, self.log)
        self.assertEqual(low.status, "active")       # resumed with remaining work only
        d.complete(5, self.log)
        self.assertEqual(low.status, "closed")

    def test_small_priority_gap_does_not_preempt(self):
        d = sc.Dispatcher({t: 1 for t in sc.TEAMS})
        a, b = make_record(priority=2), make_record(priority=1)
        a.uid, b.uid = 0, 1
        d.submit(a)
        d.allocate(1, self.log)
        d.submit(b)
        d.allocate(2, self.log)
        self.assertEqual((a.status, b.status), ("active", "queued"))


class TestSimulation(unittest.TestCase):
    def test_reproducible(self):
        a = sc.run(sc.Config(seed=5), echo=False).lines
        b = sc.run(sc.Config(seed=5), echo=False).lines
        self.assertEqual(a, b)

    def test_every_agent_fires_in_scripted_demo(self):
        r = sc.run(sc.Config(), echo=False)
        for agent in r.agents:
            self.assertGreaterEqual(agent.detected, 1, agent.name)

    def test_cross_agent_coordination_both_ways(self):
        r = sc.run(sc.Config(), echo=False)
        text = "\n".join(r.lines)
        self.assertIn("accident reported in North", text)         # Traffic -> Emergency
        self.assertIn("signal from EMERGENCY", text)              # Emergency -> Traffic

    def test_persistent_fault_gets_exactly_one_work_order(self):
        r = sc.run(sc.Config(), echo=False)
        leaks = [x for x in r.bus.records if x.agent == "WATER"]
        self.assertEqual(len(leaks), 1)
        self.assertTrue(any("still active" in l for l in r.lines))

    def test_world_responds_to_completed_work(self):
        r = sc.run(sc.Config(), echo=False)
        self.assertNotIn(("leak", "Riverside"), r.sensors.active)  # repaired
        self.assertNotIn(("fire", "Central"), r.sensors.active)    # extinguished
        # a pickup really empties the bins: the first completed collection
        # (Central -> Riverside ...) must be followed by a low fill reading
        import re
        cycle, closed_cycle, fills = 0, None, []
        for line in r.lines:
            m = re.match(r"--- CYCLE (\d+)", line)
            if m:
                cycle = int(m.group(1))
            if closed_cycle is None and "TRACK" in line and "collect:" in line:
                closed_cycle = cycle
            if closed_cycle == cycle and line.startswith("WASTE") and "SENSE" in line and "Central" in line:
                fills.append(float(re.search(r"bin\s+([\d.]+)%", line).group(1)))
        self.assertTrue(fills, "no collection completed")
        self.assertLess(fills[0], 20.0)

    def test_demo_shows_preemption(self):
        r = sc.run(sc.Config(), echo=False)
        self.assertGreaterEqual(r.summary["totals"]["preemptions"], 1)

    def test_no_false_alarms_when_nothing_happens(self):
        for seed in range(25):
            r = sc.run(sc.Config(seed=seed, ticks=30, random_events=True, event_rate=0.0), echo=False)
            for a in r.agents:
                if a.name in ("ENERGY", "WATER", "EMERGENCY"):
                    self.assertEqual(a.detected, 0, f"{a.name} false alarm, seed {seed}")

    def test_record_invariants_under_stress(self):
        for seed in range(15):
            r = sc.run(sc.Config(seed=seed, ticks=40, random_events=True, event_rate=0.3), echo=False)
            for rec in r.bus.records:
                if rec.status == "closed":
                    self.assertGreaterEqual(rec.first_started_tick, rec.opened_tick)
                    self.assertGreaterEqual(rec.closed_tick, rec.first_started_tick + 1)
            for team, slots in r.dispatcher.slots.items():
                self.assertLessEqual(sum(1 for s in slots if s), r.dispatcher.capacity[team])


class TestConfigAndExport(unittest.TestCase):
    def test_config_override_and_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w") as fh:
                json.dump({"ticks": 4, "capacity": {"Maintenance crew": 3}}, fh)
            cfg = sc.Config.load(p)
            self.assertEqual((cfg.ticks, cfg.capacity["Maintenance crew"]), (4, 3))
            self.assertEqual(cfg.capacity["Emergency responders"], 1)   # untouched default
            with open(p, "w") as fh:
                json.dump({"nonsense": 1}, fh)
            with self.assertRaises(ValueError):
                sc.Config.load(p)
            with open(p, "w") as fh:
                json.dump({"capacity": {"Maintenance crew": 0}}, fh)
            with self.assertRaises(ValueError):
                sc.Config.load(p)

    def test_more_crews_reduce_waiting(self):
        base = dict(seed=3, ticks=30, random_events=True, event_rate=0.3)
        tight = sc.run(sc.Config(**base), echo=False).summary["teams"]["Emergency responders"]["max_queue"]
        cfg = sc.Config(**base)
        cfg.capacity["Emergency responders"] = 4
        roomy = sc.run(cfg, echo=False).summary["teams"]["Emergency responders"]["max_queue"]
        self.assertLess(roomy, tight)

    def test_export_files(self):
        r = sc.run(sc.Config(), echo=False)
        with tempfile.TemporaryDirectory() as d:
            paths = sc.export(r, d)
            self.assertTrue(all(os.path.getsize(p) > 0 for p in paths))
            with open(os.path.join(d, "run_metrics.json")) as fh:
                data = json.load(fh)
            for key in ("agents", "totals", "by_priority", "teams", "waste_routing", "energy"):
                self.assertIn(key, data)
            with open(os.path.join(d, "records.csv")) as fh:
                rows = fh.read().strip().splitlines()
            self.assertEqual(len(rows) - 1, len(r.bus.records))


if __name__ == "__main__":
    unittest.main()
