#!/usr/bin/env python3
"""Unit tests for instance_bot.py (stdlib only: python3 -m unittest)."""
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import instance_bot as ib


class CommandValidationTests(unittest.TestCase):
    def test_valid_commands(self):
        for cmd in [
            "status",
            "exit",
            "shutdown now",
            "chat bcast hello",
            "chat direct abc123 hello",
            "redirect identity abc123 tcp:2101:0",
            "redirect identity abc123 tcp:2101:1:secret",
            "kick identity abc123 bye",
            "join approve somejoinid",
            "join approvespec somejoinid",
            "set host ready 1",
            "admin add-public-key abc123",
        ]:
            self.assertTrue(ib.is_valid_wz_command(cmd), cmd)

    def test_rejected_commands(self):
        for cmd in [
            "",
            "  status",
            "status ",
            "rm -rf /",
            "redirect identity",
            "chat bcast",
            "join approve",
            "shutdown now please",
            "chat bcast line\ninjected",
        ]:
            self.assertFalse(ib.is_valid_wz_command(cmd), cmd)


class MapInfoTests(unittest.TestCase):
    def test_known_map(self):
        info = ib.extract_map_info("NTW_3v3Full")
        self.assertIsNotNone(info)
        self.assertGreater(info["players"], 0)

    def test_unknown_map(self):
        self.assertIsNone(ib.extract_map_info("No_Such_Map_xyz"))


class PortTests(unittest.TestCase):
    def test_none_when_full(self):
        import socket

        class BoundSock:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def connect_ex(self, addr):
                return 0  # everything bound

        with mock.patch.object(socket, "socket", lambda *a, **k: BoundSock()):
            self.assertIsNone(ib.find_available_port(2100))


class ConfigMapTests(unittest.TestCase):
    def setUp(self):
        self._old = ib.config

    def tearDown(self):
        ib.config = self._old

    def test_configured_list_is_authoritative(self):
        ib.config = {"maps": [{"name": "A", "mods": ["m1"]}, "B"]}
        self.assertEqual(ib.get_available_maps(), ["A", "B"])
        self.assertEqual(ib.get_map_entry("A"), {"name": "A", "mods": ["m1"]})
        self.assertIsNone(ib.get_map_entry("ZZZ"))

    def test_dir_fallback_without_key(self):
        ib.config = {}
        maps = ib.get_available_maps()
        self.assertIn("NTW_3v3Full", maps)

    def test_validate_missing_map_and_mod(self):
        ib.config = {"map_name": "Ghost", "maps": [{"name": "Ghost", "mods": ["nope"]}],
                     "authorized_pkeys": []}
        errors = ib.validate_config()
        self.assertTrue(any("Ghost.wz" in e for e in errors))
        self.assertTrue(any("nope.wz" in e for e in errors))

    def test_mod_name_safety(self):
        self.assertTrue(ib.is_safe_mod_name("Cool-Mod_v2.wz".replace(".wz", "")))
        for bad in ["../evil", "a/b", "", ".", "..", "x;y", "x y"]:
            self.assertFalse(ib.is_safe_mod_name(bad), bad)


class VoteTests(unittest.TestCase):
    def setUp(self):
        ib.vote_state = None
        ib.roster = {}
        ib.process = None  # send_cmd becomes a no-op returning False

    def tearDown(self):
        ib.vote_state = None
        ib.roster = {}
        ib.process = None

    def _mk_state(self):
        timer = mock.Mock()
        ib.vote_state = {
            "target_pos": 2,
            "target_pk": "PK2",
            "target_name": "Bob",
            "initiator_pos": 0,
            "initiator_pk": "PK0",
            "eligible": {0, 1},
            "votes": {0: True},
            "timer": timer,
        }
        return timer

    def test_unanimous_yes_kicks(self):
        timer = self._mk_state()
        ib.roster = {
            "PK0": {"type": "player", "name": "Ann", "pos": 0, "is_spec": False},
            "PK1": {"type": "player", "name": "Cat", "pos": 1, "is_spec": False},
            "PK2": {"type": "player", "name": "Bob", "pos": 2, "is_spec": False},
        }
        with ib.vote_lock:
            ib._resolve_vote(timed_out=False)
        self.assertIsNone(ib.vote_state)
        timer.cancel.assert_called()

    def test_single_no_blocks_kick(self):
        self._mk_state()
        ib.roster = {
            "PK0": {"type": "player", "name": "Ann", "pos": 0, "is_spec": False},
            "PK1": {"type": "player", "name": "Cat", "pos": 1, "is_spec": False},
            "PK2": {"type": "player", "name": "Bob", "pos": 2, "is_spec": False},
        }
        ib.register_vote(1, False)
        self.assertIsNone(ib.vote_state)  # all voted -> resolved as FAILED
        self.assertIsNone(ib.process)  # no kick issued (process is None anyway)


class JoinApprovalTests(unittest.TestCase):
    def test_approve_format(self):
        sent = []
        with mock.patch.object(ib, "send_cmd", lambda cmd, log_command=True: sent.append(cmd) or True):
            ib.process = mock.Mock()
            ib.process.stdin = mock.Mock()
            ib.on_join_approval("WZEVENT: join approval needed: jid123 1.2.3.4 hash pk bmFtZQ== play")
        ib.process = None
        self.assertEqual(sent, ["join approve jid123"])

    def test_malformed_ignored(self):
        with mock.patch.object(ib, "send_cmd") as sc:
            ib.on_join_approval("WZEVENT: join approval needed: ")
            sc.assert_not_called()


class RestartCapTests(unittest.TestCase):
    def test_cap_blocks_fourth_restart(self):
        import time as _time

        ib.config = {"max_restarts_per_hour": 3}
        now = _time.time()
        with mock.patch.object(ib, "_load_restart_times", return_value=[]):
            with mock.patch("os.makedirs"), \
                 mock.patch("builtins.open", mock.mock_open()), \
                 mock.patch("os.replace"):
                self.assertTrue(ib.record_restart_and_check_cap())
        with mock.patch.object(ib, "_load_restart_times",
                               return_value=[now - 10, now - 20, now - 30]):
            self.assertFalse(ib.record_restart_and_check_cap())


if __name__ == "__main__":
    unittest.main()
