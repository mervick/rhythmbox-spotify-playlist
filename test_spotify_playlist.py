import unittest
from unittest.mock import patch

import spotify_playlist as plugin


def track(uri, title, *artists, duration_ms=180000):
    return {"uri": uri, "name": title,
            "artists": [{"name": name} for name in artists],
            "duration_ms": duration_ms}


class MatchingTests(unittest.TestCase):
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

    def test_multiple_exact_recordings_require_user_choice(self):
        first = track("first", "Song", "Artist")
        second = track("second", "Song", "Artist")
        matches = plugin._rank_candidates([first, second], "Artist", "Song", None)
        self.assertIsNone(plugin._automatic_match(matches))

    def test_duration_mismatch_requires_user_choice(self):
        candidate = track("long", "Song", "Artist", duration_ms=220000)
        matches = plugin._rank_candidates([candidate], "Artist", "Song", 180000)
        self.assertIsNone(plugin._automatic_match(matches))
        self.assertLess(matches[0][1], 100)

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


if __name__ == "__main__":
    unittest.main()
