# SPDX-License-Identifier: GPL-3.0-or-later
"""Telling someone a newer build exists, without becoming a nuisance.

The app is downloaded from a web page, so nothing else will ever tell its
owner that a fix exists. That makes the check worth having -- and makes its
manners matter, because it is the only thing in the app that touches a
network.
"""

from __future__ import annotations

import pytest

from syncmyvids import updates
from syncmyvids.updates import Release


class TestComparingVersions:
    def test_a_later_patch_is_newer(self) -> None:
        assert updates.is_newer("0.2.1", "0.2.0")

    def test_the_same_version_is_not_newer(self) -> None:
        assert not updates.is_newer("0.2.0", "0.2.0")

    def test_an_older_version_is_not_newer(self) -> None:
        assert not updates.is_newer("0.1.9", "0.2.0")

    def test_ten_comes_after_nine(self) -> None:
        # Compared as text, "0.10.0" sorts before "0.9.0" and every release
        # after the ninth would go unannounced.
        assert updates.is_newer("0.10.0", "0.9.0")
        assert updates.is_newer("1.0.0", "0.99.99")

    def test_a_leading_v_is_ignored(self) -> None:
        assert updates.is_newer("v0.3.0", "0.2.0")

    def test_a_shorter_version_is_the_older_one(self) -> None:
        assert not updates.is_newer("0.2", "0.2.1")
        assert updates.is_newer("0.2.1", "0.2")

    def test_nonsense_does_not_raise(self) -> None:
        assert updates.parse_version("") == (0,)
        assert not updates.is_newer("", "0.2.0")


class TestWhenItAsks:
    def clock(self, at: float):
        return lambda: at

    def test_it_asks_when_it_has_never_asked(self) -> None:
        prefs: dict = {}
        called: list[int] = []

        def fetch():
            called.append(1)
            return Release("0.3.0", "https://example.invalid/v0.3.0")

        release = updates.check("0.2.0", prefs, now=self.clock(1000.0), fetch=fetch)
        assert called and release is not None and release.version == "0.3.0"

    def test_it_does_not_ask_again_the_same_day(self) -> None:
        prefs = {"update_checked_at": 1000.0,
                 "update_latest": {"version": "0.3.0", "url": "u"}}
        called: list[int] = []

        def fetch():
            called.append(1)
            return None

        release = updates.check("0.2.0", prefs, now=self.clock(1500.0), fetch=fetch)
        assert not called, "a reopened window must not cost another request"
        assert release is not None, "but it still reports what it already knew"

    def test_it_asks_again_after_a_day(self) -> None:
        prefs = {"update_checked_at": 1000.0}
        called: list[int] = []

        def fetch():
            called.append(1)
            return Release("0.3.0", "u")

        updates.check("0.2.0", prefs,
                      now=self.clock(1000.0 + updates.CHECK_INTERVAL_SECONDS + 1),
                      fetch=fetch)
        assert called

    def test_turning_it_off_stops_it_entirely(self) -> None:
        prefs = {"check_updates": False}
        called: list[int] = []

        def fetch():
            called.append(1)
            return Release("9.9.9", "u")

        assert updates.check("0.2.0", prefs, now=self.clock(1e9), fetch=fetch) is None
        assert not called, "off means no request is made at all"


class TestWhenItFails:
    def test_an_unreachable_github_is_silent(self) -> None:
        prefs: dict = {}
        release = updates.check("0.2.0", prefs, now=lambda: 1000.0, fetch=lambda: None)
        assert release is None

    def test_a_failure_does_not_erase_an_unseen_notice(self) -> None:
        # Somebody who has not acted on the banner yet should still see it.
        prefs = {"update_latest": {"version": "0.3.0", "url": "u"},
                 "update_checked_at": 0.0}
        updates.check("0.2.0", prefs, now=lambda: 1e9, fetch=lambda: None)
        assert prefs["update_latest"]["version"] == "0.3.0"

    def test_a_failure_still_counts_as_having_asked(self) -> None:
        # Otherwise a machine that is offline retries on every single launch.
        prefs: dict = {}
        updates.check("0.2.0", prefs, now=lambda: 1234.0, fetch=lambda: None)
        assert prefs["update_checked_at"] == 1234.0

    def test_the_current_version_produces_no_notice(self) -> None:
        prefs: dict = {}
        release = updates.check("0.2.0", prefs, now=lambda: 1000.0,
                                fetch=lambda: Release("0.2.0", "u"))
        assert release is None


class TestWhatItSends:
    def test_the_request_carries_nothing_about_the_user(self) -> None:
        """The reply is the same for everyone, so there is nothing to report."""
        import inspect

        source = inspect.getsource(updates.fetch_latest)
        for leak in ("__version__", "platform", "uuid", "getuser", "hostname",
                     "serial", "machine", "?v=", "&v="):
            assert leak not in source, f"fetch_latest references {leak}"

    def test_it_asks_github_and_not_a_server_of_ours(self) -> None:
        # The privacy policy says there are no ActionCheese servers. Asking
        # our own host for the version would quietly make that untrue.
        assert updates.LATEST_URL.startswith("https://api.github.com/")
        assert "actioncheese.com" not in updates.LATEST_URL
