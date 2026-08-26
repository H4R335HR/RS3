#!/usr/bin/env python3
"""
rs3_daemon.py — RadioScrobbler3 MPRIS Daemon

Standalone replacement for the Rhythmbox libpeas plugin.
Monitors any MPRIS-compatible media player via D-Bus (using GDBus / Gio)
and scrobbles internet radio tracks to Last.fm.

Usage:
    python3 rs3_daemon.py              # normal mode
    python3 rs3_daemon.py --dry-run    # detect players but don't scrobble

Requires:
    python-gobject (gi.repository.Gio, GLib)
"""

import sys
import os
import signal
from gi.repository import GLib, Gio
try:
    from gi.repository import GLibUnix
    _signal_add = GLibUnix.signal_add
except ImportError:
    _signal_add = getattr(GLib, 'unix_signal_add', None)

# Ensure we can import sibling modules (S3, rs3_core) regardless of CWD
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from rs3_core import RS3Core
from S3 import bcolors

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MPRIS_PREFIX    = 'org.mpris.MediaPlayer2.'
MPRIS_PATH      = '/org/mpris/MediaPlayer2'
MPRIS_PLAYER_IF = 'org.mpris.MediaPlayer2.Player'
MPRIS_IF        = 'org.mpris.MediaPlayer2'

DRY_RUN = '--dry-run' in sys.argv


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------
class RS3Daemon:
    """
    Monitors MPRIS players on the session D-Bus and feeds metadata
    events to the RS3Core scrobbling engine.
    Uses GDBus (Gio) — native to PyGObject, requiring no extra dbus packages.
    """

    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.loop = GLib.MainLoop()

        # Read config for player preference
        import S3
        self.target_player = S3.config.get('daemon', 'player', fallback='auto')

        # State
        self.active_player_name = None   # e.g. "org.mpris.MediaPlayer2.rhythmbox"
        self.player_proxy = None
        self.props_signal_id = None
        self.name_owner_sub_id = None
        self.core = RS3Core(is_playing_fn=self._is_playing)

        # Previous metadata — used to detect what actually changed
        self._prev_status = None
        self._prev_url = None
        self._prev_title = None
        self._station_name = None

    # -------------------------------------------------------------------
    # Startup
    # -------------------------------------------------------------------

    def run(self):
        """Start monitoring. Blocks until interrupted."""
        print(bcolors.HEADER + "RadioScrobbler3 Daemon starting..." + bcolors.ENDC)
        if DRY_RUN:
            print(bcolors.WARNING + "[DRY RUN] Will detect players but not scrobble" + bcolors.ENDC)

        # Watch for new MPRIS players appearing/disappearing via NameOwnerChanged
        self.name_owner_sub_id = self.bus.signal_subscribe(
            "org.freedesktop.DBus",
            "org.freedesktop.DBus",
            "NameOwnerChanged",
            "/org/freedesktop/DBus",
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_name_owner_changed,
            None
        )

        # Try to attach to an existing player right away
        self._scan_for_players()

        # Handle clean shutdown
        if _signal_add:
            _signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._quit)
            _signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._quit)

        print(bcolors.OKGREEN + "RadioScrobbler3 is Active!" + bcolors.ENDC)
        self.loop.run()

    def _quit(self):
        print(bcolors.WARNING + "\nRadioScrobbler3 shutting down..." + bcolors.ENDC)
        self.core.delayscrobble()   # flush any pending scrobble
        self.loop.quit()
        return False

    # -------------------------------------------------------------------
    # Player discovery
    # -------------------------------------------------------------------

    def _scan_for_players(self):
        """Look for already-running MPRIS players on the bus."""
        try:
            result = self.bus.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "ListNames",
                None,
                GLib.VariantType("(as)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None
            )
            names, = result.unpack()
            for name in names:
                if name.startswith(MPRIS_PREFIX):
                    print(bcolors.OKBLUE + "Found player: %s" % name + bcolors.ENDC)
                    if self._should_attach(name):
                        self._attach_to_player(name)
                        return
            print(bcolors.GREY + "No MPRIS player found yet — waiting..." + bcolors.ENDC)
        except Exception as e:
            print(bcolors.WARNING + "Error listing D-Bus names: %s" % str(e) + bcolors.ENDC)

    def _should_attach(self, bus_name):
        """Check if we should attach to this player based on config."""
        if self.active_player_name:
            return False  # already attached to one
        if self.target_player == 'auto':
            return True
        return bus_name == self.target_player

    def _on_name_owner_changed(self, conn, sender, path, iface, signal, params, user_data):
        """Called when any D-Bus name appears or disappears."""
        name, old_owner, new_owner = params.unpack()
        if not name.startswith(MPRIS_PREFIX):
            return

        if new_owner and not old_owner:
            # New player appeared
            print(bcolors.OKBLUE + "Player appeared: %s" % name + bcolors.ENDC)
            if self._should_attach(name):
                self._attach_to_player(name)

        elif old_owner and not new_owner:
            # Player disappeared
            if name == self.active_player_name:
                print(bcolors.WARNING + "Player disappeared: %s" % name + bcolors.ENDC)
                self._detach_from_player()
                # Try to find another player
                self._scan_for_players()

    # -------------------------------------------------------------------
    # Player attachment
    # -------------------------------------------------------------------

    def _attach_to_player(self, bus_name):
        """Subscribe to PropertiesChanged on the given MPRIS player."""
        self.active_player_name = bus_name
        short_name = bus_name.replace(MPRIS_PREFIX, '')
        print(bcolors.OKGREEN + "Attached to: %s" % short_name + bcolors.ENDC)

        try:
            self.player_proxy = Gio.DBusProxy.new_sync(
                self.bus,
                Gio.DBusProxyFlags.NONE,
                None,
                bus_name,
                MPRIS_PATH,
                MPRIS_PLAYER_IF,
                None
            )
        except Exception as e:
            print(bcolors.FAIL + "Could not create DBusProxy: %s" % str(e) + bcolors.ENDC)
            return

        # Subscribe to property changes
        self.props_signal_id = self.bus.signal_subscribe(
            bus_name,
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            MPRIS_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_properties_changed,
            None
        )

        # Check if something is already playing
        try:
            status_var = self.player_proxy.get_cached_property("PlaybackStatus")
            status = status_var.unpack() if status_var else None
            if status == "Playing":
                meta_var = self.player_proxy.get_cached_property("Metadata")
                if meta_var:
                    self._handle_metadata_change(meta_var.unpack())
                self.core.on_playing()
        except Exception as e:
            print(bcolors.WARNING + "Could not read initial state: %s" % str(e) + bcolors.ENDC)

    def _detach_from_player(self):
        """Unsubscribe from the current player."""
        self.core.delayscrobble()
        if self.props_signal_id:
            self.bus.signal_unsubscribe(self.props_signal_id)
            self.props_signal_id = None
        self.player_proxy = None
        self.active_player_name = None
        self._prev_status = None
        self._prev_url = None
        self._prev_title = None
        self._station_name = None
        self.core.on_stopped()

    # -------------------------------------------------------------------
    # D-Bus helpers
    # -------------------------------------------------------------------

    def _is_playing(self):
        """
        Check if the player is currently playing.
        This is passed to RS3Core as the is_playing_fn callback.
        """
        if not self.player_proxy:
            return False
        try:
            val = self.player_proxy.get_cached_property("PlaybackStatus")
            if val is not None:
                return str(val.unpack()) == "Playing"
            # Fallback to direct D-Bus call if not cached
            res = self.player_proxy.call_sync(
                "org.freedesktop.DBus.Properties.Get",
                GLib.Variant("(ss)", (MPRIS_PLAYER_IF, "PlaybackStatus")),
                Gio.DBusCallFlags.NONE,
                -1,
                None
            )
            inner_val, = res.unpack()
            return str(inner_val) == "Playing"
        except Exception:
            return False

    # -------------------------------------------------------------------
    # Signal handling
    # -------------------------------------------------------------------

    def _on_properties_changed(self, conn, sender, path, iface, signal, params, user_data):
        """
        PropertiesChanged signal handler.

        This single signal replaces the three Rhythmbox plugin signals:
          - playing-changed        → PlaybackStatus changed
          - playing-song-changed   → Metadata URL changed (new station)
          - playing-song-property-changed → Metadata title changed (new song on same station)
        """
        iface_name, changed_props, invalidated = params.unpack()
        if iface_name != MPRIS_PLAYER_IF:
            return

        # --- PlaybackStatus changed ---
        if 'PlaybackStatus' in changed_props:
            new_status = str(changed_props['PlaybackStatus'])
            if new_status != self._prev_status:
                self._prev_status = new_status
                if new_status == 'Playing':
                    self.core.on_playing()
                elif new_status in ('Paused', 'Stopped'):
                    self.core.on_stopped()

        # --- Metadata changed ---
        if 'Metadata' in changed_props:
            metadata = changed_props['Metadata']
            self._handle_metadata_change(metadata)

    def _handle_metadata_change(self, metadata):
        """
        Process an MPRIS Metadata dict.

        Detects whether the station changed (new URL) or just the stream
        title changed (new song on the same station), and calls the
        appropriate core method.

        Radio stations often set xesam:title to the station name itself
        (e.g. "KALX - Berkley", "1.FM Samba Rock") on initial connect and
        as periodic "filler" title updates. We skip these since they are
        not real track titles — the old Rhythmbox plugin never saw them
        because it only received actual stream title changes.
        """
        url = str(metadata.get('xesam:url', ''))
        title = str(metadata.get('xesam:title', ''))

        artist_list = metadata.get('xesam:artist', [])
        if artist_list:
            if isinstance(artist_list, (list, tuple)):
                artist = str(artist_list[0]) if len(artist_list) > 0 else ''
            else:
                artist = str(artist_list)
        else:
            artist = ''
        album = str(metadata.get('xesam:album', ''))

        # Determine station name
        station_name = artist or album or title

        # Skip local files
        if url.startswith('file://'):
            return

        # Determine what changed
        url_changed = (url != self._prev_url) and url
        title_changed = (title != self._prev_title) and title

        if url_changed:
            self._prev_url = url
            self._prev_title = title
            self._station_name = station_name
            print(bcolors.HEADER + "♫ Station: %s" % station_name + bcolors.ENDC)
            print(bcolors.GREY + "  URL: %s" % url + bcolors.ENDC)
            print(bcolors.GREY + "  Stream title: %s" % title + bcolors.ENDC)
            self.core.on_station_changed(url, station_name)
            # Only pass title to core if it's an actual track, not just the
            # station name echoed back as the initial stream title.
            if title and not self._is_station_name(title):
                self.core.on_title_changed(title)
        elif title_changed:
            self._prev_title = title
            # Skip title updates that are just the station/radio name
            if self._is_station_name(title):
                print(bcolors.GREY + "♫ Skipping station-name title: %s" % title + bcolors.ENDC)
                return
            print(bcolors.HEADER + "♫ Title update: %s" % title + bcolors.ENDC)
            self.core.on_title_changed(title)

    def _is_station_name(self, title):
        """
        Check if a title is really just the radio station name rather
        than an actual 'Artist - Track' stream title.

        Many radio stations echo their name as the stream title on connect
        and between songs (e.g. "KALX - Berkley", "1.FM - Calming Piano",
        "Radio Caprice - Progressive Black/Post-Black", "1FM30 - 1FM30").
        The old Rhythmbox plugin never received these — it only got real
        track title changes.
        """
        if not hasattr(self, '_station_name') or not self._station_name:
            return False
        sn = self._station_name.strip()
        t = title.strip()
        # Exact match (case-insensitive)
        if t.lower() == sn.lower():
            return True
        # Station name is contained in the title or vice versa
        # (handles cases like "1.FM Samba Rock" title vs "1.FM Samba Rock" station)
        if sn.lower() in t.lower() and ' - ' not in t:
            return True
        # Title is "X - Y" where X matches the station/radio brand name
        # e.g. station="Radio Caprice - Progressive Black/Post-Black"
        # and title="Radio Caprice - Progressive Black/Post-Black"
        # Already caught by exact match above.
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    daemon = RS3Daemon()
    daemon.run()

if __name__ == '__main__':
    main()
