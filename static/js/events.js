/**
 * RDS Guard — events.js
 * Fetch /api/events, render event cards, filter chips, polling.
 */

const Events = (() => {
    let currentFilter = '';
    let offset = 0;
    const LIMIT = 50;
    let total = 0;
    let pollTimer = null;

    function init() {
        setupFilters();
        loadEvents(true);
        loadActiveEvents();
        pollTimer = setInterval(() => {
            loadEvents(true);
            loadActiveEvents();
        }, 10000);

        document.getElementById('load-more').addEventListener('click', () => {
            offset += LIMIT;
            loadEvents(false);
        });
    }

    function setupFilters() {
        document.querySelectorAll('.filter-chip').forEach(chip => {
            chip.addEventListener('click', () => {
                document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
                chip.classList.add('active');
                currentFilter = chip.dataset.type;
                offset = 0;
                loadEvents(true);
            });
        });
    }

    async function loadEvents(replace) {
        try {
            let url = `/api/events?limit=${LIMIT}&offset=${replace ? 0 : offset}`;
            if (currentFilter) url += `&type=${currentFilter}`;

            const resp = await fetch(url);
            if (!resp.ok) return;
            const data = await resp.json();

            total = data.total;
            const list = document.getElementById('events-list');
            const empty = document.getElementById('events-empty');
            const loadMore = document.getElementById('load-more');

            if (replace) {
                list.innerHTML = '';
                offset = 0;
            }

            if (data.events.length === 0 && replace) {
                empty.style.display = 'block';
                loadMore.style.display = 'none';
            } else {
                empty.style.display = 'none';
                data.events.forEach(ev => {
                    list.appendChild(renderEventCard(ev, false));
                });
                const shown = list.children.length;
                loadMore.style.display = shown < total ? 'block' : 'none';
            }
        } catch (e) {
            // Server not reachable
        }
    }

    async function loadActiveEvents() {
        try {
            const resp = await fetch('/api/events/active');
            if (!resp.ok) return;
            const data = await resp.json();

            const container = document.getElementById('active-events');
            container.innerHTML = '';
            data.events.forEach(ev => {
                container.appendChild(renderEventCard(ev, true));
            });
        } catch (e) {
            // Server not reachable
        }
    }

    function renderEventCard(ev, isActive) {
        const card = document.createElement('div');
        card.className = `event-card type-${ev.type}`;
        if (isActive) card.classList.add('active');

        const typeLabels = {
            traffic: 'Traffic Announcement',
            emergency: 'Emergency Broadcast',
            eon_traffic: 'EON Traffic',
            tmc: 'TMC Message',
        };

        const timeStr = formatTime(ev.started_at || ev.created_at);

        let html = `
            <div class="event-header">
                <div class="event-type">
                    <span class="event-indicator"></span>
                    ${typeLabels[ev.type] || ev.type}
                </div>
                <span class="event-time">${timeStr}</span>
            </div>
        `;

        // Station line
        const parts = [];
        if (ev.station_ps) parts.push(ev.station_ps);
        if (ev.frequency) parts.push(ev.frequency.replace('M', ' MHz'));
        if (parts.length) {
            html += `<div class="event-station">${escapeHtml(parts.join(' \u00b7 '))}</div>`;
        }

        // RadioText
        let rtList = ev.radiotext;
        if (typeof rtList === 'string') {
            try { rtList = JSON.parse(rtList); } catch (e) { rtList = [rtList]; }
        }
        if (rtList && rtList.length > 0) {
            html += '<div class="event-radiotext">';
            rtList.forEach(rt => {
                html += `<p>${escapeHtml(rt)}</p>`;
            });
            html += '</div>';
        }

        // TMC data
        if (ev.type === 'tmc') {
            let data = ev.data;
            if (typeof data === 'string') {
                try { data = JSON.parse(data); } catch (e) { data = null; }
            }
            if (data && data.tmc) {
                html += `<div class="event-radiotext"><p>TMC: ${escapeHtml(JSON.stringify(data.tmc))}</p></div>`;
            }
        }

        // EON linked station
        if (ev.type === 'eon_traffic') {
            let data = ev.data;
            if (typeof data === 'string') {
                try { data = JSON.parse(data); } catch (e) { data = null; }
            }
            if (data && data.linked_station) {
                const ls = data.linked_station;
                const lsParts = [];
                if (ls.ps) lsParts.push(ls.ps);
                if (ls.pi) lsParts.push(ls.pi);
                if (lsParts.length) {
                    html += `<div class="event-station">Linked: ${escapeHtml(lsParts.join(' \u00b7 '))}</div>`;
                }
            }
        }

        // Transcription
        if (ev.transcription) {
            html += '<div class="event-transcription">';
            html += '<div class="event-section-label">Transcription</div>';
            html += `<p>${escapeHtml(ev.transcription)}</p>`;
            html += '</div>';
        } else if (ev.transcription_status === 'recording') {
            html += '<div class="event-transcription-status">';
            html += '<span class="status-indicator recording"></span> Recording...';
            html += '</div>';
        } else if (ev.transcription_status === 'saving' || ev.transcription_status === 'transcribing') {
            html += '<div class="event-transcription-status">';
            html += '<span class="status-indicator transcribing"></span> Transcribing...';
            html += '</div>';
        } else if (ev.transcription_status === 'error') {
            html += '<div class="event-transcription-status error">';
            html += 'Transcription failed';
            html += '</div>';
        }

        // Audio player
        if (ev.audio_url) {
            html += '<div class="event-audio">';
            html += `<audio controls preload="none" src="${escapeHtml(ev.audio_url)}"></audio>`;
            html += '</div>';
        }

        // Footer
        html += '<div class="event-footer">';
        if (ev.duration_sec != null) {
            html += `<span class="event-duration">Duration: ${formatDuration(ev.duration_sec)}</span>`;
        } else {
            html += '<span class="event-duration"></span>';
        }
        if (isActive) {
            html += '<span class="event-badge badge-active">In progress</span>';
        } else if (ev.state === 'end' || ev.state === 'transcribed') {
            html += '<span class="event-badge badge-ended">Ended</span>';
        }
        html += '</div>';

        card.innerHTML = html;
        return card;
    }

    function formatTime(iso) {
        if (!iso) return '';
        try {
            const d = new Date(iso + 'Z');
            return d.toLocaleTimeString('sv-SE', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        } catch (e) {
            return iso.substring(11, 19);
        }
    }

    function formatDuration(sec) {
        if (sec < 60) return `${sec}s`;
        const m = Math.floor(sec / 60);
        const s = sec % 60;
        if (m < 60) return `${m}m ${s}s`;
        const h = Math.floor(m / 60);
        return `${h}h ${m % 60}m`;
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    return { init };
})();
