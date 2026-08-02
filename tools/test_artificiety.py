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


if __name__ == "__main__":
    unittest.main()
