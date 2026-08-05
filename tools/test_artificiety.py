"""Stdlib-only tests: `python -m unittest tools.test_artificiety`."""

import unittest

from .artificiety import ArtificietyError, Client


class UnwrapTest(unittest.TestCase):
    """A failed envelope carries `"data": null` — unwrapping must not hide it."""

    def setUp(self):
        self.client = Client.__new__(Client)  # no env/credentials needed

    def test_returns_data_on_success(self):
        self.assertEqual(
            self.client._unwrap({"success": True, "data": {"ok": 1}, "error": None}),
            {"ok": 1})

    def test_null_data_on_success_is_an_empty_dict_not_none(self):
        self.assertEqual(self.client._unwrap({"success": True, "data": None}), {})

    def test_validation_failure_raises_with_the_reason(self):
        with self.assertRaises(ArtificietyError) as caught:
            self.client._unwrap({
                "success": False, "data": None, "_httpstatus": 400,
                "error": {"error": "validation",
                          "message": "message must be at most 500 characters"},
            })
        self.assertIn("at most 500 characters", str(caught.exception))
        self.assertIn("400", str(caught.exception))

    def test_network_error_raises(self):
        with self.assertRaises(ArtificietyError):
            self.client._unwrap({"_neterror": "connection refused"})


class FakeClient:
    """Replays a scripted sequence of `look()` payloads; records issued actions."""

    def __init__(self, looks, action_result=None):
        self.looks = list(looks)
        self.actions = []
        self._action_result = action_result or {}

    def action(self, payload):
        self.actions.append(payload)
        return {"actionResult": self._action_result, **self._peek()}

    def look(self):
        return self.looks.pop(0) if len(self.looks) > 1 else self.looks[0]

    def _peek(self):
        return self.looks[0]


def _look(x, y, waypoint=None, entities=()):
    return {"surroundings": {"x": x, "y": y, "nearbyEntities": list(entities)},
            "waypoint": waypoint, "engaged": None, "instructions": [], "events": []}


class TravelArrivalTest(unittest.TestCase):
    """`waypoint is None` means navigation ended — not that we got there."""

    def setUp(self):
        from . import helpers
        self.helpers = helpers
        helpers.TICK_SECONDS = 0

    def test_route_cancelled_far_from_target_is_not_arrived(self):
        c = FakeClient([_look(148, 135, waypoint=None)])
        r = self.helpers.travel_to(c, x=134, y=126, max_hops=1)
        self.assertEqual(r["status"], "ended_short")
        self.assertEqual(r["tilesAway"], 14)

    def test_standing_on_the_tile_is_arrived(self):
        c = FakeClient([_look(134, 126, waypoint=None)])
        self.assertEqual(self.helpers.travel_to(c, x=134, y=126)["status"], "arrived")

    def test_entity_target_needs_adjacency(self):
        far = _look(10, 10, waypoint=None, entities=[{"id": "e1", "distance": 6}])
        self.assertEqual(self.helpers.travel_to(FakeClient([far]), entity_id="e1",
                                                max_hops=1)["status"], "ended_short")
        near = _look(10, 10, waypoint=None, entities=[{"id": "e1", "distance": 1}])
        self.assertEqual(self.helpers.travel_to(FakeClient([near]),
                                                entity_id="e1")["status"], "arrived")


class InterruptContractTest(unittest.TestCase):
    """Every bounded loop hands back on the FIRST snapshot that carries a decision point.

    Six consecutive review rounds each found another loop or another observation point
    that ran the action first and noticed the instruction afterwards. This pins the
    contract for all of them at once, so a new loop that forgets it fails here rather
    than in review.
    """

    def setUp(self):
        from . import helpers
        self.helpers = helpers
        self._tick = helpers.TICK_SECONDS
        helpers.TICK_SECONDS = 0

    def tearDown(self):
        self.helpers.TICK_SECONDS = self._tick

    @staticmethod
    def _snapshot(**extra):
        data = {
            "instructions": [{"id": "i1"}],
            "health": {"current": 90, "max": 100},
            "energy": {"energy": 90, "maxEnergy": 100, "resting": True},
            "hunger": {"hunger": 90, "maxHunger": 100},
            "inventory": {"items": [{"itemId": "bread", "quantity": 9}],
                          "usedSlots": 0, "maxSlots": 10},
            "surroundings": {"nearbyEntities": [
                {"id": "n1", "type": "RESOURCE", "distance": 1, "interactions": ["CHOP"]},
                {"id": "wolf", "type": "CREATURE", "distance": 1,
                 "creatureInfo": {"aggressive": True}}]},
            "currentActivity": None,
        }
        data.update(extra)
        return data

    def _client(self):
        snapshot = self._snapshot()
        sent = []

        class C:
            def action(self, payload):
                sent.append(payload)
                return snapshot

            def look(self):
                return snapshot

        return C(), sent

    def test_every_loop_returns_instruction_from_its_opening_snapshot(self):
        for name, call in (
            ("gather", lambda c: self.helpers.gather(c, "n1")),
            ("rest_until", lambda c: self.helpers.rest_until(c, energy=99)),
            ("eat", lambda c: self.helpers.eat(c, "bread")),
            ("fight", lambda c: self.helpers.fight(c, "wolf")),
            ("travel_to", lambda c: self.helpers.travel_to(c, x=1, y=1)),
        ):
            with self.subTest(loop=name):
                client, _sent = self._client()
                self.assertEqual(call(client)["status"], "instruction")

    def test_rest_until_prefers_the_instruction_over_an_already_met_target(self):
        # energy is at 90 and the target is 99 -> 'reached' would otherwise win and
        # the instruction would be dropped without the caller ever seeing it.
        client, _sent = self._client()
        self.assertEqual(self.helpers.rest_until(client, energy=50)["status"], "instruction")

    def test_fight_does_not_strike_before_handing_back(self):
        client, sent = self._client()
        self.helpers.fight(client, "wolf")
        self.assertEqual([p for p in sent if p.get("interaction") == "ATTACK"], [])


class FightBudgetTest(unittest.TestCase):
    """fight honours ONE tick ceiling across approach retries plus the watch loop."""

    def setUp(self):
        from . import helpers
        self.helpers = helpers
        self._tick, self._travel = helpers.TICK_SECONDS, helpers._travel
        helpers.TICK_SECONDS = 0

    def tearDown(self):
        self.helpers.TICK_SECONDS = self._tick
        self.helpers._travel = self._travel

    def test_approach_retries_share_the_budget(self):
        handed_out = []

        def fake_travel(client, x, y, entity_id, max_ticks, *rest):
            handed_out.append(max_ticks)
            rest[-1][0] = max_ticks           # the whole allowance is consumed
            return {"status": "arrived"}

        self.helpers._travel = fake_travel

        class C:
            def action(self, payload):
                return {"actionResult": {"success": False, "message": "You are too far."}}

            def look(self):
                return {"health": {"current": 90, "max": 100}, "inventory": {"items": []},
                        "surroundings": {"nearbyEntities": [
                            {"id": "wolf", "type": "CREATURE", "distance": 9,
                             "creatureInfo": {"aggressive": True}}]}}

        self.helpers.fight(C(), "wolf", max_ticks=40, approach_tries=4)
        self.assertLessEqual(sum(handed_out), 40,
                             "approach retries must not each get a fresh max_ticks")


if __name__ == "__main__":
    unittest.main()


class TravelHopShrinkTest(unittest.TestCase):
    """A refused hop must get SHORTER, never be re-asked verbatim from the same tile."""

    def setUp(self):
        from . import helpers
        self.helpers = helpers
        helpers.TICK_SECONDS = 0

    def _blocked_client(self):
        # never moves, always refuses: a wall between here and the destination
        return FakeClient([_look(142, 126)],
                          action_result={"reason": "OUT_OF_RANGE",
                                         "message": "route there is too complex"})

    def test_hops_shrink_and_never_repeat_a_destination(self):
        c = self._blocked_client()
        res = self.helpers.travel_to(c, x=130, y=126, hop_tiles=18, max_hops=6)
        hops = [(a["x"], a["y"]) for a in c.actions if "x" in a]
        self.assertEqual(res["status"], "out_of_range")
        self.assertEqual(len(hops), len(set(hops)), f"repeated identical hops: {hops}")
        spans = [abs(hx - 142) for hx, _ in hops]
        self.assertEqual(spans, sorted(spans, reverse=True), f"spans did not shrink: {spans}")

    def test_gives_up_once_even_one_tile_is_refused(self):
        c = self._blocked_client()
        self.helpers.travel_to(c, x=130, y=126, hop_tiles=18, max_hops=99)
        # bails on terrain instead of burning the whole (huge) hop allowance
        self.assertLess(len(c.actions), 10, f"burned {len(c.actions)} requests on a wall")

    def test_a_hop_that_gains_ground_restores_full_span(self):
        moved = FakeClient([_look(142, 126), _look(136, 126)],
                           action_result={"reason": "OUT_OF_RANGE", "message": "too complex"})
        self.helpers.travel_to(moved, x=130, y=126, hop_tiles=18, max_hops=3)
        self.assertGreaterEqual(len([a for a in moved.actions if "x" in a]), 2)


class TravelReaimStallTest(unittest.TestCase):
    """Re-aiming from the tile a route already ended short on learns nothing."""

    def setUp(self):
        from . import helpers
        self.helpers = helpers
        helpers.TICK_SECONDS = 0

    def test_reaim_stops_once_it_stops_gaining_ground(self):
        # waypoint always None, position never changes: destination tile is blocked
        c = FakeClient([_look(117, 99, waypoint=None)])
        res = self.helpers.travel_to(c, x=116, y=100, max_hops=6)
        self.assertEqual(res["status"], "ended_short")
        self.assertEqual(res["tilesAway"], 1)
        self.assertLessEqual(len(c.actions), 2, f"re-aimed {len(c.actions)}x from one tile")
