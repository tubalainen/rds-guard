/**
 * RDS Guard — satellite: starts the shared UI and adds what only the
 * satellite has: the receiver status and the Debug view (live RDS console).
 */
'use strict';

/* console.js reports its WebSocket state through App.setWsStatus */
const App = {
    setWsStatus(on) {
        const dot = document.getElementById('ws-dot');
        const text = document.getElementById('ws-text');
        if (dot) dot.className = `dot ${on ? 'ok' : 'bad'}`;
        if (text) text.textContent = on ? 'Live-ström ansluten' : 'Live-ström frånkopplad';
    },
};

const Satellite = (() => {
    const esc = RDSUI.esc;
    let status = null;

    function stationsOf(s) {
        if (!s) return [];
        if (Array.isArray(s.stations)) {
            return s.stations.filter((x) => x.pi).map((x) => ({
                pi: x.pi, ps: x.ps || x.long_ps, frequency: x.frequency, ta: x.ta, tp: x.tp,
                prog_type: x.prog_type, radiotext: x.radiotext, groups_per_sec: x.groups_per_sec,
            }));
        }
        if (s.station && s.station.pi) {
            return [{ ...s.station, ps: s.station.ps || s.station.long_ps, frequency: s.frequency,
                groups_per_sec: s.groups_per_sec }];
        }
        return [];
    }

    function fmtUptime(sec) {
        if (sec == null) return '–';
        if (sec < 3600) return `${Math.floor(sec / 60)} min`;
        const h = Math.floor(sec / 3600);
        if (h < 24) return `${h} h ${Math.floor((sec % 3600) / 60)} min`;
        return `${Math.floor(h / 24)} d ${h % 24} h`;
    }

    const PIPELINE = {
        running: ['ok', 'Mottagaren kör'], starting: ['warn', 'Startar…'],
        error: ['bad', 'Fel i mottagaren'], stopped: ['bad', 'Mottagaren stoppad'],
        not_started: ['warn', 'Inte startad'],
    };
    const UPLINK = {
        connected: 'ansluten', starting: 'ansluter…', offline: 'offline',
        unauthorized: 'fel token', incompatible: 'behöver uppdateras',
    };

    function renderStatus() {
        const top = document.getElementById('top-status');
        const s = status;
        if (!s) {
            top.innerHTML = '<span class="dot bad"></span><span>Ingen kontakt</span>';
            return;
        }
        const pipe = (s.pipeline && s.pipeline.state) || 'unknown';
        const [cls, label] = PIPELINE[pipe] || ['warn', pipe];
        const st = stationsOf(s);
        const name = st.length === 1 ? (st[0].ps || st[0].pi) : (st.length ? `${st.length} stationer` : label);
        top.innerHTML = `<span class="dot ${cls}"></span><span>${esc(pipe === 'running' ? name : label)}</span>`;
        top.title = `${label}${s.pipeline && s.pipeline.error ? ` – ${s.pipeline.error}` : ''}`;
        document.getElementById('foot-version').textContent = `RDS Guard ${s.version || ''}`.trim();
        document.getElementById('foot-mqtt').textContent = `MQTT ${s.mqtt_connected ? 'ansluten' : 'ej ansluten'}`;
        const up = s.uplink;
        document.getElementById('foot-uplink').textContent = up && up.enabled
            ? `Uplink ${UPLINK[up.state] || up.state}${up.queue_depth ? ` (${up.queue_depth} i kö)` : ''}` : '';
        renderDebugStatus();
    }

    async function poll() {
        try {
            status = await RDSUI.api('GET', '/status');
        } catch (e) {
            status = null;
        }
        renderStatus();
    }

    async function loadStations() {
        if (!status) await poll();
        const list = [];
        const add = (pi, ps) => {
            if (pi && !list.some((x) => x.pi === pi)) list.push({ pi, ps });
        };
        stationsOf(status).forEach((s) => add(s.pi, s.ps));
        try {
            const cal = await RDSUI.api('GET', '/calendar');
            cal.events.forEach((e) => add(e.pi, e.station_ps));
        } catch (e) { /* stations from the status are enough */ }
        return list;
    }

    // -------------------------------------------------------------- debug view

    function renderDebugStatus() {
        const box = document.getElementById('debug-status');
        if (!box) return;
        const s = status;
        if (!s) {
            box.innerHTML = '<div><span>Status</span><b>ingen kontakt</b></div>';
            return;
        }
        const items = [];
        stationsOf(s).forEach((st) => {
            items.push(['Station', `${st.ps || '–'} · ${RDSUI.fmtFreq(st.frequency)}`]);
            items.push(['PI', st.pi]);
            items.push(['PTY', st.prog_type || '–']);
            items.push(['TP / TA', `${st.tp ? 'TP på' : 'TP av'} · ${st.ta ? 'TA PÅ' : 'TA av'}`]);
            items.push(['Radiotext', st.radiotext || '–']);
            if (st.now_artist || st.now_title) items.push(['Spelas nu', [st.now_artist, st.now_title].filter(Boolean).join(' – ')]);
            if (st.groups_per_sec != null) items.push(['Avkodning', `${st.groups_per_sec} grp/s`]);
        });
        if (!stationsOf(s).length) items.push(['Station', `ingen ännu · ${RDSUI.fmtFreq(s.frequency) || ''}`]);
        const pipe = s.pipeline || {};
        items.push(['Mottagning', `${pipe.state || '–'}${pipe.error ? ` – ${pipe.error}` : ''}`]);
        items.push(['Drifttid', `${fmtUptime(s.uptime_sec)} · ${s.groups_total || 0} grupper`]);
        items.push(['MQTT', s.mqtt_connected ? 'ansluten' : 'ej ansluten']);
        if (s.uplink && s.uplink.enabled) {
            items.push(['Uplink', `${UPLINK[s.uplink.state] || s.uplink.state}${s.uplink.last_error ? ` – ${s.uplink.last_error}` : ''}`]);
        }
        items.push(['Version', s.version || '–']);
        box.innerHTML = items.map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');
    }

    function debugView(root, r, ctx) {
        root.innerHTML = `
            <div class="page-head"><div class="grow"><h1>Debug</h1>
                <p>Avkodad RDS-data direkt från mottagaren, för felsökning.</p></div>
                <span class="debug-ws"><span id="ws-dot" class="dot"></span><span id="ws-text">Ansluter…</span></span></div>
            <section class="debug" aria-label="RDS-konsol">
                <div class="debug-status" id="debug-status"></div>
                <div class="console-controls">
                    <button id="console-pause" class="console-btn" type="button">Pausa</button>
                    <button id="console-clear" class="console-btn" type="button">Rensa</button>
                    <label style="flex:1;display:flex"><span class="sr-only">Filtrera</span>
                        <input id="console-filter" type="text" placeholder="Filtrera meddelanden…" class="console-input"></label>
                    <span id="console-count" class="console-count">0 meddelanden</span>
                </div>
                <div class="console-group-bar">
                    <button class="console-group-chip active" data-group="" type="button">Alla</button>
                    <button class="console-group-chip" data-group="0a" type="button">PS/Flaggor</button>
                    <button class="console-group-chip" data-group="2a" type="button">Radiotext</button>
                    <button class="console-group-chip" data-group="4a" type="button">Klocka</button>
                    <button class="console-group-chip" data-group="14a" type="button">EON</button>
                    <button class="console-group-chip" data-group="alert" type="button">Larm</button>
                </div>
                <div id="console-log" class="console-log"></div>
            </section>`;
        renderDebugStatus();
        Console.init();
        Console.renderAll();
        Console.connect();
        const timer = setInterval(poll, 5000);
        ctx.cleanup = () => {
            clearInterval(timer);
            Console.disconnect();
        };
    }

    function start() {
        RDSUI.start({
            apiBase: '/api',
            idOf: (ev) => ev.id,
            loadStations,
            views: { debug: debugView },
        });
        poll();
        setInterval(() => { if (!document.hidden) poll(); }, 15000);
    }

    return { start };
})();

document.addEventListener('DOMContentLoaded', Satellite.start);
