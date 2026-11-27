/**
 * RDS Guard Aggregator — starts the shared UI and adds the node view
 * (satellites, their tokens and health).
 */
'use strict';

const Aggregator = (() => {
    const { esc, api, toast } = RDSUI;
    let status = null;

    function ago(iso) {
        const d = RDSUI.parseTs(iso);
        if (!d) return 'aldrig';
        const sec = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
        if (sec < 60) return `${sec} s sedan`;
        if (sec < 3600) return `${Math.floor(sec / 60)} min sedan`;
        if (sec < 86400) return `${Math.floor(sec / 3600)} h sedan`;
        return `${RDSUI.fmtRelDay(d)} ${RDSUI.fmtTime(d)}`;
    }

    async function poll() {
        try {
            status = await api('GET', '/status');
        } catch (e) {
            status = null;
        }
        const top = document.getElementById('top-status');
        if (!status) {
            top.innerHTML = '<span class="dot bad"></span><span>Ingen kontakt</span>';
            return;
        }
        const n = status.nodes || 0;
        const on = status.nodes_online || 0;
        const cls = !n ? 'warn' : (on === n ? 'ok' : (on ? 'warn' : 'bad'));
        top.innerHTML = `<span class="dot ${cls}"></span><span>${n ? `${on} av ${n} noder online` : 'Inga noder än'}</span>`;
        document.getElementById('foot-version').textContent = `RDS Guard Aggregator ${status.version || ''}`.trim();
        const m = status.mqtt || {};
        document.getElementById('foot-mqtt').textContent = m.enabled ? `MQTT ${m.connected ? 'ansluten' : 'ej ansluten'}` : 'MQTT av';
        document.getElementById('foot-engine').textContent = `Transkribering: ${status.transcription_engine || '–'}`;
    }

    async function loadStations() {
        const data = await api('GET', '/stations');
        // A station is frequency + PI: two P4 regions can share a PI
        return data.stations.map((s) => ({
            pi: s.station_key,
            ps: [s.display_name || s.station_ps || s.pi, RDSUI.fmtFreq(s.frequency)].filter(Boolean).join(' · '),
        }));
    }

    /** Node names. The node list is admin-only, so without a login the
     *  names come from the stations (nodes that have sent events). */
    async function loadNodes() {
        try {
            const data = await api('GET', '/nodes');
            return data.nodes.map((n) => n.name);
        } catch (e) {
            if (e.status !== 401 && e.status !== 403) throw e;
            const data = await api('GET', '/stations');
            return [...new Set(data.stations.flatMap((s) => s.nodes || []))].sort();
        }
    }

    // -------------------------------------------------------------- nodes

    function uptime(sec) {
        const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
        return d ? `${d} d ${h} h` : (h ? `${h} h ${m} min` : `${m} min`);
    }

    function nodeCard(n) {
        const state = !n.enabled ? ['', 'Inaktiverad'] : (n.online ? ['ok', 'Online'] : ['warn', 'Offline']);
        const stationLine = (s) => [s.station_name || s.ps || s.pi || 'ingen station än',
            RDSUI.fmtFreq(s.frequency), s.pi && s.ps ? `PI ${s.pi}` : '',
            s.groups_per_sec != null ? `${s.groups_per_sec} grp/s` : '',
            s.ta ? 'TA pågår' : (s.tp ? 'TP' : '')]
            .filter(Boolean).join(' · ');
        const stations = (n.stations || []).map(stationLine);
        const recState = { running: ['ok', 'kör'], starting: ['warn', 'startar'], paused: ['warn', 'kalibreras'],
            error: ['bad', 'fel'], stopped: ['warn', 'stoppad'] };
        const receivers = (n.receivers || []).map((r) => {
            const [cls, label] = recState[r.state] || ['warn', r.state || '?'];
            const tune = [r.gain_text
                    ? `gain ${r.gain_text.replace('(by hand)', '(för hand)').replace(/\(linearity (\d+)\)/, '(linjäritet $1)')}`
                      + (r.gain_mode === 'manual' && !r.gain_text.includes('(') ? ' (fast)' : '')
                    : (r.gain != null ? `gain ${r.gain} dB${r.gain_mode === 'manual' ? ' (fast)' : ''}` : ''),
                r.ppm != null ? `${r.ppm} ppm${r.ppm_mode === 'manual' ? ' (fast)' : ''}` : '',
                r.calibrated_at ? `kalibrerad ${ago(r.calibrated_at)}` : (r.calibration ? r.calibration : '')]
                .filter(Boolean).join(' · ');
            return `<div class="small" style="border-left:2px solid var(--border-strong);padding-left:8px;display:flex;flex-direction:column;gap:2px">
                <div><span class="dot ${cls}"></span> <b>${esc(r.name || r.id || r.serial || 'mottagare')}</b>
                    <span class="muted">${esc(r.serial ? `SN ${r.serial}` : '')} · ${esc(label)}</span></div>
                ${tune ? `<div class="muted">${esc(tune)}</div>` : ''}
                ${(r.stations || []).map((s) => `<div>${esc(stationLine(s))}</div>`).join('')}
                ${r.error ? `<div style="color:var(--warn)">${esc(r.error)}</div>` : ''}
                ${r.gain_mode !== 'manual' && r.serial ? `<div><button class="btn small" type="button" data-action="recalibrate" data-receiver="${esc(r.id || r.serial)}">Kalibrera om</button></div>` : ''}
            </div>`;
        });
        const located = n.located ? `${n.located.name} (${n.located.lat.toFixed(3)}, ${n.located.lon.toFixed(3)})` : '';
        const place = n.location
            ? (located ? `Plats: ${n.location} → ${located}` : `Plats: ${n.location} – hittades inte i ortregistret`)
            : 'Ingen plats: nodens meddelanden placeras inte på kartan';
        const placeFrom = n.location_set ? 'satt här' : (n.location_reported ? 'från noden' : '');
        const warn = [];
        if (n.queue_depth) warn.push(`${n.queue_depth} händelser i kö`);
        if (n.clock_offset_sec != null && Math.abs(n.clock_offset_sec) > 30) {
            warn.push(`klockan går ${Math.round(Math.abs(n.clock_offset_sec))} s fel`);
        }
        if (n.pipeline && n.pipeline.error) warn.push(n.pipeline.error);
        const meta = [`senast sedd ${ago(n.last_heartbeat_at || n.last_seen_at)}`];
        if (n.uptime_sec != null) meta.push(`igång ${uptime(n.uptime_sec)}`);
        if (n.pipeline && n.pipeline.state) meta.push(`mottagare ${n.pipeline.state}`);
        if (n.version) meta.push(n.version);
        meta.push(`token ${n.token_prefix}…`);
        return `<article class="card" data-id="${n.id}" data-name="${esc(n.name)}" style="display:flex;flex-direction:column;gap:8px">
            <div style="display:flex;align-items:center;gap:8px">
                <span class="dot ${state[0]}"></span>
                <h2 style="flex:1;min-width:0;overflow-wrap:anywhere">${esc(n.name)}${n.reported_name && n.reported_name !== n.name
            ? ` <span class="small muted">(${esc(n.reported_name)})</span>` : ''}</h2>
                <span class="pill ${state[0]}">${state[1]}</span>
            </div>
            ${receivers.length ? `<div style="display:flex;flex-direction:column;gap:8px">${receivers.join('')}</div>`
        : `<div class="small">${stations.length ? stations.map((s) => `<div>${esc(s)}</div>`).join('') : '<span class="muted">Inga stationer rapporterade än</span>'}</div>`}
            <div class="small">${esc(place)}${placeFrom ? ` <span class="muted">(${placeFrom})</span>` : ''}</div>
            <div class="small muted">${esc(meta.join(' · '))}</div>
            ${warn.length ? `<div class="small" style="color:var(--warn)">${esc(warn.join(' · '))}</div>` : ''}
            <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:4px">
                <a class="btn small" href="#/sok?nod=${encodeURIComponent(n.name)}">Händelser</a>
                <button class="btn small" type="button" data-action="location" data-location="${esc(n.location_set || n.location || '')}">Ändra plats</button>
                <button class="btn small" type="button" data-action="rotate">Ny token</button>
                <button class="btn small" type="button" data-action="toggle" data-enabled="${n.enabled}">${n.enabled ? 'Inaktivera' : 'Aktivera'}</button>
                <button class="btn small danger" type="button" data-action="remove">Ta bort</button>
            </div>
        </article>`;
    }

    function showToken(root, snippet) {
        const box = root.querySelector('#token');
        box.querySelector('pre').textContent = snippet;
        box.hidden = false;
        box.scrollIntoView({ block: 'nearest' });
    }

    async function nodesView(root, r, ctx) {
        root.innerHTML = `
            <div class="page-head"><div class="grow"><h1>Noder</h1>
                <p>Varje nod är en RDS Guard som skickar sina händelser hit.</p></div>
                <form id="add" style="display:flex;gap:8px;flex:0 1 460px;flex-wrap:wrap">
                    <label style="flex:1 1 220px"><span class="sr-only">Namn på ny nod</span>
                        <input name="name" maxlength="64" required placeholder="Namn, t.ex. Kalle, Karlstad"
                            style="width:100%;height:40px;border:1px solid var(--border-strong);border-radius:10px;padding:0 12px;background:var(--surface)"></label>
                    <button class="btn primary" type="submit">Lägg till nod</button>
                </form></div>
            <section class="notice" id="token" hidden aria-live="polite" style="display:flex;flex-direction:column;gap:10px">
                <strong>Skicka raderna nedan till den som kör noden. Token visas bara en gång – raderna läggs i nodens <code>.env</code>.</strong>
                <pre class="code"></pre>
                <div style="display:flex;gap:8px"><button class="btn primary small" type="button" id="copy">Kopiera</button>
                    <button class="btn small" type="button" id="done">Klar</button></div>
            </section>
            <p class="error-text" id="err" role="alert" hidden></p>
            <section id="pending" hidden style="display:flex;flex-direction:column;gap:10px;margin-bottom:16px">
                <h2>Väntar på godkännande</h2>
                <p class="small muted" style="margin:0">En satellit utan token ber om att få ansluta. Jämför koden med den i satellitens logg
                    (<code>docker compose logs rds-guard | grep -i join</code>) innan du godkänner.</p>
                <div id="pending-list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px"></div>
            </section>
            <div id="list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px"></div>`;
        const list = root.querySelector('#list');
        const err = root.querySelector('#err');
        const fail = (e) => { err.textContent = e.message; err.hidden = false; };

        let knownNodes = [];
        function pendingCard(p) {
            const st = (p.stations || []).map((x) => [x.ps || x.pi, RDSUI.fmtFreq(x.frequency)].filter(Boolean).join(' · '));
            return `<article class="card" data-pending="${p.id}" data-name="${esc(p.node_name || '')}"
                    data-location="${esc(p.suggested_location || '')}" style="display:flex;flex-direction:column;gap:8px">
                <div style="display:flex;align-items:center;gap:8px">
                    <span class="dot warn"></span>
                    <h2 style="flex:1;min-width:0;overflow-wrap:anywhere">${esc(p.node_name || 'Namnlös satellit')}</h2>
                    <span class="pill warn">Kod ${esc(p.verify)}</span>
                </div>
                <div class="small">${st.length ? st.map((x) => `<div>${esc(x)}</div>`).join('') : '<span class="muted">Inga stationer än</span>'}</div>
                <div class="small">${p.suggested_location ? `Föreslagen plats: ${esc(p.suggested_location)}` : '<span class="muted">Ingen plats angiven</span>'}</div>
                <div class="small muted">${esc([p.address, p.version, `frågade ${ago(p.last_seen_at)}`].filter(Boolean).join(' · '))}</div>
                <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:4px">
                    <button class="btn small primary" type="button" data-action="approve">Godkänn</button>
                    <button class="btn small" type="button" data-action="replace">Ersätt befintlig nod</button>
                    <button class="btn small danger" type="button" data-action="reject">Avvisa</button>
                </div>
            </article>`;
        }

        async function loadPending() {
            try {
                const data = await api('GET', '/nodes/pending');
                if (!ctx.alive()) return;
                const box = root.querySelector('#pending');
                box.hidden = !data.pending.length;
                root.querySelector('#pending-list').innerHTML = data.pending.map(pendingCard).join('');
            } catch (e) { /* the node list shows the error */ }
        }

        root.querySelector('#pending-list').addEventListener('click', async (e) => {
            const btn = e.target.closest('button[data-action]');
            if (!btn) return;
            const card = btn.closest('[data-pending]');
            const id = card.dataset.pending;
            err.hidden = true;
            try {
                if (btn.dataset.action === 'reject') {
                    if (!window.confirm('Avvisa satelliten? Den får veta att den är nekad.')) return;
                    await api('POST', `/nodes/pending/${id}/reject`, {});
                } else if (btn.dataset.action === 'replace') {
                    const names = knownNodes.map((n) => n.name);
                    const which = window.prompt(`Vilken nod ska satelliten ersätta? Dess historik behålls och den gamla token slutar fungera.\n\nNoder: ${names.join(', ')}`, '');
                    if (which === null) return;
                    const target = knownNodes.find((n) => n.name.toLowerCase() === which.trim().toLowerCase());
                    if (!target) { fail(new Error(`Ingen nod heter ”${which}”.`)); return; }
                    await api('POST', `/nodes/pending/${id}/approve`, { replace_node_id: target.id });
                    toast(`Satelliten rapporterar nu som ${target.name}`);
                } else {
                    const name = window.prompt('Namn på noden:', card.dataset.name || '');
                    if (name === null) return;
                    const location = window.prompt('Var står den? ”Ort, Län”, t.ex. ”Karlstad, Värmlands län” (tomt = ingen plats än):',
                        card.dataset.location || '');
                    if (location === null) return;
                    await api('POST', `/nodes/pending/${id}/approve`, { name: name.trim(), location: location.trim() || null });
                    toast('Satelliten är godkänd');
                }
                loadPending();
                load();
                poll();
            } catch (x) { fail(x); }
        });

        async function load() {
            try {
                const data = await api('GET', '/nodes');
                knownNodes = data.nodes;
                if (!ctx.alive()) return;
                list.innerHTML = data.nodes.length ? data.nodes.map(nodeCard).join('')
                    : '<div class="empty">Inga noder än. Lägg till en ovan och skicka dess token till den som kör satelliten.</div>';
            } catch (e) { fail(e); }
        }

        root.querySelector('#add').addEventListener('submit', async (e) => {
            e.preventDefault();
            err.hidden = true;
            try {
                const res = await api('POST', '/nodes', { name: e.target.name.value });
                e.target.reset();
                showToken(root, res.snippet);
                load();
                poll();
            } catch (x) { fail(x); }
        });
        root.querySelector('#copy').addEventListener('click', () => {
            const text = root.querySelector('#token pre').textContent;
            if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast('Kopierat')).catch(() => {});
        });
        root.querySelector('#done').addEventListener('click', () => {
            root.querySelector('#token').hidden = true;
            root.querySelector('#token pre').textContent = '';
        });
        list.addEventListener('click', async (e) => {
            const btn = e.target.closest('button[data-action]');
            if (!btn) return;
            const card = btn.closest('[data-id]');
            const { id, name } = card.dataset;
            err.hidden = true;
            try {
                if (btn.dataset.action === 'rotate') {
                    if (!window.confirm(`Skapa en ny token för ”${name}”? Den gamla slutar fungera direkt.`)) return;
                    showToken(root, (await api('POST', `/nodes/${id}/rotate`, {})).snippet);
                } else if (btn.dataset.action === 'location') {
                    const value = window.prompt(`Var står ”${name}”? Skriv ”Ort, Län”, t.ex. ”Karlstad, Värmlands län”. Lämna tomt för att använda platsen noden själv rapporterar.`,
                        btn.dataset.location || '');
                    if (value === null) return;
                    await api('PATCH', `/nodes/${id}`, { location: value.trim() || null });
                    toast('Platsen är sparad');
                } else if (btn.dataset.action === 'recalibrate') {
                    if (!window.confirm(`Kalibrera om gain och ppm för mottagaren ”${btn.dataset.receiver}”? Den är ur drift i ungefär en och en halv minut (aldrig mitt i en sändning).`)) return;
                    await api('POST', `/nodes/${id}/recalibrate`, { receiver: btn.dataset.receiver });
                    toast('Kalibreringen startar vid nodens nästa hjärtslag');
                } else if (btn.dataset.action === 'toggle') {
                    await api('PATCH', `/nodes/${id}`, { enabled: btn.dataset.enabled !== 'true' });
                } else if (btn.dataset.action === 'remove') {
                    if (!window.confirm(`Ta bort ”${name}” och alla händelser den rapporterat? Det går inte att ångra.`)) return;
                    await api('DELETE', `/nodes/${id}`, {});
                }
                load();
                poll();
            } catch (x) { fail(x); }
        });
        await load();
        loadPending();
        const timer = setInterval(() => { if (!document.hidden) { load(); loadPending(); } }, 10000);
        ctx.cleanup = () => clearInterval(timer);
    }

    // ----------------------------------------------------------- stations

    function stationCard(s) {
        const nameFrom = s.name ? 'satt här' : (s.rds_name ? 'från RDS'
            : (s.inherited_name ? `från ${s.inherited_from} (samma frekvens)` : 'PS'));
        const area = s.area
            ? `Område: ${s.area}`
            : s.inherited_area ? `Område: ${s.inherited_area} (från ${s.inherited_from})`
            : 'Inget område: meddelandena läses runt nodens plats';
        return `<article class="card" data-key="${esc(s.station_key)}" style="display:flex;flex-direction:column;gap:8px">
            <div style="display:flex;align-items:center;gap:8px">
                <h2 style="flex:1;min-width:0;overflow-wrap:anywhere">${esc(s.display_name || s.pi)}</h2>
                <span class="pill">${esc(RDSUI.fmtFreq(s.frequency) || '?')}</span>
            </div>
            <div class="small muted">${esc([`namn ${nameFrom}`, `PI ${s.pi}`, s.ps ? `PS ”${s.ps}”` : '',
        s.rds_name && s.name ? `RDS: ${s.rds_name}` : ''].filter(Boolean).join(' · '))}</div>
            <div class="small">${esc(area)}${!s.area && s.suggested_area
            ? ` <span class="muted">(förslag: ${esc(s.suggested_area)})</span>` : ''}</div>
            <div class="small muted">${esc([`${s.events} meddelanden`,
        s.nodes.length ? `hörd av ${s.nodes.join(', ')}` : '',
        `senast hörd ${ago(s.last_seen_at)}`].filter(Boolean).join(' · '))}</div>
            <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:4px">
                <a class="btn small" href="#/sok?pi=${encodeURIComponent(s.station_key)}">Meddelanden</a>
                <button class="btn small" type="button" data-action="name">Ändra namn</button>
                <button class="btn small" type="button" data-action="area">Ändra område</button>
                ${!s.area && s.suggested_area ? `<button class="btn small primary" type="button" data-action="suggested">Använd ${esc(s.suggested_area)}</button>` : ''}
            </div>
        </article>`;
    }

    async function stationsView(root, r, ctx) {
        root.innerHTML = `
            <div class="page-head"><div class="grow"><h1>Stationer</h1>
                <p>En station är en frekvens och en PI-kod: två P4-regioner kan ha samma PI.
                Namnet visas på kartan, i meddelandena och i aviseringarna. Området är där stationens
                trafikmeddelanden placeras på kartan, t.ex. ”Örebro län”.</p></div></div>
            <p class="error-text" id="err" role="alert" hidden></p>
            <div id="list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px"></div>`;
        const list = root.querySelector('#list');
        const err = root.querySelector('#err');
        let stations = [];
        async function load() {
            try {
                stations = (await api('GET', '/stations')).stations;
            } catch (e) {
                err.textContent = e.message;
                err.hidden = false;
                return;
            }
            if (!ctx.alive()) return;
            err.hidden = true;
            list.innerHTML = stations.length ? stations.map(stationCard).join('')
                : '<p class="muted">Inga stationer hörda än.</p>';
        }
        list.addEventListener('click', async (e) => {
            const btn = e.target.closest('button[data-action]');
            if (!btn) return;
            const s = stations.find((x) => x.station_key === btn.closest('[data-key]').dataset.key);
            if (!s) return;
            const body = { key: s.station_key, name: s.name, area: s.area };
            if (btn.dataset.action === 'name') {
                const v = window.prompt(`Namn på stationen på ${RDSUI.fmtFreq(s.frequency)}, t.ex. ”P4 Örebro”. Lämna tomt för att använda namnet från RDS${s.rds_name ? ` (${s.rds_name})` : ''}.`,
                    s.name || s.display_name || '');
                if (v === null) return;
                body.name = v.trim() || null;
            } else if (btn.dataset.action === 'area') {
                const v = window.prompt('Var sänder stationen? Skriv ett län, t.ex. ”Örebro län”, eller ”Ort, Län”. Lämna tomt för att använda nodens plats.',
                    s.area || s.suggested_area || '');
                if (v === null) return;
                body.area = v.trim() || null;
            } else if (btn.dataset.action === 'suggested') {
                body.area = s.suggested_area;
            }
            try {
                await api('PATCH', '/station-settings', body);
                toast('Stationen är sparad');
                load();
            } catch (x) {
                err.textContent = x.message;
                err.hidden = false;
            }
        });
        await load();
    }

    function start() {
        RDSUI.start({
            apiBase: '/api/v1',
            idOf: (ev) => ev.event_uid,
            loadStations,
            loadNodes,
            views: { karta: RDSMap.view },
            extraNav: [RDSMap.nav],
            adminSections: [['noder', 'Noder', nodesView], ['stationer', 'Stationer', stationsView]],
            cardMeta: RDSMap.cardMeta,
            eventExtras: RDSMap.eventExtras,
        });
        poll();
        setInterval(() => { if (!document.hidden) poll(); }, 15000);
    }

    return { start };
})();

document.addEventListener('DOMContentLoaded', Aggregator.start);
