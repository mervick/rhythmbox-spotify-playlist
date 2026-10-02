import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import DEFAULT, Mock, patch

import spotify_playlist as plugin


def track(uri, title, *artists, duration_ms=180000):
    return {"uri": uri, "name": title,
            "artists": [{"name": name} for name in artists],
            "duration_ms": duration_ms}


class MatchingTests(unittest.TestCase):
    def test_selected_entries_include_every_selected_row(self):
        entries = [Mock(), Mock()]
        view = SimpleNamespace(get_selected_entries=lambda: entries)
        page = SimpleNamespace(get_entry_view=lambda: view)
        subject = SimpleNamespace(_shell=SimpleNamespace(
            props=SimpleNamespace(selected_page=page)))

        self.assertEqual(plugin.SpotifyPlaylistPlugin._get_selected_entries(subject), entries)

    def test_later_exact_result_beats_first_search_result(self):
        wrong = track("spotify:track:wrong", "Song (Live)", "Artist")
        right = track("spotify:track:right", "Song", "Artist")
        matches = plugin._rank_candidates([wrong, right], "Artist", "Song", 180000)
        self.assertEqual(matches[0][0], right)
        self.assertEqual(plugin._automatic_match(matches), right)

    def test_all_artists_must_match_for_automatic_selection(self):
        solo = track("solo", "Song", "Artist")
        duo = track("duo", "Song", "Artist", "Guest")
        matches = plugin._rank_candidates([solo, duo], "Artist & Guest", "Song", None)
        self.assertEqual(plugin._automatic_match(matches), duo)
        self.assertFalse(plugin._candidate_match(solo, "Artist & Guest", "Song", None)[1])

    def test_featured_artist_moves_between_title_and_artist_fields(self):
        variants = [
            ("Artist 1", "Song (feat. Artist 2)"),
            ("Artist 1; Artist 2", "Song"),
            ("Artist 1 feat. Artist 2", "Song"),
            ("Artist 1", "Song - ft. Artist 2"),
            ("Artist 1 x Artist 2", "Song"),
            ("Artist 1", "Song (with Artist 2)"),
        ]
        spotify_variants = [
            track("artists", "Song", "Artist 1", "Artist 2"),
            track("title", "Song [featuring Artist 2]", "Artist 1"),
            track("duplicate", "Song (feat. Artist 2)", "Artist 1", "Artist 2"),
        ]
        for artist, title in variants:
            for candidate in spotify_variants:
                with self.subTest(artist=artist, title=title, candidate=candidate["uri"]):
                    self.assertEqual(plugin._candidate_match(candidate, artist, title, None),
                                     (100.0, True))

    def test_different_featured_artist_is_not_exact(self):
        candidate = track("wrong", "Song (feat. Artist 3)", "Artist 1")
        self.assertFalse(plugin._candidate_match(
            candidate, "Artist 1; Artist 2", "Song", None)[1])

    def test_live_version_is_not_removed_from_title(self):
        candidate = track("live", "Song (Live) (feat. Artist 2)", "Artist 1")
        self.assertFalse(plugin._candidate_match(
            candidate, "Artist 1; Artist 2", "Song", None)[1])

    def test_title_credit_alone_does_not_replace_missing_main_artist(self):
        candidate = track("incomplete", "Song (feat. Artist 2)")
        self.assertFalse(plugin._candidate_match(
            candidate, "Artist 2", "Song", None)[1])

    def test_multiple_exact_recordings_choose_first_ranked(self):
        first = track("first", "Song", "Artist")
        second = track("second", "Song", "Artist")
        matches = plugin._rank_candidates([first, second], "Artist", "Song", None)
        self.assertEqual(plugin._automatic_match(matches), first)

    def test_multiple_exact_results_skip_track_chooser(self):
        entry = Mock()
        entry.get_string.side_effect = lambda prop: {
            plugin.RB.RhythmDBPropType.ARTIST: "Artist",
            plugin.RB.RhythmDBPropType.TITLE: "Song",
            plugin.RB.RhythmDBPropType.LOCATION: "file:///song.ogg",
        }[prop]
        entry.get_ulong.return_value = 180
        events = []

        def choose_playlist(_track, **kwargs):
            events.append("choose playlist")
            kwargs["on_playlist"]("playlist", "My playlist")

        def search(_artist, _title):
            events.append("search")
            return [track("first", "Song", "Artist"),
                    track("second", "Song", "Artist")]

        subject = SimpleNamespace(
            _ensure_client=lambda: True, _ensure_authenticated=lambda: True,
            _get_selected_entries=lambda: [entry],
            _entry_metadata=lambda selected: plugin.SpotifyPlaylistPlugin._entry_metadata(
                subject, selected),
            _add_selected_tracks=lambda entries, last: plugin.SpotifyPlaylistPlugin._add_selected_tracks(
                subject, entries, last),
            _shell=SimpleNamespace(props=SimpleNamespace(window=None)),
            _settings={"use_match_cache": False},
            _client=SimpleNamespace(search_tracks=search),
            _show_error=Mock(), _show_playlist_picker=Mock(side_effect=choose_playlist),
            _show_track_picker=Mock(), _add_resolved_tracks=Mock())

        class ImmediateThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        with patch.object(plugin.Gtk, "MessageDialog"), \
             patch.object(plugin.GLib, "idle_add", side_effect=lambda callback, *args: callback(*args)), \
             patch.object(plugin.threading, "Thread", ImmediateThread):
            plugin.SpotifyPlaylistPlugin._on_add_to_playlist(subject, None, None)

        subject._show_track_picker.assert_not_called()
        self.assertEqual(events, ["choose playlist", "search"])
        self.assertIsNone(subject._show_playlist_picker.call_args.args[0])
        self.assertFalse(subject._show_playlist_picker.call_args.kwargs["add_to_last_id"])
        resolved = subject._add_resolved_tracks.call_args.args[0]
        self.assertEqual(resolved[0][0]["uri"], "first")

    def test_multiple_selected_tracks_use_one_playlist_and_keep_selection_order(self):
        def entry(title):
            selected = Mock()
            selected.get_string.side_effect = lambda prop: {
                plugin.RB.RhythmDBPropType.ARTIST: "Artist",
                plugin.RB.RhythmDBPropType.TITLE: title,
                plugin.RB.RhythmDBPropType.LOCATION: f"file:///{title}.ogg",
            }[prop]
            selected.get_ulong.return_value = 180
            return selected

        first = track("spotify:track:first", "First", "Artist")
        second = track("spotify:track:second", "Second", "Artist")
        searched = []
        added = []
        events = []

        def search(_artist, title):
            searched.append(title)
            events.append(f"search {title}")
            return [first if title == "First" else second]

        def add(_playlist, uri):
            added.append(uri)
            events.append(f"add {uri}")
            return True

        def choose_playlist(_track, **kwargs):
            events.append("choose playlist")
            kwargs["on_playlist"]("playlist", "My playlist")

        class ImmediateThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        subject = SimpleNamespace(
            _ensure_client=lambda: True, _ensure_authenticated=lambda: True,
            _get_selected_entries=lambda: [entry("First"), entry("Second")],
            _entry_metadata=lambda selected: plugin.SpotifyPlaylistPlugin._entry_metadata(
                subject, selected),
            _add_selected_tracks=lambda entries, last: plugin.SpotifyPlaylistPlugin._add_selected_tracks(
                subject, entries, last),
            _add_resolved_tracks=lambda resolved, skipped, pid, pname:
                plugin.SpotifyPlaylistPlugin._add_resolved_tracks(
                    subject, resolved, skipped, pid, pname),
            _show_playlist_picker=Mock(side_effect=choose_playlist),
            _show_track_picker=Mock(), _show_error=Mock(),
            _shell=SimpleNamespace(props=SimpleNamespace(window=None)),
            _settings={"use_match_cache": True},
            _client=SimpleNamespace(search_tracks=search, add_to_playlist=add))

        with patch.object(plugin.Gtk, "MessageDialog") as dialog, \
             patch.object(plugin.GLib, "idle_add", side_effect=lambda callback, *args: callback(*args)), \
             patch.object(plugin.threading, "Thread", ImmediateThread), \
             patch.object(plugin, "_save_match") as save:
            plugin.SpotifyPlaylistPlugin._on_add_to_playlist(subject, None, None)

        self.assertEqual(searched, ["First", "Second"])
        self.assertEqual(added, ["spotify:track:first", "spotify:track:second"])
        self.assertEqual(events, ["choose playlist", "search First", "search Second",
                                  "add spotify:track:first", "add spotify:track:second"])
        subject._show_playlist_picker.assert_called_once()
        self.assertEqual(subject._show_playlist_picker.call_args.kwargs["track_count"], 2)
        self.assertFalse(subject._show_playlist_picker.call_args.kwargs["add_to_last_id"])
        subject._show_track_picker.assert_not_called()
        subject._show_error.assert_not_called()
        self.assertEqual(save.call_count, 2)
        dialog.assert_called()
        self.assertTrue(all("Added" not in call.kwargs.get("text", "")
                            for call in dialog.call_args_list))

    def test_cancelled_playlist_selection_does_not_search_or_add(self):
        entry = Mock()
        entry.get_string.return_value = "Song"
        entry.get_ulong.return_value = 180
        subject = SimpleNamespace(
            _ensure_client=lambda: True, _ensure_authenticated=lambda: True,
            _get_selected_entries=lambda: [entry],
            _entry_metadata=lambda selected: plugin.SpotifyPlaylistPlugin._entry_metadata(
                subject, selected),
            _add_selected_tracks=lambda entries, last: plugin.SpotifyPlaylistPlugin._add_selected_tracks(
                subject, entries, last),
            _show_playlist_picker=Mock(), _add_resolved_tracks=Mock(),
            _shell=SimpleNamespace(props=SimpleNamespace(window=None)),
            _client=SimpleNamespace(search_tracks=Mock()))

        plugin.SpotifyPlaylistPlugin._on_add_to_playlist(subject, None, None)

        subject._show_playlist_picker.assert_called_once()
        subject._client.search_tracks.assert_not_called()
        subject._add_resolved_tracks.assert_not_called()

    def test_last_playlist_is_selected_before_matching_without_a_picker(self):
        subject = SimpleNamespace(
            _shell=SimpleNamespace(props=SimpleNamespace(window=None)),
            _last_pid="playlist", _last_playlist="My playlist",
            _client=SimpleNamespace(get_playlists=Mock()))
        selected = Mock()

        plugin.SpotifyPlaylistPlugin._show_playlist_picker(
            subject, None, add_to_last_id=True, on_playlist=selected)

        selected.assert_called_once_with("playlist", "My playlist")
        subject._client.get_playlists.assert_not_called()

    def test_single_track_without_matches_keeps_track_not_found_error(self):
        entry = Mock()
        entry.get_string.side_effect = lambda prop: {
            plugin.RB.RhythmDBPropType.ARTIST: "Artist",
            plugin.RB.RhythmDBPropType.TITLE: "Song",
            plugin.RB.RhythmDBPropType.LOCATION: "file:///song.ogg",
        }[prop]
        entry.get_ulong.return_value = 180

        class ImmediateThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        subject = SimpleNamespace(
            _ensure_client=lambda: True, _ensure_authenticated=lambda: True,
            _get_selected_entries=lambda: [entry],
            _entry_metadata=lambda selected: plugin.SpotifyPlaylistPlugin._entry_metadata(
                subject, selected),
            _add_selected_tracks=lambda entries, last: plugin.SpotifyPlaylistPlugin._add_selected_tracks(
                subject, entries, last),
            _show_playlist_picker=Mock(side_effect=lambda _track, **kwargs:
                                       kwargs["on_playlist"]("playlist", "My playlist")),
            _show_error=Mock(), _add_resolved_tracks=Mock(),
            _shell=SimpleNamespace(props=SimpleNamespace(window=None)),
            _settings={"use_match_cache": False},
            _client=SimpleNamespace(search_tracks=Mock(return_value=[])))

        with patch.object(plugin.Gtk, "MessageDialog"), \
             patch.object(plugin.GLib, "idle_add", side_effect=lambda callback, *args: callback(*args)), \
             patch.object(plugin.threading, "Thread", ImmediateThread):
            plugin.SpotifyPlaylistPlugin._on_add_to_playlist(subject, None, None)

        subject._show_error.assert_called_once()
        self.assertEqual(subject._show_error.call_args.args[0], "Track not found")
        subject._add_resolved_tracks.assert_not_called()

    def test_batch_add_skips_failed_tracks_and_caches_only_successes(self):
        first = track("spotify:track:first", "First", "Artist")
        second = track("spotify:track:second", "Second", "Artist")
        resolved = [(first, ("file:///first.ogg", "Artist", "First", 180000)),
                    (second, ("file:///second.ogg", "Artist", "Second", 180000))]
        subject = SimpleNamespace(
            _client=SimpleNamespace(add_to_playlist=Mock(side_effect=[True, False])),
            _settings={"use_match_cache": True},
            _shell=SimpleNamespace(props=SimpleNamespace(window=None)),
            _show_error=Mock())

        class ImmediateThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        with patch.object(plugin.Gtk, "MessageDialog") as dialog, \
             patch.object(plugin.GLib, "idle_add", side_effect=lambda callback, *args: callback(*args)), \
             patch.object(plugin.threading, "Thread", ImmediateThread), \
             patch.object(plugin, "_save_match") as save:
            plugin.SpotifyPlaylistPlugin._add_resolved_tracks(
                subject, resolved, ["Unmatched"], "playlist", "My playlist")

        self.assertEqual(subject._client.add_to_playlist.call_count, 2)
        save.assert_called_once_with(*resolved[0][1], first)
        dialog.assert_not_called()
        subject._show_error.assert_not_called()

    def test_batch_add_reports_complete_failure(self):
        first = track("spotify:track:first", "First", "Artist")
        resolved = [(first, ("file:///first.ogg", "Artist", "First", 180000))]
        subject = SimpleNamespace(
            _client=SimpleNamespace(add_to_playlist=Mock(return_value=False)),
            _settings={"use_match_cache": True},
            _show_error=Mock())

        class ImmediateThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        with patch.object(plugin.GLib, "idle_add", side_effect=lambda callback, *args: callback(*args)), \
             patch.object(plugin.threading, "Thread", ImmediateThread), \
             patch.object(plugin, "_save_match") as save:
            plugin.SpotifyPlaylistPlugin._add_resolved_tracks(
                subject, resolved, [], "playlist", "My playlist")

        save.assert_not_called()
        subject._show_error.assert_called_once()

    def test_duration_mismatch_requires_user_choice(self):
        candidate = track("long", "Song", "Artist", duration_ms=220000)
        matches = plugin._rank_candidates([candidate], "Artist", "Song", 180000)
        self.assertIsNone(plugin._automatic_match(matches))
        self.assertLess(matches[0][1], 100)

    def test_track_picker_shows_local_artist_title_and_length_on_one_line(self):
        subject = SimpleNamespace(_shell=SimpleNamespace(props=SimpleNamespace(window=None)))
        local = ("file:///song.ogg", "Local Artist", "Local Title", 180000)
        matches = [(track("candidate", "Spotify Title", "Spotify Artist"), 90.0, False)]
        with patch.multiple(plugin.Gtk, Dialog=DEFAULT, Label=DEFAULT, ListStore=DEFAULT,
                            TreeView=DEFAULT, ScrolledWindow=DEFAULT,
                            TreeViewColumn=DEFAULT, CellRendererText=DEFAULT) as widgets:
            widgets["Dialog"].return_value.run.return_value = plugin.Gtk.ResponseType.CANCEL
            widgets["TreeView"].return_value.get_selection.return_value.get_selected.return_value = (
                None, None)
            plugin.SpotifyPlaylistPlugin._show_track_picker(
                subject, matches, False, local, on_selected=Mock())

        label = widgets["Label"].call_args.kwargs["label"]
        self.assertEqual(label, "Local Artist - Local Title (3:00)")

    def test_duration_breaks_name_tie_and_logs_every_rank(self):
        wrong_length = track("long", "Song", "Artist", duration_ms=220000)
        right_length = track("right", "Song", "Artist", duration_ms=181000)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            matches = plugin._rank_candidates(
                [wrong_length, right_length], "Artist", "Song", 180000)
        self.assertEqual(plugin._automatic_match(matches), right_length)
        self.assertEqual([item[0]["uri"] for item in matches], ["right", "long"])
        log = output.getvalue()
        self.assertIn("rank=1/2 uri='right'", log)
        self.assertIn("rank=2/2 uri='long'", log)
        self.assertIn("title_match=100.0%", log)
        self.assertIn("artist_match=100.0%", log)
        self.assertIn("duration_match=100% (Δ1.0s)", log)
        self.assertIn("duration_match=0% (Δ40.0s)", log)

    def test_missing_candidate_duration_requires_choice_when_source_has_duration(self):
        candidate = track("unknown", "Song", "Artist")
        candidate.pop("duration_ms")
        matches = plugin._rank_candidates([candidate], "Artist", "Song", 180000)
        self.assertIsNone(plugin._automatic_match(matches))
        self.assertEqual(matches[0][1], 80.0)

    def test_closer_duration_breaks_equal_score_tie(self):
        two_seconds = track("two", "Song", "Artist", duration_ms=182000)
        exact_length = track("zero", "Song", "Artist", duration_ms=180000)
        matches = plugin._rank_candidates(
            [two_seconds, exact_length], "Artist", "Song", 180000)
        self.assertEqual([item[0]["uri"] for item in matches], ["zero", "two"])
        self.assertEqual(plugin._automatic_match(matches), exact_length)

    def test_three_second_difference_requires_choice(self):
        candidate = track("three", "Song", "Artist", duration_ms=183000)
        score, exact = plugin._candidate_match(candidate, "Artist", "Song", 180000)
        self.assertEqual(score, 97.0)
        self.assertFalse(exact)

    def test_search_reads_full_first_pages_and_deduplicates(self):
        first = track("first", "Wrong", "Artist")
        desired = track("desired", "Song", "Artist")

        class Response:
            ok = True

            def __init__(self, items):
                self.items = items

            def json(self):
                return {"tracks": {"items": self.items}}

        client = plugin.SpotifyClient("client")
        with patch.object(client, "access_token", return_value="token"), \
             patch.object(plugin.requests, "get", side_effect=[
                 Response([first, desired]), Response([desired])]) as get:
            found = client.search_tracks("Artist", "Song")

        self.assertEqual([item["uri"] for item in found], ["first", "desired"])
        self.assertEqual(get.call_count, 2)
        self.assertTrue(all(call.kwargs["params"]["limit"] == 50
                            for call in get.call_args_list))

    def test_search_uses_base_title_and_lead_artist(self):
        client = plugin.SpotifyClient("client")

        class Response:
            ok = True

            def json(self):
                return {"tracks": {"items": []}}

        with patch.object(client, "access_token", return_value="token"), \
             patch.object(plugin.requests, "get", return_value=Response()) as get:
            client.search_tracks("Artist 1; Artist 2", "Song (feat. Artist 2)")

        self.assertEqual(get.call_args_list[0].kwargs["params"]["q"],
                         "artist:artist 1 track:Song")


class MatchCacheTests(unittest.TestCase):
    def test_selected_track_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "matches.sqlite3"
            chosen = track("spotify:track:abc123", "Song", "Artist")
            chosen["external_urls"] = {"spotify": "https://open.spotify.com/track/abc123"}
            self.assertIsNone(plugin._load_match("file:///song.ogg", "Artist", "Song", 180000, path))
            self.assertFalse(path.exists())

            plugin._save_match("file:///song.ogg", "Artist", "Song", 180000, chosen, path)
            cached = plugin._load_match("file:///song.ogg", "Artist", "Song", 180000, path)
            self.assertEqual(cached["uri"], chosen["uri"])
            self.assertEqual(cached["external_urls"]["spotify"],
                             "https://open.spotify.com/track/abc123")
            self.assertEqual(cached["artists"], [{"name": "Artist"}])
            self.assertIsNone(plugin._load_match("file:///song.ogg", "Artist", "Song (Live)", 180000, path))
            self.assertIsNone(plugin._load_match("file:///song.ogg", "Artist", "Song", 200000, path))
            self.assertIsNone(plugin._load_match("file:///other.ogg", "Artist", "Song", 180000, path))
            self.assertEqual(plugin._clear_matches(path), 1)
            self.assertIsNone(plugin._load_match("file:///song.ogg", "Artist", "Song", 180000, path))
            self.assertEqual(plugin._clear_matches(path), 0)

    def test_saving_same_local_track_replaces_only_its_selected_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "matches.sqlite3"
            first = track("spotify:track:first", "Song", "Artist")
            second = track("spotify:track:second", "Song", "Artist")
            plugin._save_match("file:///song.ogg", "Artist", "Song", 180000, first, path)
            plugin._save_match("file:///song.ogg", "Artist", "Song", 180000, second, path)
            with sqlite3.connect(path) as db:
                rows = db.execute("SELECT spotify_uri, spotify_url FROM track_matches").fetchall()
            self.assertEqual(rows, [("spotify:track:second",
                                     "https://open.spotify.com/track/second")])

    def test_streams_are_not_cached_as_local_tracks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "matches.sqlite3"
            plugin._save_match("https://radio.example/stream", "Artist", "Song",
                               None, track("spotify:track:abc", "Song", "Artist"), path)
            self.assertFalse(path.exists())

    def test_only_successfully_added_selection_is_saved(self):
        selected = track("spotify:track:chosen", "Song", "Artist")
        local = ("file:///song.ogg", "Artist", "Song", 180000)
        client = SimpleNamespace(add_to_playlist=lambda _playlist, _uri: True)
        subject = SimpleNamespace(
            _shell=SimpleNamespace(props=SimpleNamespace(window=None)),
            _client=client, _settings={"use_match_cache": True},
            _last_pid="playlist", _last_playlist="Playlist")
        with patch.object(plugin.GLib, "idle_add", side_effect=lambda callback, *args: callback(*args)), \
             patch.object(plugin, "_save_match") as save:
            plugin.SpotifyPlaylistPlugin._show_playlist_picker(
                subject, selected, add_to_last_id=True, local_match=local)
            save.assert_called_once_with(*local, selected)
            save.reset_mock()
            subject._settings["use_match_cache"] = False
            plugin.SpotifyPlaylistPlugin._show_playlist_picker(
                subject, selected, add_to_last_id=True, local_match=local)
            save.assert_not_called()
            subject._settings["use_match_cache"] = True
            subject._client.add_to_playlist = lambda _playlist, _uri: False
            with patch.object(plugin.Gtk, "MessageDialog"):
                plugin.SpotifyPlaylistPlugin._show_playlist_picker(
                    subject, selected, add_to_last_id=True, local_match=local)
            save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
