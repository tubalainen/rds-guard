/**
 * RDS Guard Aggregator — events.js
 * Event cards from all nodes, filters, audio playback.
 */

const Events = (() => {
    const LIMIT = 50;
    let filter = { type: '', pi: '', node: '' };
    let offset = 0;
    let total = 0;

    function init() {
        document.querySelectorAll('.filter-chip').forEach(chip => {
            chip.addEventListener('click', () => {
                document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
                chip.classList.add('active');
                filter.type = chip.dataset.type;
                refresh();
            });
        });
        document.getElementById('filter-station').addEventListener('change', e => {
            filter.pi = e.target.value;
            refresh();
        });
        document.getElementById('filter-node').addEventListener('change', e => {
            filter.node = e.target.value;
            refresh();
        });
        document.getElementById('load-more').addEventListener('click', () => {
            offset += LIMIT;
            loadEvents(false);
        });
        refresh();
        loadFilterOptions();
        setInterval(() => { loadEvents(true); loadActive(); }, 10000);
        setInterval(loadFilterOptions, 60000);
    }

    function refresh() {
        offset = 0;
        loadEvents(true);
        loadActive();
    }

    function query(extra) {
        const p = new URLSearchParams();
        if (filter.type) p.set('type', filter.type);
        if (filter.pi) p.set('pi', filter.pi);
        if (filter.node) p.set('node', filter.node);
        Object.entries(extra || {}).forEach(([k, v]) => p.set(k, v));
        return p.toString();
    }

    function fillSelect(id, items, current, label) {
        const sel = document.getElementById(id);
        sel.replaceChildren();
        const all = document.createElement('option');
        all.value = '';
        all.textContent = label;
        sel.appendChild(all);
        items.forEach(([value, text]) => {
            const o = document.createElement('option');
            o.value = value;
            o.textContent = text;
            sel.appendChild(o);
        });
        sel.value = current;
    }

    async function loadFilterOptions() {
        try {
            const [st, nd] = await Promise.all([
                Util.api('GET', '/api/v1/stations'),
                Util.api('GET', '/api/v1/nodes'),
            ]);
            fillSelect('filter-station',
                st.stations.map(s => [s.pi, `${s.station_ps || s.pi}${s.frequency ? ' · ' + s.frequency : ''}`]),
                filter.pi, 'All stations');
            fillSelect('filter-node', nd.nodes.map(n => [n.name, n.name]),
                filter.node, 'All nodes');
        } catch (e) { /* ignore */ }
    }

    /** Keep a card whose audio is playing, so a refresh does not stop it. */
    function detachPlaying(container) {
        const audio = Array.from(container.querySelectorAll('audio'))
            .find(a => !a.paused && !a.ended);
        const card = audio && audio.closest('.event-card');
        if (!card) return null;
        card.remove();
        return card;
    }

    function render(container, events, isActive, append) {
        const saved = append ? null : detachPlaying(container);
        if (!append) container.replaceChildren();
        events.forEach(ev => {
            if (saved && saved.dataset.uid === ev.event_uid) {
                container.appendChild(saved);
            } else {
                container.appendChild(renderCard(ev, isActive));
            }
        });
        if (saved && !saved.isConnected) container.appendChild(saved);
    }

    async function loadEvents(replace) {
        try {
            // In-progress events are shown in their own section above
            const data = await Util.api('GET', `/api/v1/events?${query({
                active: 0, limit: LIMIT, offset: replace ? 0 : offset })}`);
            total = data.total;
            const list = document.getElementById('events-list');
            if (replace) offset = 0;
            render(list, data.events, false, !replace);
            document.getElementById('events-empty').hidden = list.children.length > 0;
            document.getElementById('load-more').hidden = list.children.length >= total;
        } catch (e) { /* ignore */ }
    }

    async function loadActive() {
        try {
            const data = await Util.api('GET', '/api/v1/events/active');
            const events = data.events.filter(ev =>
                (!filter.type || ev.type === filter.type)
                && (!filter.pi || ev.pi === filter.pi)
                && (!filter.node || ev.node_name === filter.node));
            render(document.getElementById('active-events'), events, true, false);
        } catch (e) { /* ignore */ }
    }

    function renderCard(ev, isActive) {
        const esc = Util.escapeHtml;
        const card = document.createElement('div');
        const type = ev.type === 'emergency' ? 'emergency' : 'traffic';
        card.className = `event-card type-${type}` + (isActive ? ' active' : '');
        card.dataset.uid = ev.event_uid;

        const labels = { traffic: 'Traffic Announcement', emergency: 'Emergency Broadcast' };
        let html = `
            <div class="event-header">
                <div class="event-type">
                    <span class="event-indicator"></span>
                    ${esc(labels[type])}
                    <span class="event-node" title="Reported by">${esc(ev.node_name)}</span>
                </div>
                <span class="event-time">${esc(Util.formatTime(ev.started_at || ev.received_at))}</span>
            </div>`;

        const parts = [];
        if (ev.station_ps) parts.push(ev.station_ps);
        if (ev.frequency) parts.push(ev.frequency.replace('M', ' MHz'));
        if (ev.pi) parts.push(ev.pi);
        if (parts.length) html += `<div class="event-station">${esc(parts.join(' · '))}</div>`;

        if (ev.radiotext && ev.radiotext.length) {
            html += '<div class="event-radiotext">'
                + ev.radiotext.map(rt => `<p>${esc(rt)}</p>`).join('') + '</div>';
        }

        if (ev.transcription) {
            html += '<div class="event-transcription"><div class="event-section-label">Whisper Transcription';
            if (ev.transcription_duration_sec != null) {
                html += `<span class="transcription-duration"> (processed in ${esc(Util.formatDuration(ev.transcription_duration_sec))})</span>`;
            }
            html += `</div><p>${esc(ev.transcription)}</p></div>`;
        } else if (ev.transcription_status === 'queued' || ev.transcription_status === 'transcribing') {
            html += '<div class="event-transcription-status"><span class="status-indicator transcribing"></span> Transcribing...</div>';
        } else if (ev.transcription_status === 'error') {
            html += '<div class="event-transcription-status error">Transcription failed</div>';
        } else if (isActive) {
            html += '<div class="event-transcription-status"><span class="status-indicator recording"></span> On air at the satellite...</div>';
        }

        if (ev.audio_url) {
            html += `<div class="event-audio"><audio controls preload="none" src="${esc(ev.audio_url)}"></audio></div>`;
        }

        html += '<div class="event-footer">';
        html += ev.duration_sec != null
            ? `<span class="event-duration">Broadcast Duration: ${esc(Util.formatDuration(ev.duration_sec))}</span>`
            : '<span class="event-duration"></span>';
        html += isActive
            ? '<span class="event-badge badge-active">In progress</span>'
            : '<span class="event-badge badge-ended">Ended</span>';
        html += '</div>';

        card.innerHTML = html;
        return card;
    }

    return { init };
})();
