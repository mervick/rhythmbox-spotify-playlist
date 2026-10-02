"""
Rhythmbox plugin: Add current track to a Spotify playlist.
Compatible with Rhythmbox 3.x (GtkApplication, no UIManager).

Adds actions via Gio.SimpleAction and injects menu items into
the app's GMenuModel (Tools section).

Requirements:
    pip3 install --user requests
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import unicodedata
import webbrowser
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import gi

gi.require_version("RB", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Peas", "1.0")

from gi.repository import GLib, GObject, Gio, Gtk, Peas, RB

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SPOTIFY_AUTH_URL  = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE  = "https://api.spotify.com/v1"
REDIRECT_URI      = "http://127.0.0.1:8888/callback"
SCOPES            = "playlist-modify-public playlist-modify-private playlist-read-private"
TOKEN_CACHE       = Path.home() / ".config" / "rhythmbox" / "spotify_playlist_token.json"
SETTINGS_FILE     = Path.home() / ".config" / "rhythmbox" / "spotify_playlist_settings.json"
MATCHES_DB         = Path.home() / ".config" / "rhythmbox" / "spotify_playlist_matches.sqlite3"


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    """Generate a PKCE verifier and its SHA-256 challenge."""
    # Spotify receives the challenge now; the verifier is sent only with the token request.
    verifier  = secrets.token_urlsafe(64)[:128]
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def _load_token() -> dict:
    """Read the saved Spotify token, returning an empty dict on failure."""
    try:
        return json.loads(TOKEN_CACHE.read_text())
    except Exception:
        return {}

def _save_token(data: dict) -> None:
    """Persist Spotify token data in the Rhythmbox config directory."""
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _load_settings() -> dict:
    """Read plugin settings, returning an empty dict on failure."""
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}

def _save_settings(data: dict) -> None:
    """Persist plugin settings in the Rhythmbox config directory."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data))


def _spotify_url(uri: str) -> str:
    """Build a web URL from a Spotify track URI when valid."""
    parts = uri.split(":")
    return f"https://open.spotify.com/track/{parts[2]}" if len(parts) == 3 and parts[:2] == ["spotify", "track"] else ""


def _is_local_track(location: str) -> bool:
    """Check whether a track location uses the file URI scheme."""
    return urllib.parse.urlparse(location).scheme == "file"


# Keep one confirmed Spotify match per local file; search candidates never enter this database.
def _open_matches_db(path: Path):
    """Open the local match database and ensure its table exists."""
    # Each operation gets its own connection because searches run in a worker thread.
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    connection.execute("""CREATE TABLE IF NOT EXISTS track_matches (
        location TEXT PRIMARY KEY,
        local_artist TEXT NOT NULL,
        local_title TEXT NOT NULL,
        local_duration_ms INTEGER,
        spotify_uri TEXT NOT NULL,
        spotify_url TEXT NOT NULL,
        spotify_title TEXT NOT NULL,
        spotify_artists TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )""")
    return connection


def _load_match(location: str, artist: str, title: str,
                duration_ms: Optional[int], path: Path = MATCHES_DB) -> Optional[dict]:
    """Return a saved match only when the local track metadata still agrees."""
    if not _is_local_track(location) or not path.exists():
        return None
    with closing(_open_matches_db(path)) as db, db:
        row = db.execute("""SELECT local_artist, local_title, local_duration_ms,
                            spotify_uri, spotify_url, spotify_title, spotify_artists
                            FROM track_matches WHERE location = ?""", (location,)).fetchone()
    # A file can be retagged or replaced at the same location, so verify its metadata too.
    if row is None or row[:3] != (artist, title, duration_ms):
        return None
    return {"uri": row[3], "external_urls": {"spotify": row[4]},
            "name": row[5], "artists": [{"name": name} for name in json.loads(row[6])]}


def _save_match(location: str, artist: str, title: str, duration_ms: Optional[int],
                track: dict, path: Path = MATCHES_DB) -> None:
    """Store the confirmed Spotify track for a local file."""
    # Streams have no stable local file identity, so never cache their matches.
    if not _is_local_track(location) or not track.get("uri"):
        return
    uri = track["uri"]
    url = (track.get("external_urls") or {}).get("spotify") or _spotify_url(uri)
    if not url:
        raise ValueError("Spotify track URL is unavailable")
    artists = [a.get("name", "") for a in track.get("artists", [])]
    with closing(_open_matches_db(path)) as db, db:
        db.execute("""INSERT INTO track_matches
                    (location, local_artist, local_title, local_duration_ms,
                     spotify_uri, spotify_url, spotify_title, spotify_artists, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(location) DO UPDATE SET
                    local_artist=excluded.local_artist, local_title=excluded.local_title,
                    local_duration_ms=excluded.local_duration_ms, spotify_uri=excluded.spotify_uri,
                    spotify_url=excluded.spotify_url, spotify_title=excluded.spotify_title,
                    spotify_artists=excluded.spotify_artists, updated_at=excluded.updated_at""",
                   (location, artist, title, duration_ms, uri, url, track.get("name", ""),
                    json.dumps(artists), int(time.time())))
    print(f"[spotify_playlist] Saved match: {location!r} -> {url or uri}", flush=True)


def _clear_matches(path: Path = MATCHES_DB) -> int:
    """Delete all saved track matches and return the deleted count."""
    if not path.exists():
        return 0
    with closing(_open_matches_db(path)) as db, db:
        count = db.execute("SELECT COUNT(*) FROM track_matches").fetchone()[0]
        db.execute("DELETE FROM track_matches")
    return count


def _clean_title(title: str) -> str:
    """Compare the song title without a trailing featured-artist credit."""
    return _normalize(_split_title_credit(title)[0])


def _normalize(s: str) -> str:
    """Ignore case, punctuation and whitespace, but preserve all words."""
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return " ".join(re.findall(r"[^\W_]+", s, flags=re.UNICODE))


def _artist_names(value: str) -> list[str]:
    """Split and normalize the artist names in a credit field."""
    # Rhythmbox can combine credits in one field; Spotify usually separates artists.
    parts = re.split(r"\s*(?:,|;|\s+[&×x]\s+|\s+(?:feat|ft|featuring)\.?\s+)\s*", value or "", flags=re.IGNORECASE)
    return [name for part in parts if (name := _normalize(part))]


def _split_title_credit(title: str) -> tuple[str, str]:
    """Extract only an explicit trailing feat/ft/featuring credit."""
    title = title or ""
    # Limit extraction to a trailing credit so version labels such as "Live" remain in the title.
    match = re.match(r"^(.*?)\s*[\(\[]\s*(?:feat|ft|featuring|with)\.?\s+(.+?)\s*[\)\]]\s*$",
                     title, flags=re.IGNORECASE)
    if not match:
        match = re.match(r"^(.*?)\s+(?:feat|ft|featuring)\.?\s+(.+?)\s*$",
                         title, flags=re.IGNORECASE)
    if match and match.group(1).strip(" -–—") and match.group(2).strip():
        return match.group(1).strip(" -–—"), match.group(2)
    return title, ""


def _credits(artist_fields: list[str], title: str) -> list[str]:
    # The same guest may appear in both the title and artist fields; count that artist once.
    """Collect unique normalized artists from fields and title credits."""
    names = set()
    for field in artist_fields + [_split_title_credit(title)[1]]:
        names.update(_artist_names(field))
    return sorted(names)


def _match_details(candidate: dict, want_artist: str, want_title: str,
                   duration_ms: Optional[int]) -> dict:
    """Calculate component scores used for ranking and console diagnostics."""
    title = _clean_title(candidate.get("name", ""))
    candidate_artist_fields = [artist.get("name", "") for artist in candidate.get("artists", [])]
    artist_names = _credits(candidate_artist_fields, candidate.get("name", ""))
    wanted_artists = _credits([want_artist], want_title)
    wanted_title = _clean_title(want_title)
    has_primary_artists = bool(_artist_names(want_artist)) and any(
        _artist_names(field) for field in candidate_artist_fields)
    valid = bool(title and artist_names and wanted_title and wanted_artists and has_primary_artists)
    title_score = (difflib.SequenceMatcher(None, wanted_title, title).ratio() * 100
                   if valid else 0.0)
    artist_score = (difflib.SequenceMatcher(
        None, ", ".join(wanted_artists), ", ".join(artist_names)
    ).ratio() * 100 if valid else 0.0)

    candidate_duration = candidate.get("duration_ms")
    duration_delta = None
    duration_score = None
    if duration_ms and isinstance(candidate_duration, (int, float)) and candidate_duration > 0:
        duration_delta = abs(candidate_duration - duration_ms)
        if duration_delta <= 2000:
            duration_score = 100.0
        elif duration_delta <= 4000:
            duration_score = 85.0
        elif duration_delta <= 10000:
            duration_score = 60.0
        else:
            duration_score = 0.0

    # Missing source duration leaves name matching at full weight; missing Spotify duration
    # loses the duration component and prevents an automatic match below.
    if duration_ms is None:
        score = 0.6 * title_score + 0.4 * artist_score
    else:
        score = 0.5 * title_score + 0.3 * artist_score + 0.2 * (duration_score or 0.0)
    if not valid:
        score = 0.0
    # A high similarity score is insufficient for automatic selection: names must match,
    # and known durations must differ by no more than two seconds.
    exact = (valid and title == wanted_title and artist_names == wanted_artists
             and (duration_ms is None or
                  (duration_delta is not None and duration_delta <= 2000)))
    return {
        "score": round(score, 1), "exact": exact,
        "title_score": round(title_score, 1), "artist_score": round(artist_score, 1),
        "duration_score": duration_score, "duration_delta_ms": duration_delta,
        "title": title, "artists": artist_names,
    }


def _candidate_match(candidate: dict, want_artist: str, want_title: str,
                     duration_ms: Optional[int]) -> tuple[float, bool]:
    """Return the score and exact-match flag for one candidate."""
    details = _match_details(candidate, want_artist, want_title, duration_ms)
    return details["score"], details["exact"]


def _rank_candidates(tracks: list[dict], artist: str, title: str,
                     duration_ms: Optional[int]) -> list[tuple[dict, float, bool]]:
    """Rank Spotify tracks and log the scoring details."""
    ranked = [(track, _match_details(track, artist, title, duration_ms)) for track in tracks]
    # Exact matches lead; score and then duration difference order ties.
    ranked.sort(key=lambda item: (item[1]["exact"], item[1]["score"],
                                  -(item[1]["duration_delta_ms"]
                                    if item[1]["duration_delta_ms"] is not None else float("inf"))),
                reverse=True)
    print(f"[spotify_playlist] Matching {len(ranked)} Spotify tracks for "
          f"title={title!r}, artists={artist!r}, duration_ms={duration_ms!r} "
          f"normalized_title={_clean_title(title)!r} "
          f"normalized_artists={_credits([artist], title)!r}", flush=True)
    for rank, (track, details) in enumerate(ranked, 1):
        delta = details["duration_delta_ms"]
        duration_text = (f"{details['duration_score']:.0f}% (Δ{delta / 1000:.1f}s)"
                         if delta is not None else "unavailable")
        print(f"[spotify_playlist] rank={rank}/{len(ranked)} uri={track.get('uri')!r} "
              f"title={track.get('name')!r} "
              f"artists={[a.get('name', '') for a in track.get('artists', [])]!r} "
              f"normalized_title={details['title']!r} "
              f"normalized_artists={details['artists']!r} "
              f"duration_ms={track.get('duration_ms')!r} "
              f"title_match={details['title_score']:.1f}% "
              f"artist_match={details['artist_score']:.1f}% "
              f"duration_match={duration_text} score={details['score']:.1f}% "
              f"exact={details['exact']}", flush=True)
    return [(track, details["score"], details["exact"]) for track, details in ranked]


def _automatic_match(matches: list[tuple[dict, float, bool]]) -> Optional[dict]:
    """Return the first exact match from ranked candidates, if any."""
    # The list is already ranked, so the first exact match needs no track-choice dialog.
    return next((track for track, _, is_exact in matches if is_exact), None)


# ---------------------------------------------------------------------------
# Spotify client
# ---------------------------------------------------------------------------

class SpotifyClient:
    def __init__(self, client_id: str) -> None:
        """Initialize the Spotify client with a client ID and saved token."""
        self.client_id = client_id
        self._token: dict = _load_token()

    def is_authenticated(self) -> bool:
        """Check whether a saved access token is present."""
        return bool(self._token.get("access_token"))

    def access_token(self) -> Optional[str]:
        """Return a current access token, refreshing it when expired."""
        if not self._token:
            return None
        if self._is_expired():
            self._refresh()
        return self._token.get("access_token")

    def _is_expired(self) -> bool:
        """Check whether the access token is expired or nearly expired."""
        # Refresh early so an API request does not start with a nearly expired token.
        return time.time() > self._token.get("expires_at", 0) - 30

    def _refresh(self) -> None:
        """Refresh the access token or clear unusable token data."""
        rt = self._token.get("refresh_token")
        if not rt:
            self._token = {}
            return
        resp = requests.post(SPOTIFY_TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "refresh_token": rt,
            "client_id":     self.client_id,
        })
        if resp.ok:
            data = resp.json()
            data.setdefault("refresh_token", rt)
            data["expires_at"] = time.time() + data.get("expires_in", 3600)
            self._token = data
            _save_token(data)
        else:
            self._token = {}

    def start_auth_flow(self, on_done) -> None:
        """Start browser-based PKCE authorization and report the result."""
        verifier, challenge = _pkce_pair()
        # The callback must carry this state value to belong to our authorization attempt.
        state = secrets.token_hex(8)
        params = {
            "client_id":             self.client_id,
            "response_type":         "code",
            "redirect_uri":          REDIRECT_URI,
            "code_challenge_method": "S256",
            "code_challenge":        challenge,
            "state":                 state,
            "scope":                 SCOPES,
        }
        webbrowser.open(SPOTIFY_AUTH_URL + "?" + urllib.parse.urlencode(params))

        def _serve():
            """Handle the local OAuth callback and exchange its code for a token."""
            code_holder = []

            class _H(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    """Suppress HTTP server request logging during authorization."""
                    pass
                def do_GET(self):
                    """Validate the OAuth callback and acknowledge it in the browser."""
                    qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    if qs.get("state", [""])[0] == state and "code" in qs:
                        code_holder.append(qs["code"][0])
                        self.send_response(200); self.end_headers()
                        self.wfile.write(b"<h2>Authorized! Close this tab.</h2>")
                    else:
                        self.send_response(400); self.end_headers()

            srv = HTTPServer(("127.0.0.1", 8888), _H)
            # Handle one browser callback, then stop the temporary local server.
            srv.timeout = 120
            srv.handle_request()
            srv.server_close()

            if not code_holder:
                # GTK callbacks must run on the main loop, not in this HTTP worker thread.
                GLib.idle_add(on_done, False)
                return

            resp = requests.post(SPOTIFY_TOKEN_URL, data={
                "grant_type":    "authorization_code",
                "code":          code_holder[0],
                "redirect_uri":  REDIRECT_URI,
                "client_id":     self.client_id,
                "code_verifier": verifier,
            })
            if resp.ok:
                data = resp.json()
                data["expires_at"] = time.time() + data.get("expires_in", 3600)
                self._token = data
                _save_token(data)
                GLib.idle_add(on_done, True)
            else:
                GLib.idle_add(on_done, False)

        threading.Thread(target=_serve, daemon=True).start()

    def get_playlists(self) -> list[dict]:
        """Fetch every available page of the current user’s playlists."""
        tok = self.access_token()
        if not tok:
            return []
        result, url = [], f"{SPOTIFY_API_BASE}/me/playlists?limit=50"
        while url:
            r = requests.get(url, headers={"Authorization": f"Bearer {tok}"})
            if not r.ok:
                break
            data = r.json()
            result.extend(x for x in data.get("items", []) if x)
            url = data.get("next")
        return result

    def search_tracks(self, artist: str, title: str) -> list[dict]:
        """Search Spotify with two queries and deduplicate tracks by URI."""
        tok = self.access_token()
        if not tok:
            return []
        tracks = {}
        succeeded = False
        base_title = _split_title_credit(title)[0]
        lead_artist = _artist_names(artist)
        lead_artist = lead_artist[0] if lead_artist else artist
        # The structured query is precise; the plain query recovers alternate credit formats.
        for q in [f"artist:{lead_artist} track:{base_title}", f"{lead_artist} {base_title}"]:
            try:
                r = requests.get(
                    f"{SPOTIFY_API_BASE}/search",
                    params={"q": q, "type": "track", "limit": 50},
                    headers={"Authorization": f"Bearer {tok}"}, timeout=15,
                )
            except requests.RequestException:
                continue
            if r.ok:
                succeeded = True
                # Rank both first pages together and keep the first copy of each Spotify URI.
                for item in r.json().get("tracks", {}).get("items", []):
                    if item and item.get("uri"):
                        tracks.setdefault(item["uri"], item)
        if not succeeded:
            raise RuntimeError("Spotify search failed. Check your connection and authorization.")
        return list(tracks.values())

    def add_to_playlist(self, playlist_id: str, track_uri: str) -> bool:
        """Add a Spotify track URI to the specified playlist."""
        tok = self.access_token()
        if not tok:
            return False
        r = requests.post(
            f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/tracks",
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            json={"uris": [track_uri]},
        )
        return r.ok


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class SpotifyPlaylistPlugin(GObject.Object, Peas.Activatable):
    __gtype_name__ = "SpotifyPlaylistPlugin"
    object = GObject.Property(type=GObject.Object)

    def __init__(self) -> None:
        """Initialize plugin state and load persisted settings."""
        super().__init__()
        self._last_pid = None
        self._last_playlist = None
        self._shell   = None
        self._client  = None
        self._settings = _load_settings()
        self._actions  = []           # keep refs so GC doesn't eat them
        self._win_action_group = None

    # ------------------------------------------------------------------ lifecycle

    def do_activate(self) -> None:
        """Register actions and add the Spotify menu when the plugin activates."""
        if not HAS_REQUESTS:
            print("[spotify_playlist] ERROR: 'requests' not installed. "
                  "Run: pip3 install --user requests")
            return

        self._shell = self.object
        self._build_client()

        app = Gio.Application.get_default()
        accel_group = self._shell.props.accel_group
        app.set_accels_for_action("app.spotify-add-to-playlist-last", ["<Control><Shift>s"])
        win = self._shell.props.window

        # Register actions on the APPLICATION (accessible as "app.<name>")
        self._register_app_action(app, "spotify-add-to-playlist",
                                  self._on_add_to_playlist)
        self._register_app_action(app, "spotify-add-to-playlist-last",
                                  self._on_add_to_playlist_last)
        self._register_app_action(app, "spotify-connect",
                                  self._on_auth)
        self._register_app_action(app, "spotify-preferences",
                                  self._on_preferences)

        # Inject into the app menubar (GMenuModel)
        self._inject_menu(app)

    def do_deactivate(self) -> None:
        """Remove plugin UI and release client state on deactivation."""
        app = Gio.Application.get_default()
        self._remove_menu(app)
        for name in ("spotify-add-to-playlist", "spotify-connect",
                     "spotify-preferences"):
            app.remove_action(name)
        app.set_accels_for_action("app.spotify-add-to-playlist", [])
        self._actions.clear()
        self._shell  = None
        self._client = None

    # ------------------------------------------------------------------ actions

    def _register_app_action(self, app, name: str, callback) -> None:
        """Register an application action and retain its reference."""
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        app.add_action(action)
        self._actions.append(action)

    # ------------------------------------------------------------------ menu injection

    def _inject_menu(self, app) -> None:
        """
        Walk the app's menubar GMenuModel looking for the Tools section
        (label "Tools" or "_Tools") and append our items there.
        Falls back to appending a top-level "Spotify" menu.
        """
        menubar = app.get_menubar()
        if menubar is None:
            # Build a standalone menu as fallback
            self._build_standalone_menu(app)
            return

        # Build our submenu items
        section = Gio.Menu()
        section.append("Add to Spotify playlist…", "app.spotify-add-to-playlist")
        section.append("Connect to Spotify…",       "app.spotify-connect")
        section.append("Spotify preferences…",      "app.spotify-preferences")

        if not self._try_inject_into_tools(menubar, section):
            # Fallback: add as a new top-level menu
            spotify_menu = Gio.Menu()
            spotify_menu.append_section(None, section)
            menubar.append_submenu("Spotify", spotify_menu)

        self._injected_section = section   # keep ref

    def _try_inject_into_tools(self, model, section, depth=0) -> bool:
        """Recursively search for a 'Tools' menu and append our section."""
        if depth > 5:
            return False
        n = model.get_n_items()
        for i in range(n):
            # Check label
            label = model.get_item_attribute_value(i, "label",
                                                   GLib.VariantType("s"))
            label_str = label.get_string() if label else ""
            if label_str.lower().replace("_", "") == "tools":
                # Found it — get the linked submenu and append
                link = model.get_item_link(i, Gio.MENU_LINK_SUBMENU)
                if link and isinstance(link, Gio.Menu):
                    link.append_section("Spotify", section)
                    return True
                # If it's not a mutable Gio.Menu we can't append
                break

            # Recurse into submenus and sections
            for link_name in (Gio.MENU_LINK_SUBMENU, Gio.MENU_LINK_SECTION):
                link = model.get_item_link(i, link_name)
                if link and self._try_inject_into_tools(link, section, depth + 1):
                    return True
        return False

    def _build_standalone_menu(self, app) -> None:
        """When there's no menubar at all, add a headerbar button instead."""
        win = self._shell.props.window
        btn = Gtk.MenuButton(label="Spotify ▾")
        menu = Gio.Menu()
        menu.append("Add to Spotify playlist…", "app.spotify-add-to-playlist")
        menu.append("Connect to Spotify…",       "app.spotify-connect")
        menu.append("Spotify preferences…",      "app.spotify-preferences")
        btn.set_menu_model(menu)
        btn.show()

        # Try to find a HeaderBar or just pack into window
        header = win.get_titlebar()
        if isinstance(header, Gtk.HeaderBar):
            header.pack_end(btn)
        else:
            # Last resort: floating always-on-top window with the menu
            self._fallback_window(win, menu)

        self._standalone_btn = btn   # keep ref

    def _fallback_window(self, parent, menu) -> None:
        """Tiny toolbar window docked near the main window."""
        w = Gtk.Window(title="Spotify", type_hint=Gdk.WindowTypeHint.UTILITY)
        w.set_transient_for(parent)
        w.set_keep_above(True)
        w.set_default_size(200, 40)
        btn = Gtk.MenuButton(label="Spotify ▾")
        btn.set_menu_model(menu)
        w.add(btn)
        w.show_all()
        self._fallback_win = w

    def _remove_menu(self, app) -> None:
        # Gio.Menu items added via append_section can be removed by index,
        # but it's complex to track. Simplest: just leave them — on deactivate
        # Rhythmbox will reload anyway. For cleanliness, try remove:
        """Remove Spotify menu UI where the host menu permits it."""
        try:
            menubar = app.get_menubar()
            if menubar:
                self._remove_section_from_model(menubar, "Spotify")
        except Exception:
            pass
        # Remove standalone button if any
        try:
            win = self._shell.props.window
            header = win.get_titlebar()
            if isinstance(header, Gtk.HeaderBar) and hasattr(self, "_standalone_btn"):
                header.remove(self._standalone_btn)
        except Exception:
            pass

    def _remove_section_from_model(self, model, section_label, depth=0):
        """Find and remove a labeled section from a menu model."""
        if depth > 5:
            return
        n = model.get_n_items()
        for i in range(n - 1, -1, -1):
            label = model.get_item_attribute_value(i, "label",
                                                   GLib.VariantType("s"))
            if label and label.get_string() == section_label:
                try:
                    model.remove(i)
                except Exception:
                    pass
                return
            for link_name in (Gio.MENU_LINK_SUBMENU, Gio.MENU_LINK_SECTION):
                link = model.get_item_link(i, link_name)
                if link:
                    self._remove_section_from_model(link, section_label, depth + 1)

    # ------------------------------------------------------------------ handlers

    def _on_preferences(self, action, param) -> None:
        """Show preferences and save the selected plugin settings."""
        window = self._shell.props.window
        dlg = Gtk.Dialog(title="Spotify Playlist — Preferences",
                         transient_for=window, modal=True)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OK,     Gtk.ResponseType.OK)
        dlg.set_default_size(500, 230)

        box  = dlg.get_content_area()
        grid = Gtk.Grid(column_spacing=12, row_spacing=8, margin=16)
        box.pack_start(grid, True, True, 0)

        grid.attach(Gtk.Label(label="Client ID:", xalign=1), 0, 0, 1, 1)
        entry = Gtk.Entry(hexpand=True, text=self._settings.get("client_id", ""))
        entry.set_placeholder_text("Paste Spotify App Client ID here")
        grid.attach(entry, 1, 0, 1, 1)

        note = Gtk.Label(use_markup=True, xalign=0,
                         label="Redirect URI to register in Spotify Dashboard:\n"
                               "<b>http://127.0.0.1:8888/callback</b>")
        grid.attach(note, 0, 1, 2, 1)

        cache_enabled = Gtk.CheckButton(label="Remember selected Spotify tracks locally (SQLite)")
        cache_enabled.set_active(self._settings.get("use_match_cache", False))
        grid.attach(cache_enabled, 0, 2, 2, 1)

        clear_button = Gtk.Button(label="Clear saved matches…")
        grid.attach(clear_button, 0, 3, 2, 1)

        def _confirm_clear(_button):
            """Confirm deletion of saved track matches and show the result."""
            # Clearing stays available even while caching is disabled.
            confirm = Gtk.MessageDialog(transient_for=dlg, modal=True,
                                        message_type=Gtk.MessageType.QUESTION,
                                        buttons=Gtk.ButtonsType.NONE,
                                        text="Clear all saved track matches?")
            confirm.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                "Clear", Gtk.ResponseType.OK)
            response = confirm.run()
            confirm.destroy()
            if response != Gtk.ResponseType.OK:
                return
            try:
                count = _clear_matches()
                print(f"[spotify_playlist] Cleared {count} saved matches", flush=True)
                result = Gtk.MessageDialog(transient_for=dlg, modal=True,
                                           message_type=Gtk.MessageType.INFO,
                                           buttons=Gtk.ButtonsType.OK,
                                           text=f"Cleared {count} saved matches.")
                result.run(); result.destroy()
            except (sqlite3.Error, OSError) as exc:
                result = Gtk.MessageDialog(transient_for=dlg, modal=True,
                                           message_type=Gtk.MessageType.ERROR,
                                           buttons=Gtk.ButtonsType.OK,
                                           text=f"Could not clear saved matches: {exc}")
                result.run(); result.destroy()

        clear_button.connect("clicked", _confirm_clear)

        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            cid = entry.get_text().strip()
            self._settings["use_match_cache"] = cache_enabled.get_active()
            if cid:
                self._settings["client_id"] = cid
                self._build_client()
            _save_settings(self._settings)
        dlg.destroy()

    def _on_auth(self, action, param) -> None:
        """Prompt for Spotify authorization and start the browser flow."""
        if not self._ensure_client():
            return
        window = self._shell.props.window
        d = Gtk.MessageDialog(transient_for=window, modal=True,
                              message_type=Gtk.MessageType.INFO,
                              buttons=Gtk.ButtonsType.OK,
                              text="Your browser will open for Spotify login.\n"
                                   "Return here after authorizing.")
        d.run(); d.destroy()

        def _done(success: bool):
            """Show the result of Spotify authorization."""
            msg = ("✓ Spotify connected!" if success
                   else "Authorization failed. Check your Client ID.")
            d2 = Gtk.MessageDialog(
                transient_for=window, modal=True,
                message_type=Gtk.MessageType.INFO if success else Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK, text=msg)
            d2.run(); d2.destroy()

        self._client.start_auth_flow(_done)

    def _on_add_to_playlist_last(self, action, param) -> None:
        """Add the selected track using the last chosen playlist."""
        self._on_add_to_playlist(action, param, add_to_last_id=True)


    def _on_add_to_playlist(self, action, param, add_to_last_id=False) -> None:
        """Choose a playlist before resolving selected Rhythmbox tracks."""
        if not self._ensure_client() or not self._ensure_authenticated():
            return

        entries = self._get_selected_entries()
        if not entries:
            self._show_error("No track selected", "Select a track in the library first.")
            return
        self._add_selected_tracks(entries, add_to_last_id)

    def _entry_metadata(self, entry) -> tuple[str, str, str, Optional[int]]:
        """Read the selected entry's location, artist, title, and duration."""
        artist = entry.get_string(RB.RhythmDBPropType.ARTIST) or ""
        title = entry.get_string(RB.RhythmDBPropType.TITLE) or ""
        location = entry.get_string(RB.RhythmDBPropType.LOCATION) or ""
        try:
            duration_seconds = entry.get_ulong(RB.RhythmDBPropType.DURATION)
            duration_ms = duration_seconds * 1000 if duration_seconds else None
        except (AttributeError, TypeError, ValueError):
            duration_ms = None
        return location, artist, title, duration_ms

    def _add_selected_tracks(self, entries, add_to_last_id) -> None:
        """Choose a playlist, then resolve and add the selected tracks."""
        resolved = []
        skipped = []
        local_matches = [self._entry_metadata(entry) for entry in entries]
        window = self._shell.props.window

        def _next(index, playlist_id, playlist_name):
            """Resolve the next selected track or add the matched tracks."""
            if index == len(local_matches):
                if not resolved:
                    self._show_error("No tracks to add", "No selected tracks were matched on Spotify.")
                    return
                self._add_resolved_tracks(resolved, skipped, playlist_id, playlist_name)
                return

            local_match = local_matches[index]
            location, artist, title, duration_ms = local_match
            wait = Gtk.MessageDialog(
                transient_for=window, modal=False, message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.NONE,
                text=f'Searching Spotify ({index + 1}/{len(entries)}):\n"{title}" — {artist}')
            wait.show()

            def _finish(track, matches=None, error=None):
                """Handle one search result on the GTK thread."""
                wait.destroy()
                if track:
                    resolved.append((track, local_match))
                    _next(index + 1, playlist_id, playlist_name)
                elif matches:
                    self._show_track_picker(
                        matches, add_to_last_id, local_match,
                        on_selected=lambda chosen: _selected(chosen))
                else:
                    skipped.append(title)
                    if len(local_matches) == 1:
                        if error:
                            self._show_error("Spotify search failed",
                                             GLib.markup_escape_text(error))
                        else:
                            self._show_error(
                                "Track not found",
                                f'Could not find <b>{GLib.markup_escape_text(title)}</b> by '
                                f'<b>{GLib.markup_escape_text(artist)}</b> on Spotify.')
                        return
                    if error:
                        print(f"[spotify_playlist] Skipped {title!r}: {error}", flush=True)
                    _next(index + 1, playlist_id, playlist_name)

            def _selected(track):
                """Keep a manually chosen match or skip the cancelled track."""
                if track:
                    resolved.append((track, local_match))
                else:
                    skipped.append(title)
                    if len(local_matches) == 1:
                        return
                _next(index + 1, playlist_id, playlist_name)

            def _search():
                """Read a saved match or search Spotify in a worker thread."""
                try:
                    if self._settings.get("use_match_cache") and _is_local_track(location):
                        try:
                            cached = _load_match(*local_match)
                        except (sqlite3.Error, OSError, ValueError) as exc:
                            print(f"[spotify_playlist] Match cache read failed: {exc}", flush=True)
                        else:
                            if cached:
                                GLib.idle_add(_finish, cached)
                                return
                    tracks = self._client.search_tracks(artist, title)
                    matches = _rank_candidates(tracks, artist, title, duration_ms)
                    GLib.idle_add(_finish, _automatic_match(matches), matches)
                except Exception as exc:
                    GLib.idle_add(_finish, None, None, str(exc))

            threading.Thread(target=_search, daemon=True).start()

        self._show_playlist_picker(
            None, add_to_last_id=add_to_last_id,
            on_playlist=lambda pid, pname: _next(0, pid, pname),
            track_count=len(entries))

    def _add_resolved_tracks(self, resolved, skipped, playlist_id, playlist_name) -> None:
        """Add resolved tracks in order and report a complete failure."""
        def _add():
            """Send each track to Spotify and persist only successful matches."""
            added = 0
            failed = list(skipped)
            for track, local_match in resolved:
                try:
                    ok = self._client.add_to_playlist(playlist_id, track["uri"])
                except Exception as exc:
                    ok = False
                    print(f"[spotify_playlist] Add failed for {track['uri']!r}: {exc}", flush=True)
                if not ok:
                    failed.append(local_match[2])
                    continue
                added += 1
                if self._settings.get("use_match_cache"):
                    try:
                        _save_match(*local_match, track)
                    except (sqlite3.Error, OSError, ValueError) as exc:
                        print(f"[spotify_playlist] Match cache write failed: {exc}", flush=True)
            GLib.idle_add(_done, added, failed)

        def _done(added, failed):
            """Log the result and show an error if no tracks were added."""
            print(f"[spotify_playlist] Added {added} track(s) to {playlist_name!r}; "
                  f"skipped or failed: {len(failed)}", flush=True)
            if added:
                return
            self._show_error("Failed to add tracks",
                             "No tracks were added to the playlist. "
                             "Check the Rhythmbox console for details.")

        threading.Thread(target=_add, daemon=True).start()

    def _show_track_picker(self, matches, add_to_last_id, local_match,
                           on_selected=None) -> None:
        """Let the user choose a Spotify track from ranked candidates."""
        dlg = Gtk.Dialog(title="Choose Spotify track",
                         transient_for=self._shell.props.window, modal=True)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        "Continue", Gtk.ResponseType.OK)
        dlg.set_default_size(680, 440)
        box = dlg.get_content_area()
        _, artist, title, _ = local_match
        box.pack_start(Gtk.Label(
            label=f"Select the matching recording for:\nTitle: {title}\nArtist: {artist}",
            xalign=0, margin=10, wrap=True), False, False, 0)
        store = Gtk.ListStore(str, str, str, str)
        for track, score, exact in matches:
            artists = ", ".join(a.get("name", "") for a in track.get("artists", []))
            duration = track.get("duration_ms")
            length = f"{duration // 60000}:{duration // 1000 % 60:02d}" if isinstance(duration, int) else ""
            store.append([track["name"], artists, length,
                          "Exact" if exact else f"{score:.1f}%"])
        tv = Gtk.TreeView(model=store)
        for index, heading in enumerate(("Title", "Artists", "Length", "Match")):
            tv.append_column(Gtk.TreeViewColumn(heading, Gtk.CellRendererText(), text=index))
        selection = tv.get_selection()
        selection.select_path(Gtk.TreePath.new_first())
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(tv)
        box.pack_start(sw, True, True, 0)
        dlg.show_all()
        response = dlg.run()
        model, selected = selection.get_selected()
        # Tree rows preserve ranked order, so their index identifies the selected track.
        index = model.get_path(selected).get_indices()[0] if selected else None
        dlg.destroy()
        if response != Gtk.ResponseType.OK or index is None:
            print("[spotify_playlist] Track selection cancelled", flush=True)
            if on_selected:
                on_selected(None)
            return
        track = matches[index][0]
        print(f"[spotify_playlist] User selected rank={index + 1}/{len(matches)} "
              f"uri={track['uri']!r}", flush=True)
        if on_selected:
            on_selected(track)
        else:
            self._show_playlist_picker(track, add_to_last_id=add_to_last_id,
                                       local_match=local_match)

    # ------------------------------------------------------------------ playlist picker

    def _show_playlist_picker(self, track, add_to_last_id=False, local_match=None,
                              on_playlist=None, track_count=1) -> None:
        """Choose a playlist, then add a track or pass it to a batch callback."""
        window = self._shell.props.window
        track_uri = track["uri"] if track else None
        title = track["name"] if track else ""
        artist = ", ".join(a.get("name", "") for a in track.get("artists", [])) if track else ""

        def _done(ok, pname):
            """Save a successful match or show the playlist add error."""
            # Persist only the chosen track, and only after Spotify accepts the playlist add.
            if ok and local_match and self._settings.get("use_match_cache"):
                try:
                    _save_match(*local_match, track)
                except (sqlite3.Error, OSError, ValueError) as exc:
                    print(f"[spotify_playlist] Match cache write failed: {exc}", flush=True)
            if not ok:
                d = Gtk.MessageDialog(
                    transient_for=window, modal=True,
                    message_type=Gtk.MessageType.INFO if ok else Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text=f'✓ Added to "{pname}"' if ok
                        else "Failed to add track. Check permissions.")
                d.run(); d.destroy()

        if add_to_last_id and self._last_pid is not None:
            if on_playlist:
                on_playlist(self._last_pid, self._last_playlist)
                return
            ok = self._client.add_to_playlist(self._last_pid, track_uri)
            GLib.idle_add(_done, ok, self._last_playlist)
            return

        wait = Gtk.MessageDialog(transient_for=window, modal=False,
                                 message_type=Gtk.MessageType.INFO,
                                 buttons=Gtk.ButtonsType.NONE,
                                 text="Loading your Spotify playlists…")
        wait.show()

        def _fetch():
            """Fetch playlists in a worker thread before showing the picker."""
            playlists = self._client.get_playlists()
            GLib.idle_add(_show, playlists)

        def _show(playlists):
            """Display playlists and start adding to the chosen one."""
            wait.destroy()
            if not playlists:
                self._show_error("No playlists",
                                 "No Spotify playlists found for your account.")
                return

            dlg = Gtk.Dialog(title="Choose Spotify playlist",
                             transient_for=window, modal=True)
            dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                            "Add",            Gtk.ResponseType.OK)
            dlg.set_default_size(420, 400)

            box = dlg.get_content_area()
            prompt = (f'Add {track_count} selected tracks to:' if track_count > 1 else
                      'Add selected track to:' if track is None else
                      f'Add  <b>{GLib.markup_escape_text(title)}</b>'
                      f'  by  <b>{GLib.markup_escape_text(artist)}</b>  to:')
            box.pack_start(
                Gtk.Label(use_markup=True, xalign=0, margin=10, label=prompt),
                False, False, 0)

            store = Gtk.ListStore(str, str)
            for pl in playlists:
                store.append([pl["id"], pl["name"]])

            tv  = Gtk.TreeView(model=store, headers_visible=False)
            tv.append_column(Gtk.TreeViewColumn("", Gtk.CellRendererText(), text=1))
            sel = tv.get_selection()
            sel.select_path(Gtk.TreePath.new_first())

            sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
            sw.add(tv)
            box.pack_start(sw, True, True, 0)
            dlg.show_all()

            resp = dlg.run()
            model, it = sel.get_selected()
            dlg.destroy()
            if resp != Gtk.ResponseType.OK or it is None:
                return

            pid   = model[it][0]
            pname = model[it][1]

            self._last_pid = pid
            self._last_playlist = pname
            if on_playlist:
                on_playlist(pid, pname)
                return

            def _add():
                """Add the track to the chosen playlist in a worker thread."""
                ok = self._client.add_to_playlist(pid, track_uri)
                GLib.idle_add(_done, ok, pname)

            threading.Thread(target=_add, daemon=True).start()

        threading.Thread(target=_fetch, daemon=True).start()

    # ------------------------------------------------------------------ helpers

    def _get_selected_entries(self):
        """Return all selected entries in the current page."""
        try:
            page = self._shell.props.selected_page
            if page is None:
                return []
            entry_view = page.get_entry_view()
            if entry_view is None:
                return []
            return list(entry_view.get_selected_entries() or [])
        except Exception as e:
            print(f"[spotify_playlist] _get_selected_entries error: {e}")
            return []

    def _build_client(self) -> None:
        """Create a Spotify client when a client ID is configured."""
        cid = self._settings.get("client_id", "")
        self._client = SpotifyClient(cid) if cid else None

    def _ensure_client(self) -> bool:
        """Require a configured Spotify client, showing an error if absent."""
        if self._client:
            return True
        self._show_error("Not configured",
                         "Set your Spotify Client ID first:\n"
                         "<b>Spotify → Spotify preferences…</b>")
        return False

    def _ensure_authenticated(self) -> bool:
        """Require a saved Spotify login, showing an error if absent."""
        if self._client and self._client.is_authenticated():
            return True
        self._show_error("Not connected",
                         "Connect to Spotify first:\n"
                         "<b>Spotify → Connect to Spotify…</b>")
        return False

    def _show_error(self, title: str, markup: str) -> None:
        """Display an error dialog with escaped title and supplied markup."""
        window = self._shell.props.window if self._shell else None
        d = Gtk.MessageDialog(transient_for=window, modal=True,
                              message_type=Gtk.MessageType.ERROR,
                              buttons=Gtk.ButtonsType.OK)
        d.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>\n\n{markup}")
        d.run(); d.destroy()
