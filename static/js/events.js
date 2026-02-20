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

    /**
     * Detach (not destroy) any event-card that contains a playing <audio>
     * element from the given container.  Returns { card, eventId } or null.
     */
    function detachPlayingCard(container) {
        const audio = Array.from(container.querySelectorAll('audio'))
                           .find(a => !a.paused && !a.ended);
        if (audio) {
            const card = audio.closest('.event-card');
            if (card) {
                const eventId = card.dataset.eventId;
                card.remove();          // detach from DOM, keeps audio alive
                return { card, eventId };
            }
        }
        return null;
    }

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
                // Preserve card with playing audio so playback is not interrupted
                const saved = detachPlayingCard(list);
                list.innerHTML = '';
                offset = 0;

                if (data.events.length === 0) {
                    empty.style.display = 'block';
                    loadMore.style.display = 'none';
                } else {
                    empty.style.display = 'none';
                    data.events.forEach(ev => {
                        if (saved && String(ev.id) === saved.eventId) {
                            list.appendChild(saved.card);
                        } else {
                            list.appendChild(renderEventCard(ev, false));
                        }
                    });
                    // If the playing card's event is on an older page (outside
                    // the current LIMIT/offset window), re-append it so the
                    // audio element is not garbage-collected and playback stops.
                    if (saved && !saved.card.isConnected) {
                        list.appendChild(saved.card);
                    }
                    const shown = list.children.length;
                    loadMore.style.display = shown < total ? 'block' : 'none';
                }
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
            // Preserve card with playing audio so playback is not interrupted
            const saved = detachPlayingCard(container);
            container.innerHTML = '';
            data.events.forEach(ev => {
                if (saved && String(ev.id) === saved.eventId) {
                    container.appendChild(saved.card);
                } else {
                    container.appendChild(renderEventCard(ev, true));
                }
            });
            // If the event transitioned out of active state during playback,
            // re-append the card so the audio element is not garbage-collected.
            if (saved && !saved.card.isConnected) {
                container.appendChild(saved.card);
            }
        } catch (e) {
            // Server not reachable
        }
    }

    function renderEventCard(ev, isActive) {
        const card = document.createElement('div');
        card.className = `event-card type-${ev.type}`;
        card.dataset.eventId = String(ev.id);
        if (isActive) card.classList.add('active');

        const typeLabels = {
            traffic: 'Traffic Announcement',
            emergency: 'Emergency Broadcast',
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

        // Transcription
        if (ev.transcription) {
            html += '<div class="event-transcription">';
            html += '<div class="event-section-label">Whisper Transcription';
            if (ev.transcription_duration_sec != null) {
                html += `<span class="transcription-duration"> (processed in ${formatDuration(ev.transcription_duration_sec)})</span>`;
            }
            html += '</div>';
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
            html += `<audio controls preload="none" src="${escapeHtml(ev.audio_url)}?v=3"></audio>`;
            html += '</div>';
        }

        // Footer
        html += '<div class="event-footer">';
        if (ev.duration_sec != null) {
            html += `<span class="event-duration">Broadcast Duration: ${formatDuration(ev.duration_sec)}</span>`;
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
            const now = new Date();
            const isToday = d.getFullYear() === now.getFullYear()
                         && d.getMonth()    === now.getMonth()
                         && d.getDate()     === now.getDate();
            const timeStr = d.toLocaleTimeString('sv-SE', {
                hour: '2-digit', minute: '2-digit', second: '2-digit'
            });
            if (isToday) return timeStr;
            const dateStr = d.toLocaleDateString('sv-SE', {
                day: 'numeric', month: 'short'
            });
            return `${dateStr} ${timeStr}`;
        } catch (e) {
            return iso.substring(11, 19);
        }
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

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    return { init };
})();
