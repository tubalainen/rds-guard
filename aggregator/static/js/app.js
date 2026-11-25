/**
 * RDS Guard Aggregator — app.js
 * Tab routing, status bar, shared helpers.
 */

const Util = (() => {
    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str == null ? '' : String(str);
        return div.innerHTML;
    }

    function parseTs(iso) {
        if (!iso) return null;
        const d = new Date(/[zZ]$/.test(iso) ? iso : iso + 'Z');
        return isNaN(d) ? null : d;
    }

    function formatTime(iso) {
        const d = parseTs(iso);
        if (!d) return '';
        const now = new Date();
        const timeStr = d.toLocaleTimeString('sv-SE', {
            hour: '2-digit', minute: '2-digit', second: '2-digit'
        });
        if (d.toDateString() === now.toDateString()) return timeStr;
        const dateStr = d.toLocaleDateString('sv-SE', { day: 'numeric', month: 'short' });
        return `${dateStr} ${timeStr}`;
    }

    function formatAgo(iso) {
        const d = parseTs(iso);
        if (!d) return 'never';
        const sec = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
        if (sec < 60) return `${sec}s ago`;
        if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
        if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
        return formatTime(iso);
    }

    function formatDuration(sec) {
        const rounded = Math.round(sec);
        if (rounded < 60) return `${rounded}s`;
        const m = Math.floor(rounded / 60);
        const s = rounded % 60;
        if (m < 60) return `${m}m ${s}s`;
        const h = Math.floor(m / 60);
        return `${h}h ${m % 60}m`;
    }

    async function api(method, url, body) {
        const opts = { method, headers: {} };
        if (method !== 'GET') {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body || {});
        }
        const resp = await fetch(url, opts);
        let data = null;
        try { data = await resp.json(); } catch (e) { /* empty body */ }
        if (!resp.ok) {
            throw new Error((data && (data.detail || data.error)) || `HTTP ${resp.status}`);
        }
        return data;
    }

    return { escapeHtml, formatTime, formatAgo, formatDuration, api };
})();

const App = (() => {
    function init() {
        document.querySelectorAll('.nav-btn').forEach(btn => {
            btn.addEventListener('click', () => switchView(btn.dataset.view));
        });
        pollStatus();
        setInterval(pollStatus, 10000);
    }

    function switchView(view) {
        document.querySelectorAll('.nav-btn').forEach(b =>
            b.classList.toggle('active', b.dataset.view === view));
        document.querySelectorAll('.view').forEach(v =>
            v.classList.toggle('active', v.id === `view-${view}`));
        if (view === 'nodes') Nodes.load();
    }

    async function pollStatus() {
        try {
            const s = await Util.api('GET', '/api/v1/status');
            document.getElementById('status-nodes').textContent =
                `${s.nodes_online}/${s.nodes} nodes online`;
            document.getElementById('status-events').textContent =
                `${s.events} events` + (s.active ? ` (${s.active} active)` : '');
            const mqttEl = document.getElementById('status-mqtt');
            mqttEl.hidden = !s.mqtt.enabled;
            mqttEl.classList.toggle('mqtt-on', s.mqtt.connected);
            mqttEl.title = s.mqtt.connected
                ? `MQTT connected (prefix ${s.mqtt.prefix})` : 'MQTT not connected';
            const trEl = document.getElementById('status-transcription');
            trEl.textContent = s.transcription_engine === 'none'
                ? 'no transcription' : `whisper: ${s.transcription_engine}`;
            document.getElementById('status-version').textContent = s.version;
        } catch (e) {
            // server not reachable
        }
    }

    return { init };
})();

document.addEventListener('DOMContentLoaded', () => {
    App.init();
    Events.init();
    Nodes.init();
});
