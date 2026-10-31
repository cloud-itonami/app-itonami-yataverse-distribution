"""Real HTTP failure boundaries and no-takeover checks for JUN-228 ops tools."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

from probe_reader import probe, provider


DATA = b'bounded operator delivery probe'
CID = 'b' + base64.b32encode(b'\x01\x55\x12\x20' + hashlib.sha256(DATA).digest()).decode().lower().rstrip('=')


class ReaderBoundaries(unittest.TestCase):
    def test_http_identity_redirect_size_and_deadline(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            mode = 'good'
            def log_message(self, *_):
                pass
            def do_GET(self):
                requests.append(self.path)
                if self.mode == 'redirect':
                    self.send_response(302)
                    self.send_header('Location', '/unapproved')
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                if self.mode == 'slow':
                    time.sleep(.3)
                body = {'good': DATA, 'wrong': b'x' * len(DATA),
                        'oversize': DATA + b'x', 'slow': DATA}[self.mode]
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = 'http://127.0.0.1:' + str(server.server_port)
            self.assertTrue(probe('ok', base, CID, len(DATA), 1)['verified'])
            for mode in ['wrong', 'oversize', 'redirect']:
                Handler.mode = mode
                r = probe(mode, base, CID, len(DATA), 1)
                self.assertFalse(r['verified'])
            self.assertNotIn('/unapproved', requests)
            Handler.mode = 'slow'
            r = probe('slow', base, CID, len(DATA), .05)
            self.assertFalse(r['verified'])
            self.assertLess(r['seconds'], .25)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_reject_non_loopback_plaintext_or_ambient_url_components(self):
        for value in ['p=http://10.0.0.1', 'p=http://localhost',
                      'p=https://user:pw@example.com', 'p=https://example.com/x',
                      'p=https://example.com?x=y', 'p=https://example.com#x']:
            with self.assertRaises(ValueError):
                provider(value)
        self.assertEqual(provider('p=https://example.com'), ('p', 'https://example.com'))


class RouterBoundaries(unittest.TestCase):
    def run_case(self, *, apply=False, owner='', actual='220.146.177.126',
                 after_owner='443->192.168.1.28:443', listing_exit=0):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            mock = root / 'mock'
            mock.write_text('''#!/bin/sh
name=${0##*/}
case "$name" in
ip) echo '2: eth0 inet 192.168.1.28/24';;
systemctl|nginx) exit 0;;
curl) echo 'f616962875a0850efa824b53be45fc22edce39f4c280a32cb80b72a55b020188';;
timeout) shift; exec "$@";;
upnpc)
 case " $* " in
  *' -l '*)
    echo "ExternalIPAddress = $ACTUAL"
    if [ -f "$CASE_DIR/applied" ]; then owner=$AFTER_OWNER; else owner=$OWNER; fi
    if [ -n "$owner" ]; then
      printf '%s\\n' "$owner" | while IFS= read -r item; do
        echo "0 TCP $item 'https' '' 900"
      done
    fi
    exit "$LIST_EXIT";;
  *' -a '*) echo apply >> "$CASE_DIR/calls"; touch "$CASE_DIR/applied"
    echo "external $ACTUAL:443 TCP is redirected to internal 192.168.1.28:443";;
  *) exit 9;;
 esac;;
*) exit 10;;
esac
''')
            mock.chmod(0o755)
            for command in ['ip', 'systemctl', 'nginx', 'curl', 'timeout', 'upnpc']:
                (root / command).symlink_to(mock)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                       CASE_DIR=d, ACTUAL=actual, OWNER=owner, AFTER_OWNER=after_owner,
                       LIST_EXIT=str(listing_exit))
            argv = ['/bin/sh', str(Path(__file__).with_name('renew_public_https.sh'))]
            if apply:
                argv.append('--apply')
            r = subprocess.run(argv + ['220.146.177.126'], env=env, capture_output=True)
            calls = (root / 'calls').read_text() if (root / 'calls').exists() else ''
            return r, calls

    def test_dry_run_and_ownership_or_wan_drift_never_mutate(self):
        r, calls = self.run_case()
        self.assertEqual((r.returncode, calls), (0, ''))
        for case in [dict(owner='443->192.168.1.16:443'),
                     dict(actual='220.146.170.114'), dict(listing_exit=1),
                     dict(owner='443->192.168.1.28:443\n443->192.168.1.16:443')]:
            r, calls = self.run_case(apply=True, **case)
            self.assertEqual((r.returncode, calls), (2, ''))

    def test_apply_requires_ownership_readback(self):
        r, calls = self.run_case(apply=True)
        self.assertEqual((r.returncode, calls), (0, 'apply\n'))
        r, calls = self.run_case(apply=True, after_owner='443->192.168.1.16:443')
        self.assertEqual((r.returncode, calls), (2, 'apply\n'))


if __name__ == '__main__':
    unittest.main()
