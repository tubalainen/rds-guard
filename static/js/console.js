/**
 * RDS Guard — console.js
 * WebSocket /ws/console, log rendering, pause, filter.
 */

const Console = (() => {
    let ws = null;
    let paused = false;
    let filterText = '';
    let groupFilter = '';
    let messages = [];
    const MAX_MESSAGES = 500;
    let reconnectTimer = null;

    /** Human-readable labels for RDS group types. */
    const GROUP_LABELS = {
        '0a': 'PS / Flags',
        '0b': 'PS / Flags',
        '1a': 'PIN / Slow',
        '1b': 'PIN',
        '2a': 'RadioText',
        '2b': 'RadioText',
        '3a': 'ODA',
        '4a': 'Clock',
        '10a': 'PTYN',
        '11a': 'RT+',
        '14a': 'EON',
        'alert': 'Alert',
        'transcription': 'Transcription',
    };

    function init() {
        document.getElementById('console-pause').addEventListener('click', togglePause);
        const clearBtn = document.getElementById('console-clear');
        if (clearBtn) clearBtn.addEventListener('click', clearLog);
        document.getElementById('console-filter').addEventListener('input', (e) => {
            filterText = e.target.value.toLowerCase();
            renderAll();
        });
        setupGroupFilters();
    }

    function setupGroupFilters() {
        document.querySelectorAll('.console-group-chip').forEach(chip => {
            chip.addEventListener('click', () => {
                document.querySelectorAll('.console-group-chip').forEach(c => c.classList.remove('active'));
                chip.classList.add('active');
                groupFilter = chip.dataset.group;
                renderAll();
            });
        });
    }

    function connect() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
            return;
        }
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${proto}//${location.host}/ws/console`;

        ws = new WebSocket(url);

        ws.onopen = () => {
            App.setWsStatus(true);
            clearReconnectTimer();
        };

        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                messages.push(msg);
                if (messages.length > MAX_MESSAGES) {
                    messages.shift();
                }
                updateCount();
                if (!paused) {
                    appendLine(msg);
                }
            } catch (e) {
                // Ignore malformed messages
            }
        };

        ws.onclose = () => {
            App.setWsStatus(false);
            scheduleReconnect();
        };

        ws.onerror = () => {
            App.setWsStatus(false);
        };
    }

    function disconnect() {
        clearReconnectTimer();
        if (ws) {
            ws.close();
            ws = null;
        }
        App.setWsStatus(false);
    }

    function scheduleReconnect() {
        clearReconnectTimer();
        reconnectTimer = setTimeout(() => connect(), 3000);
    }

    function clearReconnectTimer() {
        if (reconnectTimer) {
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
        }
    }

    function togglePause() {
        paused = !paused;
        const btn = document.getElementById('console-pause');
        if (paused) {
            btn.textContent = 'Resume';
            btn.classList.add('paused');
        } else {
            btn.textContent = 'Pause';
            btn.classList.remove('paused');
            renderAll();
        }
    }

    function clearLog() {
        messages = [];
        document.getElementById('console-log').innerHTML = '';
        updateCount();
    }

    function appendLine(msg) {
        if (!matchesFilter(msg)) return;

        const log = document.getElementById('console-log');
        const wasAtBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 50;

        const line = createLine(msg);
        log.appendChild(line);

        // Trim DOM if too many lines
        while (log.children.length > MAX_MESSAGES) {
            log.removeChild(log.firstChild);
        }

        if (wasAtBottom) {
            log.scrollTop = log.scrollHeight;
        }
    }

    function renderAll() {
        const log = document.getElementById('console-log');
        log.innerHTML = '';
        const filtered = messages.filter(matchesFilter);
        filtered.forEach(msg => {
            log.appendChild(createLine(msg));
        });
        log.scrollTop = log.scrollHeight;
    }

    function createLine(msg) {
        const div = document.createElement('div');
        div.className = 'console-line';

        const topic = msg.topic || '';
        const isAlert = topic === 'alert' || topic.includes('alert');
        if (isAlert) div.classList.add('is-alert');

        const ts = formatTs(msg.timestamp);
        const group = extractGroup(topic);
        const groupLabel = GROUP_LABELS[group] || group.toUpperCase();
        const detail = formatPayload(group, msg.payload, isAlert);

        div.innerHTML = `
            <span class="console-ts">${escapeHtml(ts)}</span>
            <span class="console-group">${escapeHtml(groupLabel)}</span>
            <span class="console-detail">${detail}</span>
        `;
        return div;
    }

    /** Extract the RDS group from a topic like "0xE824/2a" → "2a". */
    function extractGroup(topic) {
        if (!topic) return 'unknown';
        if (topic === 'alert' || topic.startsWith('alert')) return 'alert';
        if (topic === 'transcription') return 'transcription';
        const parts = topic.split('/');
        return parts.length > 1 ? parts[parts.length - 1].toLowerCase() : topic.toLowerCase();
    }

    /** Format payload into human-readable detail based on group type. */
    function formatPayload(group, payload, isAlert) {
        if (!payload) return '';
        const p = typeof payload === 'object' ? payload : {};

        if (isAlert) {
            const parts = [];
            if (p.type) parts.push(`<span class="cd-key">${escapeHtml(p.type)}</span>`);
            if (p.state) parts.push(escapeHtml(p.state));
            if (p.station && p.station.ps) parts.push(escapeHtml(p.station.ps));
            if (p.transcription) parts.push('"' + escapeHtml(truncate(p.transcription, 80)) + '"');
            return parts.join(' <span class="cd-sep">&middot;</span> ') || raw(payload);
        }

        switch (group) {
            case '0a':
            case '0b': {
                const parts = [];
                if (p.ps) parts.push(`<span class="cd-key">PS</span> ${escapeHtml(p.ps)}`);
                else if (p.partial_ps) parts.push(`<span class="cd-key">PS</span> ${escapeHtml(p.partial_ps)}`);
                if (p.ta != null) parts.push(flag('TA', p.ta));
                if (p.tp != null) parts.push(flag('TP', p.tp));
                if (p.prog_type) parts.push(`<span class="cd-key">PTY</span> ${escapeHtml(p.prog_type)}`);
                if (p.is_music != null) parts.push(p.is_music ? 'Music' : 'Speech');
                return parts.join(' <span class="cd-sep">&middot;</span> ') || raw(payload);
            }
            case '2a':
            case '2b': {
                const rt = p.radiotext || p.partial_radiotext;
                if (rt) return `<span class="cd-key">RT</span> ${escapeHtml(rt)}`;
                return raw(payload);
            }
            case '4a': {
                if (p.clock_time) return `<span class="cd-key">Time</span> ${escapeHtml(p.clock_time)}`;
                return raw(payload);
            }
            case '14a': {
                const on = p.other_network || {};
                const parts = [];
                if (on.ps) parts.push(escapeHtml(on.ps));
                if (on.pi) parts.push(escapeHtml(on.pi));
                if (on.ta != null) parts.push(flag('TA', on.ta));
                if (parts.length) return `<span class="cd-key">EON</span> ${parts.join(' <span class="cd-sep">&middot;</span> ')}`;
                return raw(payload);
            }
            case '1a':
            case '1b': {
                const parts = [];
                if (p.pin) parts.push(`<span class="cd-key">PIN</span> ${escapeHtml(String(p.pin))}`);
                if (p.ecc) parts.push(`ECC: ${escapeHtml(String(p.ecc))}`);
                return parts.join(' <span class="cd-sep">&middot;</span> ') || raw(payload);
            }
            case '10a': {
                if (p.ptyn) return `<span class="cd-key">PTYN</span> ${escapeHtml(p.ptyn)}`;
                return raw(payload);
            }
            case 'transcription': {
                const parts = [];
                if (p.event_id) parts.push(`Event #${p.event_id}`);
                if (p.transcription) parts.push('"' + escapeHtml(truncate(p.transcription, 120)) + '"');
                return parts.join(' <span class="cd-sep">&middot;</span> ') || raw(payload);
            }
            default:
                return raw(payload);
        }
    }

    function flag(name, on) {
        return `<span class="cd-flag ${on ? 'cd-on' : ''}">${name}:${on ? 'ON' : 'off'}</span>`;
    }

    function raw(payload) {
        const str = typeof payload === 'string' ? payload : JSON.stringify(payload);
        return `<span class="cd-raw">${escapeHtml(truncate(str, 200))}</span>`;
    }

    function matchesFilter(msg) {
        const topic = msg.topic || '';
        const group = extractGroup(topic);

        // Group chip filter
        if (groupFilter && group !== groupFilter) return false;

        // Text filter
        if (!filterText) return true;
        const topicLower = topic.toLowerCase();
        const payloadStr = JSON.stringify(msg.payload || '').toLowerCase();
        return topicLower.includes(filterText) || payloadStr.includes(filterText);
    }

    function formatTs(iso) {
        if (!iso) return '';
        try {
            if (iso.length <= 8) return iso;
            return iso.substring(11, 19);
        } catch (e) {
            return '';
        }
    }

    function truncate(str, max) {
        return str.length > max ? str.substring(0, max) + '...' : str;
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    function updateCount() {
        document.getElementById('console-count').textContent =
            `${messages.length} message${messages.length !== 1 ? 's' : ''}`;
    }

    return { init, connect, disconnect };
})();
