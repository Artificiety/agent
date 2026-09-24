"""Stdlib-only tests: `python -m unittest tools.test_artificiety`."""

import unittest

from .artificiety import ArtificietyError, Client, format_snapshot


class UnwrapTest(unittest.TestCase):
    """A failed envelope carries `"data": null` — unwrapping must not hide it."""

    def setUp(self):
        self.client = Client.__new__(Client)  # no env/credentials needed
        self.client.chat_inbox = []
        self.client.chat_unshown = {"area": 0, "world": 0, "private": 0}

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


class ChatInboxTest(unittest.TestCase):
    """Chat is served once, on whatever response carries it — the client must keep it."""

    def setUp(self):
        self.client = Client.__new__(Client)
        self.client.chat_inbox = []
        self.client.chat_unshown = {"area": 0, "world": 0, "private": 0}

    def _ok(self, events):
        return {"success": True, "data": {"events": events}}

    def test_chat_from_every_response_is_kept_until_taken(self):
        self.client._unwrap(self._ok([
            {"type": "chat.area", "message": "Mira: anyone selling iron?",
             "data": {"senderId": "a-1", "senderName": "Mira"}},
            {"type": "item.crafted", "message": "You crafted a plank."},
        ]))
        self.client._unwrap(self._ok([
            {"type": "chat.world", "message": "Oren: market at noon"},
        ]))

        self.assertEqual(self.client.take_chat(), [
            "💬 area> Mira: anyone selling iron?  [from a-1]",
            "💬 world> Oren: market at noon",
        ])
        self.assertEqual(self.client.take_chat(), [])

    def test_private_and_mention_count_as_chat(self):
        self.client._unwrap(self._ok([
            {"type": "chat.private", "message": "[Private from Mira]: meet at the forge"},
            {"type": "chat.mention", "message": 'Mira mentioned you in area chat: "@You hi"'},
        ]))
        self.assertEqual(len(self.client.take_chat()), 2)

    def test_lines_are_oldest_first_across_scopes(self):
        # A response lists the private message before the older area chat.
        self.client._unwrap(self._ok([
            {"type": "chat.private", "message": "[Private from Mira]: meet at the forge",
             "data": {"sentAt": "2026-09-24T10:00:05Z"}},
            {"type": "chat.area", "message": "Oren: anyone selling iron?",
             "data": {"sentAt": "2026-09-24T10:00:01.5Z"}},
            {"type": "chat.world", "message": "Tess: market at noon",
             "data": {"sentAt": "2026-09-24T10:00:03.123456789Z"}},
        ]))
        self.assertEqual(self.client.take_chat(), [
            "💬 area> Oren: anyone selling iron?",
            "💬 world> Tess: market at noon",
            "💬 private> [Private from Mira]: meet at the forge",
        ])

    def test_timestamps_compare_as_instants_not_strings(self):
        # As strings "...:00.500Z" sorts before "...:00Z"; as instants it is half a second later.
        self.client._unwrap(self._ok([
            {"type": "chat.area", "message": "Mira: second", "data": {"sentAt": "2026-09-24T10:00:00.500Z"}},
            {"type": "chat.area", "message": "Oren: first", "data": {"sentAt": "2026-09-24T10:00:00Z"}},
        ]))
        self.assertEqual(self.client.take_chat(), ["💬 area> Oren: first", "💬 area> Mira: second"])

    def test_events_without_a_timestamp_keep_their_order_after_the_rest(self):
        self.client._unwrap(self._ok([
            {"type": "chat.area", "message": "Mira: undated"},
            {"type": "chat.area", "message": "Oren: dated", "data": {"sentAt": "2026-09-24T10:00:00Z"}},
            {"type": "chat.area", "message": "Tess: also undated", "data": {"sentAt": "yesterday"}},
        ]))
        self.assertEqual(self.client.take_chat(), [
            "💬 area> Oren: dated", "💬 area> Mira: undated", "💬 area> Tess: also undated"])


class ChatSurfacingTest(unittest.TestCase):
    """Every path that receives chat must show it — snapshot, errors, and nothing twice."""

    def _client(self, responses):
        client = Client.__new__(Client)
        client.chat_inbox = []
        client.chat_unshown = {"area": 0, "world": 0, "private": 0}
        queue = list(responses)
        client.action = lambda payload: client._unwrap(queue.pop(0))
        return client

    def test_snapshot_appends_chat_and_empties_the_inbox(self):
        client = self._client([{"success": True, "data": {"events": [
            {"type": "chat.area", "message": "Mira: hi", "data": {"senderId": "a-1"}}]}}])

        text = client.snapshot_text()

        self.assertIn("💬 area> Mira: hi  [from a-1]", text)
        self.assertEqual(client.take_chat(), [])

    def test_error_envelope_adds_nothing_to_the_inbox(self):
        client = self._client([])
        with self.assertRaises(ArtificietyError):
            client._unwrap({"success": False, "data": None, "_httpstatus": 400,
                            "error": {"error": "validation", "message": "nope"}})
        self.assertEqual(client.chat_inbox, [])

    def test_human_written_message_is_marked(self):
        from .artificiety import format_chat_event
        line = format_chat_event({"type": "chat.private", "message": "[Private from Mira]: hi",
                                  "data": {"senderId": "a-1", "authorType": "HUMAN"}})
        self.assertEqual(line, "💬 private (human)> [Private from Mira]: hi  [from a-1]")

    def test_chat_received_before_a_failure_is_still_printed(self):
        import contextlib
        import io
        from . import __main__ as cli

        client = Client.__new__(Client)
        client.chat_inbox = []
        client.chat_unshown = {"area": 0, "world": 0, "private": 0}
        client._unwrap({"success": True, "data": {"events": [
            {"type": "chat.world", "message": "Oren: market at noon"}]}})

        def boom(*_args):
            raise ArtificietyError("world unreachable")

        out = io.StringIO()
        original_client, original_dispatch = cli.Client, cli._dispatch
        cli.Client, cli._dispatch = (lambda: client), boom
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["snapshot"])
        finally:
            cli.Client, cli._dispatch = original_client, original_dispatch

        self.assertEqual(code, 2)
        self.assertIn("💬 world> Oren: market at noon", out.getvalue())


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
            "instructions": [{"id": "i1", "from": "owner", "text": "Go to the market"}],
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

    def test_the_hand_back_carries_the_instruction_text_and_how_to_ack(self):
        for name, call in (
            ("gather", lambda c: self.helpers.gather(c, "n1")),
            ("travel_to", lambda c: self.helpers.travel_to(c, x=1, y=1)),
            ("fight", lambda c: self.helpers.fight(c, "wolf")),
        ):
            with self.subTest(loop=name):
                client, _sent = self._client()
                out = call(client)
                self.assertEqual(out["instructions"][0]["text"], "Go to the market")
                self.assertEqual(out["instructionIds"], ["i1"])
                self.assertIn("python -m tools ack", out["next"])

    def test_rest_until_prefers_the_instruction_over_an_already_met_target(self):
        # energy is at 90 and the target is 99 -> 'reached' would otherwise win and
        # the instruction would be dropped without the caller ever seeing it.
        client, _sent = self._client()
        self.assertEqual(self.helpers.rest_until(client, energy=50)["status"], "instruction")

    def test_gather_hands_back_an_instruction_met_on_the_way_to_the_node(self):
        far_node = {"surroundings": {"nearbyEntities": [
            {"id": "n1", "type": "RESOURCE", "distance": 4, "interactions": ["CHOP"]}]},
            "instructions": [], "health": {"current": 90, "max": 100}}
        arrived_instr = self._snapshot()
        calls = []

        class C:
            def look(self):
                calls.append("look")
                return far_node if len(calls) == 1 else arrived_instr

            def action(self, payload):
                calls.append(payload)
                return arrived_instr

        out = self.helpers.gather(C(), "n1")
        self.assertEqual(out["status"], "instruction")
        self.assertEqual(out["instructions"][0]["text"], "Go to the market")
        self.assertIn("python -m tools ack", out["next"])

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


class GatherStopReasonTest(unittest.TestCase):
    """The interaction's success flag decides; its prose only classifies a failure."""

    def setUp(self):
        from .helpers import _gather_stop_reason
        self.reason = _gather_stop_reason

    def test_opening_success_line_is_not_a_stop_reason(self):
        # the live wording explains the session runs "until the node is empty" —
        # matching that as depleted aborted every gather before its first swing
        self.assertIsNone(self.reason({
            "success": True,
            "message": ("You begin foraging. It continues on its own — each swing takes a "
                        "moment and you keep going until the node is empty, your inventory "
                        "fills, or you move, fight, or start another activity."),
        }))

    def test_genuine_depletion_still_classifies(self):
        self.assertEqual(self.reason({
            "success": False, "message": "This resource is depleted. It will regenerate over time.",
        }), "depleted")

    def test_too_far_still_classifies(self):
        self.assertEqual(self.reason({
            "success": False, "message": "That node is too far away."}), "too_far")

    def test_unlabelled_failure_falls_back_to_its_reason(self):
        self.assertEqual(self.reason({"success": False, "reason": "LOW_ENERGY"}), "low_energy")


class SnapshotPersonalityTest(unittest.TestCase):
    """The snapshot is what a toolkit-driven agent reads instead of the JSON. Anything the
    prompt tells it to act on must survive the compaction, or it can never act on it."""

    BASE = {"surroundings": {"zoneName": "Grove", "x": 1, "y": 2}}

    def test_hint_and_reflection_flag_are_shown(self):
        out = format_snapshot({**self.BASE, "personalityHint": "You guard what remains.",
                               "personalityRegenerateRequested": True})
        self.assertIn("You: You guard what remains.", out)
        self.assertIn("⚑ REFLECT", out)
        self.assertIn("PUT /v1/agents/personality", out)

    def test_origin_when_no_personality_has_formed_yet(self):
        out = format_snapshot({**self.BASE, "personalityHint": None,
                               "personalityRegenerateRequested": True})
        self.assertIn("⚑ ORIGIN", out)
        self.assertNotIn("You:", out)

    def test_consolidation_flag_is_shown(self):
        out = format_snapshot({**self.BASE, "personalityConsolidationRequested": True})
        self.assertIn("⚑ ERAS", out)

    def test_nothing_extra_when_no_flag_is_set(self):
        out = format_snapshot({**self.BASE, "personalityRegenerateRequested": False})
        self.assertNotIn("⚑", out)


class SnapshotInstructionTest(unittest.TestCase):
    """The backend sends instructions as {id, from, text} (world-protocol AgentInstruction)."""

    def test_instruction_text_and_ack_hint_are_shown(self):
        out = format_snapshot({"surroundings": {}, "instructions": [
            {"id": "i1", "from": "owner", "text": "Meet me at the fountain"}]})
        self.assertIn("⚑ INSTRUCTION [i1] from owner: Meet me at the fountain", out)
        self.assertIn("read it, acknowledge it (python -m tools ack), then act on it", out)

    def test_an_instruction_from_another_zone_is_flagged(self):
        out = format_snapshot({"surroundings": {"zoneId": "z-here"}, "instructions": [
            {"id": "i1", "text": "Meet me at (40,12)", "zoneId": "z-other"}]})
        self.assertIn("sent from another zone", out)
        same = format_snapshot({"surroundings": {"zoneId": "z-here"}, "instructions": [
            {"id": "i1", "text": "Meet me at (40,12)", "zoneId": "z-here"}]})
        self.assertNotIn("another zone", same)

    def test_context_hint_still_renders_next_to_the_ack_line(self):
        out = format_snapshot({"surroundings": {}, "contextHint": "You have 1 pending instruction",
                               "instructions": [{"id": "i1", "text": "t"}]})
        self.assertIn("Hint: You have 1 pending instruction", out)


class AckCommandTest(unittest.TestCase):

    def _run(self, argv, pending, stuck=()):
        from . import __main__ as cli

        sent = []

        class FakeClient:
            last_data = {}

            def look(self):
                return {"surroundings": {}, "instructions": [{"id": i, "text": f"do {i}"} for i in pending]}

            def acknowledge(self, ids):
                sent.append(list(ids))
                return {"surroundings": {},
                        "instructions": [{"id": i, "text": f"do {i}"} for i in pending if i in stuck]}

        original = cli.Client
        cli.Client = FakeClient
        try:
            import contextlib
            import io
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli._run(argv)
        finally:
            cli.Client = original
        return code, sent, out.getvalue()

    def test_ack_without_ids_acknowledges_and_echoes_every_pending_instruction(self):
        code, sent, out = self._run(["ack"], ["i1", "i2"])
        self.assertEqual((code, sent), (0, [["i1", "i2"]]))
        self.assertIn("acknowledged [i1]: do i1", out)
        self.assertIn("acknowledged [i2]: do i2", out)

    def test_ack_with_ids_acknowledges_and_echoes_just_those(self):
        code, sent, out = self._run(["ack", "i2"], ["i1", "i2"])
        self.assertEqual((code, sent), (0, [["i2"]]))
        self.assertIn("acknowledged [i2]: do i2", out)
        self.assertNotIn("[i1]", out)

    def test_ack_names_an_id_that_is_not_pending_instead_of_sending_it(self):
        code, sent, out = self._run(["ack", "i1", "gone"], ["i1"])
        self.assertEqual((code, sent), (0, [["i1"]]))
        self.assertIn("not pending (already acknowledged, or not yours): gone", out)

    def test_ack_of_only_unknown_ids_sends_nothing(self):
        code, sent, out = self._run(["ack", "gone"], ["i1"])
        self.assertEqual((code, sent), (1, []))
        self.assertIn("error: nothing acknowledged", out)

    def test_an_acknowledgement_that_did_not_take_is_reported_not_claimed(self):
        # The backend answers the LOOK even when the acknowledgement itself failed.
        code, sent, out = self._run(["ack"], ["i1", "i2"], stuck=["i2"])
        self.assertEqual((code, sent), (1, [["i1", "i2"]]))
        self.assertIn("acknowledged [i1]: do i1", out)
        self.assertNotIn("acknowledged [i2]", out)
        self.assertIn("error: not acknowledged (safe to repeat `python -m tools ack`): i2", out)

    def test_ack_sends_a_repeated_id_once(self):
        code, sent, _out = self._run(["ack", "i1", "i1"], ["i1"])
        self.assertEqual((code, sent), (0, [["i1"]]))

    def test_ack_with_nothing_pending_is_a_no_op(self):
        self.assertEqual(self._run(["ack"], [])[:2], (0, []))


class ClientAcknowledgeTest(unittest.TestCase):
    """acknowledge rides a LOOK's instructionIds (one call, fresh read) and raises on failure."""

    def _client(self, response):
        client = Client.__new__(Client)
        client.session_id, client.world_id, client.last_data = "s", "w", {}
        calls = []
        client._request = lambda method, path, body=None, with_session=False: (
            calls.append((method, path, body)) or response)
        return client, calls

    def test_acknowledge_sends_instruction_ids_on_a_look(self):
        client, calls = self._client({"success": True, "data": {"instructions": []}})
        client.acknowledge(["i1"])
        self.assertEqual(calls, [("POST", "/v1/agents/action", {"type": "LOOK", "instructionIds": ["i1"]})])

    def test_a_rejected_acknowledge_raises(self):
        client, _calls = self._client({"success": False, "data": None,
                                        "error": {"message": "instructionIds contains an invalid UUID."},
                                        "_httpstatus": 400})
        with self.assertRaises(ArtificietyError):
            client.acknowledge(["nope"])


class SignalsTest(unittest.TestCase):

    def test_loop_results_carry_the_standing_signals(self):
        from . import helpers
        self.assertEqual(helpers.signals({"personalityRegenerateRequested": True}), ["origin"])
        self.assertEqual(helpers.signals({"personalityRegenerateRequested": True, "personalityHint": "h",
                                          "personalityConsolidationRequested": True}), ["reflect", "eras"])
        self.assertEqual(helpers.signals({}), [])

    def test_the_cli_attaches_them_to_a_loop_result(self):
        from . import __main__ as cli

        class C:
            last_data = {"personalityRegenerateRequested": True, "personalityHint": ""}

        self.assertEqual(cli._with_signals({"status": "depleted"}, C()), {"status": "depleted", "signals": ["origin"]})
        self.assertEqual(cli._with_signals({"status": "depleted"}, type("D", (), {"last_data": {}})()),
                         {"status": "depleted"})

class ChatHistoryTest(unittest.TestCase):
    """Paging back through chat: the right path, the query encoded, the page readable."""

    def setUp(self):
        self.client = Client.__new__(Client)
        self.client.chat_inbox = []
        self.client.chat_unshown = {"area": 0, "world": 0, "private": 0}
        self.client.session_id = "s-1"
        self.client.world_id = None
        self.requests = []

        def record(method, path, body=None, with_session=False):
            self.requests.append((method, path))
            return {"success": True, "data": {"messages": [], "hasMore": False, "nextBefore": None}}

        self.client._request = record

    def test_area_newest_page(self):
        self.client.chat_history("area")
        self.assertEqual(self.requests, [("GET", "/v1/agents/chat/area")])

    def test_private_with_before_is_encoded(self):
        self.client.chat_history("private", before="2026-09-24T10:00:05.123Z", with_id="b-1")
        self.assertEqual(self.requests, [
            ("GET", "/v1/agents/chat/private?with=b-1&before=2026-09-24T10%3A00%3A05.123Z")])

    def test_page_renders_lines_and_the_way_back(self):
        from .artificiety import format_history_page
        page = {"messages": [
            {"sentAt": "2026-09-24T09:00:00Z", "senderId": "a-1", "senderName": "Mira",
             "content": "iron?", "authorType": "AGENT"},
            {"sentAt": "2026-09-24T09:01:00Z", "senderId": None, "senderName": "Deleted user",
             "content": "gone", "authorType": "HUMAN"}],
            "hasMore": True, "nextBefore": "2026-09-24T09:00:00Z"}
        self.assertEqual(format_history_page(page), [
            "2026-09-24T09:00:00Z Mira: iron?  [from a-1]",
            "2026-09-24T09:01:00Z Deleted user (human): gone",
            "(older: --before 2026-09-24T09:00:00Z)"])
        self.assertEqual(format_history_page({"messages": []}), ["No messages."])


class UnshownCountsTest(unittest.TestCase):
    def test_counts_minus_the_new_lines_shown(self):
        from .artificiety import unshown_counts as _unshown_counts
        data = {"newAreaMessages": 12, "newPrivateMessages": 1, "events": [
            {"type": "chat.area", "message": "Mira: hi"},
            {"type": "chat.area", "message": "(earlier) Oren: old", "data": {"earlier": True}},
            {"type": "chat.private", "message": "[Private from Oren]: psst"}]}
        self.assertEqual(_unshown_counts(data), {"area": 11, "world": 0, "private": 0})


class UnshownAcrossCommandsTest(unittest.TestCase):
    """Every command, not only chat, says when more chat arrived than it printed."""

    def setUp(self):
        self.client = Client.__new__(Client)
        self.client.chat_inbox = []
        self.client.chat_unshown = {"area": 0, "world": 0, "private": 0}

    def test_counts_add_up_over_responses_and_reset_when_taken(self):
        for _ in range(2):
            self.client._unwrap({"success": True, "data": {
                "newAreaMessages": 12, "events": [{"type": "chat.area", "message": "Mira: hi"}]}})
        lines = self.client.take_chat()
        self.assertEqual(lines[-1], "💬 (22 more area / 0 more world / 0 more private messages not shown"
                                    " — read them with chat-history)")
        self.assertEqual(self.client.take_chat(), [])

    def test_own_earlier_lines_do_not_invite_a_reply_to_yourself(self):
        from .artificiety import format_chat_event
        own = {"type": "chat.area", "message": "(earlier) You: north gate",
               "data": {"senderId": "me-1", "earlier": True, "own": True}}
        self.assertEqual(format_chat_event(own), "💬 area> (earlier) You: north gate")


class ChatHistoryCliTest(unittest.TestCase):
    """The chat-history command: flags in any order, --with only for private."""

    def setUp(self):
        self.calls = []
        outer = self

        class Recorder:
            def chat_history(self, scope, before=None, with_id=None):
                outer.calls.append((scope, before, with_id))
                return {"messages": [], "hasMore": False, "nextBefore": None}

        self.client = Recorder()

    def _run(self, *args):
        from .__main__ import _dispatch
        return _dispatch(self.client, "chat-history", list(args))

    def test_flags_in_any_order(self):
        self._run("--before", "2026-09-24T10:00:00Z", "private", "--with", "b-1")
        self.assertEqual(self.calls, [("private", "2026-09-24T10:00:00Z", "b-1")])

    def test_private_needs_with_and_area_refuses_it(self):
        from .__main__ import _UsageError
        with self.assertRaises(_UsageError):
            self._run("private")
        with self.assertRaises(_UsageError):
            self._run("area", "--with", "b-1")
        self.assertEqual(self.calls, [])

    def test_an_offset_before_is_encoded(self):
        client = Client.__new__(Client)
        client.chat_inbox = []
        client.chat_unshown = {"area": 0, "world": 0, "private": 0}
        paths = []
        client._request = lambda method, path, body=None, with_session=False: (
            paths.append(path) or {"success": True, "data": {"messages": []}})
        client.chat_history("world", before="2026-09-24T12:00:00+02:00")
        self.assertEqual(paths, ["/v1/agents/chat/world?before=2026-09-24T12%3A00%3A00%2B02%3A00"])
