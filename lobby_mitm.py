#!/usr/bin/env python3
"""
lobby_mitm.py — transparent TLS-intercept proxy for the Warzone 2100 lobby.

Purpose: capture the lobby-issued ``hostToken`` ( dealt out in the
``POST /api/v1/game`` create response ) so Python can make authenticated
lobby API calls (notably ``DELETE /api/v1/game/<id>``) on behalf of a
stock, unmodified Warzone 2100 instance.

Why transparent instead of env proxy: WZ sets CURLOPT_NOPROXY "*" for lobby
traffic, so https_proxy env vars are ignored. Intercept at NAT layer:

    iptables -t nat -A OUTPUT -m owner --uid-owner <wz-user> \\
        -p tcp --dport 443 -j REDIRECT --to-port 8080

Only SNI-matching hosts (default: wzlobby.wz2100.net) are intercepted;
everything else is relayed byte-for-byte to its original destination.

Requirements on the intercepted instance:
  * WZ must trust our CA (system CA store). Prefer the apt/system WZ binary;
    the flatpak runtime may ship its own CA bundle.
  * ``openssl`` CLI must exist (CA + per-SNI cert generation).

Run (as root, or with NET_ADMIN for REDIRECT + cert paths readable):
    python3 lobby_mitm.py --port 8080 --ca mitm-ca \\
        --tokens instance_configs/.lobby_tokens.json

Stdlib only. One thread per connection; lobby traffic is tiny.
"""
import argparse
import datetime
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_SNI_ALLOW = ["wzlobby.wz2100.net"]
TOKEN_STORE_DEFAULT = os.path.join(SCRIPT_DIR, "instance_configs", ".lobby_tokens.json")

SO_ORIGINAL_DST = 80  # linux/netfilter_ipv4.h


# ─── logging ──────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [mitm] {msg}", flush=True, file=sys.stderr)


# ─── TLS ClientHello SNI parsing ──────────────────────────────────────────────

def extract_sni(data: bytes) -> str | None:
    """Return the SNI hostname from a TLS ClientHello, or None if incomplete."""
    try:
        if len(data) < 5 or data[0] != 0x16:
            return None
        rec_len = int.from_bytes(data[3:5], "big")
        if len(data) < 5 + rec_len:
            return None  # need more bytes
        hs = data[5:5 + rec_len]
        if len(hs) < 4 or hs[0] != 0x01:  # not ClientHello
            return None
        hs_len = int.from_bytes(hs[1:4], "big")
        if len(hs) < 4 + hs_len:
            return None
        p = 4 + 2 + 32  # body + client_version + random
        if p + 1 > len(hs):
            return None
        sess_len = hs[p]
        p += 1 + sess_len
        if p + 2 > len(hs):
            return None
        cs_len = int.from_bytes(hs[p:p + 2], "big")
        p += 2 + cs_len
        if p + 1 > len(hs):
            return None
        comp_len = hs[p]
        p += 1 + comp_len
        if p + 2 > len(hs):
            return None
        ext_total = int.from_bytes(hs[p:p + 2], "big")
        p += 2
        end = p + ext_total
        while p + 4 <= end and p + 4 <= len(hs):
            ext_type = int.from_bytes(hs[p:p + 2], "big")
            ext_len = int.from_bytes(hs[p + 2:p + 4], "big")
            p += 4
            if ext_type == 0:  # server_name
                if p + 2 > len(hs):
                    return None
                lst_len = int.from_bytes(hs[p:p + 2], "big")
                q = p + 2
                q_end = q + lst_len
                while q + 3 <= q_end and q + 3 <= len(hs):
                    if hs[q] == 0:  # host_name
                        name_len = int.from_bytes(hs[q + 1:q + 3], "big")
                        q += 3
                        return hs[q:q + name_len].decode("ascii", errors="replace")
                    else:
                        name_len = int.from_bytes(hs[q + 1:q + 3], "big")
                        q += 3 + name_len
                return None
            p += ext_len
        return None
    except Exception:
        return None


def get_original_dst(conn: socket.socket) -> tuple[str, int] | None:
    """SO_ORIGINAL_DST of a REDIRECTed socket. Returns (ip, port) or None."""
    try:
        raw = conn.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
        _, port, a, b, c, d = struct.unpack("!HHBBBB", raw[:8])
        return (f"{a}.{b}.{c}.{d}", port)
    except Exception:
        return None


# ─── CA / per-SNI certs (via openssl CLI) ────────────────────────────────────

def ensure_ca(ca_dir: str) -> tuple[str, str]:
    """Create ca.key/ca.crt if missing. Returns (key_path, crt_path)."""
    os.makedirs(ca_dir, exist_ok=True)
    key = os.path.join(ca_dir, "ca.key")
    crt = os.path.join(ca_dir, "ca.crt")
    if not (os.path.exists(key) and os.path.exists(crt)):
        log(f"Generating MITM CA in {ca_dir} (keep ca.key secret!)")
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", key, "-out", crt, "-days", "825",
             "-subj", "/CN=WZ2100-Lobby-MITM",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
            check=True, capture_output=True,
        )
        os.chmod(key, 0o600)
    return key, crt


def ensure_server_cert(ca_key: str, ca_crt: str, certs_dir: str, sni: str) -> tuple[str, str]:
    """Issue (and cache) a server cert for sni signed by our CA."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", sni)
    os.makedirs(certs_dir, exist_ok=True)
    key = os.path.join(certs_dir, f"{safe}.key")
    crt = os.path.join(certs_dir, f"{safe}.crt")
    if not (os.path.exists(key) and os.path.exists(crt)):
        log(f"Issuing forged cert for {sni}")
        csr = os.path.join(certs_dir, f"{safe}.csr")
        ext = os.path.join(certs_dir, f"{safe}.ext")
        with open(ext, "w", encoding="utf-8") as f:
            f.write(f"subjectAltName=DNS:{sni}\n")
        subprocess.run(
            ["openssl", "req", "-newkey", "rsa:2048", "-nodes",
             "-keyout", key, "-out", csr, "-subj", f"/CN={sni}"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["openssl", "x509", "-req", "-in", csr,
             "-CA", ca_crt, "-CAkey", ca_key, "-CAcreateserial",
             "-days", "825", "-extfile", ext, "-out", crt],
            check=True, capture_output=True,
        )
        for tmpf in (csr, ext):
            try:
                os.remove(tmpf)
            except Exception:
                pass
    return crt, key


# ─── token capture ────────────────────────────────────────────────────────────

GAMEID_RE = re.compile(rb'"gameId"\s*:\s*"([^"]{4,256})"')
HOSTTOKEN_RE = re.compile(rb'"hostToken"\s*:\s*"([^"]{4,1024})"')
LOBBYTOKEN_RE = re.compile(rb'"lobbyToken"\s*:\s*"([^"]{4,1024})"')

_tokens_lock = threading.Lock()


def extract_tokens(buf: bytes) -> dict | None:
    """Pull {game_id, host_token, lobby_token?} from a response buffer."""
    m_id = GAMEID_RE.search(buf)
    m_tok = HOSTTOKEN_RE.search(buf)
    if not m_id or not m_tok:
        return None
    out = {
        "game_id": m_id.group(1).decode("ascii", errors="replace"),
        "host_token": m_tok.group(1).decode("ascii", errors="replace"),
    }
    m_lob = LOBBYTOKEN_RE.search(buf)
    if m_lob:
        out["lobby_token"] = m_lob.group(1).decode("ascii", errors="replace")
    return out


def store_token(store_path: str, game_id: str, host_token: str, lobby_token: str | None = None):
    """Persist a captured token (0600, atomic replace)."""
    with _tokens_lock:
        data = {}
        try:
            if os.path.exists(store_path):
                with open(store_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        data = loaded
        except Exception:
            data = {}
        entry = {"host_token": host_token, "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        if lobby_token:
            entry["lobby_token"] = lobby_token
        if data.get(game_id) == entry:
            return
        data[game_id] = entry
        os.makedirs(os.path.dirname(store_path) or ".", exist_ok=True)
        tmp = store_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, store_path)
        try:
            os.chmod(store_path, 0o600)
        except Exception:
            pass
        log(f"Captured hostToken for game {game_id}")


def load_lobby_token(store_path: str, game_id: str) -> dict | None:
    """Return stored token entry for game_id, or None."""
    try:
        with open(store_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get(game_id), dict):
            return data[game_id]
    except Exception:
        pass
    return None


# ─── lobby API calls on behalf of an instance ─────────────────────────────────

def build_lobby_headers(host_token: str, referer_kind: str = "mp.host") -> dict:
    return {
        "Authorization": f"HostToken {host_token}",
        "Origin": "wz2100://wz2100.net",
        "Referer": f"https://github.com/Warzone2100/warzone2100?wz_internal={referer_kind}",
        "Accept": "application/json",
        "User-Agent": "Warzone2100 (+https://github.com/Warzone2100/warzone2100)",
    }


def delete_lobby_listing(game_id: str, host_token: str,
                         lobby_address: str = "https://wzlobby.wz2100.net/lobby",
                         started: bool = False, timeout: int = 30) -> tuple[bool, str]:
    """DELETE a game listing with the instance's own credential.

    Returns (ok, detail). 2xx counts as success (listing gone).
    """
    import urllib.request
    import urllib.error
    url = f"{lobby_address.rstrip('/')}/api/v1/game/{game_id}"
    if started:
        url += "?started=1"
    req = urllib.request.Request(url, method="DELETE", headers=build_lobby_headers(host_token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", 200)
            return (200 <= code < 300, f"HTTP {code}")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            body = ""
        return (False, f"HTTP {e.code} {body}")
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")


def delete_listing_by_game_id(game_id: str, store_path: str = TOKEN_STORE_DEFAULT,
                              lobby_address: str = "https://wzlobby.wz2100.net/lobby",
                              started: bool = False) -> tuple[bool, str]:
    """Load the captured token for game_id and DELETE its listing."""
    entry = load_lobby_token(store_path, game_id)
    if not entry or not entry.get("host_token"):
        return (False, f"no captured hostToken for game {game_id}")
    return delete_lobby_listing(game_id, entry["host_token"], lobby_address, started)


# ─── relay ────────────────────────────────────────────────────────────────────

BUF = 65536


def _pump(src: socket.socket, dst: socket.socket, sniffer=None):
    try:
        while True:
            chunk = src.recv(BUF)
            if not chunk:
                break
            if sniffer is not None:
                try:
                    sniffer(chunk)
                except Exception:
                    pass
            dst.sendall(chunk)
    except Exception:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass


class TokenSniffer:
    """Incremental scanner for create-response credentials (server -> client)."""

    def __init__(self, store_path: str, maxbuf: int = 256 * 1024):
        self.store_path = store_path
        self.buf = b""
        self.maxbuf = maxbuf
        self.done_ids: set = set()

    def __call__(self, chunk: bytes):
        self.buf += chunk
        if len(self.buf) > self.maxbuf:
            self.buf = self.buf[-self.maxbuf:]
        found = extract_tokens(self.buf)
        if found and found["game_id"] not in self.done_ids:
            self.done_ids.add(found["game_id"])
            store_token(self.store_path, found["game_id"],
                        found["host_token"], found.get("lobby_token"))


def strip_accept_encoding(head: bytes) -> bytes:
    """Force identity encoding so captured bodies stay plaintext JSON."""
    lines = head.split(b"\r\n")
    kept = [ln for ln in lines if not ln.lower().startswith(b"accept-encoding:")]
    if len(kept) != len(lines):
        kept.insert(1, b"Accept-Encoding: identity")
    return b"\r\n".join(kept)


def handle_client(client: socket.socket, sni_allow: list, ca_key: str, ca_crt: str,
                  certs_dir: str, store_path: str, upstream_cafile: str | None = None):
    try:
        client.settimeout(15)
        orig_dst = get_original_dst(client)
        # Peek (not consume) so the real socket can be handed to TLS below.
        data = b""
        sni = None
        while len(data) < 65536:
            try:
                peeked = client.recv(16384, socket.MSG_PEEK)
            except Exception:
                break
            if not peeked:
                break
            data = peeked
            sni = extract_sni(data)
            if sni is not None or (data and data[0] != 0x16):
                break
        if sni and sni in sni_allow:
            handle_intercepted(client, sni, orig_dst, ca_key, ca_crt, certs_dir, store_path,
                                 upstream_cafile)
        else:
            # Not TLS or not our target: nothing consumed, relay as-is.
            handle_passthrough_prefixed(client, b"", orig_dst, sni)
    except Exception as e:
        log(f"connection error: {type(e).__name__}: {e}")
        try:
            client.close()
        except Exception:
            pass


def handle_intercepted(client: socket.socket, sni: str, orig_dst: tuple[str, int] | None,
                               ca_key: str, ca_crt: str, certs_dir: str, store_path: str,
                               upstream_cafile: str | None = None):
    upstream_host = orig_dst[0] if orig_dst else sni
    upstream_port = orig_dst[1] if orig_dst else 443
    crt, key = ensure_server_cert(ca_key, ca_crt, certs_dir, sni)
    import ssl as _ssl
    server_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(crt, key)
    tls_client = None
    upstream = None
    try:
        tls_client = server_ctx.wrap_socket(client, server_side=True)
        head = b""
        tls_client.settimeout(15)
        while b"\r\n\r\n" not in head and len(head) < 65536:
            chunk = tls_client.recv(BUF)
            if not chunk:
                return
            head += chunk
        cut = head.find(b"\r\n\r\n") + 4
        head, rest = head[:cut], head[cut:]
        head = strip_accept_encoding(head)
        upstream_ctx = (_ssl.create_default_context(cafile=upstream_cafile)
                          if upstream_cafile else _ssl.create_default_context())
        upstream = upstream_ctx.wrap_socket(
            socket.create_connection((upstream_host, upstream_port), timeout=15),
            server_hostname=sni,
        )
        upstream.sendall(head + rest)
        sniffer = TokenSniffer(store_path)
        t1 = threading.Thread(target=_pump, args=(tls_client, upstream), daemon=True)
        t2 = threading.Thread(target=_pump, args=(upstream, tls_client, sniffer), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        log(f"intercepted session for {sni} closed")
    except Exception as e:
        log(f"intercept {sni} error: {type(e).__name__}: {e}")
    finally:
        for s in (tls_client, upstream):
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        # tls_client.close() already closes the underlying socket; guard anyway.
        try:
            client.close()
        except Exception:
            pass


def handle_passthrough_prefixed(client: socket.socket, prefix: bytes,
                                orig_dst: tuple[str, int] | None, sni: str | None):
    target = orig_dst or ((sni, 443) if sni else None)
    if not target:
        try:
            client.close()
        except Exception:
            pass
        return
    try:
        upstream = socket.create_connection(target, timeout=15)
    except Exception as e:
        log(f"passthrough to {target} failed: {e}")
        try:
            client.close()
        except Exception:
            pass
        return
    try:
        if prefix:
            upstream.sendall(prefix)
    except Exception:
        pass
    t1 = threading.Thread(target=_pump, args=(client, upstream), daemon=True)
    t2 = threading.Thread(target=_pump, args=(upstream, client), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    for s in (client, upstream):
        try:
            s.close()
        except Exception:
            pass


def serve(port: int, sni_allow: list, ca_dir: str, certs_dir: str, store_path: str,
          upstream_cafile: str | None = None):
    ca_key, ca_crt = ensure_ca(ca_dir)
    log(f"CA ready: {ca_crt} (install this cert into the system trust store)")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(64)
    log(f"Listening on 127.0.0.1:{port}, intercepting SNI {sni_allow}")
    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(
                target=handle_client,
                args=(conn, sni_allow, ca_key, ca_crt, certs_dir, store_path,
                      upstream_cafile),
                daemon=True,
            ).start()
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description="Transparent lobby MITM token capturer")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--ca", default=os.path.join(SCRIPT_DIR, "mitm-ca"),
                    help="dir holding ca.key/ca.crt")
    ap.add_argument("--certs", default=os.path.join(SCRIPT_DIR, "mitm-ca", "certs"),
                    help="dir for cached per-SNI certs")
    ap.add_argument("--tokens", default=TOKEN_STORE_DEFAULT)
    ap.add_argument("--sni", default=",".join(DEFAULT_SNI_ALLOW),
                    help="comma-separated SNI allowlist to intercept")
    ap.add_argument("--upstream-cafile", default=None,
                    help="extra CA bundle for verifying upstream (testing only)")
    args = ap.parse_args()
    serve(args.port, [s.strip() for s in args.sni.split(",") if s.strip()],
          args.ca, args.certs, args.tokens, args.upstream_cafile)


if __name__ == "__main__":
    main()
