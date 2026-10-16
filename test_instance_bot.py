#!/usr/bin/env python3
"""Unit tests for instance_bot.py (stdlib only: python3 -m unittest)."""
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import instance_bot as ib
import wz_config as wz


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
        info = wz.extract_map_info(wz.DEFAULT_MAPS_DIR, "NTW_3v3Full")
        self.assertIsNotNone(info)
        self.assertGreater(info["players"], 0)

    def test_unknown_map(self):
        self.assertIsNone(wz.extract_map_info(wz.DEFAULT_MAPS_DIR, "No_Such_Map_xyz"))


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
        entries = [{"name": "A", "mods": ["m1"]}, "B"]
        ib.config = {"maps": entries}
        self.assertEqual(ib.get_available_maps(), ["A", "B"])
        self.assertEqual(wz.get_map_entry(entries, "A"),
                         {"name": "A", "file": "A.wz", "mods": ["m1"],
                          "survival": False, "survival_ai_js": "SurvivalMod.js"})
        self.assertIsNone(wz.get_map_entry(entries, "ZZZ"))

    def test_map_file_field(self):
        entries = [{"name": "W", "file": "5c-W.wz", "mods": []}]
        self.assertEqual(wz.resolve_map_file(entries, "W"), "5c-W.wz")
        self.assertEqual(wz.resolve_map_file(entries, "Other"), "Other.wz")

    def test_dir_fallback_without_key(self):
        ib.config = {}
        maps = ib.get_available_maps()
        self.assertIn("NTW_3v3Full", maps)

    def test_validate_missing_map_and_mod(self):
        entries = [{"name": "Ghost", "mods": ["nope"]}]
        errors = wz.validate_config(entries, wz.DEFAULT_MAPS_DIR,
                                    wz.DEFAULT_MODS_DIR, "Ghost")
        self.assertTrue(any("Ghost.wz" in e for e in errors))
        self.assertTrue(any("nope" in e and "mod" in e for e in errors))

    def test_mod_name_safety(self):
        self.assertTrue(wz.is_safe_mod_name("Cool-Mod_v2.wz".replace(".wz", "")))
        for bad in ["../evil", "a/b", "", ".", "..", "x;y", "x y"]:
            self.assertFalse(wz.is_safe_mod_name(bad), bad)


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
        with unittest.mock.patch.object(wz, "generate_configs", return_value="AH_M"):
            cmd = ib.build_command("system")
        self.assertEqual(cmd[0], "/usr/games/warzone2100.real")
        self.assertIn("--gameport=2110", cmd)

    def test_mod_flags(self):
        ib.config = self._base_config()
        ib.port_global = 2110
        ib.session_global = "S"
        ib.current_map_mods = ["modA", "modB"]
        with unittest.mock.patch.object(wz, "generate_configs", return_value="AH_M"):
            cmd = ib.build_command("system")
        self.assertIn("--mod_mp=modA.wz", cmd)
        self.assertIn("--mod_mp=modB.wz", cmd)

    def test_async_flag(self):
        ib.config = self._base_config(wz_async_join_approve=True)
        ib.port_global = 2110
        ib.session_global = "S"
        ib.current_map_mods = []
        with unittest.mock.patch.object(wz, "generate_configs", return_value="AH_M"):
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

    def test_redirect_path_via_relay(self):
        ib.roster = {"P1": {"type": "player", "name": "Ann", "pos": 0, "idx": 0,
                            "is_spec": False, "ip": "1.1.1.1"}}
        ib.config = {"game_name": "G", "migration_name_suffix": " 2",
                     "migration_drain_seconds": 600, "relay_port": 2200}
        calls = []

        def fake_send(cmd, log_command=True):
            calls.append(cmd)
            return True

        with unittest.mock.patch.object(ib, "send_cmd", fake_send), \
             unittest.mock.patch.object(ib, "publish_relay_target", return_value=True), \
             unittest.mock.patch.object(ib, "request_roster_refresh", return_value=True), \
             unittest.mock.patch.object(ib, "lobby_has_drained", return_value=True), \
             unittest.mock.patch.object(ib, "get_public_ip", return_value="9.9.9.9"), \
             unittest.mock.patch.object(ib.time, "sleep", return_value=None):
            self.assertTrue(ib.migrate_to_successor(2101, "S", "gid-1", "G 2", "t"))
        redir = [c for c in calls if c.startswith("redirect identity P1 ")]
        self.assertEqual(len(redir), 1)
        self.assertIn("tcp:2200:", redir[0])

    def test_fallback_path(self):
        ib.roster = {}
        with unittest.mock.patch.object(ib, "publish_relay_target", return_value=False), \
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
             unittest.mock.patch.object(ib, "publish_relay_target", return_value=True), \
             unittest.mock.patch.object(ib, "get_public_ip", return_value="9.9.9.9"), \
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
        markers = ["spun up successfully", "Handover channel open",
                   "only once per game session"]
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
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_file_and_dir_sources(self):
        import os
        open(os.path.join(self.tmp, "M.wz"), "w").write("x")
        os.makedirs(os.path.join(self.tmp, "D", "sub"))
        self.assertEqual(wz.resolve_mod_source(self.tmp, "M")[0], "file")
        self.assertEqual(wz.resolve_mod_source(self.tmp, "D")[0], "dir")
        self.assertIsNone(wz.resolve_mod_source(self.tmp, "Nope"))

    def test_old_format_lev_players(self):
        import os
        import zipfile
        zpath = os.path.join(self.tmp, "Old.wz")
        with zipfile.ZipFile(zpath, "w") as z:
            z.writestr("x.lev", "level Foo\nplayers 5\ntype 14\n")
        info = wz.extract_map_info(self.tmp, "Old")
        self.assertEqual(info["players"], 5)


class StageModsTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(self.src, "D", "sub"))
        open(os.path.join(self.src, "D", "f.txt"), "w").write("x")
        open(os.path.join(self.src, "D", "sub", "g.txt"), "w").write("y")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_zip_is_deterministic(self):
        out = os.path.join(self.tmp, "out")
        self.assertTrue(wz.stage_mods(
            self.src, out, [("D", "dir", os.path.join(self.src, "D"))],
            "9.9", log=lambda t, m: None))
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
    ENTRIES = [{"name": "S", "mods": ["Survival_Mod"]},
               {"name": "N", "mods": []}]

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.ahdir = os.path.join(self.tmp, "ah")
        self.maps = os.path.join(self.tmp, "maps")
        os.makedirs(self.maps)
        self.mods = os.path.join(self.tmp, "mods")
        os.makedirs(os.path.join(self.mods, "Survival_Mod", "stats"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gen(self, name, mods):
        import zipfile, json as _json
        players = 5 if name == "S" else 4
        with zipfile.ZipFile(os.path.join(self.maps, f"{name}.wz"), "w") as z:
            z.writestr("level.json", _json.dumps({"players": players}))
        cfg = os.path.join(self.tmp, "cfg-" + name)
        return wz.generate_configs(
            self.maps, self.mods, self.ahdir, name, self.ENTRIES, cfg,
            "H", "G", 30, 30, 30, "", "", mods=mods, log=lambda t, m: None)

    def test_survival_detection(self):
        self.assertEqual(wz.is_survival_map(self.ENTRIES, "S", ["Survival_Mod"])[0], True)
        self.assertEqual(wz.is_survival_map(self.ENTRIES, "N", [])[0], False)
        entries = [{"name": "S2", "mods": [], "survival": True, "survival_ai_js": "X.js"}]
        self.assertEqual(wz.is_survival_map(entries, "S2", []), (True, "X.js"))

    def test_survival_layout(self):
        import json as _json
        ah = self._gen("S", ["Survival_Mod"])
        self.assertTrue(ah)
        data = _json.load(open(os.path.join(self.tmp, "cfg-S", "autohost", ah)))
        self.assertEqual(data["player_0"], {"team": 0, "ai": "SurvivalMod.js"})
        for i in range(1, 5):
            self.assertEqual(data[f"player_{i}"], {"team": 1})

    def test_normal_layout_untouched(self):
        import json as _json
        ah = self._gen("N", [])
        data = _json.load(open(os.path.join(self.tmp, "cfg-N", "autohost", ah)))
        self.assertNotIn("ai", data["player_0"])


class AutostartTests(unittest.TestCase):
    ENTRIES = [{"name": "S", "mods": ["Survival_Mod"]},
               {"name": "N", "mods": []},
               {"name": "C", "mods": [], "autostart_players": 4}]

    def setUp(self):
        self._old = (ib.config, ib.port_global, ib.session_global,
                     ib.current_map_name, ib.current_map_mods)
        ib.config = {"maps": self.ENTRIES}
        ib.port_global = 2110
        ib.session_global = "S"

    def tearDown(self):
        (ib.config, ib.port_global, ib.session_global,
         ib.current_map_name, ib.current_map_mods) = self._old

    def test_defaults(self):
        self.assertEqual(wz.get_autostart_players(self.ENTRIES, "S", ["Survival_Mod"]), 2)
        self.assertEqual(wz.get_autostart_players(self.ENTRIES, "N", []), 0)
        self.assertEqual(wz.get_autostart_players(self.ENTRIES, "C", []), 4)
        self.assertEqual(wz.get_autostart_players(self.ENTRIES, "???", []), 0)

    def test_flag_present_only_when_on(self):
        with unittest.mock.patch.object(wz, "generate_configs", return_value="AH_X"):
            ib.current_map_name = "S"
            ib.current_map_mods = ["Survival_Mod"]
            self.assertIn("--startplayers=2", ib.build_command("system"))
            ib.current_map_name = "N"
            ib.current_map_mods = []
            self.assertNotIn("--startplayers=2", ib.build_command("system"))

    def test_readybeforefull_only_for_survival(self):
        import tempfile, shutil, zipfile, json as _json, os
        tmp = tempfile.mkdtemp()
        try:
            for name in ("S", "N"):
                with zipfile.ZipFile(os.path.join(tmp, f"{name}.wz"), "w") as z:
                    z.writestr("level.json", _json.dumps({"players": 4}))
            os.makedirs(os.path.join(tmp, "Survival_Mod", "stats"))
            cfg = os.path.join(tmp, "cfg")
            ah = wz.generate_configs(
                tmp, tmp, os.path.join(tmp, "ah"), "S", self.ENTRIES, cfg,
                "H", "G", 30, 30, 30, "", "", mods=["Survival_Mod"],
                log=lambda t, m: None)
            data = _json.load(open(os.path.join(cfg, "autohost", ah)))
            self.assertEqual(data["locked"].get("readybeforefull"), False)
            cfg2 = os.path.join(tmp, "cfg2")
            ah2 = wz.generate_configs(
                tmp, tmp, os.path.join(tmp, "ah"), "N", self.ENTRIES, cfg2,
                "H", "G", 30, 30, 30, "", "", mods=[],
                log=lambda t, m: None)
            data2 = _json.load(open(os.path.join(cfg2, "autohost", ah2)))
            self.assertNotIn("readybeforefull", data2["locked"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TemplateTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.maps = os.path.join(self.tmp, "maps")
        os.makedirs(self.maps)
        self.ahdir = os.path.join(self.tmp, "ah")
        self.entries = [{"name": "T", "mods": []}]
        import zipfile, json as _json
        with zipfile.ZipFile(os.path.join(self.maps, "T.wz"), "w") as z:
            z.writestr("level.json", _json.dumps({"players": 2}))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gen(self, **kw):
        cfg = os.path.join(self.tmp, "cfg")
        return wz.generate_configs(
            self.maps, self.tmp, self.ahdir, "T", self.entries, cfg,
            "H", kw.get("game_name", "G"), 30, 30, 30,
            kw.get("game_password", ""), "", mods=[],
            log=lambda t, m: None), cfg

    def test_custom_template_respected(self):
        import json as _json, os
        os.makedirs(self.ahdir, exist_ok=True)
        with open(os.path.join(self.ahdir, "T.json"), "w") as f:
            _json.dump({"locked": {"power": False},
                        "challenge": {"alliances": 1, "name": "Old"},
                        "player_0": {"team": 5}}, f)
        ah, cfg = self._gen(game_name="GameX", game_password="pw")
        data = _json.load(open(os.path.join(cfg, "autohost", ah)))
        self.assertFalse(data["locked"]["power"])
        self.assertEqual(data["challenge"]["alliances"], 1)
        self.assertEqual(data["player_0"], {"team": 5})
        # identity always patched in:
        self.assertEqual(data["challenge"]["name"], "GameX")
        self.assertEqual(data["challenge"]["map"], "T")
        self.assertEqual(data["challenge"]["gamePassword"], "pw")

    def test_missing_template_generates_and_persists(self):
        import json as _json, os
        ah, cfg = self._gen()
        self.assertTrue(ah)
        self.assertTrue(os.path.exists(os.path.join(self.ahdir, "T.json")))

    def test_invalid_template_falls_back(self):
        import os
        os.makedirs(self.ahdir, exist_ok=True)
        with open(os.path.join(self.ahdir, "T.json"), "w") as f:
            f.write("{not json")
        ah, cfg = self._gen()
        self.assertTrue(ah)


class MigrationClaimTests(unittest.TestCase):
    def setUp(self):
        with ib.migration_lock:
            ib.map_change_in_progress = False

    def tearDown(self):
        with ib.migration_lock:
            ib.map_change_in_progress = False

    def test_claim_is_single_flight(self):
        self.assertTrue(ib._try_claim_migration())
        self.assertFalse(ib._try_claim_migration())
        ib._release_migration_claim()
        self.assertTrue(ib._try_claim_migration())
        ib._release_migration_claim()

    def test_timeout_skipped_when_claim_held(self):
        ib.map_change_in_progress = True
        ib.game_started = False
        ib.process = mock.Mock()
        ib.process.poll.return_value = None  # running
        with mock.patch.object(ib, "spawn_new_instance") as spawn:
            ib._start_timeout_expired()
            spawn.assert_not_called()
        ib.process = None


class ExtraArgsTests(unittest.TestCase):
    def setUp(self):
        self._old = (ib.config, ib.port_global, ib.session_global,
                     ib.current_map_name, ib.current_map_mods)

    def tearDown(self):
        (ib.config, ib.port_global, ib.session_global,
         ib.current_map_name, ib.current_map_mods) = self._old

    def _cmd(self, extras):
        ib.config = {"map_name": "M", "host_name": "H", "game_name": "G",
                     "wz_headless": True, "wz_nosound": True,
                     "wz_debug_flags": [], "wz_extra_args": extras}
        ib.port_global = 2110
        ib.session_global = "S"
        ib.current_map_name = "M"
        ib.current_map_mods = []
        with mock.patch.object(wz, "generate_configs", return_value="AH_M"):
            return ib.build_command("system")

    def test_bot_managed_flags_rejected(self):
        cmd = self._cmd(["--gameport=9999", "--configdir=/tmp/x",
                         "--autohost=Y", "--enablecmdinterface=socket",
                         "--mod_mp=evil.wz"])
        self.assertNotIn("--gameport=9999", cmd)
        self.assertIn("--gameport=2110", cmd)
        self.assertFalse(any(a.startswith("--mod_mp=evil") for a in cmd))

    def test_duplicate_keys_rejected(self):
        cmd = self._cmd(["--headless", "--portmapping=false", "--foo=1", "--foo=2"])
        self.assertEqual(cmd.count("--headless"), 1)
        self.assertEqual([a for a in cmd if a.startswith("--foo=")], ["--foo=1"])

    def test_allowed_extra_passes(self):
        cmd = self._cmd(["--foo=1"])
        self.assertIn("--foo=1", cmd)


class SavePkeysLockTests(unittest.TestCase):
    def test_lockfile_serializes_and_preserves(self):
        import json as _json
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp()
        try:
            cfg = os.path.join(tmp, "instances.json")
            with open(cfg, "w") as f:
                _json.dump({"game_name": "G", "authorized_pkeys": ["old"]}, f)
            with mock.patch.object(ib, "CONFIG_FILE", cfg), \
                 mock.patch.object(ib, "CONFIG_LOCK_FILE", cfg + ".lock"):
                ib.config = {"authorized_pkeys": ["a", "b"]}
                ib.save_authorized_pkeys()
                data = _json.load(open(cfg))
                self.assertEqual(data["authorized_pkeys"], ["a", "b"])
                self.assertEqual(data["game_name"], "G")
                self.assertTrue(os.path.exists(cfg + ".lock"))
        finally:
            ib.config = {}
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
class GameSuccessorTests(unittest.TestCase):
    def setUp(self):
        self._old = (ib.config, ib.process, ib.game_started,
                     ib.game_successor_session, ib.map_change_in_progress,
                     ib.quit_after_game, ib.current_map_name,
                     ib.current_map_mods)
        ib.config = {"game_name": "FreeHost", "migration_name_suffix": " 2"}
        ib.quit_after_game = False
        ib.game_started = False
        ib.game_successor_session = None
        ib.map_change_in_progress = False
        ib.current_map_name = "M"
        ib.current_map_mods = ["modA"]

    def tearDown(self):
        (ib.config, ib.process, ib.game_started,
         ib.game_successor_session, ib.map_change_in_progress,
         ib.quit_after_game, ib.current_map_name,
         ib.current_map_mods) = self._old

    class _SyncThread:
        def __init__(self, target=None, args=(), **kw):
            self._t = target
            self._a = args
        def start(self):
            self._t(*self._a)

    def test_game_start_marks_and_spawns_thread(self):
        timer = unittest.mock.Mock()
        ib.start_timeout_timer = timer
        started = []
        real_thread = ib.threading.Thread
        try:
            ib.threading.Thread = lambda target=None, args=(), **kw: (
                started.append(target), self._SyncThread(target, args, **kw))[1]
            with unittest.mock.patch.object(ib, "_spawn_game_successor", lambda: started.append("ran")):
                ib._on_game_started()
        finally:
            ib.threading.Thread = real_thread
            ib.start_timeout_timer = None
        self.assertTrue(ib.game_started)
        timer.cancel.assert_called_once_with()
        self.assertIn("ran", started)

    def test_spawn_worker_records_base_name_successor(self):
        ib.game_started = True
        ib.process = unittest.mock.Mock()
        ib.process.poll.return_value = None
        with unittest.mock.patch.object(ib, "family_listing_ids",
                                        return_value=[]), \
             unittest.mock.patch.object(ib, "spawn_new_instance",
                                        return_value=(2101, "S")) as sp, \
             unittest.mock.patch.object(ib, "wait_for_new_instance_ready",
                                        return_value={"lobby_id": "lid-1"}):
            ib._spawn_game_successor()
        sp.assert_called_once_with("M", ["modA"], name_suffix="")
        self.assertEqual(ib.game_successor_session, "S")
        self.assertFalse(ib.map_change_in_progress)
        ib.process = None

    def test_spawn_worker_skips_when_claim_held(self):
        ib.game_started = True
        ib.map_change_in_progress = True
        with unittest.mock.patch.object(ib, "spawn_new_instance") as sp:
            ib._spawn_game_successor()
        sp.assert_not_called()
        self.assertIsNone(ib.game_successor_session)

    def test_spawn_worker_records_nothing_on_failure(self):
        ib.game_started = True
        ib.process = unittest.mock.Mock()
        ib.process.poll.return_value = None
        with unittest.mock.patch.object(ib, "family_listing_ids",
                                        return_value=[]), \
             unittest.mock.patch.object(ib, "spawn_new_instance",
                                        return_value=(2101, "S")), \
             unittest.mock.patch.object(ib, "wait_for_new_instance_ready",
                                        return_value=None):
            ib._spawn_game_successor()
        self.assertIsNone(ib.game_successor_session)
        self.assertFalse(ib.map_change_in_progress)
        ib.process = None

    def test_game_end_hands_over_to_live_successor(self):
        ib.game_started = True
        ib.game_successor_session = "S"
        ib.process = unittest.mock.Mock()
        ib.process.poll.return_value = None
        sent = []
        with unittest.mock.patch.object(ib, "read_live_successor",
                                        return_value=(2101, "lid-1")), \
             unittest.mock.patch.object(ib.threading, "Thread", self._SyncThread), \
             unittest.mock.patch.object(ib, "migrate_to_successor",
                                        return_value=True) as mg, \
             unittest.mock.patch.object(ib, "send_cmd",
                                        lambda c, log_command=True: sent.append(c) or True):
            ib.check_game_ended("started")
        mg.assert_called_once_with(2101, "S", "lid-1", "FreeHost", "post-game")
        self.assertTrue(ib.quit_after_game)
        self.assertIn("shutdown now", sent)
        self.assertIsNone(ib.game_successor_session)
        ib.process = None
        ib.quit_after_game = False
        ib.map_change_in_progress = False

    def test_game_end_rearms_when_successor_dead(self):
        ib.game_started = True
        ib.game_successor_session = "S"
        ib.process = unittest.mock.Mock()
        ib.process.poll.return_value = None
        with unittest.mock.patch.object(ib, "read_live_successor",
                                        return_value=None), \
             unittest.mock.patch.object(ib, "arm_start_timeout") as arm, \
             unittest.mock.patch.object(ib, "migrate_to_successor") as mg:
            ib.check_game_ended("started")
        arm.assert_called_once_with()
        mg.assert_not_called()
        self.assertFalse(ib.game_started)
        self.assertIsNone(ib.game_successor_session)
        ib.process = None

    def test_read_live_successor_shapes(self):
        def fake_run(cmd, **kw):
            m = unittest.mock.Mock()
            m.returncode = 0 if cmd[-1] == "S_LIVE" else 1
            return m
        with unittest.mock.patch.object(ib, "read_session_status",
                                        return_value={"status": "ready", "port": 2101,
                                                      "lobby_id": "lid"}), \
             unittest.mock.patch.object(ib.subprocess, "run", fake_run):
            self.assertEqual(ib.read_live_successor("S_LIVE"), (2101, "lid"))
            self.assertIsNone(ib.read_live_successor("S_DEAD"))
        with unittest.mock.patch.object(ib, "read_session_status",
                                        return_value={"status": "exited"}):
            self.assertIsNone(ib.read_live_successor("S_LIVE"))
        self.assertIsNone(ib.read_live_successor(None))

    def test_crash_respawn_allowed(self):
        with unittest.mock.patch.object(ib, "successor_covering",
                                        return_value=True):
            ib.game_successor_session = "S"
            self.assertFalse(ib.crash_respawn_allowed())
        with unittest.mock.patch.object(ib, "successor_covering",
                                        return_value=False):
            ib.game_successor_session = "S"
            self.assertTrue(ib.crash_respawn_allowed())
        ib.game_successor_session = None
        self.assertTrue(ib.crash_respawn_allowed())

    def test_successor_covering_shapes(self):
        def fake_run(cmd, **kw):
            m = unittest.mock.Mock()
            m.returncode = 0 if cmd[-1] == "S_LIVE" else 1
            return m
        with unittest.mock.patch.object(ib, "read_session_status",
                                        return_value={"status": "starting"}), \
             unittest.mock.patch.object(ib.subprocess, "run", fake_run):
            self.assertTrue(ib.successor_covering("S_LIVE"))
            self.assertFalse(ib.successor_covering("S_DEAD"))
        with unittest.mock.patch.object(ib, "read_session_status",
                                        return_value={"status": "failed"}), \
             unittest.mock.patch.object(ib.subprocess, "run", fake_run):
            self.assertFalse(ib.successor_covering("S_LIVE"))
        with unittest.mock.patch.object(ib, "read_session_status",
                                        return_value=None):
            self.assertFalse(ib.successor_covering("S_LIVE"))
        self.assertFalse(ib.successor_covering(None))

class SpawnCapTests(unittest.TestCase):
    def _spawn(self, live):
        with unittest.mock.patch.object(ib, "prune_stale_status_files"), \
             unittest.mock.patch.object(ib, "reap_dead_sessions", return_value=0), \
             unittest.mock.patch.object(ib, "count_live_sessions", return_value=live), \
             unittest.mock.patch.object(ib, "find_available_port", return_value=2105), \
             unittest.mock.patch.object(ib, "clear_session_status"), \
             unittest.mock.patch.object(ib.subprocess, "Popen"):
            return ib.spawn_new_instance("M", [])

    def test_spawn_allowed_below_cap(self):
        got = self._spawn(2)
        self.assertIsNotNone(got)
        self.assertEqual(got[0], 2105)

    def test_spawn_refused_at_cap(self):
        self.assertIsNone(self._spawn(3))

    def test_default_cap_leaves_migration_spare(self):
        self.assertGreaterEqual(ib.MAX_LIVE_SESSIONS, 3)

class SingleListingTests(unittest.TestCase):
    def setUp(self):
        self._old = (ib.config, ib.lobby_id, ib.instance_start_time,
                     ib.game_successor_session, ib.map_change_in_progress)
        ib.config = {"game_name": "FreeHost", "migration_name_suffix": " 2"}
        ib.lobby_id = "own-id"
        ib.instance_start_time = ib.time.time() - 7200
        ib.game_successor_session = None
        ib.map_change_in_progress = False

    def tearDown(self):
        (ib.config, ib.lobby_id, ib.instance_start_time,
         ib.game_successor_session, ib.map_change_in_progress) = self._old

    def _games(self, *entries):
        return [{"id": i, "name": n, "map": "M"} for i, n in entries]

    def test_family_matching_and_exclusion(self):
        games = self._games(("own-id", "FreeHost"), ("s-id", "FreeHost 2"),
                            ("other", "OtherHost"), ("prefix", "FreeHostess"))
        with unittest.mock.patch.object(ib.lobby_mitm, "list_lobby_games",
                                        return_value=games):
            self.assertEqual(ib.family_listing_ids(), ["own-id", "s-id"])
            self.assertEqual(ib.family_listing_ids(exclude_id="own-id"), ["s-id"])

    def test_family_empty_on_fetch_failure(self):
        with unittest.mock.patch.object(ib.lobby_mitm, "list_lobby_games",
                                        side_effect=RuntimeError("down")):
            self.assertEqual(ib.family_listing_ids(), [])

    def test_enforce_deletes_others_keeps_new(self):
        deleted = []
        fam = lambda exclude_id=None: [g for g in ["old-a", "new-id"] if g != exclude_id]
        with unittest.mock.patch.object(ib, "family_listing_ids",
                                        side_effect=fam), \
             unittest.mock.patch.object(ib.lobby_mitm, "delete_listing_by_game_id",
                                        lambda gid, *a: (deleted.append(gid), (True, "HTTP 200"))[1]):
            ib.enforce_single_listing("new-id")
        self.assertEqual(deleted, ["old-a"])

    def test_migrate_enforces_on_drained_success(self):
        ib.roster = {}
        with unittest.mock.patch.object(ib, "publish_relay_target", return_value=True), \
             unittest.mock.patch.object(ib, "request_roster_refresh", return_value=True), \
             unittest.mock.patch.object(ib, "redirect_all_players", return_value=0), \
             unittest.mock.patch.object(ib, "lobby_has_drained", return_value=True), \
             unittest.mock.patch.object(ib, "get_public_ip", return_value="9.9.9.9"), \
             unittest.mock.patch.object(ib, "enforce_single_listing") as enf, \
             unittest.mock.patch.object(ib.time, "sleep", return_value=None):
            self.assertTrue(ib.migrate_to_successor(2101, "S", "new-id", "FreeHost 2", "t"))
        enf.assert_called_once_with("new-id")

    def test_game_start_skips_when_covered(self):
        ib.game_started = True
        ib.process = unittest.mock.Mock()
        ib.process.poll.return_value = None
        try:
            with unittest.mock.patch.object(ib, "family_listing_ids",
                                            return_value=["live-id"]), \
                 unittest.mock.patch.object(ib, "spawn_new_instance") as sp:
                ib._spawn_game_successor()
            sp.assert_not_called()
            self.assertIsNone(ib.game_successor_session)
        finally:
            ib.process = None
            ib.game_started = False

    def test_crash_skips_when_covered(self):
        with unittest.mock.patch.object(ib, "record_restart_and_check_cap",
                                        return_value=True), \
             unittest.mock.patch.object(ib, "unlist_self_via_api",
                                        return_value=True) as un, \
             unittest.mock.patch.object(ib, "family_listing_ids",
                                        return_value=["live-id"]), \
             unittest.mock.patch.object(ib, "spawn_new_instance") as sp:
            ib.maybe_spawn_crash_replacement()
        un.assert_called_once_with()
        sp.assert_not_called()

    def test_crash_spawns_when_clean(self):
        with unittest.mock.patch.object(ib, "record_restart_and_check_cap",
                                        return_value=True), \
             unittest.mock.patch.object(ib, "unlist_self_via_api",
                                        return_value=True), \
             unittest.mock.patch.object(ib, "family_listing_ids",
                                        return_value=[]), \
             unittest.mock.patch.object(ib, "spawn_new_instance") as sp:
            ib.maybe_spawn_crash_replacement()
        sp.assert_called_once_with("", [])

    def test_crash_refuses_short_uptime(self):
        ib.instance_start_time = ib.time.time()
        with unittest.mock.patch.object(ib, "spawn_new_instance") as sp:
            ib.maybe_spawn_crash_replacement()
        sp.assert_not_called()

class ExitReasonTests(unittest.TestCase):
    def setUp(self):
        self._old = (ib.config, ib.lobby_id, ib.instance_start_time)
        ib.config = {"game_name": "FreeHost", "migration_name_suffix": " 2"}
        ib.lobby_id = "own-id"
        ib.instance_start_time = ib.time.time() - 7200

    def tearDown(self):
        (ib.config, ib.lobby_id, ib.instance_start_time) = self._old

    def _clean(self):
        return (unittest.mock.patch.object(ib, "record_restart_and_check_cap",
                                           return_value=True),
                unittest.mock.patch.object(ib, "unlist_self_via_api",
                                           return_value=True),
                unittest.mock.patch.object(ib, "family_listing_ids",
                                           return_value=[]))

    def test_game_end_label(self):
        p1, p2, p3 = self._clean()
        with p1, p2, p3, \
             unittest.mock.patch.object(ib, "spawn_new_instance") as sp, \
             unittest.mock.patch.object(ib, "log") as lg:
            ib.maybe_spawn_crash_replacement(reason="game-ended")
        sp.assert_called_once()
        msgs = " ".join(str(c.args[1]) for c in lg.call_args_list)
        self.assertIn("Game ended", msgs)
        self.assertNotIn("crashed", msgs)

    def test_crash_label_default(self):
        p1, p2, p3 = self._clean()
        with p1, p2, p3, \
             unittest.mock.patch.object(ib, "spawn_new_instance") as sp, \
             unittest.mock.patch.object(ib, "log") as lg:
            ib.maybe_spawn_crash_replacement()
        sp.assert_called_once()
        msgs = " ".join(str(c.args[1]) for c in lg.call_args_list)
        self.assertIn("crashed", msgs)
