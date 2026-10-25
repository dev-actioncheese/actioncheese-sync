# SPDX-License-Identifier: GPL-3.0-or-later
"""Telling you when a newer build exists.

The app is downloaded from a web page, not an app store, so nothing else will
ever tell its owner that a fix exists. That is the whole reason this module
is here, and it shapes what it may do:

- It asks **GitHub**, where the releases live, not a server of ours. Nothing
  about the person reaches us, and the privacy policy's "no ActionCheese
  servers" stays literally true.
- It sends nothing but the request. No identifier, no version, no count --
  the reply is the same for everyone, so there is nothing to report.
- It runs once a day at most, off the UI thread, and a failure is silent.
  An update check must never delay or interrupt a sync.
- It can be turned off, and then it never runs at all.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

LATEST_URL = "https://api.github.com/repos/dev-actioncheese/actioncheese-sync/releases/latest"
#: Once a day is plenty for a tool someone opens to do a job.
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
TIMEOUT_SECONDS = 6.0


@dataclass(frozen=True, slots=True)
class Release:
    version: str
    url: str


def parse_version(text: str) -> tuple[int, ...]:
    """``"v0.10.0"`` -> ``(0, 10, 0)``.

    Compared as numbers, never as text: "0.10.0" sorts *before* "0.9.0"
    alphabetically, which would hide every release after the ninth.
    """
    numbers = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in numbers) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    """Is ``candidate`` a later version than ``current``?

    Shorter is older at equal prefixes, so 0.2 is older than 0.2.1.
    """
    a, b = parse_version(candidate), parse_version(current)
    length = max(len(a), len(b))
    return a + (0,) * (length - len(a)) > b + (0,) * (length - len(b))


def fetch_latest(url: str = LATEST_URL) -> Optional[Release]:
    """The newest published release, or None if that cannot be established."""
    request = urllib.request.Request(url, headers={
        "User-Agent": "ActionCheese-Sync",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        # Offline, blocked, rate-limited, or GitHub having a bad minute. None
        # of those are the operator's problem and none of them are worth a
        # dialog.
        log.debug("update check failed: %s", exc)
        return None

    tag = str(payload.get("tag_name") or "").strip()
    page = str(payload.get("html_url") or "").strip()
    if not tag or not page:
        return None
    return Release(version=tag.lstrip("vV"), url=page)


def check(current_version: str, prefs: dict, *,
          now: Optional[Callable[[], float]] = None,
          fetch: Callable[[], Optional[Release]] = fetch_latest) -> Optional[Release]:
    """A newer release than ``current_version``, or None.

    ``prefs`` is read and updated in place: ``check_updates`` turns it off,
    and the last result is remembered so closing and reopening the app within
    the day costs nothing. The caller saves the dict.
    """
    if not prefs.get("check_updates", True):
        return None

    clock = now or time.time
    moment = clock()
    recorded = prefs.get("update_checked_at")
    # "Never asked" has to be its own case rather than a zero to subtract
    # from: treating a missing value as the epoch makes the answer depend on
    # what the clock happens to read, which is how a fresh install ends up
    # deciding it has already checked.
    last = float(recorded) if recorded is not None else None

    if last is not None and moment - last < CHECK_INTERVAL_SECONDS:
        remembered = prefs.get("update_latest")
        if isinstance(remembered, dict) and remembered.get("version"):
            release = Release(version=str(remembered["version"]),
                              url=str(remembered.get("url") or ""))
            return release if is_newer(release.version, current_version) else None
        return None

    release = fetch()
    prefs["update_checked_at"] = moment
    if release is None:
        # Leave the remembered result alone: a failed check should not erase
        # a notice the operator has not acted on yet.
        return None

    prefs["update_latest"] = {"version": release.version, "url": release.url}
    return release if is_newer(release.version, current_version) else None
