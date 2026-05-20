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


