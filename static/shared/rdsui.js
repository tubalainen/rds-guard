/**
 * RDS Guard — shared web UI for the satellite and the aggregator.
 *
 * Views (hash routes):
 *   #/arkiv[/YYYY-MM-DD]   calendar over the stored days + one day's events
 *   #/sok?q=…              search in RadioText and transcriptions
 *   #/event/<id>           one event (push messages link here)
 *   #/admin[/aviseringar[/<id>|/ny]]  password-protected admin (ADMIN_PASSWORD);
 *                          first section: push rules sent over MQTT
 * plus views the host page registers (debug) and admin sections (noder).
 *
 * The host page calls RDSUI.start({...}) with what differs between the two
 * apps: API prefix, how an event is identified, stations and nodes, plus
 * optional extras:
 *   views: {name: async (root, route, ctx) => {}}, extraNav: [[name, label, svg]]
 *   adminSections: [[name, label, view]]  extra tabs under #/admin
 *   cardMeta: (ev) => html       extra label in every event card's meta row
 *   eventExtras: async (ev, el, ctx) => {}   fills an empty element on #/event
 */
'use strict';

const RDSUI = (() => {
    const cfg = {};
    const views = {};
    const cache = { calendar: null, calendarAt: 0 };
    let routeSeq = 0;
    let current = null;
    let lastRouteName = null;

    // ------------------------------------------------------------------ icons

    const ICON = {
        play: '<svg width="16" height="16" viewBox="0 0 24 24" fill="#fff" aria-hidden="true"><path d="M7 4.5v15l13-7.5z"/></svg>',
        pause: '<svg width="16" height="16" viewBox="0 0 24 24" fill="#fff" aria-hidden="true"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>',
        download: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#344054" stroke-width="2" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>',
        bell: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.9 1.9 0 0 0 3.4 0"/></svg>',
        search: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#475467" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
        left: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg>',
        right: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>',
        calendar: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>',
        searchNav: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
        bellNav: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.9 1.9 0 0 0 3.4 0"/></svg>',
        lockNav: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
        nodes: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M8 7.5 10.5 16M16 7.5 13.5 16M8.5 6h7"/></svg>',
        logo: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="2"/><path d="M8.5 8.5a5 5 0 0 0 0 7M15.5 8.5a5 5 0 0 1 0 7M5.6 5.6a9 9 0 0 0 0 12.8M18.4 5.6a9 9 0 0 1 0 12.8"/></svg>',
    };

    // ------------------------------------------------------------------ utils

    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const pad = (n) => String(n).padStart(2, '0');

    /** Timestamps from the API are UTC, with or without a trailing Z. */
    function parseTs(ts) {
        if (!ts) return null;
        let s = String(ts);
        if (!/[zZ]$|[+-]\d\d:?\d\d$/.test(s)) s += 'Z';
        const d = new Date(s);
        return isNaN(d) ? null : d;
    }
    const dayKey = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    function keyToDate(k) {
        const [y, m, d] = k.split('-').map(Number);
        return new Date(y, m - 1, d);
    }
    const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
    const utcBound = (d) => d.toISOString().slice(0, 19);
    const fmtTime = (d) => (d ? d.toLocaleTimeString('sv-SE', { hour: '2-digit', minute: '2-digit' }) : '');
    const cap = (t) => (t ? t.charAt(0).toUpperCase() + t.slice(1) : t);
    const fmtDay = (d) => cap(d.toLocaleDateString('sv-SE', { weekday: 'long', day: 'numeric', month: 'long' }));
    const fmtDate = (d) => d.toLocaleDateString('sv-SE', { day: 'numeric', month: 'short' });

    function fmtRelDay(d) {
        if (!d) return '';
        const k = dayKey(d);
        const today = new Date();
        if (k === dayKey(today)) return 'idag';
        if (k === dayKey(addDays(today, -1))) return 'igår';
        return fmtDate(d);
    }

    function fmtClock(sec) {
        if (sec == null || isNaN(sec)) return '–:––';
        const s = Math.max(0, Math.round(sec));
        return `${Math.floor(s / 60)}:${pad(s % 60)}`;
    }

    function fmtDur(sec) {
        if (sec == null) return '';
        const s = Math.round(sec);
        if (s < 60) return `${s} s`;
        const m = Math.floor(s / 60);
        if (m < 60) return s % 60 ? `${m} min ${s % 60} s` : `${m} min`;
        return `${Math.floor(m / 60)} h ${m % 60} min`;
    }

    function fmtFreq(f) {
        if (!f) return '';
        const n = parseFloat(String(f));
        return isNaN(n) ? String(f) : `${String(n).replace('.', ',')} MHz`;
    }

    function radiotext(ev) {
        let rt = ev.radiotext;
        if (typeof rt === 'string') {
            try { rt = JSON.parse(rt); } catch (e) { rt = [rt]; }
        }
        return (Array.isArray(rt) ? rt : []).filter((r) => r && String(r).trim());
    }

    function headline(ev) {
        const rt = radiotext(ev);
        if (rt.length) return rt[rt.length - 1];
        const st = ev.station_ps || ev.pi || 'okänd station';
        return ev.type === 'emergency'
            ? `Viktigt meddelande till allmänheten på ${st}`
            : `Trafikmeddelande på ${st}`;
    }

    const evStart = (ev) => parseTs(ev.started_at || ev.created_at || ev.received_at);
    const isLive = (ev) => ['start', 'update', 'active'].includes(ev.state);
    const stationLabel = (ev) => [ev.station_ps || ev.pi, fmtFreq(ev.frequency)].filter(Boolean).join(' · ');

    /** Escape text and wrap the search words in <mark>. */
    function hl(text, words) {
        text = String(text ?? '');
        const ws = (words || []).filter((w) => w && w.length > 0);
        if (!ws.length) return esc(text);
        const re = new RegExp(`(${ws.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'giu');
        return text.split(re).map((part, i) => (i % 2 ? `<mark>${esc(part)}</mark>` : esc(part))).join('');
    }

    function slugify(text) {
        let t = String(text || '').toLowerCase();
        t = t.replace(/[åä]/g, 'a').replace(/ö/g, 'o').replace(/é/g, 'e').replace(/ü/g, 'u');
        t = t.replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40).replace(/-+$/, '');
        return t || 'regel';
    }

    let toastTimer = null;
    function toast(msg) {
        let el = document.getElementById('rg-toast');
        if (!el) {
            el = document.createElement('div');
            el.id = 'rg-toast';
            el.className = 'toast';
            el.setAttribute('role', 'status');
            document.body.appendChild(el);
        }
        el.textContent = msg;
        el.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
    }

    // ------------------------------------------------------------------ API

    async function api(method, path, body) {
        const opt = { method, headers: {} };
        if (body !== undefined) {
            opt.headers['Content-Type'] = 'application/json';
            opt.body = JSON.stringify(body);
        }
        const resp = await fetch(cfg.apiBase + path, opt);
        let data = null;
        try { data = await resp.json(); } catch (e) { /* not JSON */ }
        if (!resp.ok) {
            const err = new Error((data && (data.detail || data.error)) || `HTTP ${resp.status}`);
            err.status = resp.status;
            throw err;
        }
        return data;
    }

    async function loadCalendar(force) {
        if (!force && cache.calendar && Date.now() - cache.calendarAt < 20000) return cache.calendar;
        cache.calendar = await api('GET', '/calendar');
        cache.calendarAt = Date.now();
        return cache.calendar;
    }

    // ------------------------------------------------------------------ router

    function parseHash() {
        const h = location.hash.replace(/^#\/?/, '');
        const [path, qs] = h.split('?');
        const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
        return { name: parts[0] || 'arkiv', args: parts.slice(1), params: new URLSearchParams(qs || '') };
    }

    function go(hash) {
        if (location.hash === hash) route();
        else location.hash = hash;
    }

    function setNav(name) {
        document.querySelectorAll('.nav a').forEach((a) => {
            const on = a.dataset.view === name || (name === 'event' && a.dataset.view === 'arkiv');
            if (on) a.setAttribute('aria-current', 'page');
            else a.removeAttribute('aria-current');
        });
    }

    async function route() {
        const r = parseHash();
        if (!views[r.name] && adminSections.some(([n]) => n === r.name)) {
            // old address of a section that has moved into admin
            location.replace(`#/admin/${location.hash.replace(/^#\/?/, '')}`);
            return;
        }
        const view = views[r.name] || views.arkiv;
        const seq = ++routeSeq;
        if (current && current.cleanup) current.cleanup();
        Player.destroyAll();
        const ctx = { alive: () => seq === routeSeq, cleanup: null };
        current = ctx;
        setNav(r.name);
        const root = document.getElementById('view');
        if (r.name !== lastRouteName) window.scrollTo(0, 0);
        lastRouteName = r.name;
        try {
            await view(root, r, ctx);
        } catch (e) {
            if (ctx.alive()) {
                root.innerHTML = `<div class="empty">Kunde inte ladda sidan: ${esc(e.message)}</div>`;
            }
        }
    }

    // ------------------------------------------------------------------ player

    const Player = (() => {
        const speeds = [1, 1.25, 1.5, 2];
        let all = [];
        let active = null;

        function html(url, dur, big) {
            return `<div class="player${big ? ' big' : ''}" data-src="${esc(url)}" data-dur="${dur ?? ''}">
                <button class="play" type="button" aria-label="Spela upp">${ICON.play}</button>
                <div class="wave"><span class="wave-idle"></span></div>
                <span class="time">0:00 / ${fmtClock(dur)}</span>
                <button class="speed" type="button" aria-label="Uppspelningshastighet">1×</button>
                <a class="dl" href="${esc(url)}" download aria-label="Ladda ner ljudet">${ICON.download}</a>
            </div>`;
        }

        function setPlaying(p, on) {
            p.playing = on;
            const btn = p.el.querySelector('.play');
            if (btn) {
                btn.innerHTML = on ? ICON.pause : ICON.play;
                btn.setAttribute('aria-label', on ? 'Pausa' : 'Spela upp');
            }
            if (on) {
                if (active && active !== p) active.pause();
                active = p;
            }
        }

        function updateTime(p) {
            const t = p.el.querySelector('.time');
            if (!t || !p.ws) return;
            const dur = p.ws.getDuration() || Number(p.el.dataset.dur) || null;
            t.textContent = `${fmtClock(p.ws.getCurrentTime())} / ${fmtClock(dur)}`;
        }

        function fallback(p, playNow) {
            if (p.fellBack) return;
            p.fellBack = true;
            try { if (p.ws) p.ws.destroy(); } catch (e) { /* ignore */ }
            p.ws = null;
            const wave = p.el.querySelector('.wave');
            p.el.querySelectorAll('.play, .time, .speed').forEach((x) => { x.hidden = true; });
            wave.innerHTML = `<audio controls preload="metadata" src="${esc(p.el.dataset.src)}"></audio>`;
            const audio = wave.querySelector('audio');
            audio.addEventListener('play', () => {
                if (active && active !== p) active.pause();
                active = p;
            });
            audio.addEventListener('error', () => {
                wave.innerHTML = '<span class="small muted">Ljudet kan inte spelas här – ladda ner filen.</span>';
            });
            p.pause = () => audio.pause();
            if (playNow) audio.play().catch(() => {});
        }

        function init(el) {
            if (el._player) return el._player;
            const p = { el, ws: null, playing: false, rate: 1, fellBack: false, ready: false, wantPlay: false };
            p.pause = () => { if (p.ws) p.ws.pause(); };
            el._player = p;
            all.push(p);
            const wave = el.querySelector('.wave');
            if (!window.WaveSurfer) {
                fallback(p, true);
                return p;
            }
            wave.innerHTML = '<span class="wave-msg">Laddar…</span>';
            try {
                p.ws = window.WaveSurfer.create({
                    container: wave,
                    url: el.dataset.src,
                    height: wave.clientHeight || 36,
                    waveColor: '#B8C2D1',
                    progressColor: '#1D4ED8',
                    cursorWidth: 0,
                    barWidth: 3,
                    barGap: 2,
                    barRadius: 2,
                    normalize: true,
                    dragToSeek: true,
                });
            } catch (e) {
                fallback(p, true);
                return p;
            }
            p.ws.on('ready', () => {
                const msg = wave.querySelector('.wave-msg');
                if (msg) msg.remove();
                p.ready = true;
                updateTime(p);
                if (p.wantPlay) {
                    p.wantPlay = false;
                    start(p);
                }
            });
            p.ws.on('timeupdate', () => updateTime(p));
            p.ws.on('play', () => setPlaying(p, true));
            p.ws.on('pause', () => setPlaying(p, false));
            p.ws.on('finish', () => setPlaying(p, false));
            p.ws.on('error', () => fallback(p, true));
            return p;
        }

        function start(p) {
            const res = p.ws.play();
            if (res && res.catch) res.catch(() => fallback(p, true));
        }

        function toggle(p) {
            if (p.fellBack || !p.ws) return;
            if (p.playing) p.ws.pause();
            else if (!p.ready) p.wantPlay = !p.wantPlay;   // starts on 'ready'
            else start(p);
        }

        document.addEventListener('click', (e) => {
            const el = e.target.closest('.player');
            if (!el) return;
            if (e.target.closest('.play')) {
                toggle(init(el));
            } else if (e.target.closest('.speed')) {
                const p = init(el);
                p.rate = speeds[(speeds.indexOf(p.rate) + 1) % speeds.length];
                if (p.ws) p.ws.setPlaybackRate(p.rate, true);
                e.target.closest('.speed').textContent = `${String(p.rate).replace('.', ',')}×`;
            } else if (e.target.closest('.wave') && !el._player) {
                toggle(init(el));
            }
        });

        return {
            html,
            isPlaying: () => !!(active && (active.playing
                || (active.fellBack && active.el.querySelector('audio') && !active.el.querySelector('audio').paused))),
            destroyAll() {
                all.forEach((p) => { try { if (p.ws) p.ws.destroy(); } catch (e) { /* ignore */ } });
                all = [];
                active = null;
            },
        };
    })();

    // ------------------------------------------------------------------ event cards

    function typePill(ev) {
        return ev.type === 'emergency'
            ? '<span class="pill vma">VMA</span>'
            : '<span class="pill traffic">Trafik</span>';
    }

    function transcriptionStatus(ev) {
        const s = ev.transcription_status;
        if (isLive(ev) || s === 'recording') return '<span class="ev-status"><span class="rec"></span>Spelar in…</span>';
        if (s === 'saving' || s === 'transcribing' || s === 'queued') return '<span class="ev-status"><span class="spin"></span>Transkriberar…</span>';
        if (s === 'error') return '<span class="ev-status">Transkriberingen misslyckades</span>';
        return '';
    }

    function pushPill(ev) {
        const n = ev.notify || [];
        if (!n.length) return '';
        const sent = n.filter((x) => x.status === 'sent');
        if (sent.length) {
            const names = sent.map((x) => x.rule_name).filter(Boolean).join(', ');
            return `<span class="pill ok" title="Regel: ${esc(names)}">${ICON.bell} Push skickad ${esc(fmtTime(parseTs(sent[0].created_at)))}</span>`;
        }
        if (n.some((x) => x.status === 'failed')) return `<span class="pill bad">${ICON.bell} Push misslyckades</span>`;
        if (n.some((x) => x.status === 'mqtt_off')) return `<span class="pill" title="MQTT är inte påslaget">${ICON.bell} Ingen push – MQTT av</span>`;
        return `<span class="pill">${ICON.bell} Ingen push – ingen regel matchade</span>`;
    }

    /**
     * Transcription as HTML. When Claude corrected misheard words, the
     * corrected text is shown, with a "Rättad" tag that switches to the
     * original (see the click handler in start()).
     */
    function txHtml(ev, words) {
        const orig = ev.transcription || '';
        const fixed = ev.transcription_corrected || '';
        if (!fixed || fixed === orig) return { text: orig ? hl(orig, words) : '', toggle: '' };
        return {
            text: `<span class="tx-fixed">${hl(fixed, words)}</span><span class="tx-orig" hidden>${hl(orig, words)}</span>`,
            toggle: '<button type="button" class="pill info fix-toggle" aria-pressed="false" '
                + 'title="Felhörda ortnamn och ord är rättade. Tryck för att se originaltranskriptionen.">Rättad</button>',
        };
    }

    function evCard(ev, opt = {}) {
        const id = cfg.idOf(ev);
        const start = evStart(ev);
        const live = isLive(ev);
        const title = headline(ev);
        const words = opt.highlight || [];
        const tx = txHtml(ev, words);
        const text = tx.text || hl(radiotext(ev).filter((r) => r !== title).join(' – '), words);
        const meta = [typePill(ev), `<span>${esc(stationLabel(ev))}</span>`];
        if (opt.showDate) meta.push(`<span>· ${esc(fmtRelDay(start))}</span>`);
        if (ev.node_name) meta.push(`<span>· ${esc(ev.node_name)}</span>`);
        if (cfg.cardMeta) {
            // Host hook (e.g. a place label from the map): trusted HTML
            try { meta.push(cfg.cardMeta(ev) || ''); } catch (e) { /* ignore */ }
        }
        return `<article class="ev${ev.type === 'emergency' ? ' vma-ev' : ''}${live ? ' live' : ''}">
            <div class="ev-when">
                <span class="ev-time">${esc(fmtTime(start))}</span>
                <span class="ev-dur">${live ? 'pågår' : esc(fmtClock(ev.duration_sec))}</span>
            </div>
            <div class="ev-body">
                <div class="ev-meta">${meta.join('')}</div>
                <h3 class="ev-title"><a href="#/event/${encodeURIComponent(id)}">${hl(title, words)}</a></h3>
                ${text ? `<p class="ev-text">${text}</p>` : ''}
                ${ev.audio_url ? Player.html(ev.audio_url, ev.duration_sec) : ''}
                <div class="ev-foot">${tx.toggle}${transcriptionStatus(ev)}${pushPill(ev)}</div>
            </div>
        </article>`;
    }

    // ------------------------------------------------------------------ archive

    function groupCalendar(events) {
        const days = {};
        events.forEach((ev) => {
            const d = evStart(ev);
            if (!d) return;
            const k = dayKey(d);
            const day = days[k] || (days[k] = { n: 0, vma: 0, pushed: 0 });
            day.n += 1;
            if (ev.type === 'emergency') day.vma += 1;
            if (ev.pushed) day.pushed += 1;
        });
        return days;
    }

    function kpis(cal) {
        const ev = cal.events;
        const traffic = ev.filter((e) => e.type === 'traffic');
        const vma = ev.filter((e) => e.type === 'emergency');
        const pushed = ev.filter((e) => e.pushed).length;
        const hours = new Array(24).fill(0);
        traffic.forEach((e) => { const d = evStart(e); if (d) hours[d.getHours()] += 1; });
        let best = -1;
        let bestN = 0;
        for (let h = 0; h < 23; h++) {
            const n = hours[h] + hours[h + 1];
            if (n > bestN) { bestN = n; best = h; }
        }
        const lastVma = vma.length ? evStart(vma[0]) : null;
        const tiles = [
            ['Trafikmeddelanden', String(traffic.length), `senaste ${cal.retention_days} dagarna`],
            ['VMA', String(vma.length), lastVma ? `senast ${fmtDate(lastVma)} ${fmtTime(lastVma)}` : 'inga under perioden'],
            ['Vanligaste tid', traffic.length >= 3 && best >= 0 ? `${pad(best)}–${pad(best + 2)}` : '–',
                traffic.length >= 3 ? `${bestN} av ${traffic.length} meddelanden` : 'för få meddelanden än'],
            ['Push skickade', String(pushed), `av ${ev.length} händelser`],
        ];
        return tiles.map(([l, v, s]) => `<div class="kpi"><div class="kpi-label">${esc(l)}</div>
            <div class="kpi-value">${esc(v)}</div><div class="kpi-sub">${esc(s)}</div></div>`).join('');
    }

    function calendarHtml(month, sel, byDay, retention) {
        const [y, m] = month.split('-').map(Number);
        const first = new Date(y, m - 1, 1);
        const today = new Date();
        const oldest = addDays(today, -retention);
        const startOffset = (first.getDay() + 6) % 7; // Monday first
        const daysInMonth = new Date(y, m, 0).getDate();
        const prev = new Date(y, m - 2, 1);
        const next = new Date(y, m, 1);
        const prevOk = new Date(y, m - 1, 0) >= new Date(oldest.getFullYear(), oldest.getMonth(), oldest.getDate());
        const nextOk = next <= today;
        const monthKey = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
        let cells = '';
        for (let i = 0; i < startOffset; i++) cells += '<span class="cal-day blank" aria-hidden="true"></span>';
        for (let d = 1; d <= daysInMonth; d++) {
            const date = new Date(y, m - 1, d);
            const k = dayKey(date);
            const info = byDay[k];
            const future = date > today;
            const old = date < new Date(oldest.getFullYear(), oldest.getMonth(), oldest.getDate());
            const cls = ['cal-day'];
            let count = '';
            let aria = date.toLocaleDateString('sv-SE', { day: 'numeric', month: 'long' });
            if (future || old) {
                cls.push('out');
                aria += old ? ', inte sparad längre' : '';
                cells += `<span class="${cls.join(' ')}" aria-label="${esc(aria)}">${d}</span>`;
                continue;
            }
            const n = info ? info.n : 0;
            if (n >= 5) cls.push('l3'); else if (n >= 3) cls.push('l2'); else if (n >= 1) cls.push('l1');
            if (info && info.vma) cls.push('vma');
            if (k === dayKey(today)) cls.push('today');
            if (k === sel) cls.push('sel');
            count = n ? `${n} st` : '–';
            aria += `, ${n ? `${n} meddelanden` : 'inga meddelanden'}${info && info.vma ? ', varav VMA' : ''}`;
            cells += `<a class="${cls.join(' ')}" href="#/arkiv/${k}" aria-label="${esc(aria)}"${k === sel ? ' aria-current="date"' : ''}>${d}<small>${count}</small></a>`;
        }
        const title = cap(first.toLocaleDateString('sv-SE', { month: 'long', year: 'numeric' }));
        return `<div class="cal-head">
                <h2>${esc(title)}</h2>
                ${prevOk ? `<a class="icon-btn" href="#/arkiv/${sel}?m=${monthKey(prev)}" aria-label="Föregående månad">${ICON.left}</a>`
        : `<button class="icon-btn" disabled aria-label="Föregående månad">${ICON.left}</button>`}
                ${nextOk ? `<a class="icon-btn" href="#/arkiv/${sel}?m=${monthKey(next)}" aria-label="Nästa månad">${ICON.right}</a>`
        : `<button class="icon-btn" disabled aria-label="Nästa månad">${ICON.right}</button>`}
            </div>
            <div class="cal-grid">
                ${['mån', 'tis', 'ons', 'tor', 'fre', 'lör', 'sön'].map((w) => `<span class="cal-wd">${w}</span>`).join('')}
                ${cells}
            </div>
            <div class="cal-legend">Färre
                <span class="sw" style="background:#F1F5F9"></span><span class="sw" style="background:var(--l1)"></span>
                <span class="sw" style="background:var(--l2)"></span><span class="sw" style="background:var(--l3)"></span>Fler
                <span style="flex:1"></span>
                <span class="sw" style="box-shadow:inset 0 0 0 2px #DC2626"></span>VMA
            </div>
            <p class="small muted" style="margin:12px 0 0">Händelser sparas i ${retention} dagar.</p>`;
    }

    async function renderDay(el, key, ctx) {
        const start = keyToDate(key);
        const data = await api('GET', `/events?from=${utcBound(start)}&to=${utcBound(addDays(start, 1))}&limit=200`);
        if (!ctx.alive()) return;
        const ev = data.events;
        const traffic = ev.filter((e) => e.type === 'traffic').length;
        const vma = ev.length - traffic;
        const pushes = ev.filter((e) => (e.notify || []).some((x) => x.status === 'sent')).length;
        const secs = ev.reduce((a, e) => a + (e.duration_sec || 0), 0);
        const parts = [];
        if (traffic) parts.push(`${traffic} trafikmeddelande${traffic === 1 ? '' : 'n'}`);
        if (vma) parts.push(`${vma} VMA`);
        if (secs) parts.push(`${fmtDur(secs)} sändning`);
        if (pushes) parts.push(`${pushes} push`);
        const isToday = key === dayKey(new Date());
        el.innerHTML = `<div class="day-head"><h2>${esc(fmtDay(start))}</h2>
                <span class="muted">${esc(parts.join(' · '))}</span></div>
            ${ev.length ? `<div class="ev-list">${ev.map((e) => evCard(e)).join('')}</div>`
        : `<div class="empty">${isToday ? 'Inga meddelanden hittills idag.' : 'Inga meddelanden den här dagen.'}</div>`}`;
    }

    views.arkiv = async (root, r, ctx) => {
        const cal = await loadCalendar(r.params.has('ny'));
        if (!ctx.alive()) return;
        const today = new Date();
        const sel = /^\d{4}-\d\d-\d\d$/.test(r.args[0] || '') ? r.args[0] : dayKey(today);
        const month = /^\d{4}-\d\d$/.test(r.params.get('m') || '') ? r.params.get('m') : sel.slice(0, 7);
        root.innerHTML = `<section class="kpis" aria-label="Senaste ${cal.retention_days} dagarna">${kpis(cal)}</section>
            <div class="cols">
                <section class="card col-narrow" aria-label="Kalender">${calendarHtml(month, sel, groupCalendar(cal.events), cal.retention_days)}</section>
                <section class="col-main" aria-label="Vald dag" id="day"><div class="empty">Laddar…</div></section>
            </div>`;
        await renderDay(root.querySelector('#day'), sel, ctx);
        if (sel === dayKey(today)) {
            const timer = setInterval(async () => {
                if (!ctx.alive() || document.hidden || Player.isPlaying()) return;
                try {
                    const fresh = await loadCalendar(true);
                    if (!ctx.alive() || Player.isPlaying()) return;
                    root.querySelector('.kpis').innerHTML = kpis(fresh);
                    root.querySelector('.col-narrow').innerHTML = calendarHtml(month, sel, groupCalendar(fresh.events), fresh.retention_days);
                    Player.destroyAll();
                    await renderDay(root.querySelector('#day'), sel, ctx);
                } catch (e) { /* try again next time */ }
            }, 30000);
            ctx.cleanup = () => clearInterval(timer);
        }
    };

    // ------------------------------------------------------------------ search

    function hashWith(name, params) {
        const qs = new URLSearchParams();
        Object.entries(params).forEach(([k, v]) => { if (v) qs.set(k, v); });
        const s = qs.toString();
        return `#/${name}${s ? `?${s}` : ''}`;
    }

    views.sok = async (root, r, ctx) => {
        const p = r.params;
        const state = {
            q: p.get('q') || '', typ: p.get('typ') || '', dagar: p.get('dagar') || '',
            pi: p.get('pi') || '', nod: p.get('nod') || '',
        };
        const [stations, nodes] = await Promise.all([cfg.loadStations(), cfg.loadNodes ? cfg.loadNodes() : null]);
        if (!ctx.alive()) return;
        const seg = (key, opts) => `<div class="seg" role="group">${opts.map(([v, l]) =>
            `<button type="button" data-k="${key}" data-v="${v}" aria-pressed="${state[key] === v}">${l}</button>`).join('')}</div>`;
        root.innerHTML = `
            <form class="search-form" role="search" id="sform">
                <label class="q"><span class="sr-only">Sök</span>${ICON.search}
                    <input type="search" name="q" value="${esc(state.q)}" placeholder="Sök väg, plats eller ord, t.ex. E4 eller halka" autocomplete="off"></label>
                <button class="btn primary" type="submit" style="min-height:52px;padding:0 24px">Sök</button>
            </form>
            <div class="filters">
                ${seg('typ', [['', 'Alla'], ['traffic', 'Trafik'], ['emergency', 'VMA']])}
                ${seg('dagar', [['', 'Hela perioden'], ['7', '7 dagar'], ['30', '30 dagar']])}
                <label><span class="sr-only">Station</span><select class="sel" data-k="pi">
                    <option value="">Alla stationer</option>
                    ${stations.map((s) => `<option value="${esc(s.pi)}"${s.pi === state.pi ? ' selected' : ''}>${esc(s.ps || s.pi)}</option>`).join('')}
                </select></label>
                ${nodes ? `<label><span class="sr-only">Nod</span><select class="sel" data-k="nod">
                    <option value="">Alla noder</option>
                    ${nodes.map((n) => `<option${n === state.nod ? ' selected' : ''}>${esc(n)}</option>`).join('')}
                </select></label>` : ''}
            </div>
            <div id="results"></div>`;
        const form = root.querySelector('#sform');
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            go(hashWith('sok', { ...state, q: form.q.value.trim() }));
        });
        root.querySelectorAll('.seg button').forEach((b) => b.addEventListener('click', () => {
            go(hashWith('sok', { ...state, q: form.q.value.trim(), [b.dataset.k]: b.dataset.v }));
        }));
        root.querySelectorAll('select[data-k]').forEach((s) => s.addEventListener('change', () => {
            go(hashWith('sok', { ...state, q: form.q.value.trim(), [s.dataset.k]: s.value }));
        }));

        const out = root.querySelector('#results');
        // With no filters at all, list every stored event (newest first).
        const hint = state.q ? '' : `<p class="small muted" style="margin:0 0 8px">Sök i radiotext och transkriptioner. Prova: ${
            ['E4', 'olycka', 'halka', 'kö'].map((w) => `<a href="${hashWith('sok', { q: w })}">${esc(w)}</a>`).join(' · ')}</p>`;
        if (!state.q) form.q.focus();
        const words = state.q.split(/\s+/).filter(Boolean);
        let offset = 0;
        let shown = 0;
        let lastDay = null;
        const params = () => {
            const qs = new URLSearchParams({ limit: '50', offset: String(offset) });
            if (state.q) qs.set('q', state.q);
            if (state.typ) qs.set('type', state.typ);
            if (state.pi) qs.set('pi', state.pi);
            if (state.nod) qs.set('node', state.nod);
            if (state.dagar) qs.set('from', utcBound(addDays(new Date(), -Number(state.dagar))));
            return qs.toString();
        };
        out.innerHTML = '<div class="empty">Söker…</div>';
        const first = await api('GET', `/events?${params()}`);
        if (!ctx.alive()) return;
        const watch = state.q ? `<div class="watch">${ICON.bell}<span><strong>Vill du få push nästa gång?</strong>
            Gör sökningen till en regel som skickas via MQTT.</span>
            <a class="btn primary" href="${hashWith('admin/aviseringar/ny', { ord: state.q })}">Bevaka ”${esc(state.q)}”</a></div>` : '';
        out.innerHTML = `${watch}${hint}
            <div class="day-head" style="margin-top:4px"><h2 style="font-size:22px;text-transform:none">${first.total} träff${first.total === 1 ? '' : 'ar'}</h2>
                <span class="muted">nyast först</span></div>
            <div class="ev-list" id="hits"></div>
            <button class="btn more" id="more" hidden>Visa fler</button>`;
        const hits = out.querySelector('#hits');
        const more = out.querySelector('#more');
        const add = (data) => {
            let html = '';
            data.events.forEach((ev) => {
                const d = evStart(ev);
                const k = d ? dayKey(d) : '';
                if (k !== lastDay) {
                    html += `<h3 class="label group-label">${esc(d ? fmtDay(d) : 'Okänt datum')}</h3>`;
                    lastDay = k;
                }
                html += evCard(ev, { highlight: words });
            });
            hits.insertAdjacentHTML('beforeend', html);
            shown += data.events.length;
            more.hidden = shown >= data.total;
        };
        add(first);
        if (!first.total) hits.innerHTML = '<div class="empty">Inga meddelanden matchar sökningen.</div>';
        more.addEventListener('click', async () => {
            offset += 50;
            more.disabled = true;
            try { add(await api('GET', `/events?${params()}`)); } finally { more.disabled = false; }
        });
    };

    // ------------------------------------------------------------------ one event

    views.event = async (root, r, ctx) => {
        const id = r.args[0];
        let ev;
        try {
            ev = await api('GET', `/events/${encodeURIComponent(id)}`);
        } catch (e) {
            if (e.status === 404) {
                root.innerHTML = '<div class="empty">Händelsen finns inte längre – den kan ha rensats bort efter lagringstiden.<br><a href="#/arkiv">Till arkivet</a></div>';
                return;
            }
            throw e;
        }
        if (!ctx.alive()) return;
        const start = evStart(ev);
        const end = parseTs(ev.ended_at);
        const rt = radiotext(ev);
        const live = isLive(ev);
        const notify = ev.notify || [];
        const tStatus = transcriptionStatus(ev);
        const engine = ev.transcription_duration_sec != null ? `klar på ${fmtDur(ev.transcription_duration_sec)}` : '';
        const tx = txHtml(ev, []);
        const kv = [
            ['Station', ev.station_ps || ev.long_ps || '–'],
            ['Frekvens', fmtFreq(ev.frequency) || '–'],
            ['PI', ev.pi || '–'],
            ['Programtyp', ev.prog_type || (ev.data && ev.data.prog_type) || '–'],
            ['Start', start ? `${fmtDate(start)} ${fmtTime(start)}` : '–'],
            ['Slut', end ? fmtTime(end) : (live ? 'pågår' : '–')],
            ['Längd', ev.duration_sec != null ? fmtDur(ev.duration_sec) : '–'],
        ];
        if (ev.node_name) kv.push(['Nod', ev.node_name]);
        const pushRows = notify.filter((x) => x.status !== 'no_match');
        const pushLabel = { sent: 'skickad', failed: 'misslyckades', mqtt_off: 'MQTT av' };
        root.innerHTML = `
            <a href="#/arkiv/${start ? dayKey(start) : ''}" class="small">← ${esc(start ? fmtDay(start) : 'Arkivet')}</a>
            <div class="detail-head">
                <div class="ev-meta">${typePill(ev)}<span class="pill">${esc(stationLabel(ev))}</span>
                    <span class="pill">${esc(start ? `${fmtRelDay(start)} ${fmtTime(start)}` : '')}${ev.duration_sec != null ? ` · ${esc(fmtClock(ev.duration_sec))}` : ''}</span>
                    ${live ? '<span class="pill warn">Pågår</span>' : ''}</div>
                <h1>${esc(headline(ev))}</h1>
            </div>
            <div class="cols">
                <div class="col-main">
                    ${ev.audio_url ? `<section class="card" aria-label="Ljud">${Player.html(ev.audio_url, ev.duration_sec, true)}</section>` : ''}
                    <section class="card">
                        <div class="card-head"><h2>Transkription</h2>${tx.toggle}<span class="small muted">${esc(engine)}</span></div>
                        ${ev.transcription ? `<p class="prose">${tx.text}</p>`
        : `<p class="muted" style="margin:0">${tStatus || 'Ingen transkription för den här händelsen.'}</p>`}
                    </section>
                    <div id="event-extras"></div>
                </div>
                <div class="col-side">
                    <section class="card"><h2 style="margin-bottom:10px">Radiotext</h2>
                        ${rt.length ? `<ol class="rt-list">${rt.map((t, i) => `<li><b>${i + 1}</b><span>${esc(t)}</span></li>`).join('')}</ol>`
        : '<p class="muted small" style="margin:0">Ingen radiotext sändes.</p>'}</section>
                    <section class="card"><h2 style="margin-bottom:10px">Push</h2>
                        ${pushRows.length ? pushRows.map((x) => `<div class="kv"><span>${esc(x.rule_name || '')} · <code>${esc(x.topic || '')}</code></span>
                            <span>${esc(pushLabel[x.status] || x.status)} ${esc(fmtTime(parseTs(x.created_at)))}</span></div>`).join('')
        : `<p class="muted small" style="margin:0">${notify.length ? 'Ingen regel matchade, så ingen push skickades.'
            : (live || ev.transcription_status === 'transcribing' || ev.transcription_status === 'saving' || ev.transcription_status === 'queued'
                ? 'Reglerna kontrolleras när sändningen och transkriptionen är klara.' : 'Ingen push skickades.')}</p>`}
                        <p class="small" style="margin:10px 0 0"><a href="#/admin/aviseringar">Ändra regler</a></p></section>
                    <section class="card"><h2 style="margin-bottom:6px">Detaljer</h2>
                        ${kv.map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join('')}</section>
                </div>
            </div>`;
        if (cfg.eventExtras) {
            // Host hook: fill the empty element (e.g. a mini map)
            try {
                await cfg.eventExtras(ev, root.querySelector('#event-extras'), ctx);
            } catch (e) { /* the event itself is already shown */ }
        }
        const pending = live || ['recording', 'saving', 'transcribing', 'queued'].includes(ev.transcription_status);
        if (pending) {
            const timer = setInterval(() => {
                if (ctx.alive() && !document.hidden && !Player.isPlaying()) route();
            }, 10000);
            ctx.cleanup = () => clearInterval(timer);
        }
    };

    // ------------------------------------------------------------------ push rules

    const DAY_NAMES = ['Mån', 'Tis', 'Ons', 'Tor', 'Fre', 'Lör', 'Sön'];
    const PRIO = { normal: 'normal', high: 'hög', critical: 'kritisk' };

    function describeDays(days) {
        if (!days || !days.length) return '';
        const s = days.join(',');
        if (s === '0,1,2,3,4') return 'vardagar';
        if (s === '5,6') return 'helger';
        return days.map((d) => DAY_NAMES[d].toLowerCase()).join(', ');
    }

    function describeRule(rule, stations) {
        const parts = [];
        parts.push(rule.event_types.map((t) => (t === 'emergency' ? 'VMA' : 'Trafik')).join(' + '));
        if (rule.stations && rule.stations.length) {
            parts.push(rule.stations.map((pi) => {
                const s = stations.find((x) => normPi(x.pi) === normPi(pi));
                return s ? (s.ps || s.pi) : pi;
            }).join(', '));
        } else parts.push('alla stationer');
        if (rule.nodes && rule.nodes.length) parts.push(rule.nodes.join(', '));
        if (rule.keywords && rule.keywords.length) parts.push(`nämner ${rule.keywords.join(', ')}`);
        const days = describeDays(rule.days);
        if (days) parts.push(days);
        if (rule.windows && rule.windows.length) parts.push(rule.windows.map((w) => `${w.from}–${w.to}`).join(', '));
        return parts.join(' · ');
    }

    const normPi = (pi) => {
        const p = String(pi || '').trim().toUpperCase();
        return p.startsWith('0X') ? p.slice(2) : p;
    };

    function newRule(params) {
        const ord = params && params.get('ord');
        return {
            name: ord ? `Bevaka ${ord}` : 'Ny regel', enabled: true, event_types: ['traffic'],
            stations: [], nodes: [], keywords: ord ? ord.split(/\s+/).filter(Boolean) : [],
            days: [], windows: [], priority: 'normal', topic: '',
        };
    }

    const notifySection = async (root, r, ctx) => {
        const [data, stations, nodes, logData] = await Promise.all([
            api('GET', '/notify'), cfg.loadStations(), cfg.loadNodes ? cfg.loadNodes() : null,
            api('GET', '/notify/log?limit=8'),
        ]);
        if (!ctx.alive()) return;
        const sel = r.args[0];
        const base = sel === 'ny' ? newRule(r.params)
            : (data.rules.find((x) => String(x.id) === sel) || data.rules[0] || newRule());
        const draft = JSON.parse(JSON.stringify(base));
        let dirty = sel === 'ny';
        const prefix = data.mqtt.prefix;
        const allStations = stations.slice();
        draft.stations.forEach((pi) => {
            if (!allStations.some((s) => normPi(s.pi) === normPi(pi))) allStations.push({ pi, ps: pi });
        });
        const allNodes = nodes ? nodes.slice() : null;
        if (allNodes) draft.nodes.forEach((n) => { if (!allNodes.includes(n)) allNodes.push(n); });

        const m = data.mqtt;
        const brokerState = !m.enabled ? ['', 'Av'] : (m.connected ? ['ok', 'Ansluten'] : ['bad', 'Inte ansluten']);
        const statusText = { sent: ['ok', 'Skickad'], failed: ['bad', 'Misslyckades'], mqtt_off: ['', 'MQTT av – inte skickad'] };

        root.innerHTML = `
            <div class="page-head"><div class="grow"><h1>Pushnotiser via MQTT</h1>
                <p>När en händelse matchar en regel får din MQTT-broker ett färdigt notismeddelande. Home Assistant, Node-RED
                eller liknande tar emot det och skickar vidare till mobilen.</p></div>
                <a class="btn primary" href="#/admin/aviseringar/ny">+ Ny regel</a></div>
            ${!m.enabled ? `<div class="notice warn">MQTT är inte påslaget, så inga notiser skickas. Sätt <code>MQTT_ENABLED=true</code>
                och <code>MQTT_HOST</code> i <code>.env</code>. Du kan ändå skapa regler och provköra dem mot historiken.</div>` : ''}
            <div class="cols">
                <div class="col-narrow" style="display:flex;flex-direction:column;gap:18px">
                    <section aria-label="Regler"><h2 class="label" style="margin-bottom:10px">Regler</h2>
                        <div class="rule-list">${data.rules.length ? data.rules.map((x) => `
                            <a class="rule-item" href="#/admin/aviseringar/${x.id}"${String(x.id) === String(draft.id) ? ' aria-current="true"' : ''}>
                                <span class="row"><span class="type-mark${x.event_types.includes('emergency') ? ' vma' : ''}"></span>
                                    <span class="name">${esc(x.name)}</span>
                                    <span class="pill ${x.enabled ? 'ok' : ''}">${x.enabled ? 'På' : 'Av'}</span></span>
                                <span class="desc">${esc(describeRule(x, allStations))}</span>
                                <code>${esc(prefix)}/notify/${esc(x.topic)}</code>
                            </a>`).join('') : '<div class="empty">Inga regler än.</div>'}</div>
                    </section>
                    <section class="card" aria-label="Broker">
                        <div class="card-head"><h2>Broker</h2><span class="pill ${brokerState[0]}">${brokerState[1]}</span></div>
                        <div class="kv"><span>Adress</span><span><code>${esc(m.host ? `${m.host}:${m.port}` : '–')}</code></span></div>
                        <div class="kv"><span>Klient-ID</span><span><code>${esc(m.client_id)}</code></span></div>
                        <div class="kv"><span>Ämnen</span><span><code>${esc(prefix)}/notify/…</code></span></div>
                        <p class="small muted" style="margin:8px 0 0">Anslutningen ställs in i <code>.env</code>.
                            ${data.web_ui_url ? '' : 'Sätt <code>WEB_UI_URL</code> så får notiserna en länk till händelsen.'}</p>
                    </section>
                    <section class="card" aria-label="Senast skickat"><h2 style="margin-bottom:6px">Senast skickat</h2>
                        ${logData.log.length ? logData.log.map((l) => {
        const t = parseTs(l.created_at);
        const st = statusText[l.status] || ['', l.status];
        return `<div class="log-row"><span class="when">${esc(fmtRelDay(t))} ${esc(fmtTime(t))}</span>
                                <div class="what"><div>${esc(l.title || '')}</div>
                                <div class="small"><span class="pill ${st[0]}">${esc(st[1])}</span> ${esc(l.rule_name || '')}${l.test ? ' · test' : ''}</div></div></div>`;
    }).join('') : '<p class="small muted" style="margin:0">Inget skickat än.</p>'}
                    </section>
                    <details class="card help"><summary>Ta emot i Home Assistant</summary>
                        <pre class="code">automation:
  - alias: "RDS Guard push"
    trigger:
      - platform: mqtt
        topic: "${esc(prefix)}/notify/#"
    action:
      - service: notify.mobile_app_din_telefon
        data:
          title: "{{ trigger.payload_json.title }}"
          message: "{{ trigger.payload_json.message }}"
          data:
            url: "{{ trigger.payload_json.url }}"</pre>
                        <p class="small muted" style="margin:10px 0 0">Meddelandet har också <code>priority</code>, <code>event_type</code>,
                        <code>station</code>, <code>matched_keywords</code> och <code>audio_url</code>.</p>
                    </details>
                </div>
                <section class="card col-main editor" id="editor" aria-label="Redigera regel"></section>
            </div>`;

        const ed = root.querySelector('#editor');
        let previewTimer = null;

        const pressed = (on) => `aria-pressed="${on ? 'true' : 'false'}"`;

        function renderEditor() {
            const t = draft.event_types;
            ed.innerHTML = `
                <div class="editor-top">
                    <label><span class="sr-only">Regelns namn</span>
                        <input class="name-input" id="r-name" maxlength="60" value="${esc(draft.name)}" placeholder="Namn på regeln"></label>
                    <span class="switch-row"><span class="small muted">Aktiv</span>
                        <button type="button" class="switch" role="switch" id="r-enabled" aria-checked="${draft.enabled}" aria-label="Regeln är aktiv"></button></span>
                </div>
                <div class="sentence">
                    <div class="clause"><span class="word">När</span>
                        <button type="button" class="toggle-pill" data-type="traffic" ${pressed(t.includes('traffic'))}>ett trafikmeddelande</button>
                        <span class="txt">eller</span>
                        <button type="button" class="toggle-pill" data-type="emergency" ${pressed(t.includes('emergency'))}>ett VMA</button>
                        <span class="txt">sänds på</span>
                        <button type="button" class="toggle-pill" data-pi="" ${pressed(!draft.stations.length)}>alla stationer</button>
                        ${allStations.map((s) => `<button type="button" class="toggle-pill" data-pi="${esc(s.pi)}" ${pressed(draft.stations.some((x) => normPi(x) === normPi(s.pi)))}>${esc(s.ps || s.pi)}</button>`).join('')}
                    </div>
                    ${allNodes ? `<div class="clause"><span class="word">Från</span>
                        <button type="button" class="toggle-pill" data-node="" ${pressed(!draft.nodes.length)}>alla noder</button>
                        ${allNodes.map((n) => `<button type="button" class="toggle-pill" data-node="${esc(n)}" ${pressed(draft.nodes.includes(n))}>${esc(n)}</button>`).join('')}
                    </div>` : ''}
                    <div class="clause"><span class="word">Och</span><span class="txt">texten nämner</span>
                        ${draft.keywords.length ? '' : '<span class="txt muted">vad som helst</span><span class="txt">– eller</span>'}
                        ${draft.keywords.map((k, i) => `<span class="chip">${esc(k)}<button type="button" data-rmkw="${i}" aria-label="Ta bort ${esc(k)}">×</button></span>`).join('')}
                        <label><span class="sr-only">Lägg till ord</span>
                            <input class="chip-input" id="r-kw" placeholder="+ lägg till ord" maxlength="60"></label>
                    </div>
                    <div class="clause"><span class="word">Under</span>
                        <button type="button" class="toggle-pill" data-day="" ${pressed(!draft.days.length)}>alla dagar</button>
                        ${DAY_NAMES.map((d, i) => `<button type="button" class="toggle-pill" data-day="${i}" ${pressed(draft.days.includes(i))}>${d}</button>`).join('')}
                    </div>
                    <div class="clause"><span class="word">Klockan</span>
                        ${draft.windows.length ? draft.windows.map((w, i) => `<span class="time-row">
                            <label><span class="sr-only">Från</span><input type="time" data-w="${i}" data-f="from" value="${esc(w.from)}"></label>–
                            <label><span class="sr-only">Till</span><input type="time" data-w="${i}" data-f="to" value="${esc(w.to)}"></label>
                            <button type="button" class="icon-btn" data-rmw="${i}" aria-label="Ta bort tiden">×</button></span>`).join('<span class="txt">och</span>')
        : '<span class="txt">hela dygnet</span>'}
                        <button type="button" class="btn small" id="r-addw">+ tid</button>
                    </div>
                    <div class="clause"><span class="word">Skicka</span><span class="txt">till</span>
                        <label class="topic-input"><span>${esc(prefix)}/notify/</span>
                            <input id="r-topic" value="${esc(draft.topic)}" placeholder="${esc(slugify(draft.name))}" maxlength="40" aria-label="Ämne"></label>
                        <span class="txt">med prioritet</span>
                        <label><span class="sr-only">Prioritet</span><select class="sel" id="r-prio">
                            ${Object.entries(PRIO).map(([v, l]) => `<option value="${v}"${draft.priority === v ? ' selected' : ''}>${l}</option>`).join('')}
                        </select></label>
                    </div>
                </div>
                <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">
                    <button type="button" class="btn primary" id="r-save">${draft.id ? 'Spara' : 'Skapa regel'}</button>
                    <button type="button" class="btn" id="r-test">Skicka test</button>
                    ${draft.id ? '<button type="button" class="btn danger" id="r-del">Ta bort</button>' : ''}
                    <span id="r-msg" class="error-text" role="alert"></span>
                </div>
                <div class="preview" id="r-preview" aria-live="polite"></div>`;
            bindEditor();
        }

        function changed(rerender) {
            dirty = true;
            if (rerender) renderEditor();
            clearTimeout(previewTimer);
            previewTimer = setTimeout(runPreview, 350);
        }

        function toggleIn(list, value) {
            const i = list.indexOf(value);
            if (i >= 0) list.splice(i, 1); else list.push(value);
        }

        function addKeyword(input) {
            input.value.split(',').map((w) => w.trim()).filter(Boolean).forEach((w) => {
                if (!draft.keywords.some((k) => k.toLowerCase() === w.toLowerCase())) draft.keywords.push(w);
            });
            input.value = '';
            changed(true);
            const again = ed.querySelector('#r-kw');
            if (again) again.focus();
        }

        function bindEditor() {
            ed.querySelector('#r-name').addEventListener('input', (e) => {
                draft.name = e.target.value;
                ed.querySelector('#r-topic').placeholder = slugify(draft.name);
                dirty = true;
            });
            ed.querySelector('#r-enabled').addEventListener('click', () => { draft.enabled = !draft.enabled; changed(true); });
            ed.querySelectorAll('[data-type]').forEach((b) => b.addEventListener('click', () => {
                toggleIn(draft.event_types, b.dataset.type);
                draft.event_types = ['traffic', 'emergency'].filter((x) => draft.event_types.includes(x));
                changed(true);
            }));
            ed.querySelectorAll('[data-pi]').forEach((b) => b.addEventListener('click', () => {
                if (!b.dataset.pi) draft.stations = [];
                else {
                    const i = draft.stations.findIndex((x) => normPi(x) === normPi(b.dataset.pi));
                    if (i >= 0) draft.stations.splice(i, 1); else draft.stations.push(b.dataset.pi);
                }
                changed(true);
            }));
            ed.querySelectorAll('[data-node]').forEach((b) => b.addEventListener('click', () => {
                if (!b.dataset.node) draft.nodes = []; else toggleIn(draft.nodes, b.dataset.node);
                changed(true);
            }));
            ed.querySelectorAll('[data-day]').forEach((b) => b.addEventListener('click', () => {
                if (b.dataset.day === '') draft.days = [];
                else {
                    toggleIn(draft.days, Number(b.dataset.day));
                    draft.days.sort();
                    if (draft.days.length === 7) draft.days = [];
                }
                changed(true);
            }));
            ed.querySelectorAll('[data-rmkw]').forEach((b) => b.addEventListener('click', () => {
                draft.keywords.splice(Number(b.dataset.rmkw), 1);
                changed(true);
            }));
            const kw = ed.querySelector('#r-kw');
            kw.addEventListener('keydown', (e) => {
                if ((e.key === 'Enter' || e.key === ',') && kw.value.trim()) {
                    e.preventDefault();
                    addKeyword(kw);
                }
            });
            kw.addEventListener('blur', () => { if (kw.value.trim()) addKeyword(kw); });
            ed.querySelectorAll('input[type=time]').forEach((inp) => inp.addEventListener('change', () => {
                draft.windows[Number(inp.dataset.w)][inp.dataset.f] = inp.value;
                changed(false);
            }));
            ed.querySelectorAll('[data-rmw]').forEach((b) => b.addEventListener('click', () => {
                draft.windows.splice(Number(b.dataset.rmw), 1);
                changed(true);
            }));
            ed.querySelector('#r-addw').addEventListener('click', () => {
                draft.windows.push(draft.windows.length ? { from: '15:00', to: '19:00' } : { from: '06:00', to: '09:00' });
                changed(true);
            });
            ed.querySelector('#r-topic').addEventListener('input', (e) => { draft.topic = e.target.value.trim(); dirty = true; });
            ed.querySelector('#r-prio').addEventListener('change', (e) => { draft.priority = e.target.value; dirty = true; });
            ed.querySelector('#r-save').addEventListener('click', save);
            ed.querySelector('#r-test').addEventListener('click', test);
            const del = ed.querySelector('#r-del');
            if (del) del.addEventListener('click', remove);
        }

        const body = () => {
            const b = { ...draft };
            delete b.created_at;
            delete b.updated_at;
            return b;
        };
        const showErr = (e) => { ed.querySelector('#r-msg').textContent = e.message; };

        async function save() {
            ed.querySelector('#r-msg').textContent = '';
            try {
                const saved = draft.id
                    ? await api('PUT', `/notify/rules/${draft.id}`, body())
                    : await api('POST', '/notify/rules', body());
                dirty = false;
                toast(`Regeln ”${saved.name}” är sparad`);
                go(`#/admin/aviseringar/${saved.id}`);
            } catch (e) { showErr(e); }
        }

        async function test() {
            ed.querySelector('#r-msg').textContent = '';
            try {
                const res = await api('POST', '/notify/test', body());
                toast({
                    sent: `Testnotis skickad till ${res.topic}`,
                    mqtt_off: 'MQTT är inte påslaget – inget skickades',
                    failed: 'Kunde inte skicka – är brokern ansluten?',
                }[res.status] || res.status);
            } catch (e) { showErr(e); }
        }

        async function remove() {
            if (!window.confirm(`Ta bort regeln ”${draft.name}”?`)) return;
            try {
                await api('DELETE', `/notify/rules/${draft.id}`, {});
                dirty = false;
                toast('Regeln är borttagen');
                go('#/admin/aviseringar');
            } catch (e) { showErr(e); }
        }

        async function runPreview() {
            const box = ed.querySelector('#r-preview');
            if (!box) return;
            if (!draft.event_types.length) {
                box.innerHTML = '<p class="muted small" style="margin:0">Välj trafikmeddelande, VMA eller båda.</p>';
                return;
            }
            let res;
            try {
                res = await api('POST', '/notify/preview', body());
            } catch (e) {
                box.innerHTML = `<p class="error-text" style="margin:0">${esc(e.message)}</p>`;
                return;
            }
            if (!ctx.alive() || !ed.contains(box)) return;
            const byDate = {};
            res.days.forEach((d) => { byDate[d.date] = d; });
            const ret = data.retention_days;
            const today = new Date();
            const list = [];
            for (let i = ret - 1; i >= 0; i--) {
                const k = dayKey(addDays(today, -i));
                list.push({ k, ...(byDate[k] || { total: 0, matched: 0 }) });
            }
            const max = Math.max(1, ...list.map((d) => d.total));
            const weekdays = list.filter((d) => keyToDate(d.k).getDay() % 6 !== 0).length;
            const perDay = weekdays ? res.matched / list.length : 0;
            const rate = res.matched === 0 ? '' : perDay >= 1.5 ? ` – omkring ${Math.round(perDay)} om dagen`
                : perDay >= 0.8 ? ' – ungefär en om dagen' : perDay >= 0.12 ? ` – ungefär ${Math.max(1, Math.round(perDay * 7))} i veckan` : '';
            box.innerHTML = `
                <div class="day-head"><h3 style="font-size:16px">Provkör mot historiken</h3>
                    <span class="muted small">Regeln hade gett <strong style="color:var(--text)">${res.matched} push</strong>
                    av ${res.total} meddelanden de senaste ${ret} dagarna${rate}.</span></div>
                <div class="bars" role="img" aria-label="Meddelanden per dag, och hur många som hade skickats som push">
                    ${list.map((d) => `<span style="height:${Math.max(3, Math.round(d.total / max * 48))}px" title="${esc(d.k)}: ${d.matched} av ${d.total}">
                        <i style="height:${d.total ? Math.round(d.matched / d.total * 100) : 0}%"></i></span>`).join('')}
                </div>
                <div class="legend"><span><i style="background:var(--accent)"></i>Hade skickats</span>
                    <span><i style="background:#E3E8EF"></i>Övriga meddelanden</span></div>
                ${res.samples.map((s) => {
        const d = evStart(s);
        return `<div class="sample"><span>${esc(d ? `${fmtRelDay(d)} ${fmtTime(d)}` : '')}</span>
                        <a href="#/event/${encodeURIComponent(cfg.idOf(s))}">${esc(headline(s))}</a></div>`;
    }).join('')}`;
        }

        root.querySelectorAll('.rule-item').forEach((a) => a.addEventListener('click', (e) => {
            if (dirty && !window.confirm('Du har ändringar som inte är sparade. Lämna regeln ändå?')) e.preventDefault();
        }));

        renderEditor();
        runPreview();
    };

    // ------------------------------------------------------------------ admin

    // Sections of #/admin, in tab order: [route, label, view]. Each view gets
    // the route with the section name taken off its args.
    const adminSections = [
        ['aviseringar', 'Aviseringar', notifySection],
    ];

    function adminLogin(root, session) {
        if (!session.enabled) {
            root.innerHTML = `
                <div class="page-head"><div class="grow"><h1>Admin</h1></div></div>
                <section class="card admin-login">
                    <h2>Admin är avstängt</h2>
                    <p class="muted">Sätt ett lösenord i <code>ADMIN_PASSWORD</code> i <code>.env</code> och starta om
                    containern. Därefter loggar du in här för att ändra aviseringar.</p>
                </section>`;
            return;
        }
        root.innerHTML = `
            <div class="page-head"><div class="grow"><h1>Admin</h1>
                <p>Inställningar som bara den som har lösenordet får ändra.</p></div></div>
            <form class="card admin-login" id="admin-login">
                <h2>Logga in</h2>
                <label class="field"><span class="label">Lösenord</span>
                    <input type="password" name="password" autocomplete="current-password" required></label>
                <div class="row-actions"><button class="btn primary" type="submit">Logga in</button>
                    <span class="error-text" id="admin-msg" role="alert"></span></div>
            </form>`;
        const form = root.querySelector('#admin-login');
        form.password.focus();
        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = form.querySelector('button');
            btn.disabled = true;
            try {
                await api('POST', '/admin/login', { password: form.password.value });
                route();
            } catch (err) {
                root.querySelector('#admin-msg').textContent = err.message;
                form.password.select();
            } finally {
                btn.disabled = false;
            }
        });
    }

    views.admin = async (root, r, ctx) => {
        const session = await api('GET', '/admin/session');
        if (!ctx.alive()) return;
        if (!session.logged_in) { adminLogin(root, session); return; }

        const name = r.args[0] || adminSections[0][0];
        const section = adminSections.find(([n]) => n === name) || adminSections[0];
        root.innerHTML = `
            <div class="admin-bar">
                <nav class="seg" aria-label="Admin">${adminSections.map(([n, l]) =>
        `<a href="#/admin/${n}"${n === section[0] ? ' aria-current="true"' : ''}>${esc(l)}</a>`).join('')}</nav>
                <span class="grow"></span>
                <button class="btn" type="button" id="admin-logout">Logga ut</button>
            </div>
            <div id="admin-section" style="display:flex;flex-direction:column;gap:22px"></div>`;
        root.querySelector('#admin-logout').addEventListener('click', async () => {
            try { await api('POST', '/admin/logout', {}); } catch (e) { /* logged out anyway */ }
            toast('Du är utloggad');
            route();
        });
        const sub = { ...r, args: r.args.slice(1) };
        try {
            await section[2](root.querySelector('#admin-section'), sub, ctx);
        } catch (e) {
            if (e.status === 401 && ctx.alive()) { adminLogin(root, { enabled: true }); return; }
            throw e;
        }
    };

    // ------------------------------------------------------------------ start

    function start(options) {
        Object.assign(cfg, options);
        Object.assign(views, options.views || {});
        adminSections.push(...(options.adminSections || []));
        const nav = [
            ['arkiv', 'Arkiv', ICON.calendar],
            ['sok', 'Sök', ICON.searchNav],
            ...(options.extraNav || []),
            ['admin', 'Admin', ICON.lockNav],
        ];
        document.getElementById('nav').innerHTML = nav.map(([v, l, icon]) =>
            `<a href="#/${v}" data-view="${v}">${icon || ''}<span>${esc(l)}</span></a>`).join('');
        const form = document.getElementById('top-search');
        if (form) {
            form.addEventListener('submit', (e) => {
                e.preventDefault();
                const q = form.q.value.trim();
                go(hashWith('sok', { q }));
                form.q.value = '';
                form.q.blur();
            });
        }
        // "Rättad" tag: switch between the corrected and the original text
        document.addEventListener('click', (e) => {
            const b = e.target.closest('.fix-toggle');
            if (!b) return;
            e.preventDefault();
            const box = b.closest('.ev, .card');
            const showOrig = b.getAttribute('aria-pressed') !== 'true';
            box.querySelectorAll('.tx-fixed').forEach((x) => { x.hidden = showOrig; });
            box.querySelectorAll('.tx-orig').forEach((x) => { x.hidden = !showOrig; });
            b.setAttribute('aria-pressed', String(showOrig));
            b.textContent = showOrig ? 'Original' : 'Rättad';
            b.classList.toggle('info', !showOrig);
        });
        window.addEventListener('hashchange', route);
        route();
    }

    return {
        start, api, esc, route, go, toast, parseTs, fmtTime, fmtRelDay, fmtDur, fmtFreq, ICON, Player, txHtml,
    };
})();
