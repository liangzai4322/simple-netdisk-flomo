#!/usr/bin/env python3
"""Obtain drive.file credentials with a Desktop OAuth client and a loopback callback.

Run on the desktop; open the printed URL in the specified account's Chrome profile.
Secrets are written with mode 0600, never printed. This does not change sharing.
"""
import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-secret", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--account", required=True)
    args = parser.parse_args()
    client = json.loads(args.client_secret.read_text())["installed"]
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Authorization codes must not enter HTTP logs.

        def do_GET(self):
            values = parse_qs(urlsplit(self.path).query)
            if urlsplit(self.path).path != "/callback" or values.get("state") != [state]:
                self.send_error(400)
                return
            result.update(values)
            body = "授权已收到，请返回 Codex。".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Callback)
    server.timeout = 300
    redirect = "http://127.0.0.1:%s/callback" % server.server_port
    scope = "https://www.googleapis.com/auth/drive.file"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect,
        "response_type": "code", "scope": scope, "access_type": "offline",
        "prompt": "consent", "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "login_hint": args.account,
    })
    print(url, flush=True)
    deadline = time.monotonic() + 600
    while not result and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()
    if "code" not in result:
        sys.exit("Authorization was declined")
    request = Request("https://oauth2.googleapis.com/token", data=urlencode({
        "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
        "code": result["code"][0], "code_verifier": verifier,
        "redirect_uri": redirect, "grant_type": "authorization_code",
    }).encode())
    with urlopen(request, timeout=45) as response:
        token = json.load(response)
    if not token.get("refresh_token") or scope not in token.get("scope", "").split():
        sys.exit("Required offline drive.file permission was not granted")
    credentials = {"type": "authorized_user", "client_id": client["client_id"],
                   "client_secret": client.get("client_secret", ""), "refresh_token": token["refresh_token"]}
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(credentials, handle)
    print("Saved credentials with mode 0600: " + str(args.output), flush=True)


if __name__ == "__main__":
    main()
