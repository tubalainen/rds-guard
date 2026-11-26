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
        return data.stations.map((s) => ({ pi: s.pi, ps: s.station_ps }));
    }

    async function loadNodes() {
        const data = await api('GET', '/nodes');
        return data.nodes.map((n) => n.name);
    }

    // -------------------------------------------------------------- nodes

    function nodeCard(n) {
        const state = !n.enabled ? ['', 'Inaktiverad'] : (n.online ? ['ok', 'Online'] : ['warn', 'Offline']);
        const stations = (n.stations || []).map((s) => [s.ps || s.pi || 'ingen station än',
            RDSUI.fmtFreq(s.frequency), s.groups_per_sec != null ? `${s.groups_per_sec} grp/s` : '']
            .filter(Boolean).join(' · '));
        const warn = [];
        if (n.queue_depth) warn.push(`${n.queue_depth} händelser i kö`);
        if (n.clock_offset_sec != null && Math.abs(n.clock_offset_sec) > 30) {
            warn.push(`klockan går ${Math.round(Math.abs(n.clock_offset_sec))} s fel`);
        }
        if (n.pipeline && n.pipeline.error) warn.push(n.pipeline.error);
        const meta = [`senast sedd ${ago(n.last_heartbeat_at || n.last_seen_at)}`];
        if (n.version) meta.push(n.version);
        meta.push(`token ${n.token_prefix}…`);
        return `<article class="card" data-id="${n.id}" data-name="${esc(n.name)}" style="display:flex;flex-direction:column;gap:8px">
            <div style="display:flex;align-items:center;gap:8px">
                <span class="dot ${state[0]}"></span>
                <h2 style="flex:1;min-width:0;overflow-wrap:anywhere">${esc(n.name)}${n.reported_name && n.reported_name !== n.name
            ? ` <span class="small muted">(${esc(n.reported_name)})</span>` : ''}</h2>
                <span class="pill ${state[0]}">${state[1]}</span>
            </div>
            <div class="small">${stations.length ? stations.map((s) => `<div>${esc(s)}</div>`).join('') : '<span class="muted">Inga stationer rapporterade än</span>'}</div>
            <div class="small muted">${esc(meta.join(' · '))}</div>
            ${warn.length ? `<div class="small" style="color:var(--warn)">${esc(warn.join(' · '))}</div>` : ''}
            <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:4px">
                <a class="btn small" href="#/sok?nod=${encodeURIComponent(n.name)}">Händelser</a>
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
            <div id="list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px"></div>`;
        const list = root.querySelector('#list');
        const err = root.querySelector('#err');
        const fail = (e) => { err.textContent = e.message; err.hidden = false; };

        async function load() {
            try {
                const data = await api('GET', '/nodes');
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
        const timer = setInterval(() => { if (!document.hidden) load(); }, 10000);
        ctx.cleanup = () => clearInterval(timer);
    }

    function start() {
        RDSUI.start({
            apiBase: '/api/v1',
            idOf: (ev) => ev.event_uid,
            loadStations,
            loadNodes,
            views: { noder: nodesView },
            extraNav: [['noder', 'Noder', RDSUI.ICON.nodes]],
        });
        poll();
        setInterval(() => { if (!document.hidden) poll(); }, 15000);
    }

    return { start };
})();

document.addEventListener('DOMContentLoaded', Aggregator.start);
