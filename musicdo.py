"""
musicdo.py — Terminal controller for Apple Music and YouTube.

Uses Chrome DevTools Protocol (CDP) to drive music.apple.com and
youtube.com running in a browser window.

Setup:
  pip install websockets textual
  brave-browser --remote-debugging-port=9222 https://music.apple.com
  Log in to Apple Music in that window, then:  python musicdo.py

YouTube streams are defined in streams.json alongside this file.
Select source 5 to browse and play them. Controls: space=play/pause, r=restart.
"""

import asyncio
import json
import pathlib
import urllib.parse
import urllib.request

import websockets
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static


CDP_HOST = "http://localhost:9222"

_STREAMS_FILE = pathlib.Path(__file__).parent / "streams.json"

_PLAYBACK_STATE = {
    0:  "—",
    1:  "loading...",
    2:  "▶  playing",
    3:  "⏸  paused",
    4:  "■  stopped",
    5:  "■  ended",
    10: "■  completed",
}

# Browse sources: key → (label, API path, extra fetch params)
# Source "5" is handled entirely in Python (no MusicKit JS call).
_BROWSE_SOURCES = {
    "1": ("Added",    "/v1/me/library/recently-added",  {}),
    "2": ("Related",  "",                               {}),
    "3": ("Library",  "/v1/me/library/albums",          {"sort": "-dateAdded"}),
    "4": ("Mixes",    "/v1/me/recommendations",         {}),
    "5": ("YouTube",  "",                               {}),
}

# ---------------------------
# JavaScript templates
# ---------------------------

# Single JS call that returns everything needed to update the Apple Music display.
_STATE_JS = """
(function() {
    const mk = window.MusicKit && MusicKit.getInstance();
    if (!mk) return null;
    const item = mk.nowPlayingItem;
    const queueItems = [];
    const queuePos = (mk.queue && typeof mk.queue.position === 'number')
        ? mk.queue.position : 0;
    try {
        const items = Array.from((mk.queue && mk.queue.items) || []);
        const windowSize = 13;
        const tailPad    = 3;
        const headLen    = windowSize - 1 - tailPad;
        const start = queuePos <= headLen ? 0 : queuePos - headLen;
        const end   = Math.min(items.length, start + windowSize);
        for (let i = start; i < end; i++) {
            queueItems.push({
                title:     items[i].title      || '',
                artist:    items[i].artistName || '',
                isCurrent: i === queuePos,
            });
        }
    } catch(e) {}
    return {
        title:       item ? item.title        : null,
        artist:      item ? item.artistName   : null,
        album:       item ? item.albumName    : null,
        state:       mk.playbackState,
        currentTime: mk.currentPlaybackTime    || 0,
        duration:    mk.currentPlaybackDuration || 0,
        volume:      mk.volume,
        queue:       queueItems,
    };
})()
"""

# Polls the HTML5 video element on a YouTube page.
_YT_STATE_JS = """
(function() {
    const video = document.querySelector('video');
    if (!video) return null;
    return {
        title:       document.title.replace(/ - YouTube$/, ''),
        paused:      video.paused,
        currentTime: video.currentTime,
        duration:    video.duration || 0,
        ended:       video.ended,
        volume:      video.volume,
    };
})()
"""


# ---------------------------
# JS builders (Apple Music)
# ---------------------------

def _build_search_js(term: str) -> str:
    safe = term.replace("\\", "\\\\").replace("`", "\\`").replace("'", "\\'")
    return f"""
    (async function() {{
        const mk = MusicKit.getInstance();
        let songs = [], albums = [];
        try {{
            const sf  = mk.storefrontId || 'us';
            const res = await mk.api.music('/v1/catalog/' + sf + '/search', {{
                term: '{safe}', types: 'songs,albums', limit: 10
            }});
            const r = (res.data && res.data.results) || res.results || {{}};
            songs  = (r.songs  && r.songs.data)  || [];
            albums = (r.albums && r.albums.data) || [];
        }} catch(e1) {{
            try {{
                const res = await mk.api.search('{safe}', {{
                    types: ['songs', 'albums'], limit: 10
                }});
                const r = res.results || res;
                songs  = (r.songs  && r.songs.data)  || [];
                albums = (r.albums && r.albums.data) || [];
            }} catch(e2) {{ return {{error: e2.toString()}}; }}
        }}
        const mapItem = (i, kind) => ({{
            id:    i.attributes.playParams.id,
            kind:  kind,
            title: i.attributes.name,
            artist: i.attributes.artistName || '',
            extra: kind === 'album'
                ? (i.attributes.trackCount ? i.attributes.trackCount + ' tracks' : '')
                : (i.attributes.albumName || ''),
        }});
        return [...albums.map(i => mapItem(i,'album')), ...songs.map(i => mapItem(i,'song'))];
    }})()
    """


def _build_browse_js(source_key: str, artist: str = "") -> str:
    """Build JS to fetch browse items for Apple Music sources (1–4)."""
    if source_key == "1":
        return """
        (async function() {
            const mk = MusicKit.getInstance();
            const res = await mk.api.music('/v1/me/library/recently-added', {limit: 25});
            return (res.data && res.data.data || [])
                .filter(i => i.attributes && i.attributes.playParams)
                .map(i => ({
                    id:     i.attributes.playParams.id,
                    kind:   i.attributes.playParams.kind,
                    title:  i.attributes.name,
                    artist: i.attributes.artistName || i.attributes.curatorName || '',
                    extra:  i.attributes.trackCount ? i.attributes.trackCount + ' tracks' : '',
                }));
        })()
        """
    elif source_key == "2":
        safe = artist.replace("\\", "\\\\").replace("`", "\\`").replace("'", "\\'")
        return f"""
        (async function() {{
            const mk = MusicKit.getInstance();
            const sf = mk.storefrontId || 'us';
            const res = await mk.api.music(`/v1/catalog/${{sf}}/search`, {{
                term: '{safe}', types: 'albums', limit: 25
            }});
            const r = (res.data && res.data.results) || {{}};
            return (r.albums && r.albums.data || [])
                .filter(i => i.attributes && i.attributes.playParams)
                .map(i => ({{
                    id:     i.attributes.playParams.id,
                    kind:   i.attributes.playParams.kind,
                    title:  i.attributes.name,
                    artist: i.attributes.artistName || '',
                    extra:  i.attributes.trackCount ? i.attributes.trackCount + ' tracks' : '',
                }}));
        }})()
        """
    elif source_key == "3":
        return """
        (async function() {
            const mk = MusicKit.getInstance();
            const res = await mk.api.music('/v1/me/library/albums', {
                limit: 100, sort: '-dateAdded'
            });
            return (res.data && res.data.data || []).map(i => ({
                id:     i.attributes.playParams.id,
                kind:   i.attributes.playParams.kind,
                title:  i.attributes.name,
                artist: i.attributes.artistName || '',
                extra:  i.attributes.trackCount ? i.attributes.trackCount + ' tracks' : '',
            }));
        })()
        """
    else:  # "4" — Mixes
        return """
        (async function() {
            const mk = MusicKit.getInstance();
            const res = await mk.api.music('/v1/me/recommendations', {limit: 10});
            const items = [];
            for (const reco of (res.data && res.data.data || [])) {
                for (const c of (reco.relationships &&
                                 reco.relationships.contents &&
                                 reco.relationships.contents.data || [])) {
                    if (c.type === 'playlists') {
                        items.push({
                            id:     c.attributes.playParams.id,
                            kind:   c.attributes.playParams.kind,
                            title:  c.attributes.name,
                            artist: c.attributes.curatorName || 'Apple Music',
                            extra:  (c.attributes.description &&
                                     c.attributes.description.short) || '',
                        });
                    }
                }
            }
            return items;
        })()
        """


# ---------------------------
# CDP helpers
# ---------------------------

def _discover_tab(host_fragment: str) -> dict | None:
    """Return the first browser tab whose URL contains host_fragment."""
    try:
        with urllib.request.urlopen(f"{CDP_HOST}/json", timeout=2) as resp:
            tabs = json.loads(resp.read())
        for tab in tabs:
            if host_fragment in tab.get("url", ""):
                return tab
    except Exception:
        pass
    return None


def _load_streams() -> list[dict]:
    """Load YouTube stream definitions from streams.json."""
    try:
        with open(_STREAMS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


async def _eval(ws, expression: str, msg_id: int, await_promise: bool = False):
    """Send a Runtime.evaluate command and return the result value."""
    await ws.send(json.dumps({
        "id":     msg_id,
        "method": "Runtime.evaluate",
        "params": {
            "expression":    expression,
            "returnByValue": True,
            "awaitPromise":  await_promise,
        },
    }))
    while True:
        raw = json.loads(await ws.recv())
        if raw.get("id") == msg_id:
            return raw.get("result", {}).get("result", {}).get("value")


# ---------------------------
# Display formatters
# ---------------------------

def _fmt_time(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _progress_bar(current: float, duration: float, width: int) -> str:
    if not duration or width < 4:
        return "─" * max(width, 0)
    ratio  = min(current / duration, 1.0)
    filled = int(ratio * (width - 1))
    return "━" * filled + "○" + "─" * (width - 1 - filled)


def _volume_bar(volume: float, width: int = 10) -> str:
    filled = round(min(max(volume, 0.0), 1.0) * width)
    return "█" * filled + "░" * (width - filled)


def _browse_tab_line(active: str, related_artist: str = "") -> str:
    """Render the source-selector tab line, highlighting the active source."""
    parts = []
    for key, (label, _, _) in _BROWSE_SOURCES.items():
        display = label
        if key == "2" and related_artist:
            short = related_artist[:20] + "…" if len(related_artist) > 20 else related_artist
            display = f"Related: {short}"
        if key == active:
            parts.append(f"[bold #fb4934]{key} {display}[/bold #fb4934]")
        else:
            parts.append(f"[dim]{key} {display}[/dim]")
    return "  ".join(parts)


# ---------------------------
# App
# ---------------------------

class MusicDoApp(App):
    TITLE    = "musicdo"
    CSS_PATH = "musicdo.tcss"

    BINDINGS = [
        Binding("space",  "play_pause",  "Play/Pause"),
        Binding("n",      "next_track",  "Next"),
        Binding("p",      "prev_track",  "Prev"),
        Binding("r",      "restart",     "Restart",  show=False),
        Binding("=",      "vol_up",      "Vol+"),
        Binding("-",      "vol_down",    "Vol-"),
        Binding("m",      "music_mode",  "Music",    show=False),
        Binding("slash",  "open_search", "Search"),
        Binding("escape", "close_panel", "Close",    show=False),
        Binding("q",      "quit",        "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="main"):
            yield Static("connecting...", id="status")

            with Horizontal(id="top_row"):
                with Vertical(id="now_playing"):
                    yield Static("", classes="vspace")
                    yield Static("", id="track",        markup=True)
                    yield Static("", id="sub",           markup=True)
                    yield Static("", classes="vspace")
                    yield Static("", id="progress_bar",  markup=True)
                    yield Static("", classes="vspace")
                    with Horizontal(id="state_vol_row"):
                        yield Static("", id="pb_state", markup=True)
                        yield Static("", id="vol",      markup=True)
                    yield Static("", classes="vspace")

                with Vertical(id="queue_panel"):
                    yield Static(
                        "[bold #fabd2f]Queue[/bold #fabd2f]",
                        id="queue_header", markup=True,
                    )
                    yield Static("", id="queue_list", markup=True)

            with Vertical(id="browse_area"):
                yield Static("", id="browse_tabs", markup=True)
                with Vertical(id="browse_panel"):
                    yield ListView(id="browse_results")
                with Vertical(id="search_panel"):
                    yield Input(id="search_input", placeholder="search artists, albums, songs...")
                    yield ListView(id="search_results")

        yield Footer()

    async def on_mount(self) -> None:
        # Apple Music state
        self._ws             = None
        self._msg_id         = 0
        self._lock           = asyncio.Lock()
        self._browse_source  = "1"
        self._search_mode    = False
        self._browse_cache: dict[str, list] = {}
        self._current_artist = ""

        # YouTube state
        self._mode       = "music"   # "music" | "youtube"
        self._ws_yt      = None
        self._yt_lock    = asyncio.Lock()
        self._yt_tab_id  = None      # CDP tab id of the YouTube tab
        self._yt_session = 0         # incremented on each new stream; cancels old poll loop
        self._yt_volume  = 0.25      # remembered across streams; starts at 25%

        self.query_one("#search_panel").display = False
        self.query_one("#search_input", Input).disabled = True
        self.query_one("#browse_tabs",  Static).update(_browse_tab_line("1"))
        self.set_focus(None)
        asyncio.create_task(self._connect_loop())

    async def on_unmount(self) -> None:
        """Stop both streams cleanly on quit."""
        if self._ws_yt:
            try:
                async with self._yt_lock:
                    self._msg_id += 1
                    await _eval(self._ws_yt,
                        "(function(){ const v=document.querySelector('video');"
                        " if(v) v.pause(); })()",
                        self._msg_id)
            except Exception:
                pass
        if self._ws:
            try:
                async with self._lock:
                    self._msg_id += 1
                    await _eval(self._ws,
                        "(function(){ const mk = window.MusicKit && MusicKit.getInstance();"
                        " if(mk) mk.pause(); })()",
                        self._msg_id)
            except Exception:
                pass

    # ----------------------------------------
    # Apple Music — CDP connection + polling
    # ----------------------------------------

    async def _connect_loop(self) -> None:
        """Background loop: find the AM tab, connect, poll every second."""
        while True:
            tab = _discover_tab("music.apple.com")
            if not tab:
                self._set_status(
                    "No Apple Music tab found — "
                    "launch browser with --remote-debugging-port=9222"
                )
                await asyncio.sleep(3)
                continue
            try:
                async with websockets.connect(tab["webSocketDebuggerUrl"]) as ws:
                    self._ws = ws
                    if self._mode == "music":
                        self._set_status("● connected")
                    asyncio.create_task(self._load_browse(self._browse_source))
                    while True:
                        await self._refresh()
                        await asyncio.sleep(1)
            except Exception:
                self._ws = None
                if self._mode == "music":
                    self._set_status("● disconnected — retrying...")
                await asyncio.sleep(3)

    async def _refresh(self) -> None:
        """Poll Apple Music state. Skips display updates when in YouTube mode."""
        async with self._lock:
            self._msg_id += 1
            state = await _eval(self._ws, _STATE_JS, self._msg_id)

        if self._mode == "youtube":
            # Keep connection alive but don't touch the display
            return

        if not state:
            self.query_one("#track", Static).update(
                "[dim]Not ready — are you logged in to Apple Music?[/dim]"
            )
            for wid in ("#sub", "#progress_bar", "#pb_state", "#vol", "#queue_list"):
                self.query_one(wid, Static).update("")
            return

        title    = state.get("title")  or "—"
        artist   = state.get("artist") or "—"
        album    = state.get("album")  or "—"
        current  = state.get("currentTime", 0)
        duration = state.get("duration",    0)
        volume   = state.get("volume",      0.5)
        pb_state = _PLAYBACK_STATE.get(state.get("state", 0), "—")

        try:
            panel_w = self.query_one("#now_playing").size.width - 16
        except Exception:
            panel_w = 40
        bar      = _progress_bar(current, duration, max(panel_w, 10))
        time_str = f"{_fmt_time(current)} / {_fmt_time(duration)}"
        vol_bar  = _volume_bar(volume)

        self.query_one("#track",        Static).update(f"[bold #ebdbb2]{title}[/bold #ebdbb2]")
        self.query_one("#sub",          Static).update(f"[dim]{artist}  ·  {album}[/dim]")
        self.query_one("#progress_bar", Static).update(
            f"[#fabd2f]{bar}[/#fabd2f]  [dim]{time_str}[/dim]"
        )
        self.query_one("#pb_state", Static).update(f"[#fe8019]{pb_state}[/#fe8019]")
        self.query_one("#vol",      Static).update(
            f"[dim]vol[/dim]  [#b8bb26]{vol_bar}[/#b8bb26]  [dim]{int(volume * 100)}%[/dim]"
        )

        queue = state.get("queue", [])
        if queue:
            lines = []
            for item in queue:
                t          = item.get("title",     "—")
                a          = item.get("artist",    "")
                is_current = item.get("isCurrent", False)
                marker = "[#fabd2f]▸[/#fabd2f]" if is_current else " "
                a_part = f"  [dim #a89984]{a}[/dim #a89984]" if a else ""
                lines.append(f"{marker} [dim]{t}[/dim]{a_part}")
            self.query_one("#queue_list", Static).update("\n".join(lines))
        else:
            self.query_one("#queue_list", Static).update("[dim](queue empty)[/dim]")

        # Invalidate Related cache when artist changes
        if artist and artist != "—" and artist != self._current_artist:
            self._current_artist = artist
            self._browse_cache.pop("2", None)
            if self._browse_source == "2":
                asyncio.create_task(self._load_browse("2"))

    def _set_status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    # ----------------------------------------
    # Apple Music — CDP command helper
    # ----------------------------------------

    async def _js(self, expression: str, await_promise: bool = False) -> object:
        if not self._ws:
            return None
        async with self._lock:
            self._msg_id += 1
            return await _eval(self._ws, expression, self._msg_id, await_promise)

    async def _js_fire(self, expression: str) -> None:
        """Send a CDP command without waiting for a response.

        Bypasses the poll lock so commands execute immediately even while a
        poll round-trip is in progress.  The poll loop's _eval discards any
        response whose id doesn't match what it's waiting for, so stray
        responses are harmless.
        """
        if not self._ws:
            return
        self._msg_id += 1
        await self._ws.send(json.dumps({
            "id":     self._msg_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression":    expression,
                "returnByValue": True,
                "awaitPromise":  False,
            },
        }))

    # ----------------------------------------
    # YouTube — connection + polling
    # ----------------------------------------

    async def _open_stream(self, url: str) -> None:
        """Navigate to (or open) a YouTube URL and switch to YouTube mode."""
        # Stop Apple Music before starting YouTube
        await self._js("""
            (function() {
                const mk = window.MusicKit && MusicKit.getInstance();
                if (mk) mk.pause();
            })()
        """)
        self._set_status("▶ opening YouTube...")

        # Look for an existing YouTube tab by saved ID first, then by URL scan
        tab = None
        if self._yt_tab_id:
            try:
                with urllib.request.urlopen(f"{CDP_HOST}/json", timeout=2) as resp:
                    for t in json.loads(resp.read()):
                        if t.get("id") == self._yt_tab_id:
                            tab = t
                            break
            except Exception:
                pass
        if not tab:
            tab = _discover_tab("youtube.com")

        if tab:
            # Navigate existing tab to the new URL
            self._yt_tab_id = tab["id"]
            try:
                async with websockets.connect(
                    tab["webSocketDebuggerUrl"], open_timeout=5
                ) as ws:
                    await ws.send(json.dumps({
                        "id": 1, "method": "Page.navigate", "params": {"url": url}
                    }))
                    await asyncio.sleep(0.3)
            except Exception:
                pass
        else:
            # Open a brand new tab
            try:
                with urllib.request.urlopen(
                    f"{CDP_HOST}/json/new?{url}", timeout=5
                ) as resp:
                    new_tab = json.loads(resp.read())
                self._yt_tab_id = new_tab.get("id")
            except Exception as exc:
                self.notify(f"Could not open YouTube tab: {exc}", severity="error")
                return

        # Switch mode and start a new poll loop (session counter cancels the old one)
        self._mode = "youtube"
        self._yt_session += 1
        session = self._yt_session

        self._set_status("▶ loading YouTube...")
        # Give the page time to load before we start polling
        await asyncio.sleep(4)
        asyncio.create_task(self._yt_poll_loop(session))

    async def _yt_poll_loop(self, session: int) -> None:
        """Poll YouTube video state. Exits when mode changes or a new session starts."""
        while self._mode == "youtube" and self._yt_session == session:
            tab = None
            if self._yt_tab_id:
                try:
                    with urllib.request.urlopen(f"{CDP_HOST}/json", timeout=2) as resp:
                        for t in json.loads(resp.read()):
                            if t.get("id") == self._yt_tab_id:
                                tab = t
                                break
                except Exception:
                    pass

            if not tab:
                self._set_status("● youtube — tab not found")
                await asyncio.sleep(3)
                continue

            try:
                async with websockets.connect(tab["webSocketDebuggerUrl"]) as ws:
                    self._ws_yt = ws
                    self._set_status("● youtube")
                    # Restore saved volume before first poll
                    async with self._yt_lock:
                        self._msg_id += 1
                        await _eval(ws,
                            f"(function(){{ const v=document.querySelector('video');"
                            f" if(v) v.volume={self._yt_volume:.2f}; }})()",
                            self._msg_id)
                    while self._mode == "youtube" and self._yt_session == session:
                        await self._yt_refresh()
                        await asyncio.sleep(1)
            except Exception:
                self._ws_yt = None
                if self._mode == "youtube" and self._yt_session == session:
                    await asyncio.sleep(3)

        self._ws_yt = None

    async def _yt_refresh(self) -> None:
        """Poll YouTube video element and update the now-playing display."""
        if not self._ws_yt:
            return

        async with self._yt_lock:
            self._msg_id += 1
            state = await _eval(self._ws_yt, _YT_STATE_JS, self._msg_id)

        if not state:
            self.query_one("#track", Static).update("[dim]Loading player...[/dim]")
            for wid in ("#sub", "#progress_bar", "#pb_state", "#vol"):
                self.query_one(wid, Static).update("")
            return

        title    = state.get("title",       "—")
        paused   = state.get("paused",      True)
        current  = state.get("currentTime", 0)
        duration = state.get("duration",    0)
        volume   = state.get("volume",      self._yt_volume)
        pb_state = "⏸  paused" if paused else "▶  playing"

        try:
            panel_w = self.query_one("#now_playing").size.width - 16
        except Exception:
            panel_w = 40
        bar      = _progress_bar(current, duration, max(panel_w, 10))
        time_str = f"{_fmt_time(current)} / {_fmt_time(duration)}"
        vol_bar  = _volume_bar(volume)

        self.query_one("#track",        Static).update(f"[bold #ebdbb2]{title}[/bold #ebdbb2]")
        self.query_one("#sub",          Static).update("[dim]YouTube[/dim]")
        self.query_one("#progress_bar", Static).update(
            f"[#fabd2f]{bar}[/#fabd2f]  [dim]{time_str}[/dim]"
        )
        self.query_one("#pb_state", Static).update(f"[#fe8019]{pb_state}[/#fe8019]")
        self.query_one("#vol",      Static).update(
            f"[dim]vol[/dim]  [#b8bb26]{vol_bar}[/#b8bb26]  [dim]{int(volume * 100)}%[/dim]"
        )
        self.query_one("#queue_list", Static).update("[dim]— YouTube —[/dim]")

    async def _yt_js(self, expression: str) -> object:
        """Execute JS in the YouTube tab."""
        if not self._ws_yt:
            return None
        async with self._yt_lock:
            self._msg_id += 1
            return await _eval(self._ws_yt, expression, self._msg_id)

    async def _yt_js_fire(self, expression: str) -> None:
        """Send a CDP command to the YouTube tab without waiting for a response."""
        if not self._ws_yt:
            return
        self._msg_id += 1
        await self._ws_yt.send(json.dumps({
            "id":     self._msg_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression":    expression,
                "returnByValue": True,
                "awaitPromise":  False,
            },
        }))

    # ----------------------------------------
    # Panel management
    # ----------------------------------------

    def _show_panel(self, panel: str) -> None:
        self.query_one("#browse_panel").display = (panel == "browse")
        self.query_one("#search_panel").display = (panel == "search")

    def action_close_panel(self) -> None:
        inp = self.query_one("#search_input", Input)
        inp.clear()
        inp.disabled = True
        self.query_one("#search_results", ListView).clear()
        self._search_mode = False
        self._show_panel("browse")
        self.set_focus(None)

    # ----------------------------------------
    # Playback actions — mode-aware
    # ----------------------------------------

    async def action_play_pause(self) -> None:
        if self._mode == "youtube":
            await self._yt_js_fire(
                "(function(){ const v=document.querySelector('video');"
                " if(v) v.paused ? v.play() : v.pause(); })()"
            )
        else:
            await self._js_fire("""
                (function() {
                    const mk = MusicKit.getInstance();
                    mk.playbackState === 2 ? mk.pause() : mk.play();
                })()
            """)

    async def action_restart(self) -> None:
        """Restart from beginning — YouTube only."""
        if self._mode != "youtube":
            return
        await self._yt_js_fire(
            "(function(){ const v=document.querySelector('video');"
            " if(v){ v.currentTime=0; v.play(); } })()"
        )

    async def action_next_track(self) -> None:
        if self._mode == "youtube":
            return
        await self._js_fire("MusicKit.getInstance().skipToNextItem()")

    async def action_prev_track(self) -> None:
        if self._mode == "youtube":
            return
        await self._js_fire("MusicKit.getInstance().skipToPreviousItem()")

    async def action_vol_up(self) -> None:
        if self._mode == "youtube":
            self._yt_volume = round(min(1.0, self._yt_volume + 0.05), 2)
            await self._yt_js_fire(
                f"(function(){{ const v=document.querySelector('video');"
                f" if(v) v.volume={self._yt_volume:.2f}; }})()"
            )
        else:
            await self._js_fire("""
                (function() {
                    const mk = MusicKit.getInstance();
                    mk.volume = Math.min(1.0, Math.round((mk.volume + 0.05) * 100) / 100);
                })()
            """)

    async def action_vol_down(self) -> None:
        if self._mode == "youtube":
            self._yt_volume = round(max(0.0, self._yt_volume - 0.05), 2)
            await self._yt_js_fire(
                f"(function(){{ const v=document.querySelector('video');"
                f" if(v) v.volume={self._yt_volume:.2f}; }})()"
            )
        else:
            await self._js_fire("""
                (function() {
                    const mk = MusicKit.getInstance();
                    mk.volume = Math.max(0.0, Math.round((mk.volume - 0.05) * 100) / 100);
                })()
            """)

    # ----------------------------------------
    # Browse
    # ----------------------------------------

    async def _load_browse(self, source_key: str) -> None:
        """Populate the browse list for the given source key."""
        self._browse_source = source_key
        self.query_one("#browse_tabs", Static).update(
            _browse_tab_line(source_key, self._current_artist)
        )

        # Source 5: YouTube streams from local file — no JS call needed
        if source_key == "5":
            self._populate_yt_browse()
            return

        # Cache hit for Apple Music sources
        if source_key in self._browse_cache:
            self._populate_browse(self._browse_cache[source_key])
            self.query_one("#browse_results", ListView).focus()
            return

        if source_key == "2" and not self._current_artist:
            lv = self.query_one("#browse_results", ListView)
            lv.clear()
            lv.append(ListItem(Label("[dim]Nothing playing yet[/dim]", markup=True)))
            return

        lv = self.query_one("#browse_results", ListView)
        lv.clear()
        loading_label = (
            f"loading related: {self._current_artist}..."
            if source_key == "2"
            else f"loading {_BROWSE_SOURCES[source_key][0]}..."
        )
        lv.append(ListItem(Label(f"[dim]{loading_label}[/dim]", markup=True)))

        self._set_status(loading_label)
        results = await self._js(
            _build_browse_js(source_key, self._current_artist),
            await_promise=True,
        )
        self._set_status("● connected" if self._mode == "music" else "● youtube")

        if not results:
            lv.clear()
            lv.append(ListItem(Label("[dim]No items found[/dim]", markup=True)))
            return

        self._browse_cache[source_key] = results
        self._populate_browse(results)
        self.query_one("#browse_results", ListView).focus()

    def _populate_browse(self, items: list) -> None:
        """Populate browse list with Apple Music items."""
        lv = self.query_one("#browse_results", ListView)
        lv.clear()
        for item in items:
            title  = item.get("title",  "—")
            artist = item.get("artist", "")
            extra  = item.get("extra",  "")
            a_part = f"  [dim]{artist}[/dim]" if artist else ""
            e_part = f"  [dim #a89984]{extra}[/dim #a89984]" if extra else ""
            li = ListItem(Label(
                f"[bold #ebdbb2]{title}[/bold #ebdbb2]{a_part}{e_part}",
                markup=True,
            ))
            li._item_id   = item["id"]
            li._item_kind = item["kind"]
            lv.append(li)

    def _populate_yt_browse(self) -> None:
        """Populate browse list with YouTube streams from streams.json."""
        lv = self.query_one("#browse_results", ListView)
        lv.clear()
        streams = _load_streams()
        if not streams:
            lv.append(ListItem(Label(
                "[dim]No streams — add entries to streams.json[/dim]", markup=True
            )))
            return
        for stream in streams:
            title = stream.get("title", "—")
            li = ListItem(Label(
                f"[bold #ebdbb2]{title}[/bold #ebdbb2]",
                markup=True,
            ))
            li._item_id   = stream.get("url", "")
            li._item_kind = "youtube"
            lv.append(li)
        lv.focus()

    def action_music_mode(self) -> None:
        """Pause YouTube and switch display back to Apple Music."""
        asyncio.create_task(self._pause_yt_and_switch())

    async def _pause_yt_and_switch(self) -> None:
        await self._yt_js(
            "(function(){ const v=document.querySelector('video'); if(v) v.pause(); })()"
        )
        self._mode = "music"
        self._set_status("● connected" if self._ws else "● disconnected — retrying...")

    async def on_key(self, event) -> None:
        """Handle source-switching keys 1–5."""
        if event.key in _BROWSE_SOURCES:
            event.stop()
            if self._search_mode:
                self._show_panel("browse")
                self._search_mode = False
            # Browsing sources 1–4 snaps display back to music mode
            if event.key != "5" and self._mode == "youtube":
                await self._yt_js(
                    "(function(){ const v=document.querySelector('video'); if(v) v.pause(); })()"
                )
                self._mode = "music"
            await self._load_browse(event.key)

    # ----------------------------------------
    # Search (Apple Music only)
    # ----------------------------------------

    def action_open_search(self) -> None:
        if self._search_mode:
            return
        self._search_mode = True
        self._show_panel("search")
        inp = self.query_one("#search_input", Input)
        inp.disabled = False
        inp.focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search_input":
            return
        term = event.value.strip()
        if not term:
            return

        self._set_status("searching...")
        results = await self._js(_build_search_js(term), await_promise=True)
        self._set_status("● connected")

        if not results:
            self.notify("No results", severity="warning")
            return
        if isinstance(results, dict) and "error" in results:
            self.notify(f"Search error: {results['error']}", severity="error")
            return

        lv = self.query_one("#search_results", ListView)
        lv.clear()
        for item in results:
            kind   = item.get("kind",   "song")
            title  = item.get("title",  "—")
            artist = item.get("artist", "")
            extra  = item.get("extra",  "")
            prefix = "[dim #b8bb26]album[/dim #b8bb26]" if kind == "album" \
                else "[dim #fb4934] song[/dim #fb4934]"
            e_part = f"  [dim]{extra}[/dim]" if extra else ""
            li = ListItem(Label(
                f"{prefix}  [bold #ebdbb2]{title}[/bold #ebdbb2]"
                f"  [dim]{artist}[/dim]{e_part}",
                markup=True,
            ))
            li._item_id   = item["id"]
            li._item_kind = kind
            lv.append(li)
        lv.focus()

    # ----------------------------------------
    # Play selected item
    # ----------------------------------------

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id not in ("search_results", "browse_results"):
            return
        item_id   = getattr(event.item, "_item_id",   None)
        item_kind = getattr(event.item, "_item_kind", "album")
        if not item_id:
            return

        if item_kind == "youtube":
            await self._open_stream(item_id)
            self.action_close_panel()
            return

        # Apple Music selection — switch back to music mode if needed
        self._mode = "music"
        self._set_status("● connected")
        self.query_one("#queue_header", Static).update(
            "[bold #fabd2f]Queue[/bold #fabd2f]"
        )
        await self._js(f"""
            (async function() {{
                const mk = MusicKit.getInstance();
                await mk.setQueue({{ {item_kind}: '{item_id}' }});
                await mk.play();
            }})()
        """, await_promise=True)
        self.action_close_panel()


if __name__ == "__main__":
    MusicDoApp().run()
