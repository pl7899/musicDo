"""
musicdo.py — Terminal controller for Apple Music web player.

Uses Chrome DevTools Protocol (CDP) to drive music.apple.com running
in a browser window. The browser handles auth and DRM; this app sends
JavaScript commands and polls for state.

Setup:
  pip install websockets textual
  brave-browser --remote-debugging-port=9222 https://music.apple.com
  Log in to Apple Music in that window, then:  python musicdo.py
"""

import asyncio
import json
import urllib.request

import websockets
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static


CDP_HOST = "http://localhost:9222"

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
_BROWSE_SOURCES = {
    "1": ("Added",    "/v1/me/library/recently-added",  {}),
    "2": ("Related",  "",                               {}),  # dynamic — built from current artist
    "3": ("Library",  "/v1/me/library/albums",          {"sort": "-dateAdded"}),
    "4": ("Mixes",    "/v1/me/recommendations",         {}),
}

# Single JS call that returns everything needed to update the display.
_STATE_JS = """
(function() {
    const mk = window.MusicKit && MusicKit.getInstance();
    if (!mk) return null;
    const item = mk.nowPlayingItem;
    const queueItems = [];
    try {
        const items = Array.from((mk.queue && mk.queue.items) || []);
        for (let i = 0; i < Math.min(items.length, 10); i++) {
            queueItems.push({title: items[i].title || '', artist: items[i].artistName || ''});
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
    """Build JS to fetch browse items for the given source key (1–4)."""
    if source_key == "1":          # Recently added to library
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
    elif source_key == "2":        # Related — catalog albums by current artist
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
    elif source_key == "3":        # Library albums sorted by date added
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
    else:                          # Mixes — personal playlists from recommendations
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

def _discover_tab() -> dict | None:
    """Return the first Apple Music tab found via the CDP discovery endpoint."""
    try:
        with urllib.request.urlopen(f"{CDP_HOST}/json", timeout=2) as resp:
            tabs = json.loads(resp.read())
        for tab in tabs:
            if "music.apple.com" in tab.get("url", ""):
                return tab
    except Exception:
        pass
    return None


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
    # Drain messages until we see the one matching our id —
    # the browser may send unsolicited events between sends.
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
            parts.append(f"[bold #C4622D]{key} {display}[/bold #C4622D]")
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
        Binding("=",      "vol_up",      "Vol+"),
        Binding("-",      "vol_down",    "Vol-"),
        Binding("slash",  "open_search", "Search"),
        Binding("escape", "close_panel", "Close",  show=False),
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
                        "[bold #C9A84C]Queue[/bold #C9A84C]",
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
        self._ws             = None
        self._msg_id         = 0
        self._lock           = asyncio.Lock()
        self._browse_source  = "1"
        self._search_mode    = False
        self._browse_cache: dict[str, list] = {}
        self._current_artist = ""

        self.query_one("#search_panel").display = False
        self.query_one("#search_input", Input).disabled = True
        self.query_one("#browse_tabs", Static).update(_browse_tab_line("1"))
        self.set_focus(None)
        asyncio.create_task(self._connect_loop())

    # ---------------------------
    # CDP connection + polling
    # ---------------------------

    async def _connect_loop(self) -> None:
        """Background loop: find the tab, connect, poll every second, reconnect on drop."""
        while True:
            tab = _discover_tab()
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
                    self._set_status("● connected")
                    asyncio.create_task(self._load_browse(self._browse_source))
                    while True:
                        await self._refresh()
                        await asyncio.sleep(1)
            except Exception:
                self._ws = None
                self._set_status("● disconnected — retrying...")
                await asyncio.sleep(3)

    async def _refresh(self) -> None:
        """Poll MusicKit for current state and update the now-playing widgets."""
        async with self._lock:
            self._msg_id += 1
            state = await _eval(self._ws, _STATE_JS, self._msg_id)

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

        self.query_one("#track",        Static).update(f"[bold #EDD9A3]{title}[/bold #EDD9A3]")
        self.query_one("#sub",          Static).update(f"[dim]{artist}  ·  {album}[/dim]")
        self.query_one("#progress_bar", Static).update(
            f"[#C9A84C]{bar}[/#C9A84C]  [dim]{time_str}[/dim]"
        )
        self.query_one("#pb_state", Static).update(f"[#D4882A]{pb_state}[/#D4882A]")
        self.query_one("#vol",      Static).update(
            f"[dim]vol[/dim]  [#7A9E58]{vol_bar}[/#7A9E58]  [dim]{int(volume * 100)}%[/dim]"
        )

        queue = state.get("queue", [])
        if queue:
            lines = []
            for i, item in enumerate(queue):
                t      = item.get("title",  "—")
                a      = item.get("artist", "")
                marker = "[#C9A84C]▸[/#C9A84C]" if i == 0 else " "
                a_part = f"  [dim #8A7355]{a}[/dim #8A7355]" if a else ""
                lines.append(f"{marker} [dim]{t}[/dim]{a_part}")
            self.query_one("#queue_list", Static).update("\n".join(lines))
        else:
            self.query_one("#queue_list", Static).update("[dim](queue empty)[/dim]")

        # Invalidate the Related cache when the artist changes
        new_artist = artist  # already extracted above
        if new_artist and new_artist != "—" and new_artist != self._current_artist:
            self._current_artist = new_artist
            self._browse_cache.pop("2", None)
            if self._browse_source == "2":
                asyncio.create_task(self._load_browse("2"))

    def _set_status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    # ---------------------------
    # CDP command helper
    # ---------------------------

    async def _js(self, expression: str, await_promise: bool = False) -> object:
        if not self._ws:
            return None
        async with self._lock:
            self._msg_id += 1
            return await _eval(self._ws, expression, self._msg_id, await_promise)

    # ---------------------------
    # Panel management
    # ---------------------------

    def _show_panel(self, panel: str) -> None:
        """Switch the bottom area between browse and search."""
        self.query_one("#browse_panel").display = (panel == "browse")
        self.query_one("#search_panel").display = (panel == "search")

    def action_close_panel(self) -> None:
        """ESC returns to browse view."""
        inp = self.query_one("#search_input", Input)
        inp.clear()
        inp.disabled = True
        self.query_one("#search_results", ListView).clear()
        self._search_mode = False
        self._show_panel("browse")
        self.set_focus(None)

    # ---------------------------
    # Playback actions
    # ---------------------------

    async def action_play_pause(self) -> None:
        await self._js("""
            (function() {
                const mk = MusicKit.getInstance();
                mk.playbackState === 2 ? mk.pause() : mk.play();
            })()
        """)

    async def action_next_track(self) -> None:
        await self._js("MusicKit.getInstance().skipToNextItem()")

    async def action_prev_track(self) -> None:
        await self._js("MusicKit.getInstance().skipToPreviousItem()")

    async def action_vol_up(self) -> None:
        await self._js("""
            (function() {
                const mk = MusicKit.getInstance();
                mk.volume = Math.min(1.0, Math.round((mk.volume + 0.05) * 100) / 100);
            })()
        """)

    async def action_vol_down(self) -> None:
        await self._js("""
            (function() {
                const mk = MusicKit.getInstance();
                mk.volume = Math.max(0.0, Math.round((mk.volume - 0.05) * 100) / 100);
            })()
        """)

    # ---------------------------
    # Browse
    # ---------------------------

    async def _load_browse(self, source_key: str) -> None:
        """Fetch browse items for the given source and populate the ListView."""
        self._browse_source = source_key
        label = _BROWSE_SOURCES[source_key][0]

        self.query_one("#browse_tabs", Static).update(
            _browse_tab_line(source_key, self._current_artist)
        )

        if source_key in self._browse_cache:
            self._populate_browse(self._browse_cache[source_key])
            return

        if source_key == "2" and not self._current_artist:
            lv = self.query_one("#browse_results", ListView)
            lv.clear()
            lv.append(ListItem(Label("[dim]Nothing playing yet[/dim]", markup=True)))
            return

        lv = self.query_one("#browse_results", ListView)
        lv.clear()
        loading_label = f"loading {label}..." if source_key != "2" \
            else f"loading related: {self._current_artist}..."
        lv.append(ListItem(Label(f"[dim]{loading_label}[/dim]", markup=True)))

        self._set_status(loading_label)
        results = await self._js(
            _build_browse_js(source_key, self._current_artist),
            await_promise=True,
        )
        self._set_status("● connected")

        if not results:
            lv.clear()
            lv.append(ListItem(Label("[dim]No items found[/dim]", markup=True)))
            return

        self._browse_cache[source_key] = results
        self._populate_browse(results)

    def _populate_browse(self, items: list) -> None:
        lv = self.query_one("#browse_results", ListView)
        lv.clear()
        for item in items:
            title  = item.get("title",  "—")
            artist = item.get("artist", "")
            extra  = item.get("extra",  "")
            a_part = f"  [dim]{artist}[/dim]" if artist else ""
            e_part = f"  [dim #8A7355]{extra}[/dim #8A7355]" if extra else ""
            li = ListItem(Label(
                f"[bold #EDD9A3]{title}[/bold #EDD9A3]{a_part}{e_part}",
                markup=True,
            ))
            li._item_id   = item["id"]
            li._item_kind = item["kind"]
            lv.append(li)

    async def on_key(self, event) -> None:
        """Handle source-switching keys 1–4."""
        if event.key in _BROWSE_SOURCES:
            event.stop()
            if self._search_mode:
                self._show_panel("browse")
                self._search_mode = False
            await self._load_browse(event.key)

    # ---------------------------
    # Search
    # ---------------------------

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
            prefix = "[dim #7A9E58]album[/dim #7A9E58]" if kind == "album" \
                else "[dim #C4622D] song[/dim #C4622D]"
            e_part = f"  [dim]{extra}[/dim]" if extra else ""
            li = ListItem(Label(
                f"{prefix}  [bold #EDD9A3]{title}[/bold #EDD9A3]"
                f"  [dim]{artist}[/dim]{e_part}",
                markup=True,
            ))
            li._item_id   = item["id"]
            li._item_kind = kind
            lv.append(li)
        lv.focus()

    # ---------------------------
    # Play selected item
    # ---------------------------

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id not in ("search_results", "browse_results"):
            return
        item_id   = getattr(event.item, "_item_id",   None)
        item_kind = getattr(event.item, "_item_kind", "album")
        if not item_id:
            return

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
