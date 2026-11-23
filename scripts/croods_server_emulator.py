#!/usr/bin/env python3
"""
Emulator backendu Rovio Cloud dla "The Croods" (Stone 1.3.1) - stdlib only.

STATUS WIEDZY (z analizy binarki/Lua) - patrz komentarze [OK] / [GUESS]:
  [OK]    baza: https://cloud.rovio.com   (FUN_000525e0 @0x525e0)
  [OK]    POST /identity/2.0/access  - JSON: clientId, clientSignature, clientSalt,
          clientVersion, persistentGuid, installationId, deviceType, os, osVersion,
          [distributionChannel, locale, utcOffset, definition, buildId]
          odpowiedz 200 JSON: {"accessToken","refreshToken","expiresIn"}  (FUN_00506c68)
  [OK]    GET  /identity/2.0/config  - naglowek "X-Access-Token: <token>",
          odpowiedz 200 JSON: {"services":[{<id>,<url>,<properties{}>} ...]}  (FUN_00507634)
  [OK]    GET  <url uslugi czasu> + "time"  -> JSON {"time": <sekundy unix>}  (FUN_0051101c/FUN_005112ec)
  [GUESS] nazwy kluczy wewnatrz elementu "services" (literaly @0x6cb8f4.. nieczytelne
          statycznie) - serwer wysyla kilka aliasow naraz.
  [GUESS] identyfikator uslugi czasu, sciezki i format storage/assets
          (storage = protobuf + gzip wg naglowkow w binarce; schematu NIE zdekodowano).
          Serwer wiec: loguje wszystko, zapisuje surowe body do ./croods_data/raw/
          i dla storage odsyla to, co klient wczesniej wyslal (204 gdy brak = nowy profil).

Uruchom:  python croods_server.py --http 8080            (bez TLS, do testow curl)
          python croods_server.py --https 443 --cert cert.pem --key key.pem
"""
import argparse
import json
import os
import ssl
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "croods_data")
RAW = os.path.join(DATA, "raw")
STORE = os.path.join(DATA, "storage")
ASSETS = os.path.join(DATA, "assets")  # tu wrzuc oryginalne pliki (version_check, *.csv)
for d in (DATA, RAW, STORE, ASSETS):
    os.makedirs(d, exist_ok=True)

BASE = "https://cloud.rovio.com"  # nadpisywane --public-base
# Kandydaci na id uslug; id uslugi czasu jest nieznane ([GUESS]).
SERVICE_IDS = ["time", "cloud", "identity", "storage", "assets", "wallet3", "catalog3",
               "analytics", "friends", "profile", "sync", "push", "payment"]
_lock = threading.Lock()
_tokens = {}  # accessToken -> clientId
_counter = [0]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def show_body(b, limit=600):
    if not b:
        return "<empty>"
    try:
        t = b.decode("utf-8")
        if t.isprintable() or "\n" in t:
            return t[:limit] + ("..." if len(t) > limit else "")
    except UnicodeDecodeError:
        pass
    return "<%d bytes binary> hex=%s%s" % (len(b), b[:48].hex(), "..." if len(b) > 48 else "")


def services_doc(base):
    out = []
    for sid in SERVICE_IDS:
        url = "%s/%s/" % (base.rstrip("/"), sid)
        out.append({
            # aliasy nazw kluczy (prawdziwe nazwy nieznane):
            "id": sid, "name": sid, "serviceId": sid,
            "url": url, "uri": url, "endpoint": url, "baseUrl": url,
            "properties": {}, "parameters": {}, "config": {},
        })
    return {"services": out}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CloudEmu/0.1"

    def log_message(self, *a):  # wylaczamy domyslny log
        pass

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            return self.rfile.read(n)
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            data = b""
            while True:
                size = int(self.rfile.readline().strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    break
                data += self.rfile.read(size)
                self.rfile.readline()
            return data
        return b""

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        if body or code != 204:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)
        log("  <-- %d %s | %s" % (code, ctype, show_body(body, 300)))

    def handle_any(self):
        body = self._read()
        host = self.headers.get("Host", "?")
        full = "%s://%s%s" % ("https" if isinstance(self.connection, ssl.SSLSocket) else "http", host, self.path)
        with _lock:
            _counter[0] += 1
            n = _counter[0]
        log("=" * 70)
        log("#%d %s %s" % (n, self.command, full))
        for k, v in self.headers.items():
            log("  %s: %s" % (k, v))
        log("  BODY: " + show_body(body))
        if body:
            with open(os.path.join(RAW, "%05d_%s_%s.bin" % (n, self.command, self.path.strip("/").replace("/", "_")[:60])), "wb") as f:
                f.write(body)
        try:
            self.route(body)
        except Exception as e:  # nie zabijaj polaczenia
            log("  !! blad routingu:", repr(e))
            self._send(500, {"error": str(e)})

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = handle_any

    # --------------------------------------------------------------------
    def route(self, body):
        path = self.path.split("?", 1)[0]
        base = BASE if BASE else "https://%s" % self.headers.get("Host", "cloud.rovio.com")

        # [OK] identity level 1 login
        if path.startswith("/identity/2.0/access") and self.command == "POST":
            try:
                req = json.loads(body or b"{}")
            except ValueError:
                req = {}
            tok = uuid.uuid4().hex
            _tokens[tok] = req.get("clientId", "")
            log("  login clientId=%s installationId=%s" % (req.get("clientId"), req.get("installationId")))
            return self._send(200, {"accessToken": tok, "refreshToken": uuid.uuid4().hex, "expiresIn": 86400})

        # [OK] konfiguracja uslug
        if path.startswith("/identity/2.0/config"):
            return self._send(200, services_doc(base))

        # [OK] czas serwera: <url>time -> {"time": sekundy}
        if path.rstrip("/").endswith("time") and self.command == "GET":
            return self._send(200, {"time": int(time.time())})

        # [GUESS] storage - przechowaj surowe body, odeslij je przy odczycie
        if "/storage" in path or "sync" in path:
            key = path.strip("/").replace("/", "_")
            fn = os.path.join(STORE, key[:120] or "root")
            if self.command in ("POST", "PUT"):
                with open(fn, "wb") as f:
                    f.write(body)
                return self._send(200, b"{}")
            if self.command == "GET":
                if os.path.exists(fn):
                    with open(fn, "rb") as f:
                        data = f.read()
                    return self._send(200, data, "application/octet-stream")
                return self._send(204)  # brak zapisu = nowa gra (Lua: kod 204)

        # [GUESS] assets - plik z croods_data/assets/<ostatni segment>
        if "/asset" in path:
            name = os.path.basename(path)
            fn = os.path.join(ASSETS, name)
            if os.path.isfile(fn):
                with open(fn, "rb") as f:
                    return self._send(200, f.read(), "application/octet-stream")
            log("  (brak pliku asset %r w %s)" % (name, ASSETS))
            return self._send(404, {"error": "asset not found"})

        log("  (nieobsluzona sciezka - zwracam 404; sprawdz log i dopisz obsluge)")
        return self._send(404, {"error": "unhandled", "path": path})


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", type=int, help="port HTTP (bez TLS)")
    ap.add_argument("--https", type=int, help="port HTTPS")
    ap.add_argument("--cert")
    ap.add_argument("--key")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--public-base", default="https://cloud.rovio.com",
                    help="baza URL wpisywana do /identity/2.0/config")
    a = ap.parse_args()
    BASE = a.public_base
    if not a.http and not a.https:
        a.http = 8080
    servers = []
    if a.http:
        servers.append(ThreadingHTTPServer((a.bind, a.http), H))
        log("HTTP  na %s:%d" % (a.bind, a.http))
    if a.https:
        if not (a.cert and a.key):
            sys.exit("--https wymaga --cert i --key")
        s = ThreadingHTTPServer((a.bind, a.https), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(a.cert, a.key)
        s.socket = ctx.wrap_socket(s.socket, server_side=True)
        servers.append(s)
        log("HTTPS na %s:%d" % (a.bind, a.https))
    log("dane:", DATA)
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
