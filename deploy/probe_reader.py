#!/usr/bin/env python3
"""Bounded operations measurement, not an app runtime or native qualification.

Probe explicitly selected read-only providers sequentially. Never redirects,
discovers providers, retries, mutates retention, or changes production routing.
"""
import argparse
import datetime
import hashlib
import ipaddress
import json
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from raw_block_store import MAX_BLOCK, RawBlockStoreError, _sha256_digest, verify_cid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def provider(value):
    label, separator, base = value.partition('=')
    p = urllib.parse.urlsplit(base)
    if (not separator or not label or not p.hostname or p.username or p.password
            or p.query or p.fragment or p.path not in ('', '/')
            or p.scheme not in ('http', 'https')):
        raise ValueError('provider must be LABEL=https://ORIGIN (or loopback HTTP)')
    if p.scheme == 'http':
        try:
            if not ipaddress.ip_address(p.hostname).is_loopback:
                raise ValueError('plaintext HTTP is limited to loopback')
        except ValueError as exc:
            raise ValueError('plaintext HTTP is limited to numeric loopback') from exc
    # Validate port eagerly; no operation has begun yet.
    _ = p.port
    return label, base.rstrip('/')


def probe(label, base, cid, expected_bytes, timeout):
    start = time.monotonic()
    result = dict(provider=label, origin=base, cid=cid, expected_bytes=expected_bytes,
                  verified=False)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    # POSIX operations CLI only. A socket timeout alone permits a trickling
    # response to run forever. SIGALRM bounds DNS, headers, and body together.
    if signal.getitimer(signal.ITIMER_REAL)[0]:
        raise RuntimeError('probe cannot replace an existing process timer')
    def deadline(_signum, _frame):
        raise TimeoutError('total read deadline exceeded')
    previous_handler = signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        request = urllib.request.Request(base + '/ipfs/' + cid, method='GET',
                                         headers={'Accept': 'application/octet-stream'})
        with opener.open(request, timeout=timeout) as response:
            result['status'] = response.status
            body = bytearray()
            while len(body) <= expected_bytes:
                if time.monotonic() - start >= timeout:
                    raise TimeoutError('total read deadline exceeded')
                chunk = response.read1(min(65536, expected_bytes + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                result['bytes'] = len(body)
            result['bytes'] = len(body)
            if len(body) != expected_bytes:
                raise ValueError('response size differs from inventory')
            verify_cid(cid, body)
            result.update(verified=True, sha256=hashlib.sha256(body).hexdigest())
    except (OSError, ValueError, RawBlockStoreError, urllib.error.URLError) as exc:
        result['error'] = type(exc).__name__
        if isinstance(exc, urllib.error.HTTPError):
            result['status'] = exc.code
            exc.close()
        # Do not persist arbitrary remote response bodies or exception URLs.
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    result['seconds'] = time.monotonic() - start
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--provider', action='append', required=True)
    p.add_argument('--cid', required=True)
    p.add_argument('--expected-bytes', type=int, required=True)
    p.add_argument('--timeout', type=float, default=12)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    try:
        providers = [provider(v) for v in args.provider]
        _sha256_digest(args.cid)
        if (not 1 <= args.expected_bytes <= min(MAX_BLOCK, 8 * 1024 * 1024)
                or not 0 < args.timeout <= 60 or len(providers) > 4
                or len({x[0] for x in providers}) != len(providers)):
            raise ValueError('invalid probe limits or duplicate label')
    except (ValueError, RawBlockStoreError) as exc:
        p.error(str(exc))
    results = [probe(label, base, args.cid, args.expected_bytes, args.timeout)
               for label, base in providers]
    report = dict(schema='yataverse.reader-probe.v1',
                  measured_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  scope='operator-selected HTTP reads; no independence or retention proof',
                  results=results, verified_providers=sum(r['verified'] for r in results))
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(dict(output=str(args.output), verified_providers=report['verified_providers'])))
    return 0 if report['verified_providers'] == len(providers) else 1


if __name__ == '__main__':
    raise SystemExit(main())
