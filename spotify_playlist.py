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
import gi
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

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


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    verifier  = secrets.token_urlsafe(64)[:128]
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def _load_token() -> dict:
    try:
        return json.loads(TOKEN_CACHE.read_text())
    except Exception:
        return {}

def _save_token(data: dict) -> None:
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}

def _save_settings(data: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# Spotify client
# ---------------------------------------------------------------------------

class SpotifyClient:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self._token: dict = _load_token()

    def is_authenticated(self) -> bool:
        return bool(self._token.get("access_token"))

    def access_token(self) -> Optional[str]:
        if not self._token:
            return None
        if self._is_expired():
            self._refresh()
        return self._token.get("access_token")

    def _is_expired(self) -> bool:
        return time.time() > self._token.get("expires_at", 0) - 30

    def _refresh(self) -> None:
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
        verifier, challenge = _pkce_pair()
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
            code_holder = []

            class _H(BaseHTTPRequestHandler):
                def log_message(self, *_): pass
                def do_GET(self):
                    qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    if qs.get("state", [""])[0] == state and "code" in qs:
                        code_holder.append(qs["code"][0])
                        self.send_response(200); self.end_headers()
                        self.wfile.write(b"<h2>Authorized! Close this tab.</h2>")
                    else:
                        self.send_response(400); self.end_headers()

            srv = HTTPServer(("127.0.0.1", 8888), _H)
            srv.timeout = 120
            srv.handle_request()
            srv.server_close()

            if not code_holder:
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

    def search_track(self, artist: str, title: str) -> Optional[str]:
        tok = self.access_token()
        if not tok:
            return None
        for q in [f"artist:{artist} track:{title}", f"{artist} {title}"]:
            r = requests.get(
                f"{SPOTIFY_API_BASE}/search",
                params={"q": q, "type": "track", "limit": 1},
                headers={"Authorization": f"Bearer {tok}"},
            )
            if r.ok:
                items = r.json().get("tracks", {}).get("items", [])
                if items:
                    return items[0]["uri"]
        return None

    def add_to_playlist(self, playlist_id: str, track_uri: str) -> bool:
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
        window = self._shell.props.window
        dlg = Gtk.Dialog(title="Spotify Playlist — Preferences",
                         transient_for=window, modal=True)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OK,     Gtk.ResponseType.OK)
        dlg.set_default_size(440, 160)

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

        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            cid = entry.get_text().strip()
            if cid:
                self._settings["client_id"] = cid
                _save_settings(self._settings)
                self._build_client()
        dlg.destroy()

    def _on_auth(self, action, param) -> None:
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
            msg = ("✓ Spotify connected!" if success
                   else "Authorization failed. Check your Client ID.")
            d2 = Gtk.MessageDialog(
                transient_for=window, modal=True,
                message_type=Gtk.MessageType.INFO if success else Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK, text=msg)
            d2.run(); d2.destroy()

        self._client.start_auth_flow(_done)

    def _on_add_to_playlist_last(self, action, param) -> None:
        self._on_add_to_playlist(action, param, add_to_last_id=True)


    def _on_add_to_playlist(self, action, param, add_to_last_id=False) -> None:
        if not self._ensure_client() or not self._ensure_authenticated():
            return

        entry = self._get_selected_entry()
        if entry is None:
            self._show_error("No track selected", "Select a track in the library first.")
            return

        artist = entry.get_string(RB.RhythmDBPropType.ARTIST) or ""
        title  = entry.get_string(RB.RhythmDBPropType.TITLE)  or ""
        window = self._shell.props.window

        wait = Gtk.MessageDialog(transient_for=window, modal=False,
                                 message_type=Gtk.MessageType.INFO,
                                 buttons=Gtk.ButtonsType.NONE,
                                 text=f'Searching Spotify for:\n"{title}" — {artist}')
        wait.show()

        def _search():
            uri = self._client.search_track(artist, title)
            GLib.idle_add(_got_uri, uri)

        def _got_uri(track_uri):
            wait.destroy()
            if not track_uri:
                self._show_error(
                    "Track not found",
                    f'Could not find <b>{GLib.markup_escape_text(title)}</b> by '
                    f'<b>{GLib.markup_escape_text(artist)}</b> on Spotify.')
                return
            self._show_playlist_picker(track_uri, title, artist, add_to_last_id=add_to_last_id)

        threading.Thread(target=_search, daemon=True).start()

    # ------------------------------------------------------------------ playlist picker

    def _show_playlist_picker(self, track_uri, title, artist, add_to_last_id=False) -> None:
        window = self._shell.props.window

        def _done(ok, pname):
            if not ok:
                d = Gtk.MessageDialog(
                    transient_for=window, modal=True,
                    message_type=Gtk.MessageType.INFO if ok else Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text=f'✓ Added to "{pname}"' if ok
                        else "Failed to add track. Check permissions.")
                d.run(); d.destroy()

        if add_to_last_id and self._last_pid is not None:
            ok = self._client.add_to_playlist(self._last_pid, track_uri)
            GLib.idle_add(_done, ok, self._last_playlist)
            return

        wait = Gtk.MessageDialog(transient_for=window, modal=False,
                                 message_type=Gtk.MessageType.INFO,
                                 buttons=Gtk.ButtonsType.NONE,
                                 text="Loading your Spotify playlists…")
        wait.show()

        def _fetch():
            playlists = self._client.get_playlists()
            GLib.idle_add(_show, playlists)

        def _show(playlists):
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
            box.pack_start(
                Gtk.Label(use_markup=True, xalign=0, margin=10,
                          label=f'Add  <b>{GLib.markup_escape_text(title)}</b>'
                                f'  by  <b>{GLib.markup_escape_text(artist)}</b>  to:'),
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

            def _add():
                ok = self._client.add_to_playlist(pid, track_uri)
                GLib.idle_add(_done, ok, pname)

            threading.Thread(target=_add, daemon=True).start()

        threading.Thread(target=_fetch, daemon=True).start()

    # ------------------------------------------------------------------ helpers

    def _get_selected_entry(self):
        """Return the first selected entry in the current page, or None."""
        try:
            page = self._shell.props.selected_page
            if page is None:
                return None
            entry_view = page.get_entry_view()
            if entry_view is None:
                return None
            entries = entry_view.get_selected_entries()
            return entries[0] if entries else None
        except Exception as e:
            print(f"[spotify_playlist] _get_selected_entry error: {e}")
            return None

    def _build_client(self) -> None:
        cid = self._settings.get("client_id", "")
        self._client = SpotifyClient(cid) if cid else None

    def _ensure_client(self) -> bool:
        if self._client:
            return True
        self._show_error("Not configured",
                         "Set your Spotify Client ID first:\n"
                         "<b>Spotify → Spotify preferences…</b>")
        return False

    def _ensure_authenticated(self) -> bool:
        if self._client and self._client.is_authenticated():
            return True
        self._show_error("Not connected",
                         "Connect to Spotify first:\n"
                         "<b>Spotify → Connect to Spotify…</b>")
        return False

    def _show_error(self, title: str, markup: str) -> None:
        window = self._shell.props.window if self._shell else None
        d = Gtk.MessageDialog(transient_for=window, modal=True,
                              message_type=Gtk.MessageType.ERROR,
                              buttons=Gtk.ButtonsType.OK)
        d.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>\n\n{markup}")
        d.run(); d.destroy()
