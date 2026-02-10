/**
 * RDS Guard — app.js
 * Tab routing, status bar polling, WebSocket connection indicator.
 */

const App = (() => {
    let currentView = 'events';
    let statusTimer = null;

    function init() {
        setupNav();
        pollStatus();
        statusTimer = setInterval(pollStatus, 5000);
    }

    function setupNav() {
        document.querySelectorAll('.nav-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                switchView(btn.dataset.view);
            });
        });
    }

    function switchView(view) {
        currentView = view;

        document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
        document.querySelector(`.nav-btn[data-view="${view}"]`).classList.add('active');

        document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
        document.getElementById(`view-${view}`).classList.add('active');
    }

    async function pollStatus() {
        try {
            const resp = await fetch('/api/status');
            if (!resp.ok) return;
            const data = await resp.json();
            updateStatusBar(data);
        } catch (e) {
            // Server not reachable
        }
    }

    function updateStatusBar(data) {
        const station = data.station;

        // --- Row 1: Station identity + flags + pipeline ---

        if (station) {
            document.getElementById('status-station').textContent =
                station.ps || station.long_ps || '---';
            document.getElementById('status-pi').textContent =
                station.pi || '---';

            // Programme type
            const ptyEl = document.getElementById('status-pty');
            ptyEl.textContent = station.prog_type || '---';
            ptyEl.title = 'Programme Type: ' + (station.prog_type || 'unknown');

            // TP flag
            const tpEl = document.getElementById('status-tp');
            tpEl.classList.toggle('tp-on', !!station.tp);
            tpEl.title = station.tp ? 'Traffic Programme: ON' : 'Traffic Programme: OFF';

            // TA flag
            const taEl = document.getElementById('status-ta');
            taEl.classList.toggle('ta-on', !!station.ta);
            taEl.title = station.ta ? 'Traffic Announcement: ACTIVE' : 'Traffic Announcement: OFF';
        } else {
            document.getElementById('status-station').textContent = '---';
            document.getElementById('status-pi').textContent = '---';
            document.getElementById('status-pty').textContent = '---';
            document.getElementById('status-tp').classList.remove('tp-on');
            document.getElementById('status-ta').classList.remove('ta-on');
        }

        // Frequency
        document.getElementById('status-freq').textContent =
            data.frequency ? data.frequency.replace('M', ' MHz') : '---';

        // Decode rate
        document.getElementById('status-rate').textContent =
            data.groups_per_sec != null ? `${data.groups_per_sec} grp/s` : '--- grp/s';

        // Uptime
        const uptimeEl = document.getElementById('status-uptime');
        if (data.uptime_sec != null) {
            uptimeEl.textContent = formatUptime(data.uptime_sec);
            uptimeEl.title = `Uptime: ${data.uptime_sec}s — ${data.groups_total || 0} groups decoded`;
        } else {
            uptimeEl.textContent = '---';
        }

        // Pipeline status dot
        const pipeEl = document.getElementById('status-pipeline');
        const pipeState = data.pipeline ? data.pipeline.state : 'unknown';
        pipeEl.className = 'status-pipeline ' + pipeState;
        const pipeLabels = {
            running: 'Pipeline: running',
            starting: 'Pipeline: starting...',
            error: 'Pipeline error: ' + (data.pipeline?.error || 'unknown'),
            stopped: 'Pipeline: stopped',
            not_started: 'Pipeline: not started',
            unknown: 'Pipeline: unknown',
        };
        pipeEl.title = pipeLabels[pipeState] || 'Pipeline: ' + pipeState;

        // --- Row 2: RadioText + Now Playing ---

        if (station) {
            // RadioText
            const rtEl = document.getElementById('status-radiotext');
            rtEl.textContent = station.radiotext || '---';
            rtEl.title = station.radiotext || '';

            // Now playing (RT+ artist + title)
            const npContainer = document.getElementById('status-nowplaying');
            const npText = document.getElementById('status-np-text');
            if (station.now_artist || station.now_title) {
                const parts = [];
                if (station.now_artist) parts.push(station.now_artist);
                if (station.now_title) parts.push(station.now_title);
                npText.textContent = parts.join(' — ');
                npContainer.style.display = 'flex';
            } else {
                npContainer.style.display = 'none';
            }
        } else {
            document.getElementById('status-radiotext').textContent = '---';
            document.getElementById('status-nowplaying').style.display = 'none';
        }
    }

    function formatUptime(seconds) {
        if (seconds < 60) return `${seconds}s`;
        if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        if (h < 24) return `${h}h ${m}m`;
        const d = Math.floor(h / 24);
        return `${d}d ${h % 24}h`;
    }

    function setWsStatus(connected) {
        const dot = document.getElementById('ws-status');
        if (connected) {
            dot.classList.remove('disconnected');
            dot.classList.add('connected');
            dot.title = 'WebSocket connected';
        } else {
            dot.classList.remove('connected');
            dot.classList.add('disconnected');
            dot.title = 'WebSocket disconnected';
        }
    }

    return { init, setWsStatus };
})();

document.addEventListener('DOMContentLoaded', () => {
    App.init();
    Events.init();
    Console.init();
    Console.connect();
});
