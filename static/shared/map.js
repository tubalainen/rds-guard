/**
 * RDS Guard — map view (#/karta), shared by the satellite and the aggregator.
 *
 * Shows the incidents traffic messages mentioned in the last 12 hours:
 *   - a pin, faded as it gets older, gone after 12 hours (then history)
 *   - a red circle around it: the larger, the less certain the place
 *   - an arrow for the direction of traffic affected, a double arrow when
 *     both directions are, no arrow when the message does not say
 *   - grey when a later message said the road is open again
 *
 * "Visa historiska händelser" shows every incident still stored, older
 * ones in violet. The first time, the messages stored before the map
 * existed are placed (POST /incidents/backfill), with Claude if it is on.
 *
 * Also supplies the RDSUI hooks: a place label on every event card
 * (cardMeta) and a small map on the event page (eventExtras).
 * Leaflet is loaded on first use. Map data © OpenStreetMap contributors.
 */
'use strict';

const RDSMap = (() => {
    const HOURS = 12;
    const TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
    const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
    const SWEDEN = [[55.3, 10.9], [69.1, 24.2]];
    const HISTORY_KEY = 'rdsguard.map.history';
    const COLOR = { active: '#D92D20', cleared: '#667085', hist: '#7F56D9' };
    const esc = (s) => RDSUI.esc(s);

    function savedHistory() {
        try { return localStorage.getItem(HISTORY_KEY) === '1'; } catch (e) { return false; }
    }
    function saveHistory(on) {
        try { localStorage.setItem(HISTORY_KEY, on ? '1' : '0'); } catch (e) { /* private mode */ }
    }

    const PIN_ICON = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/></svg>';
    const PIN_SMALL = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true"><path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/></svg>';

    // ------------------------------------------------------------- Leaflet

    let leaflet = null;
    function loadLeaflet() {
        if (window.L) return Promise.resolve();
        if (!leaflet) {
            leaflet = new Promise((resolve, reject) => {
                const css = document.createElement('link');
                css.rel = 'stylesheet';
                css.href = '/static/shared/vendor/leaflet/leaflet.css';
                document.head.appendChild(css);
                const js = document.createElement('script');
                js.src = '/static/shared/vendor/leaflet/leaflet.js';
                js.onload = () => resolve();
                js.onerror = () => { leaflet = null; reject(new Error('Kartbiblioteket kunde inte laddas')); };
                document.head.appendChild(js);
            });
        }
        return leaflet;
    }

    function newMap(el, opts = {}) {
        const map = L.map(el, { zoomControl: true, ...opts });
        L.tileLayer(TILES, { maxZoom: 18, attribution: ATTRIBUTION }).addTo(map);
        return map;
    }

    // ------------------------------------------------------------- formatting

    function ageHours(inc) {
        const d = RDSUI.parseTs(inc.last_seen);
        return d ? Math.max(0, (Date.now() - d.getTime()) / 3.6e6) : HOURS;
    }

    /** Older than 12 hours: part of the history. */
    const isHist = (inc) => ageHours(inc) > HOURS;

    /** 1 when just mentioned, fading to 0.2 at 12 hours; history is even. */
    const freshness = (inc) => (isHist(inc) ? 0.75 : Math.max(0.2, 1 - ageHours(inc) / HOURS));

    const kind = (inc) => (isHist(inc) ? 'hist' : inc.status === 'cleared' ? 'cleared' : 'active');

    const DAY = { weekday: 'long', day: 'numeric', month: 'long' };
    const fmtDay = (d) => d.toLocaleDateString('sv-SE', DAY);
    const fmtShort = (d) => `${d.toLocaleDateString('sv-SE', { day: 'numeric', month: 'short' })} ${RDSUI.fmtTime(d)}`;

    function ago(inc) {
        const h = ageHours(inc);
        if (h < 1 / 60) return 'nyss';
        if (h < 1) return `för ${Math.round(h * 60)} min sedan`;
        if (h <= HOURS) return `för ${Math.floor(h)} h ${Math.round((h % 1) * 60)} min sedan`;
        return when(inc);
    }

    /** When an incident was mentioned: "24 nov. 08:10–09:40". */
    function when(inc) {
        const a = RDSUI.parseTs(inc.first_seen);
        const b = RDSUI.parseTs(inc.last_seen);
        if (!b) return '';
        if (!a || b - a < 60000) return fmtShort(b);
        if (a.toDateString() === b.toDateString()) return `${fmtShort(a)}–${RDSUI.fmtTime(b)}`;
        return `${fmtShort(a)} – ${fmtShort(b)}`;
    }

    function precision(m) {
        if (m < 1000) return `±${Math.round(m / 50) * 50} m`;
        return `±${(m / 1000).toFixed(m < 10000 ? 1 : 0).replace('.', ',')} km`;
    }

    const COMPASS = ['norrut', 'mot nordost', 'österut', 'mot sydost', 'söderut', 'mot sydväst', 'västerut', 'mot nordväst'];
    function direction(inc) {
        if (inc.both_directions) return 'Båda riktningarna';
        if (inc.bearing == null) return 'Riktning okänd';
        return `En riktning, ${COMPASS[Math.round(inc.bearing / 45) % 8]}`;
    }

    const LEVEL = {
        'nära en plats': 'nära en namngiven plats',
        'mellan två platser': 'mellan två namngivna platser',
        'på en sträcka': 'någonstans på en sträcka',
        'i eller nära en ort': 'i eller nära en ort',
    };

    // ------------------------------------------------------------- markers

    function arrowSvg(inc) {
        const arrow = '<path d="M18 1.5 25 11h-4.5v5h-5v-5H11z"/>';
        if (inc.both_directions) {
            const b = inc.bearing ?? 0;
            return `<svg class="inc-arrow" width="36" height="36" viewBox="0 0 36 36" aria-hidden="true">
                <g transform="rotate(${b} 18 18)">${arrow}<g transform="rotate(180 18 18)">${arrow}</g></g></svg>`;
        }
        if (inc.bearing == null) return '';
        return `<svg class="inc-arrow" width="36" height="36" viewBox="0 0 36 36" aria-hidden="true">
            <g transform="rotate(${inc.bearing} 18 18)">${arrow}</g></svg>`;
    }

    function pinIcon(inc) {
        const k = kind(inc);
        return L.divIcon({
            className: 'inc-icon',
            iconSize: [36, 36],
            iconAnchor: [18, 18],
            popupAnchor: [0, -14],
            html: `<div class="inc-pin${k === 'active' ? '' : ` ${k}`}" style="opacity:${freshness(inc).toFixed(2)}">
                ${arrowSvg(inc)}<span class="inc-dot"></span></div>`,
        });
    }

    function addIncident(layer, inc) {
        const hist = isHist(inc);
        const f = hist ? 0.25 : freshness(inc);
        const color = COLOR[kind(inc)];
        const circle = L.circle([inc.lat, inc.lon], {
            radius: inc.radius_m, color, weight: hist ? 1 : 1.5, opacity: 0.35 + 0.55 * f,
            fillColor: color, fillOpacity: 0.05 + 0.13 * f, interactive: false,
        }).addTo(layer);
        const marker = L.marker([inc.lat, inc.lon], {
            icon: pinIcon(inc), title: inc.label, riseOnHover: true,
            // Current incidents above the history
            zIndexOffset: hist ? -1000 : Math.round(f * 1000),
        }).addTo(layer);
        marker.bindPopup(() => popupHtml(inc), { maxWidth: 320, minWidth: 220 });
        return { marker, circle };
    }

    function popupHtml(inc) {
        const latest = inc.latest;
        const text = latest && latest.transcription ? latest.transcription : '';
        const short = text.length > 240 ? `${text.slice(0, 240)}…` : text;
        const status = isHist(inc) ? '<span class="pill hist">Historisk</span>'
            : inc.status === 'cleared' ? '<span class="pill">Avklarad</span>' : '<span class="pill bad">Pågår</span>';
        return `<div class="inc-pop">
            <strong>${esc(inc.label)}</strong>
            <div class="inc-pop-meta">${status}<span>${esc(ago(inc))}</span></div>
            ${inc.what ? `<p>${esc(inc.what)}</p>` : ''}
            <div class="kv"><span>Plats</span><span>${esc(precision(inc.radius_m))}, ${esc(LEVEL[inc.level] || inc.level || '')}</span></div>
            <div class="kv"><span>Trafik</span><span>${esc(direction(inc))}</span></div>
            <div class="kv"><span>Omnämnd</span><span>${inc.events.length} ${inc.events.length === 1 ? 'gång' : 'gånger'}</span></div>
            ${inc.nodes && inc.nodes.length ? `<div class="kv"><span>Hörd av</span><span>${esc(inc.nodes.join(', '))}</span></div>` : ''}
            ${short ? `<p class="inc-pop-text">”${esc(short)}”</p>` : ''}
            ${latest ? `<a href="#/event/${encodeURIComponent(latest.id)}">Öppna senaste meddelandet</a>` : ''}
        </div>`;
    }

    // ------------------------------------------------------------- notices

    function notice(geo) {
        if (!geo) return '';
        if (!geo.enabled && geo.reason === 'off') {
            return `<div class="map-note">Den här satelliten behandlar inte sina inspelningar själv
                (<code>LOCAL_PROCESSING=false</code>): de skickas till aggregatorn, som transkriberar dem
                och placerar dem på kartan. Kartan finns där.</div>`;
        }
        if (!geo.enabled) {
            return '<div class="map-note">Kartan är avstängd (<code>GEO_ENGINE=none</code>).</div>';
        }
        if (geo.location === null) {
            return `<div class="map-note">Ange var mottagaren står i <code>.env</code>, till exempel
                <code>NODE_LOCATION=Karlstad, Värmlands län</code>, så kan meddelandena placeras på kartan.</div>`;
        }
        if (geo.location && geo.ready !== false && !geo.located) {
            return `<div class="map-note">Platsen <b>${esc(geo.location)}</b> i <code>NODE_LOCATION</code> hittades inte.
                Skriv den som ”Ort, Län”, till exempel <code>Örebro, Örebro län</code>.</div>`;
        }
        return '';
    }

    /** How far placing the older messages has come. */
    function backfillNote(bf, ranHere) {
        if (!bf) return '';
        const claude = bf.engine === 'claude' ? ' med Claude' : '';
        if (bf.running) {
            const of = bf.total ? `${bf.done} av ${bf.total}` : 'förbereder';
            return `<div class="map-note info"><span class="spinner" aria-hidden="true"></span>
                Placerar äldre meddelanden på kartan${esc(claude)}: ${esc(of)}. Det görs en gång;
                nya meddelanden placeras när de kommer.</div>`;
        }
        if (ranHere && bf.finished) {
            const left = bf.skipped
                ? ` ${bf.skipped} kunde inte placeras eftersom mottagarens plats (<code>NODE_LOCATION</code>) saknas.` : '';
            return `<div class="map-note info">Klart: ${bf.placed} av ${bf.total} äldre meddelanden
                kunde placeras på kartan.${left}</div>`;
        }
        return '';
    }

    function engineLabel(geo) {
        if (!geo || !geo.enabled) return '';
        return geo.engine === 'claude'
            ? 'Platserna tolkas med Claude och slås upp i ett lokalt ortnamnsregister.'
            : 'Platserna tolkas lokalt med regler och ett ortnamnsregister.';
    }

    // ------------------------------------------------------------- the view

    async function view(root, route, ctx) {
        root.innerHTML = `
            <div class="page-head"><div class="grow"><h1>Karta</h1>
                <p><span id="map-intro">Händelser som trafikmeddelandena nämnt de senaste ${HOURS} timmarna.</span>
                Ju större cirkel, desto osäkrare är platsen. Pilen visar vilken riktning som påverkas.</p></div>
                <label class="map-check"><input type="checkbox" id="map-history"> Visa historiska händelser</label></div>
            <div id="map-note" class="map-note-slot"></div>
            <div id="map-backfill" class="map-note-slot"></div>
            <div class="map-layout">
                <div class="map-box"><div id="map" class="map" role="application" aria-label="Karta över händelser"></div>
                    <div class="map-legend" aria-hidden="true">
                        <span><i class="lg-dot"></i>Pågår</span><span><i class="lg-dot grey"></i>Avklarad</span>
                        <span><i class="lg-ring"></i>Osäkerhet</span><span>Blekare = äldre</span>
                        <span id="lg-hist" hidden><i class="lg-dot hist"></i>Historisk (äldre än ${HOURS} h)</span></div></div>
                <aside class="map-list card" aria-label="Händelser på kartan">
                    <div class="card-head"><h2 id="map-list-title">Senaste ${HOURS} timmarna</h2></div>
                    <div id="map-items"><p class="muted small">Laddar…</p></div>
                    <p id="map-engine" class="small muted" style="margin:12px 0 0"></p>
                </aside>
            </div>`;
        try {
            await loadLeaflet();
        } catch (e) {
            root.querySelector('#map-items').innerHTML = `<p class="muted">${esc(e.message)}</p>`;
            return;
        }
        if (!ctx.alive()) return;
        const map = newMap(root.querySelector('#map'));
        map.fitBounds(SWEDEN);
        const layer = L.layerGroup().addTo(map);
        const nodeLayer = L.layerGroup().addTo(map);
        const markers = new Map();
        let first = true;
        const focus = route.params.get('i');
        const box = root.querySelector('#map-history');
        let history = savedHistory();
        box.checked = history;
        let backfillAsked = false;   // started (or tried) from this page
        let backfillRunning = false;
        let timer = null;

        box.addEventListener('change', () => {
            history = box.checked;
            saveHistory(history);
            first = true;            // fit the map to what is shown now
            refresh();
        });

        async function startBackfill() {
            backfillAsked = true;
            let res;
            try {
                res = await RDSUI.api('POST', '/incidents/backfill', {});
            } catch (e) {
                root.querySelector('#map-backfill').innerHTML = `<div class="map-note">De äldre meddelandena
                    kunde inte placeras: ${esc(e.message)}</div>`;
                return;
            }
            if (!ctx.alive()) return;
            backfillRunning = !!(res.backfill && res.backfill.running);
            if (history) root.querySelector('#map-backfill').innerHTML = backfillNote(res.backfill, true);
            schedule();
        }

        async function refresh() {
            let data;
            const asked = history;
            try {
                data = await RDSUI.api('GET', asked ? '/incidents?history=1' : `/incidents?hours=${HOURS}`);
            } catch (e) {
                root.querySelector('#map-items').innerHTML = `<p class="muted">Kunde inte hämta händelserna: ${esc(e.message)}</p>`;
                return;
            }
            if (!ctx.alive() || asked !== history) return;
            const incidents = (data.incidents || []).filter((i) => history || ageHours(i) <= HOURS);
            if (first && !history && focus && !incidents.some((i) => String(i.id) === focus)) {
                // A link to an older incident (from the archive): show the history
                history = true;
                box.checked = true;
                return refresh();
            }
            root.querySelector('#map-note').innerHTML = notice(data.geo);
            root.querySelector('#map-engine').textContent = engineLabel(data.geo);
            root.querySelector('#lg-hist').hidden = !history;
            root.querySelector('#map-intro').textContent = history
                ? `Alla händelser som finns sparade. De senaste ${HOURS} timmarna i rött, äldre i lila.`
                : `Händelser som trafikmeddelandena nämnt de senaste ${HOURS} timmarna.`;
            root.querySelector('#map-list-title').textContent = history
                ? `Alla händelser (${incidents.length})` : `Senaste ${HOURS} timmarna`;

            const bf = history ? data.backfill : null;
            const wasRunning = backfillRunning;
            backfillRunning = !!(bf && bf.running);
            root.querySelector('#map-backfill').innerHTML = backfillNote(bf, backfillAsked);
            if (bf && bf.runnable && !backfillAsked) startBackfill();
            if (wasRunning && !backfillRunning) first = true;  // refit to the new pins

            layer.clearLayers();
            markers.clear();
            incidents.slice().reverse().forEach((inc) => markers.set(String(inc.id), addIncident(layer, inc)));

            nodeLayer.clearLayers();
            const receivers = data.nodes || (data.geo && data.geo.located ? [{ name: 'Mottagaren', ...data.geo.located }] : []);
            receivers.forEach((n) => L.circleMarker([n.lat, n.lon], {
                radius: 5, color: '#1D4ED8', weight: 2, fillColor: '#fff', fillOpacity: 1,
            }).bindTooltip(n.name).addTo(nodeLayer));

            const list = root.querySelector('#map-items');
            const item = (inc) => {
                const k = kind(inc);
                return `<button type="button" class="map-item${k === 'active' ? '' : ` ${k}`}" data-id="${inc.id}">
                    <span class="map-item-dot" style="opacity:${freshness(inc).toFixed(2)}"></span>
                    <span class="map-item-body"><b>${esc(inc.label)}</b>
                        ${inc.what ? `<span class="small muted map-item-what">${esc(inc.what)}</span>` : ''}
                        <span class="small muted">${esc(ago(inc))} · ${esc(precision(inc.radius_m))}${k === 'cleared' ? ' · avklarad' : ''}</span>
                    </span></button>`;
            };
            let html = '';
            if (history) {
                // Grouped: the last 12 hours, then one heading per day
                let group = null;
                incidents.forEach((inc) => {
                    const d = RDSUI.parseTs(inc.last_seen);
                    const g = !isHist(inc) ? `Senaste ${HOURS} timmarna` : d ? fmtDay(d) : 'Okänt datum';
                    if (g !== group) {
                        group = g;
                        html += `<h3 class="map-group">${esc(g)}</h3>`;
                    }
                    html += item(inc);
                });
            } else {
                html = incidents.map(item).join('');
            }
            list.innerHTML = html || (history
                ? '<p class="muted small" style="margin:0">Inga händelser finns sparade ännu.</p>'
                : '<p class="muted small" style="margin:0">Inga händelser har nämnts de senaste timmarna.</p>');
            list.querySelectorAll('.map-item').forEach((b) => b.addEventListener('click', () => show(b.dataset.id)));

            if (first) {
                first = false;
                if (focus && markers.has(focus)) {
                    show(focus);
                } else if (incidents.length) {
                    const bounds = L.featureGroup([...markers.values()].map((m) => m.circle)).getBounds();
                    map.fitBounds(bounds.pad(0.2), { maxZoom: 12 });
                } else if (receivers.length) {
                    map.fitBounds(L.featureGroup(nodeLayer.getLayers()).getBounds().pad(0.5), { maxZoom: 8 });
                }
            }
        }

        function show(id) {
            const m = markers.get(String(id));
            if (!m) return;
            map.fitBounds(m.circle.getBounds().pad(0.4), { maxZoom: 13 });
            m.marker.openPopup();
            if (window.matchMedia('(max-width: 760px)').matches) {
                root.querySelector('#map').scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
        }

        // Every minute; every few seconds while older messages are placed
        function schedule() {
            clearTimeout(timer);
            timer = setTimeout(async () => {
                if (!ctx.alive()) return;
                if (!document.hidden) await refresh();
                schedule();
            }, backfillRunning ? 4000 : 60000);
        }

        await refresh();
        schedule();
        ctx.cleanup = () => {
            clearTimeout(timer);
            map.remove();
        };
    }

    // ------------------------------------------------------------- RDSUI hooks

    /** Place labels in an event card's meta row. */
    function cardMeta(ev) {
        const inc = ev.incidents || [];
        return inc.slice(0, 2).map((i) => `<a class="pill place" href="#/karta?i=${encodeURIComponent(i.id)}"
            title="Visa på kartan">${PIN_SMALL}${esc(i.label)}</a>`).join('')
            + (inc.length > 2 ? `<span class="pill">+${inc.length - 2}</span>` : '');
    }

    /** A small map on the event page, or why there is none. */
    async function eventExtras(ev, el, ctx) {
        const inc = ev.incidents || [];
        if (!inc.length) {
            const why = {
                none: 'Ingen plats kunde utläsas ur meddelandet.',
                no_location: 'Mottagarens plats är inte angiven (NODE_LOCATION), så meddelandet kunde inte placeras.',
                error: 'Placeringen på kartan misslyckades.',
            }[ev.geo_status];
            if (why) el.innerHTML = `<section class="card"><h2 style="margin-bottom:6px">Plats</h2><p class="muted small" style="margin:0">${esc(why)}</p></section>`;
            return;
        }
        el.innerHTML = `<section class="card">
            <div class="card-head"><h2>Plats</h2><a class="small" href="#/karta?i=${encodeURIComponent(inc[0].id)}">Visa på kartan</a></div>
            <div class="map mini" id="event-map"></div>
            ${inc.map((i) => `<div class="kv"><span>${esc(i.label)}</span>
                <span>${esc(precision(i.radius_m))} · ${esc(direction(i))}${i.status === 'cleared' ? ' · avklarad' : ''}</span></div>`).join('')}
        </section>`;
        await loadLeaflet();
        if (!ctx.alive()) return;
        const map = newMap(el.querySelector('#event-map'), { scrollWheelZoom: false });
        // A circle's extent can only be measured on a map that has a view
        map.setView([inc[0].lat, inc[0].lon], 11);
        const layer = L.featureGroup().addTo(map);
        inc.forEach((i) => addIncident(layer, { ...i, events: [], what: '', level: '' }));
        map.fitBounds(L.featureGroup(layer.getLayers().filter((x) => x instanceof L.Circle)).getBounds().pad(0.3), { maxZoom: 13 });
    }

    return { view, cardMeta, eventExtras, nav: ['karta', 'Karta', PIN_ICON] };
})();
