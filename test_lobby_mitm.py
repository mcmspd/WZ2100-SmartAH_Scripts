#!/usr/bin/env python3
"""Unit + loopback tests for lobby_mitm.py (stdlib only)."""
import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lobby_mitm as mitm

HAVE_OPENSSL = shutil.which("openssl") is not None


def build_client_hello(sni: str) -> bytes:
    """Minimal valid TLS ClientHello advertising sni (RFC 6066 ServerNameList)."""
    name = sni.encode()
    entry = b"\x00" + len(name).to_bytes(2, "big") + name
    lst = len(entry).to_bytes(2, "big") + entry
    extensions = b"\x00\x00" + len(lst).to_bytes(2, "big") + lst
    body = (b"\x03\x03" + b"R" * 32 + b"\x00"
            + b"\x00\x02\x13\x01" + b"\x01\x00"
            + len(extensions).to_bytes(2, "big") + extensions)
    hs = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(hs).to_bytes(2, "big") + hs


class SniTests(unittest.TestCase):
    def test_extract(self):
        self.assertEqual(mitm.extract_sni(build_client_hello("wzlobby.wz2100.net")),
                         "wzlobby.wz2100.net")

    def test_incomplete_returns_none(self):
        full = build_client_hello("example.com")
        self.assertIsNone(mitm.extract_sni(full[:20]))

    def test_non_tls_returns_none(self):
        self.assertIsNone(mitm.extract_sni(b"GET / HTTP/1.0\r\n\r\n"))


class TokenTests(unittest.TestCase):
    def test_split_chunks(self):
        buf = b'{"gameId": "abc-123", "hostToken": "tok-'
        self.assertIsNone(mitm.extract_tokens(buf))
        buf += b'xyz", "lobbyToken": "lob-1"}'
        found = mitm.extract_tokens(buf)
        self.assertEqual(found, {"game_id": "abc-123", "host_token": "tok-xyz",
                                 "lobby_token": "lob-1"})

    def test_no_match(self):
        self.assertIsNone(mitm.extract_tokens(b'{"ok": true}'))

    def test_store_roundtrip_and_perms(self):
        tmp = tempfile.mkdtemp()
        try:
            store = os.path.join(tmp, ".lobby_tokens.json")
            mitm.store_token(store, "g1", "tok", "lob")
            entry = mitm.load_lobby_token(store, "g1")
            self.assertEqual(entry["host_token"], "tok")
            self.assertEqual(entry["lobby_token"], "lob")
            mode = stat.S_IMODE(os.stat(store).st_mode)
            self.assertEqual(mode, 0o640)
            self.assertIsNone(mitm.load_lobby_token(store, "nope"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DeleteTests(unittest.TestCase):
    def test_headers_and_url(self):
        import urllib.request
        seen = {}

        class FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            seen["auth"] = req.get_header("Authorization")
            seen["origin"] = req.get_header("Origin")
            return FakeResp()

        with unittest.mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            ok, detail = mitm.delete_lobby_listing(
                "gid-9", "tok-9", lobby_address="https://example.invalid/lobby")
        self.assertTrue(ok)
        self.assertEqual(seen["url"], "https://example.invalid/lobby/api/v1/game/gid-9")
        self.assertEqual(seen["method"], "DELETE")
        self.assertEqual(seen["auth"], "HostToken tok-9")
        self.assertEqual(seen["origin"], "wz2100://wz2100.net")

    def test_http_error(self):
        import urllib.request
        import urllib.error
        err = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
        with unittest.mock.patch.object(urllib.request, "urlopen", side_effect=err):
            ok, detail = mitm.delete_lobby_listing("g", "bad")
        self.assertFalse(ok)
        self.assertIn("401", detail)

    def test_missing_token(self):
        ok, detail = mitm.delete_listing_by_game_id("g", store_path="/nonexistent/x.json")
        self.assertFalse(ok)
        self.assertIn("no captured hostToken", detail)


@unittest.skipUnless(HAVE_OPENSSL, "openssl CLI required")
def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _mkcert(d, cn):
    key = os.path.join(d, f"{cn}.key")
    crt = os.path.join(d, f"{cn}.crt")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", key, "-out", crt, "-days", "2",
                    "-subj", f"/CN={cn}", "-addext", f"subjectAltName=DNS:{cn}"],
                   check=True, capture_output=True)
    return crt, key


class LoopbackProxyTests(unittest.TestCase):
    """Real TLS through the proxy: forged cert accepted, token captured."""

    def test_intercept_and_capture(self):
        tmp = tempfile.mkdtemp()
        try:
            ca_dir = os.path.join(tmp, "ca")
            certs_dir = os.path.join(tmp, "certs")
            store = os.path.join(tmp, "tokens.json")

            # Fake upstream lobby: serves canned create-response over TLS.
            up_crt, up_key = _mkcert(tmp, "upstream.test")
            up_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            up_ctx.load_cert_chain(up_crt, up_key)
            up_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            up_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            up_srv.bind(("127.0.0.1", 0))
            up_srv.listen(5)
            up_port = up_srv.getsockname()[1]
            body = json.dumps({"gameId": "game-1", "hostToken": "tok-1",
                               "lobbyToken": "lob-1"}).encode()
            stop = threading.Event()

            def upstream():
                up_srv.settimeout(10)
                while not stop.is_set():
                    try:
                        c, _ = up_srv.accept()
                    except socket.timeout:
                        continue
                    try:
                        t = up_ctx.wrap_socket(c, server_side=True)
                        req = b""
                        while b"\r\n\r\n" not in req:
                            chunk = t.recv(4096)
                            if not chunk:
                                break
                            req += chunk
                        head_part = req.split(b"\r\n\r\n")[0]
                        self.assertNotIn(b"gzip", head_part)
                        self.assertIn(b"Accept-Encoding: identity", head_part)
                        resp = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                                b"Connection: close\r\n\r\n" + body)
                        t.sendall(resp)
                        t.close()
                    except Exception:
                        pass
            threading.Thread(target=upstream, daemon=True).start()

            # Proxy under test (trust the fake upstream's self-signed cert).
            proxy_port = _free_port()
            threading.Thread(target=mitm.serve,
                             args=(proxy_port, ["upstream.test"], ca_dir, certs_dir, store,
                                   os.path.join(tmp, "upstream.test.crt")),
                             daemon=True).start()
            time.sleep(0.5)
            mitm_ca = os.path.join(ca_dir, "ca.crt")
            self.assertTrue(os.path.exists(mitm_ca), "proxy must generate its CA")

            # Point the proxy at our fake upstream by overriding orig-dst lookup.
            real_get_dst = mitm.get_original_dst
            mitm.get_original_dst = lambda conn: ("127.0.0.1", up_port)
            try:
                cli_ctx = ssl.create_default_context(cafile=mitm_ca)
                cli = cli_ctx.wrap_socket(socket.create_connection(("127.0.0.1", proxy_port)),
                                          server_hostname="upstream.test")
                cli.sendall(b"POST /api/v1/game HTTP/1.1\r\nHost: upstream.test\r\n"
                            b"Accept-Encoding: gzip, deflate\r\nContent-Length: 2\r\n"
                            b"Connection: close\r\n\r\n{}")
                resp = b""
                while True:
                    chunk = cli.recv(4096)
                    if not chunk:
                        break
                    resp += chunk
                cli.close()
            finally:
                mitm.get_original_dst = real_get_dst
            stop.set()

            self.assertIn(b'"hostToken": "tok-1"', resp)
            entry = mitm.load_lobby_token(store, "game-1")
            self.assertIsNotNone(entry, "token must be captured to store")
            self.assertEqual(entry["host_token"], "tok-1")
            self.assertEqual(entry["lobby_token"], "lob-1")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class BlocklistTests(unittest.TestCase):
    def test_roundtrip(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "block.json")
            self.assertEqual(mitm.load_blocklist(path), set())
            self.assertEqual(mitm.load_blocklist(None), set())
            mitm.block_game(path, "g1")
            mitm.block_game(path, "g1")
            self.assertEqual(mitm.load_blocklist(path), {"g1"})
            mitm.unblock_game(path, "g1")
            self.assertEqual(mitm.load_blocklist(path), set())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_request_matching(self):
        head = b"PATCH /lobby/api/v1/game/g1 HTTP/1.1\r\nHost: x\r\n\r\n"
        self.assertEqual(mitm.request_targets_blocked_game(head, {"g1"}), "g1")
        self.assertIsNone(mitm.request_targets_blocked_game(head, {"g2"}))
        self.assertIsNone(mitm.request_targets_blocked_game(head, set()))
        create = b"POST /lobby/api/v1/game HTTP/1.1\r\nHost: x\r\n\r\n"
        self.assertIsNone(mitm.request_targets_blocked_game(create, {"g1"}))


class StorePermsTests(unittest.TestCase):
    def test_store_is_group_readable(self):
        import stat as _stat
        tmp = tempfile.mkdtemp()
        try:
            store = os.path.join(tmp, "t.json")
            mitm.store_token(store, "g1", "tok-1", None)
            mode = _stat.S_IMODE(os.stat(store).st_mode)
            self.assertEqual(mode, 0o640)
            self.assertEqual(mitm.load_lobby_token(store, "g1")["host_token"], "tok-1")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

class DeleteGoneTests(unittest.TestCase):
    def test_404_counts_as_freed(self):
        import urllib.request
        import urllib.error
        err = urllib.error.HTTPError(
            "u", 404, "not found", {},
            __import__("io").BytesIO(b'{"code":"GAME_NOT_FOUND"}'))
        with unittest.mock.patch.object(urllib.request, "urlopen", side_effect=err):
            ok, detail = mitm.delete_lobby_listing("g", "tok")
        self.assertTrue(ok)
        self.assertIn("slot free", detail)

class GameRelayTests(unittest.TestCase):
    def test_splice_and_target_file(self):
        import socket
        import threading
        received = []

        echo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        echo.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        echo.bind(("127.0.0.1", 0))
        echo.listen(1)
        echo_port = echo.getsockname()[1]

        def echo_loop():
            conn, _ = echo.accept()
            try:
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    received.append(data)
                    conn.sendall(data)
            finally:
                conn.close()

        threading.Thread(target=echo_loop, daemon=True).start()
        relay = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        relay.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        relay.bind(("127.0.0.1", 0))
        relay.listen(5)
        relay_port = relay.getsockname()[1]

        def accept_one():
            conn, _ = relay.accept()
            mitm.handle_game_client(conn, None, echo_port)

        threading.Thread(target=accept_one, daemon=True).start()
        c = socket.create_connection(("127.0.0.1", relay_port), timeout=5)
        c.sendall(b"ping-relay")
        self.assertEqual(c.recv(4096), b"ping-relay")
        c.close()
        echo.close()
        relay.close()
        self.assertEqual(b"".join(received), b"ping-relay")

    def test_target_file_routing(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            target = os.path.join(tmp, "target")
            open(target, "w").write("2345\n")
            self.assertEqual(mitm.read_relay_target(target, 2100), 2345)
            self.assertEqual(mitm.read_relay_target(os.path.join(tmp, "missing"), 2100), 2100)
            self.assertEqual(mitm.read_relay_target(None, 2100), 2100)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_closed_when_no_target(self):
        import socket
        import threading
        relay = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        relay.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        relay.bind(("127.0.0.1", 0))
        relay.listen(5)
        relay_port = relay.getsockname()[1]

        def accept_one():
            conn, _ = relay.accept()
            mitm.handle_game_client(conn, "/nonexistent-target-file-xyz", None)

        threading.Thread(target=accept_one, daemon=True).start()
        c = socket.create_connection(("127.0.0.1", relay_port), timeout=5)
        c.settimeout(5)
        self.assertEqual(c.recv(4096), b"")
        c.close()
        relay.close()


class ForgeTests(unittest.TestCase):
    def test_forge_response_shape(self):
        import json as _json
        # The canned body must parse as CheckJoinRequestResult {pass, error}.
        body = b'{"pass": true}'
        o = _json.loads(body)
        self.assertTrue(o["pass"] is True)

    def test_forge_intercepts_check_request(self):
        import socket
        import ssl
        import subprocess
        import threading
        import time
        import tempfile
        import json as _json
        tmp = tempfile.mkdtemp()
        try:
            ca_dir = os.path.join(tmp, "ca")
            certs_dir = os.path.join(tmp, "certs")
            store = os.path.join(tmp, "tokens.json")
            up_seen = []

            up_crt, up_key = _mkcert(tmp, "forge.test")
            up_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            up_ctx.load_cert_chain(up_crt, up_key)
            up_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            up_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            up_srv.bind(("127.0.0.1", 0))
            up_srv.listen(5)
            up_port = up_srv.getsockname()[1]
            stop = threading.Event()

            def upstream():
                up_srv.settimeout(10)
                while not stop.is_set():
                    try:
                        c, _ = up_srv.accept()
                    except socket.timeout:
                        continue
                    try:
                        t = up_ctx.wrap_socket(c, server_side=True)
                        req = b""
                        while b"\r\n\r\n" not in req:
                            chunk = t.recv(4096)
                            if not chunk:
                                break
                            req += chunk
                        up_seen.append(req.split(b"\r\n", 1)[0])
                        body = b'{"pass": false, "error": "denied"}'
                        t.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                  b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                                  b"Connection: close\r\n\r\n" + body)
                        t.close()
                    except Exception:
                        pass

            threading.Thread(target=upstream, daemon=True).start()
            proxy_port = _free_port()
            threading.Thread(
                target=mitm.serve, daemon=True,
                kwargs=dict(port=proxy_port, sni_allow=["forge.test"],
                            ca_dir=ca_dir, certs_dir=certs_dir, store_path=store,
                            upstream_cafile=os.path.join(tmp, "forge.test.crt"),
                            forge_join_checks=True)).start()
            time.sleep(0.5)
            real_get_dst = mitm.get_original_dst
            mitm.get_original_dst = lambda conn: ("127.0.0.1", up_port)
            try:
                cli_ctx = ssl.create_default_context(
                    cafile=os.path.join(ca_dir, "ca.crt"))
                cli = cli_ctx.wrap_socket(
                    socket.create_connection(("127.0.0.1", proxy_port)),
                    server_hostname="forge.test")
                body = b'{"joinToken": "x"}'
                cli.sendall(b"POST /lobby/api/v1/game/g/check-join-request HTTP/1.1\r\n"
                            b"Host: forge.test\r\nContent-Length: " + str(len(body)).encode() + b"\r\n"
                            b"Connection: close\r\n\r\n" + body)
                resp = b""
                while True:
                    chunk = cli.recv(4096)
                    if not chunk:
                        break
                    resp += chunk
                cli.close()
            finally:
                mitm.get_original_dst = real_get_dst
            stop.set()
            payload = resp.split(b"\r\n\r\n", 1)[1]
            self.assertEqual(_json.loads(payload), {"pass": True})
            # upstream must never have seen the check request (forged locally)
            self.assertFalse(any(b"check-join-request" in r for r in up_seen),
                             up_seen)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FragmentedHelloTests(unittest.TestCase):
    def test_fragmented_hello_still_intercepted(self):
        import socket
        import threading
        import time
        full = build_client_hello("wzlobby.wz2100.net")
        c1, c2 = socket.socketpair()
        try:
            seen = {}

            def fake_intercepted(client, sni, *a, **k):
                seen["sni"] = sni
                try:
                    client.close()
                except Exception:
                    pass

            with unittest.mock.patch.object(mitm, "handle_intercepted",
                                            side_effect=fake_intercepted):
                t = threading.Thread(
                    target=mitm.handle_client,
                    args=(c1, ["wzlobby.wz2100.net"], "k", "c", "d", "s"),
                    daemon=True)
                t.start()
                # Deliver the ClientHello in two fragments with a gap.
                c2.sendall(full[:20])
                time.sleep(0.5)
                c2.sendall(full[20:])
                t.join(timeout=10)
                self.assertFalse(t.is_alive(), "handle_client hung on fragmented hello")
            self.assertEqual(seen.get("sni"), "wzlobby.wz2100.net")
        finally:
            for s in (c1, c2):
                try:
                    s.close()
                except Exception:
                    pass

    def test_non_tls_passthrough_does_not_hang(self):
        import socket
        import threading
        c1, c2 = socket.socketpair()
        try:
            with unittest.mock.patch.object(
                    mitm, "handle_passthrough_prefixed") as pt:
                t = threading.Thread(
                    target=mitm.handle_client,
                    args=(c1, ["wzlobby.wz2100.net"], "k", "c", "d", "s"),
                    daemon=True)
                t.start()
                c2.sendall(b"GET / HTTP/1.0\r\n\r\n")
                t.join(timeout=10)
                self.assertFalse(t.is_alive(), "handle_client hung on plain HTTP")
                pt.assert_called_once()
        finally:
            for s in (c1, c2):
                try:
                    s.close()
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
