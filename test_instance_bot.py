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
        self.assertEqual(ib.get_map_entry("A"),
                         {"name": "A", "file": "A.wz", "mods": ["m1"],
                          "survival": False, "survival_ai_js": "SurvivalMod.js"})
        self.assertIsNone(ib.get_map_entry("ZZZ"))

    def test_map_file_field(self):
        ib.config = {"maps": [{"name": "W", "file": "5c-W.wz", "mods": []}]}
        self.assertEqual(ib.resolve_map_file("W"), "5c-W.wz")
        self.assertEqual(ib.resolve_map_file("Other"), "Other.wz")

    def test_dir_fallback_without_key(self):
        ib.config = {}
        maps = ib.get_available_maps()
        self.assertIn("NTW_3v3Full", maps)

    def test_validate_missing_map_and_mod(self):
        ib.config = {"map_name": "Ghost", "maps": [{"name": "Ghost", "mods": ["nope"]}],
                     "authorized_pkeys": []}
        errors = ib.validate_config()
        self.assertTrue(any("Ghost.wz" in e for e in errors))
        self.assertTrue(any("nope" in e and "mod" in e for e in errors))

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

    # idx and pos deliberately diverge (chat index 5 sits in lobby slot 0, ...)
    # to prove sender translation instead of index/position conflation.
    ROSTER = {
        "PK0": {"type": "player", "name": "Ann", "pos": 0, "idx": 5, "is_spec": False},
        "PK1": {"type": "player", "name": "Cat", "pos": 1, "idx": 1, "is_spec": False},
        "PK2": {"type": "player", "name": "Bob", "pos": 2, "idx": 2, "is_spec": False},
    }

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
        ib.roster = dict(self.ROSTER)
        with ib.vote_lock:
            ib._resolve_vote(timed_out=False)
        self.assertIsNone(ib.vote_state)
        timer.cancel.assert_called()

    def test_single_no_blocks_kick(self):
        self._mk_state()
        ib.roster = dict(self.ROSTER)
        ib.register_vote(1, "PK1", False)
        self.assertIsNone(ib.vote_state)  # all voted -> resolved as FAILED
        self.assertIsNone(ib.process)  # no kick issued (process is None anyway)


    def test_idx_translated_to_pos(self):
        # Ann chats from NetPlay index 5 but sits in lobby position 0.
        ib.roster = dict(self.ROSTER)
        self.assertEqual(ib.get_pos_by_sender("PK0", 5), 0)
        self.assertEqual(ib.get_pos_by_sender(None, 5), 0)
        self.assertEqual(ib.get_pos_by_idx(2), 2)
        self.assertIsNone(ib.get_pos_by_idx(99))

    def test_start_vote_translates_initiator(self):
        ib.roster = dict(self.ROSTER)
        ib.start_vote(5, "PK0", 2)  # idx 5 == position 0
        try:
            self.assertIsNotNone(ib.vote_state)
            self.assertEqual(ib.vote_state["initiator_pos"], 0)
            self.assertEqual(ib.vote_state["votes"], {0: True})
        finally:
            if ib.vote_state is not None:
                ib.vote_state["timer"].cancel()
                ib.vote_state = None

    def test_cancel_by_index(self):
        timer = mock.Mock()
        ib.roster = dict(self.ROSTER)
        ib.vote_state = {
            "target_pos": 2, "target_pk": "PK2", "target_name": "Bob",
            "initiator_pos": 0, "initiator_pk": "PK0",
            "eligible": {0, 1}, "votes": {0: True}, "timer": timer,
        }
        ib.cancel_vote_for_idx(2)  # Bob's NetPlay index is 2 here
        self.assertIsNone(ib.vote_state)
        timer.cancel.assert_called()


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


class UnlistTests(unittest.TestCase):
    def setUp(self):
        self._old_config = ib.config
        self._old_lobby = ib.lobby_id
        ib.config = {"lobby_address": "https://example.invalid"}
        ib.process = None

    def tearDown(self):
        ib.config = self._old_config
        ib.lobby_id = self._old_lobby
        ib.process = None

    def test_no_lobby_id(self):
        ib.lobby_id = None
        with unittest.mock.patch.object(ib.lobby_mitm, "delete_listing_by_game_id") as dl:
            self.assertFalse(ib.unlist_self_via_api())
            dl.assert_not_called()

    def test_delete_success(self):
        ib.lobby_id = "gid-1"
        with unittest.mock.patch.object(ib, "get_wz_lobbyserver", return_value=None), \
             unittest.mock.patch.object(
                ib.lobby_mitm, "delete_listing_by_game_id",
                return_value=(True, "HTTP 200")) as dl:
            self.assertTrue(ib.unlist_self_via_api())
            dl.assert_called_once_with(
                "gid-1", ib.lobby_mitm.TOKEN_STORE_DEFAULT,
                "https://example.invalid", False)

    def test_delete_failure(self):
        ib.lobby_id = "gid-1"
        with unittest.mock.patch.object(ib, "get_wz_lobbyserver", return_value=None), \
             unittest.mock.patch.object(
                ib.lobby_mitm, "delete_listing_by_game_id",
                return_value=(False, "HTTP 401")):
            self.assertFalse(ib.unlist_self_via_api())

    def test_delete_uses_wz_lobbyserver(self):
        ib.lobby_id = "gid-1"
        with unittest.mock.patch.object(
                ib, "get_wz_lobbyserver",
                return_value="https://lobby.example.test/custom"), \
             unittest.mock.patch.object(
                ib.lobby_mitm, "delete_listing_by_game_id",
                return_value=(True, "HTTP 200")) as dl:
            self.assertTrue(ib.unlist_self_via_api())
            dl.assert_called_once_with(
                "gid-1", ib.lobby_mitm.TOKEN_STORE_DEFAULT,
                "https://lobby.example.test/custom", False)

    def test_lobbyserver_from_wz_config(self):
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp()
        try:
            fake_dir = os.path.join(tmp, "instance_configs", "SESS")
            os.makedirs(fake_dir)
            with open(os.path.join(fake_dir, "config"), "w") as f:
                f.write("[General]\nlobbyserver=https://lobby.example.test/custom\n")
            with unittest.mock.patch.object(ib, "SCRIPT_DIR", tmp):
                ib.session_global = "SESS"
                self.assertEqual(ib.get_wz_lobbyserver(),
                                 "https://lobby.example.test/custom")
                ib.session_global = "MISSING"
                self.assertIsNone(ib.get_wz_lobbyserver())
        finally:
            ib.session_global = ""
            shutil.rmtree(tmp, ignore_errors=True)
class BuildCommandTests(unittest.TestCase):
    def setUp(self):
        self._old = (ib.config, ib.port_global, ib.session_global, ib.current_map_mods)

    def tearDown(self):
        ib.config, ib.port_global, ib.session_global, ib.current_map_mods = self._old

    def _base_config(self, **kw):
        cfg = {"map_name": "M", "host_name": "H", "game_name": "G",
               "wz_headless": True, "wz_nosound": True, "wz_debug_flags": [],
               "wz_extra_args": []}
        cfg.update(kw)
        return cfg

    def test_system_binary_override(self):
        ib.config = self._base_config(wz_binary="/usr/games/warzone2100.real")
        ib.port_global = 2110
        ib.session_global = "S"
        ib.current_map_mods = []
        with unittest.mock.patch.object(ib, "generate_configs", return_value="AH_M"):
            cmd = ib.build_command("system")
        self.assertEqual(cmd[0], "/usr/games/warzone2100.real")
        self.assertIn("--gameport=2110", cmd)

    def test_mod_flags(self):
        ib.config = self._base_config()
        ib.port_global = 2110
        ib.session_global = "S"
        ib.current_map_mods = ["modA", "modB"]
        with unittest.mock.patch.object(ib, "generate_configs", return_value="AH_M"):
            cmd = ib.build_command("system")
        self.assertIn("--mod_mp=modA.wz", cmd)
        self.assertIn("--mod_mp=modB.wz", cmd)

    def test_async_flag(self):
        ib.config = self._base_config(wz_async_join_approve=True)
        ib.port_global = 2110
        ib.session_global = "S"
        ib.current_map_mods = []
        with unittest.mock.patch.object(ib, "generate_configs", return_value="AH_M"):
            cmd = ib.build_command("system")
        self.assertIn("--async-join-approve", cmd)

class DrainTests(unittest.TestCase):
    def setUp(self):
        self._old = (ib.config, ib.process, ib.roster)
        ib.config = {"host_name": "FreeHost", "game_password": "2100"}
        ib.process = None

    def tearDown(self):
        ib.config, ib.process, ib.roster = self._old

    def test_host_only_means_drained(self):
        ib.roster = {
            "HOSTPK": {"type": "spec", "name": "FreeHost", "pos": None, "idx": None,
                       "is_spec": True, "ip": "-"},
        }
        self.assertTrue(ib.lobby_has_drained())

    def test_real_players_block_drain(self):
        ib.roster = {
            "HOSTPK": {"type": "spec", "name": "FreeHost", "pos": None, "idx": None,
                       "is_spec": True, "ip": "-"},
            "P1": {"type": "player", "name": "Ann", "pos": 0, "idx": 0,
                   "is_spec": False, "ip": "1.1.1.1"},
        }
        self.assertFalse(ib.lobby_has_drained())

    def test_wait_for_drain_success(self):
        ib.roster = {"HOSTPK": {"type": "spec", "name": "FreeHost", "pos": None,
                                "idx": None, "is_spec": True, "ip": "-"}}
        with unittest.mock.patch.object(ib, "request_roster_refresh", return_value=True):
            with unittest.mock.patch.object(ib.time, "sleep", return_value=None):
                self.assertTrue(ib.wait_for_drain(1))

    def test_wait_for_drain_timeout(self):
        ib.roster = {"P1": {"type": "player", "name": "Ann", "pos": 0, "idx": 0,
                            "is_spec": False, "ip": "1.1.1.1"}}
        with unittest.mock.patch.object(ib, "request_roster_refresh", return_value=True):
            self.assertFalse(ib.wait_for_drain(0))

    def test_announce_mentions_password(self):
        ib.config = {"game_password": "2100"}
        sent = []
        with unittest.mock.patch.object(ib, "send_cmd",
                                        lambda cmd, log_command=True: sent.append(cmd) or True):
            ib.process = unittest.mock.Mock()
            ib.process.stdin = unittest.mock.Mock()
            ib.announce_successor("FreeHost (new)", 2101)
        ib.process = None
        joined = " ".join(sent)
        self.assertIn("FreeHost (new)", joined)
        self.assertIn("2100", joined)


class UnlistStoreTests(unittest.TestCase):
    def test_custom_store_path_used(self):
        ib.config = {"lobby_token_store": "/tmp/custom-tokens.json"}
        ib.lobby_id = "gid-9"
        try:
            with unittest.mock.patch.object(ib, "get_wz_lobbyserver", return_value=None), \
                 unittest.mock.patch.object(
                    ib.lobby_mitm, "delete_listing_by_game_id",
                    return_value=(True, "HTTP 200")) as dl:
                self.assertTrue(ib.unlist_self_via_api())
                dl.assert_called_once_with("gid-9", "/tmp/custom-tokens.json",
                                           "https://wzlobby.wz2100.net/lobby", False)
        finally:
            ib.config = {}
            ib.lobby_id = None

class LobbyNameTests(unittest.TestCase):
    def test_safe_names(self):
        for good in ["FreeHost", "FreeHost 2", "A B C1", "dumbyhost"]:
            self.assertTrue(ib.is_lobby_safe_name(good), good)

    def test_unsafe_names(self):
        for bad in ["", "FreeHost (new)", "FreeHost [new]", "FreeHost [2]",
                    "a/b", "x" * 61, "semi;colon", "quo\"te", "under_score"]:
            self.assertFalse(ib.is_lobby_safe_name(bad), bad)

class MigrateFlowTests(unittest.TestCase):
    def setUp(self):
        self._old = (ib.config, ib.process, ib.roster)
        ib.config = {"game_name": "G", "migration_name_suffix": " [new]",
                     "migration_drain_seconds": 600}
        ib.process = None

    def tearDown(self):
        ib.config, ib.process, ib.roster = self._old

    def test_redirect_path_unblocks(self):
        ib.roster = {}
        with unittest.mock.patch.object(ib.lobby_mitm, "block_game"), \
             unittest.mock.patch.object(ib.lobby_mitm, "unblock_game") as ug, \
             unittest.mock.patch.object(ib, "wait_for_abandon", return_value=True), \
             unittest.mock.patch.object(ib, "request_roster_refresh", return_value=True), \
             unittest.mock.patch.object(ib, "redirect_all_players", return_value=1), \
             unittest.mock.patch.object(ib, "lobby_has_drained", return_value=True), \
             unittest.mock.patch.object(ib.time, "sleep", return_value=None):
            self.assertTrue(ib.migrate_to_successor(2101, "S", "gid-1", "G 2", "t"))
        ug.assert_called_once_with(ib.blocklist_path(), "gid-1")

    def test_redirect_path(self):
        ib.roster = {"P1": {"type": "player", "name": "Ann", "pos": 0, "idx": 0,
                            "is_spec": False, "ip": "1.1.1.1"}}
        calls = []

        def fake_send(cmd, log_command=True):
            calls.append(cmd)
            return True

        with unittest.mock.patch.object(ib, "send_cmd", fake_send), \
             unittest.mock.patch.object(ib.lobby_mitm, "block_game") as bg, \
             unittest.mock.patch.object(ib, "wait_for_abandon", return_value=True), \
             unittest.mock.patch.object(ib, "request_roster_refresh", return_value=True), \
             unittest.mock.patch.object(ib, "lobby_has_drained", return_value=True), \
             unittest.mock.patch.object(ib.time, "sleep", return_value=None):
            self.assertTrue(ib.migrate_to_successor(2101, "S", "gid-1", "G [new]", "t"))
        bg.assert_called_once()
        self.assertTrue(any(c.startswith("redirect identity P1 ") for c in calls))

    def test_fallback_path(self):
        ib.roster = {}
        with unittest.mock.patch.object(ib.lobby_mitm, "block_game",
                                        side_effect=OSError("disk full")), \
             unittest.mock.patch.object(ib, "announce_and_drain",
                                        return_value=True) as ad:
            self.assertTrue(ib.migrate_to_successor(2101, "S", "gid-1", "G [new]", "t"))
        ad.assert_called_once_with("G [new]", 2101)

class RateLimitTests(unittest.TestCase):
    def test_is_rate_limited(self):
        self.assertTrue(ib.is_rate_limited("lobbyerror (RATE_LIMITED): xyz"))
        self.assertFalse(ib.is_rate_limited("lobbyerror (GAME_NOT_FOUND): xyz"))
        self.assertFalse(ib.is_rate_limited(None))
        self.assertFalse(ib.is_rate_limited(""))

    def test_wait_returns_failed_status(self):
        with unittest.mock.patch.object(
                ib, "read_session_status",
                return_value={"status": "failed", "error": "lobbyerror (RATE_LIMITED): x"}):
            got = ib.wait_for_new_instance_ready("S", timeout=2)
        self.assertIsNotNone(got)
        self.assertEqual(got["status"], "failed")
        self.assertTrue(ib.is_rate_limited(got["error"]))

    def test_wait_none_on_timeout(self):
        with unittest.mock.patch.object(ib, "read_session_status", return_value=None):
            self.assertIsNone(ib.wait_for_new_instance_ready("S", timeout=1))

class ReapTests(unittest.TestCase):
    def test_reaps_dead_only(self):
        import subprocess as _sp
        killed = []

        def fake_run(cmd, **kw):
            m = unittest.mock.Mock()
            if cmd[:2] == ["tmux", "ls"]:
                m.returncode = 0
                m.stdout = "WZ_OLD\nWZ_LIVE\nOTHER\n"
            elif cmd[0] == "pgrep":
                m.returncode = 0 if "WZ_LIVE" in cmd[-1] else 1
            elif cmd[:2] == ["tmux", "kill-session"]:
                killed.append(cmd[-1])
                m.returncode = 0
            return m

        with unittest.mock.patch.object(_sp, "run", fake_run):
            with unittest.mock.patch.object(ib, "clear_session_status"):
                self.assertEqual(ib.reap_dead_sessions(), 1)
        self.assertEqual(killed, ["WZ_OLD"])

class MigrateMessagesTests(unittest.TestCase):
    def setUp(self):
        self._old = (ib.config, ib.process, ib.roster)
        ib.config = {"game_name": "G", "migration_name_suffix": " 2",
                     "migration_drain_seconds": 600}
        ib.process = None

    def tearDown(self):
        ib.config, ib.process, ib.roster = self._old

    def _run_success(self):
        ib.roster = {}
        sent = []

        def fake_send(cmd, log_command=True):
            sent.append(cmd)
            return True

        with unittest.mock.patch.object(ib, "send_cmd", fake_send), \
             unittest.mock.patch.object(ib.lobby_mitm, "block_game"), \
             unittest.mock.patch.object(ib.lobby_mitm, "unblock_game"), \
             unittest.mock.patch.object(ib, "wait_for_abandon", return_value=True), \
             unittest.mock.patch.object(ib, "request_roster_refresh", return_value=True), \
             unittest.mock.patch.object(ib, "redirect_all_players", return_value=0), \
             unittest.mock.patch.object(ib, "lobby_has_drained", return_value=True), \
             unittest.mock.patch.object(ib.time, "sleep", return_value=None):
            ib.process = unittest.mock.Mock()
            ib.process.stdin = unittest.mock.Mock()
            self.assertTrue(ib.migrate_to_successor(2101, "S", "gid-1", "G 2", "t"))
        ib.process = None
        bcasts = [c[len("chat bcast "):] for c in sent if c.startswith("chat bcast ")]
        return " ".join(bcasts)

    def test_progress_messages_in_order(self):
        text = self._run_success()
        markers = ["spun up successfully", "Cutting lobby access",
                   "ready for joins", "Redirecting you", "only once per game session"]
        idx = -1
        for m in markers:
            self.assertIn(m, text, m)
            now = text.index(m)
            self.assertGreater(now, idx, m)
            idx = now

    def test_fallback_mentions_manual(self):
        ib.roster = {}
        sent = []

        def fake_send(cmd, log_command=True):
            sent.append(cmd)
            return True

        with unittest.mock.patch.object(ib, "send_cmd", fake_send), \
             unittest.mock.patch.object(ib, "wait_for_drain", return_value=True):
            ib.process = unittest.mock.Mock()
            ib.process.stdin = unittest.mock.Mock()
            ib.announce_and_drain("G 2", 2101)
        ib.process = None
        text = " ".join(sent)
        self.assertIn("rejoin", text)
        self.assertIn("one automatic", text)

class ModSourceTests(unittest.TestCase):
    def setUp(self):
        import tempfile, shutil
        self._old_mods = ib.MODS_DIR
        self.tmp = tempfile.mkdtemp()
        ib.MODS_DIR = self.tmp

    def tearDown(self):
        ib.MODS_DIR = self._old_mods
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_file_and_dir_sources(self):
        import os
        open(os.path.join(self.tmp, "M.wz"), "w").write("x")
        os.makedirs(os.path.join(self.tmp, "D", "sub"))
        self.assertEqual(ib.resolve_mod_source("M")[0], "file")
        self.assertEqual(ib.resolve_mod_source("D")[0], "dir")
        self.assertIsNone(ib.resolve_mod_source("Nope"))

    def test_old_format_lev_players(self):
        import os
        import zipfile
        zpath = os.path.join(self.tmp, "Old.wz")
        with zipfile.ZipFile(zpath, "w") as z:
            z.writestr("x.lev", "level Foo\nplayers 5\ntype 14\n")
        ib.MODS_DIR = self.tmp  # noqa (maps test below overrides MAPS_DIR)
        old_maps = ib.MAPS_DIR
        ib.MAPS_DIR = self.tmp
        try:
            info = ib.extract_map_info("Old")
            self.assertEqual(info["players"], 5)
        finally:
            ib.MAPS_DIR = old_maps

class StageModsTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._old = (ib.config, ib.MODS_DIR)
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(self.src, "D", "sub"))
        open(os.path.join(self.src, "D", "f.txt"), "w").write("x")
        open(os.path.join(self.src, "D", "sub", "g.txt"), "w").write("y")
        ib.MODS_DIR = self.src
        ib.config = {"wz_mods_version": "9.9"}

    def tearDown(self):
        ib.config, ib.MODS_DIR = self._old[0], self._old[1]
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_zip_is_deterministic(self):
        out = os.path.join(self.tmp, "out")
        self.assertTrue(ib.stage_mods(out, [("D", "dir",
                                            os.path.join(self.src, "D"))]))
        p1 = os.path.join(out, "mods", "9.9", "multiplay", "D.wz")
        p2 = os.path.join(out, "mods", "multiplay", "D.wz")
        self.assertTrue(os.path.exists(p1))
        self.assertTrue(os.path.exists(p2))
        import hashlib
        h = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
        self.assertEqual(h(p1), h(p2))
        import zipfile
        names = zipfile.ZipFile(p1).namelist()
        self.assertEqual(names, sorted(names))

class SurvivalTests(unittest.TestCase):
    def setUp(self):
        self._old = (ib.config, ib.MAPS_DIR)
        ib.config = {"maps": [{"name": "S", "mods": ["Survival_Mod"]},
                              {"name": "N", "mods": []}]}

    def tearDown(self):
        ib.config = self._old[0]

    def test_survival_detection(self):
        self.assertEqual(ib.is_survival_map("S", ["Survival_Mod"])[0], True)
        self.assertEqual(ib.is_survival_map("N", [])[0], False)
        ib.config = {"maps": [{"name": "S2", "mods": [], "survival": True,
                               "survival_ai_js": "X.js"}]}
        self.assertEqual(ib.is_survival_map("S2", []), (True, "X.js"))

    def test_survival_layout(self):
        import tempfile, shutil, zipfile, json as _json, os
        tmp = tempfile.mkdtemp()
        old_maps = ib.MAPS_DIR
        ib.MAPS_DIR = tmp
        try:
            zpath = os.path.join(tmp, "S.wz")
            with zipfile.ZipFile(zpath, "w") as z:
                z.writestr("level.json", _json.dumps({"players": 5}))
            cfg = os.path.join(tmp, "cfg")
            ah = ib.generate_configs("S", cfg, "H", "G", 30, 30, 30, "", "",
                                     ["Survival_Mod"])
            self.assertTrue(ah)
            data = _json.load(open(os.path.join(cfg, "autohost", ah)))
            self.assertEqual(data["player_0"], {"team": 0, "ai": "SurvivalMod.js"})
            for i in range(1, 5):
                self.assertEqual(data[f"player_{i}"], {"team": 1})
        finally:
            ib.MAPS_DIR = old_maps
            shutil.rmtree(tmp, ignore_errors=True)

    def test_normal_layout_untouched(self):
        import tempfile, shutil, zipfile, json as _json, os
        tmp = tempfile.mkdtemp()
        old_maps = ib.MAPS_DIR
        ib.MAPS_DIR = tmp
        try:
            zpath = os.path.join(tmp, "N.wz")
            with zipfile.ZipFile(zpath, "w") as z:
                z.writestr("level.json", _json.dumps({"players": 4}))
            cfg = os.path.join(tmp, "cfg")
            ah = ib.generate_configs("N", cfg, "H", "G", 30, 30, 30, "", "", [])
            data = _json.load(open(os.path.join(cfg, "autohost", ah)))
            self.assertNotIn("ai", data["player_0"])
        finally:
            ib.MAPS_DIR = old_maps
            shutil.rmtree(tmp, ignore_errors=True)

class AutostartTests(unittest.TestCase):
    def setUp(self):
        self._old = (ib.config, ib.port_global, ib.session_global,
                     ib.current_map_name, ib.current_map_mods)
        ib.config = {"maps": [{"name": "S", "mods": ["Survival_Mod"]},
                              {"name": "N", "mods": []},
                              {"name": "C", "mods": [], "autostart_players": 4}]}
        ib.port_global = 2110
        ib.session_global = "S"

    def tearDown(self):
        (ib.config, ib.port_global, ib.session_global,
         ib.current_map_name, ib.current_map_mods) = self._old

    def test_defaults(self):
        self.assertEqual(ib.get_autostart_players("S"), 2)
        self.assertEqual(ib.get_autostart_players("N"), 0)
        self.assertEqual(ib.get_autostart_players("C"), 4)
        self.assertEqual(ib.get_autostart_players("???"), 0)

    def test_flag_present_only_when_on(self):
        ib.current_map_mods = []
        with unittest.mock.patch.object(ib, "generate_configs", return_value="AH_X"):
            ib.current_map_name = "S"
            self.assertIn("--startplayers=2", ib.build_command("system"))
            ib.current_map_name = "N"
            self.assertNotIn("--startplayers=2", ib.build_command("system"))

    def test_readybeforefull_only_for_survival(self):
        import tempfile, shutil, zipfile, json as _json, os
        tmp = tempfile.mkdtemp()
        old_maps = ib.MAPS_DIR
        ib.MAPS_DIR = tmp
        try:
            for name in ("S", "N"):
                with zipfile.ZipFile(os.path.join(tmp, f"{name}.wz"), "w") as z:
                    z.writestr("level.json", _json.dumps({"players": 4}))
            cfg = os.path.join(tmp, "cfg")
            ah = ib.generate_configs("S", cfg, "H", "G", 30, 30, 30, "", "",
                                     ["Survival_Mod"])
            data = _json.load(open(os.path.join(cfg, "autohost", ah)))
            self.assertEqual(data["locked"].get("readybeforefull"), False)
            cfg2 = os.path.join(tmp, "cfg2")
            ah2 = ib.generate_configs("N", cfg2, "H", "G", 30, 30, 30, "", "", [])
            data2 = _json.load(open(os.path.join(cfg2, "autohost", ah2)))
            self.assertNotIn("readybeforefull", data2["locked"])
        finally:
            ib.MAPS_DIR = old_maps
            shutil.rmtree(tmp, ignore_errors=True)
