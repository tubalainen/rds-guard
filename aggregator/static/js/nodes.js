/**
 * RDS Guard Aggregator — nodes.js
 * Satellite overview and token management.
 */

const Nodes = (() => {
    let timer = null;

    function init() {
        document.getElementById('node-add').addEventListener('submit', async e => {
            e.preventDefault();
            const input = document.getElementById('node-add-name');
            try {
                const res = await Util.api('POST', '/api/v1/nodes', { name: input.value });
                input.value = '';
                showToken(res.snippet);
                load();
            } catch (err) {
                showError(err.message);
            }
        });
        document.getElementById('node-token-copy').addEventListener('click', () => {
            const text = document.getElementById('node-token-snippet').textContent;
            if (navigator.clipboard) navigator.clipboard.writeText(text).catch(() => {});
        });
        document.getElementById('node-token-close').addEventListener('click', () => {
            document.getElementById('node-token').hidden = true;
            document.getElementById('node-token-snippet').textContent = '';
        });
        document.getElementById('nodes-list').addEventListener('click', onAction);
        timer = setInterval(() => {
            if (document.getElementById('view-nodes').classList.contains('active')) load();
        }, 10000);
    }

    function showToken(snippet) {
        document.getElementById('node-error').hidden = true;
        document.getElementById('node-token-snippet').textContent = snippet;
        document.getElementById('node-token').hidden = false;
    }

    function showError(msg) {
        const el = document.getElementById('node-error');
        el.textContent = msg;
        el.hidden = false;
    }

    async function onAction(e) {
        const btn = e.target.closest('button[data-action]');
        if (!btn) return;
        const card = btn.closest('.node-card');
        const id = card.dataset.id;
        const name = card.dataset.name;
        try {
            if (btn.dataset.action === 'rotate') {
                if (!confirm(`Create a new token for "${name}"? The old token stops working immediately.`)) return;
                const res = await Util.api('POST', `/api/v1/nodes/${id}/rotate`);
                showToken(res.snippet);
            } else if (btn.dataset.action === 'toggle') {
                await Util.api('PATCH', `/api/v1/nodes/${id}`, { enabled: btn.dataset.enabled !== 'true' });
            } else if (btn.dataset.action === 'remove') {
                if (!confirm(`Remove "${name}" and all events it reported? This cannot be undone.`)) return;
                await Util.api('DELETE', `/api/v1/nodes/${id}`);
            }
            load();
        } catch (err) {
            showError(err.message);
        }
    }

    async function load() {
        try {
            const data = await Util.api('GET', '/api/v1/nodes');
            const list = document.getElementById('nodes-list');
            list.replaceChildren(...data.nodes.map(renderNode));
            document.getElementById('nodes-empty').hidden = data.nodes.length > 0;
        } catch (e) { /* ignore */ }
    }

    function renderNode(n) {
        const esc = Util.escapeHtml;
        const card = document.createElement('div');
        card.className = 'node-card' + (n.enabled ? '' : ' disabled');
        card.dataset.id = n.id;
        card.dataset.name = n.name;

        const state = !n.enabled ? 'disabled' : (n.online ? 'online' : 'offline');
        let html = `<div class="node-header">
            <span class="node-dot${n.online ? ' online' : ''}" title="${esc(state)}"></span>
            <span class="node-name">${esc(n.name)}</span>`;
        if (n.reported_name && n.reported_name !== n.name) {
            html += `<span class="node-reported">(${esc(n.reported_name)})</span>`;
        }
        html += `<div class="node-actions">
                <button class="console-btn" data-action="rotate">New token</button>
                <button class="console-btn" data-action="toggle" data-enabled="${n.enabled}">${n.enabled ? 'Disable' : 'Enable'}</button>
                <button class="console-btn danger" data-action="remove">Remove</button>
            </div></div>`;

        if (n.stations.length) {
            html += '<div class="node-stations">' + n.stations.map(s => {
                const bits = [s.ps || s.pi || 'no station yet', s.frequency ? s.frequency.replace('M', ' MHz') : null,
                              s.groups_per_sec != null ? `${s.groups_per_sec} grp/s` : null].filter(Boolean);
                return `<span>${esc(bits.join(' · '))}</span>`;
            }).join('') + '</div>';
        }

        const meta = [`${state}`, `last seen ${Util.formatAgo(n.last_seen_at)}`];
        if (n.version) meta.push(`version ${n.version}`);
        if (n.pipeline && n.pipeline.state) meta.push(`pipeline ${n.pipeline.state}`);
        meta.push(`token ${n.token_prefix}…`);
        let metaHtml = meta.map(m => `<span>${esc(m)}</span>`).join('');
        if (n.queue_depth) metaHtml += `<span class="node-warn">${esc(n.queue_depth)} queued</span>`;
        if (n.clock_offset_sec != null && Math.abs(n.clock_offset_sec) > 30) {
            metaHtml += `<span class="node-warn">clock off by ${esc(Math.round(n.clock_offset_sec))}s</span>`;
        }
        if (n.pipeline && n.pipeline.error) {
            metaHtml += `<span class="node-warn">${esc(n.pipeline.error)}</span>`;
        }
        html += `<div class="node-meta">${metaHtml}</div>`;

        card.innerHTML = html;
        return card;
    }

    return { init, load };
})();
