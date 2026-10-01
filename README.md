# rhythmbox-spotify-playlist

Rhythmbox plugin that adds a **"Add to Spotify playlist…"** entry to the
Tools menu. It searches Spotify for the currently playing track and lets you
pick one of your playlists to add it to.


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
5. Select any song → **Tools → Add to Spotify playlist…**

The plugin compares tracks from the first page of Spotify search results by
title, all credited artists (including trailing `feat.` credits in the title),
and duration when available. It continues
automatically only when exactly one result has an exact title and artist match
with a compatible duration. Otherwise, choose the recording from the ranked
list before adding it to a playlist.

The Rhythmbox console logs every candidate in ranked order with separate title,
artist, and duration scores, the duration difference, total score, and whether
the match can be selected automatically.
When both durations are known, the score weights are 50% title, 30% artists,
and 20% duration; automatic matching allows at most a two-second difference.
