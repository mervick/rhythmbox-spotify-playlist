# rhythmbox-spotify-playlist

Rhythmbox plugin that adds a **"Add to Spotify playlist…"** entry to the
Tools menu. It searches Spotify for the selected tracks and lets you
pick one of your playlists to add them to.


## Spotify app setup

1. Log in at <https://developer.spotify.com/dashboard>
2. Click **Create app**
3. Fill in any name/description, set **Redirect URI** to exactly:
   ```
   http://127.0.0.1:8888/callback
   ```
4. Under "Which API/SDKs are you planning to use?" select **Web API**
5. Save — copy the **Client ID** from the app overview page

## Installation

```bash
cd rhythmbox-spotify
bash install.sh
```

Then in Rhythmbox:

1. **Edit → Plugins** → enable *Spotify Playlist*
2. **Tools → Spotify plugin preferences…** → paste your Client ID → OK
3. **Tools → Connect to Spotify…** → your browser opens for Spotify OAuth
4. Authorize, return to Rhythmbox
5. Select one or more songs → **Tools → Add to Spotify playlist…**

When multiple songs are selected, the plugin matches them in order. A track
chooser opens for each song without an exact match. Cancel that chooser to
skip that song. After matching, choose a playlist once; the plugin adds the
matched songs in selection order. The result is logged to the Rhythmbox console;
an error dialog appears if no tracks could be added.

The plugin compares tracks from the first page of Spotify search results by
title, all credited artists (including trailing `feat.` credits in the title),
and duration when available. If any result has an exact title and artist match
with a compatible duration, the first ranked exact result is selected without
opening the track chooser. Otherwise, choose the recording from the ranked
list before adding it to a playlist.

The Rhythmbox console logs every candidate in ranked order with separate title,
artist, and duration scores, the duration difference, total score, and whether
the match can be selected automatically.
When both durations are known, the score weights are 50% title, 30% artists,
and 20% duration; automatic matching allows at most a two-second difference.

In **Spotify preferences**, enable **Remember selected Spotify tracks locally
(SQLite)** to reuse a saved match before searching Spotify. The plugin saves
only the track successfully added to a playlist, keyed by the local track's
location and checked against its current title, artists, and duration. The
SQLite file is `~/.config/rhythmbox/spotify_playlist_matches.sqlite3` and
stores the Spotify track URI and URL. **Clear saved matches…** removes all
saved matches; this works even when the setting is off.
