#!/usr/bin/env python3
"""Run HorseCamp's feed fetch with adaptive Forest Service website protection.

The main fetch logic remains in fetch_camps.py. This runner overrides only the
fs.usda.gov website supplement request behavior so the nightly pipeline is
polite under throttling and can retain the last accepted website supplement
instead of publishing a sharply reduced USFS source.
"""

import json
import math
import random
import subprocess
import time
from pathlib import Path

import fetch_camps as fc

USFS_BASE_DELAY_SECONDS = 2.0
USFS_JITTER_SECONDS = 0.5
USFS_403_RETRY_SECONDS = 60
USFS_MAX_CONSECUTIVE_403_FAILURES = 5
USFS_MIN_RETAIN_RATIO_AFTER_THROTTLING = 0.80
USFS_CACHE_FILE = fc.DATA_DIR / "usfs_website_supplement.json"

# Bootstrap only: this is the last known-good nightly feed before the Aug. 13
# fs.usda.gov 403-blocked run. Once the cache file is committed, normal runs no
# longer depend on this historical commit.
USFS_BOOTSTRAP_GOOD_FEED_COMMIT = "2b38b08d5b8281228b664627c0b75edc6d3da6f6"

_last_request_at = 0.0
_consecutive_403_failures = 0
_throttle_failures_total = 0


class USFSWebsiteBlocked(RuntimeError):
    """Raised after sustained HTTP 403 blocking from fs.usda.gov."""


def _polite_pause():
    global _last_request_at
    target_delay = USFS_BASE_DELAY_SECONDS + random.uniform(0.0, USFS_JITTER_SECONDS)
    now = time.monotonic()
    wait = target_delay - (now - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _retry_after_seconds(response, attempt):
    retry_after = str(response.headers.get("Retry-After") or "").strip()
    if retry_after.isdigit():
        return min(max(int(retry_after), 5), fc.USFS_WEBSITE_MAX_429_RETRY_SECONDS)
    backoff = (15, 30, 60)
    return min(backoff[min(attempt, len(backoff) - 1)], fc.USFS_WEBSITE_MAX_429_RETRY_SECONDS)


def _guarded_fetch_html(url, retries=4):
    """Fetch an official USFS page with adaptive 429/403 handling."""
    global _consecutive_403_failures, _throttle_failures_total

    # Existing callers often pass retries=2. Four attempts allow the intended
    # 15s -> 30s -> 60s 429 backoff while remaining bounded.
    max_attempts = max(4, retries)
    retried_403 = False

    for attempt in range(max_attempts):
        try:
            _polite_pause()
            response = fc.requests.get(
                url,
                timeout=20,
                headers={"User-Agent": fc.USFS_WEBSITE_USER_AGENT},
                allow_redirects=True,
            )

            if response.status_code == 200 and response.text:
                _consecutive_403_failures = 0
                return response.url, response.text

            if response.status_code == 429:
                if attempt < max_attempts - 1:
                    wait = _retry_after_seconds(response, attempt)
                    print(f"  USFS website rate limited for {url}; waiting {wait}s before retry")
                    time.sleep(wait)
                    continue
                _throttle_failures_total += 1
                print(f"  USFS website HTTP 429 for {url} after all retries")
                return "", ""

            if response.status_code == 403:
                if not retried_403:
                    retried_403 = True
                    print(
                        f"  USFS website HTTP 403 for {url}; "
                        f"waiting {USFS_403_RETRY_SECONDS}s before one retry"
                    )
                    time.sleep(USFS_403_RETRY_SECONDS)
                    continue

                _consecutive_403_failures += 1
                _throttle_failures_total += 1
                print(
                    f"  USFS website HTTP 403 for {url} after retry "
                    f"({_consecutive_403_failures}/{USFS_MAX_CONSECUTIVE_403_FAILURES} "
                    "consecutive blocked requests)"
                )
                if _consecutive_403_failures >= USFS_MAX_CONSECUTIVE_403_FAILURES:
                    raise USFSWebsiteBlocked(
                        "Forest Service website returned repeated HTTP 403 responses"
                    )
                return "", ""

            print(f"  USFS website HTTP {response.status_code} for {url}")
            return "", ""

        except USFSWebsiteBlocked:
            raise
        except Exception as exc:
            print(f"  USFS website request error (attempt {attempt + 1}) for {url}: {exc}")
            if attempt < max_attempts - 1:
                time.sleep(3 * (attempt + 1))

    return "", ""


def _extract_website_supplement(feed):
    camps = feed.get("camps", []) if isinstance(feed, dict) else []
    return [
        dict(camp)
        for camp in camps
        if isinstance(camp, dict)
        and camp.get("source") == "U.S. Forest Service"
        and str(camp.get("id") or "").startswith("usfs-page-")
    ]


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_bootstrap_good_supplement():
    """Read the Aug. 12 known-good supplement from local git history once."""
    try:
        raw = subprocess.check_output(
            ["git", "show", f"{USFS_BOOTSTRAP_GOOD_FEED_COMMIT}:camps.json"],
            cwd=fc.REPO_ROOT,
            text=True,
            stderr=subprocess.STDOUT,
        )
        feed = json.loads(raw)
        supplement = _extract_website_supplement(feed)
        if supplement:
            print(
                "  Bootstrapped U.S. Forest Service website supplement from "
                f"Aug. 12 known-good feed: {len(supplement)} listings"
            )
        return supplement
    except Exception as exc:
        print(f"  WARNING: Could not bootstrap prior USFS supplement from git history: {exc}")
        return []


def _load_previous_supplement():
    cached = _load_json(USFS_CACHE_FILE)
    if isinstance(cached, list) and cached:
        print(f"  Cached U.S. Forest Service website supplement available: {len(cached)} listings")
        return [dict(camp) for camp in cached if isinstance(camp, dict)]

    # First guarded run: seed from the last known-good Aug. 12 feed rather than
    # from the currently degraded Aug. 13 camps.json.
    bootstrap = _load_bootstrap_good_supplement()
    if bootstrap:
        return bootstrap

    current = _load_json(fc.REPO_ROOT / "camps.json")
    supplement = _extract_website_supplement(current or {})
    if supplement:
        print(
            "  WARNING: Falling back to the current camps.json USFS supplement "
            f"because no cache/bootstrap was available: {len(supplement)} listings"
        )
    return supplement


def _write_supplement_cache(supplement):
    USFS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    USFS_CACHE_FILE.write_text(
        json.dumps(supplement, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"  U.S. Forest Service website supplement cache saved: {len(supplement)} listings")


def _install_usfs_guards():
    global _consecutive_403_failures, _throttle_failures_total

    # Existing crawler loops also sleep this global value between accepted pages.
    fc.USFS_WEBSITE_REQUEST_DELAY_SECONDS = USFS_BASE_DELAY_SECONDS
    fc._usfs_polite_website_pause = _polite_pause
    fc._usfs_retry_after_seconds = _retry_after_seconds
    fc._usfs_fetch_html = _guarded_fetch_html

    original_supplement_fetch = fc.fetch_usfs_official_website_supplement

    def guarded_supplement_fetch(primary_camps):
        global _consecutive_403_failures, _throttle_failures_total
        _consecutive_403_failures = 0
        _throttle_failures_total = 0
        previous = _load_previous_supplement()

        try:
            current = original_supplement_fetch(primary_camps)
        except USFSWebsiteBlocked as exc:
            if not previous:
                raise RuntimeError(
                    "USFS website crawl was blocked and no previous accepted supplement is available; "
                    "refusing to publish an incomplete USFS feed."
                ) from exc
            print(
                "  WARNING: U.S. Forest Service website supplement stopped after sustained 403 blocking; "
                f"retaining {len(previous)} listings from the last accepted supplement."
            )
            _write_supplement_cache(previous)
            return previous

        if previous and _throttle_failures_total:
            minimum_expected = math.ceil(len(previous) * USFS_MIN_RETAIN_RATIO_AFTER_THROTTLING)
            if len(current) < minimum_expected:
                print(
                    "  WARNING: U.S. Forest Service website supplement was partially throttled and fell from "
                    f"{len(previous)} previous listings to {len(current)} current listings; retaining the "
                    f"previous supplement instead (minimum accepted after throttling: {minimum_expected})."
                )
                _write_supplement_cache(previous)
                return previous

        _write_supplement_cache(current)
        return current

    fc.fetch_usfs_official_website_supplement = guarded_supplement_fetch


def main():
    _install_usfs_guards()
    fc.main()


if __name__ == "__main__":
    main()
