/* Helpdesk — standalone API-first frontend. Talks only to the REST API. */
(() => {
  'use strict';

  const API_BASE =
    window.HELPDESK_API_BASE ||
    (location.origin.startsWith('http') ? location.origin + '/api/v1' : 'http://localhost:8091/api/v1');

  const TOKEN_KEY = 'helpdesk.token';

  const state = {
    token: localStorage.getItem(TOKEN_KEY) || null,
    workspaces: [],
    activeConv: null,
    user: null,
    users: [],
    channels: [],
    conversations: [],
    convTotal: 0,
    activeId: null,
    messages: [],
    view: 'inbox',
    filters: { status: 'open', channel_id: '', assignee_id: '', q: '' },
    pollTimer: null,
  };

  /* ---------------- utilities ---------------- */
  const $ = (sel, root = document) => root.querySelector(sel);
  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'dataset') Object.assign(node.dataset, v);
      else if (k === 'html') node.innerHTML = v;
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  };

  async function api(path, { method = 'GET', body, auth = true } = {}) {
    const headers = { 'Content-Type': 'application/json' };
    if (auth && state.token) headers.Authorization = `Bearer ${state.token}`;
    const res = await fetch(API_BASE + path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (res.status === 401) {
      logout(true);
      throw new Error('Сессия истекла');
    }
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!res.ok) {
      const detail = data && data.detail ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : res.statusText;
      throw new Error(detail);
    }
    return data;
  }

  function toast(message, kind = '') {
    const node = el('div', { class: `toast ${kind}` }, message);
    $('#toast-root').append(node);
    setTimeout(() => node.remove(), 3600);
  }

  function initials(name) {
    if (!name) return '?';
    const parts = String(name).trim().split(/\s+/).slice(0, 2);
    return parts.map((p) => p[0]).join('').toUpperCase();
  }

  function colorFor(seed) {
    const palette = ['#4d6bfe', '#7c3aed', '#db2777', '#ea580c', '#12a150', '#0891b2', '#c47f0a'];
    let h = 0;
    for (const ch of String(seed || '?')) h = (h * 31 + ch.charCodeAt(0)) % 9973;
    return palette[h % palette.length];
  }

  function fmtAbs(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' });
  }
  function fmtTime(iso) {
    if (!iso) return '';
    return new Date(iso).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }
  function fmtRel(iso) {
    if (!iso) return '';
    const diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diff < 60) return 'только что';
    if (diff < 3600) return `${Math.floor(diff / 60)} мин`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} ч`;
    if (diff < 604800) return `${Math.floor(diff / 86400)} дн`;
    return fmtAbs(iso).slice(0, 5);
  }
  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined) return '—';
    const s = Math.round(seconds);
    if (s < 60) return `${s} с`;
    if (s < 3600) return `${Math.floor(s / 60)} мин ${s % 60} с`;
    if (s < 86400) return `${Math.floor(s / 3600)} ч ${Math.floor((s % 3600) / 60)} мин`;
    return `${Math.floor(s / 86400)} дн ${Math.floor((s % 86400) / 3600)} ч`;
  }
  function fmtDay(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    const today = new Date();
    const isToday = d.toDateString() === today.toDateString();
    const yest = new Date(today); yest.setDate(today.getDate() - 1);
    if (isToday) return 'Сегодня';
    if (d.toDateString() === yest.toDateString()) return 'Вчера';
    return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' });
  }

  const STATUS_LABELS = { open: 'Открыт', pending: 'В ожидании', resolved: 'Решён', closed: 'Закрыт' };
  const PRIORITY_LABELS = { low: 'Низкий', normal: 'Обычный', high: 'Высокий', urgent: 'Срочный' };
  const CHANNEL_LABELS = { whatsapp: 'WhatsApp', vk: 'VK', email: 'Email' };

  /* ---------------- auth ---------------- */
  async function login(email, password) {
    const data = await api('/auth/login', { method: 'POST', body: { email, password }, auth: false });
    state.token = data.access_token;
    state.user = data.user;
    localStorage.setItem(TOKEN_KEY, state.token);
  }

  function logout(silent = false) {
    state.token = null;
    state.user = null;
    localStorage.removeItem(TOKEN_KEY);
    if (state.pollTimer) clearInterval(state.pollTimer);
    $('#app').hidden = true;
    $('#login-screen').hidden = false;
    if (!silent) toast('Вы вышли из системы');
  }

  async function restoreSession() {
    if (!state.token) return false;
    try {
      state.user = await api('/auth/me');
      return true;
    } catch {
      state.token = null;
      localStorage.removeItem(TOKEN_KEY);
      return false;
    }
  }

  function showLogin() {
    $('#boot-splash').hidden = true;
    $('#app').hidden = true;
    $('#login-screen').hidden = false;
    $('#login-email').focus();
  }

  function enterApp() {
    $('#boot-splash').hidden = true;
    $('#login-screen').hidden = true;
    $('#app').hidden = false;
    const av = $('#me-avatar');
    av.textContent = initials(state.user.name);
    av.style.background = state.user.avatar_color || colorFor(state.user.name);
    av.title = `${state.user.name} · ${state.user.email} · ${state.user.role}`;
    av.onclick = openPasswordModal;
    // the companies section is a platform-level tool
    if (isSuperadmin()) $('#rail-companies').hidden = false;
    boot();
    handleVkOauthReturn();
  }

  /* ---------------- boot ---------------- */
  async function boot() {
    await Promise.all([loadChannels(), loadUsers()]);
    await loadConversations();
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(() => {
      if (state.view === 'inbox') refreshInbox();
      if (state.view === 'metrics') loadMetrics();
    }, 6000);
  }

  async function loadChannels() {
    state.channels = await api('/channels');
    const sel = $('#filter-channel');
    sel.innerHTML = '<option value="">Все каналы</option>';
    for (const ch of state.channels) {
      sel.append(el('option', { value: ch.id }, `${CHANNEL_LABELS[ch.type] || ch.type} — ${ch.name}`));
    }
    renderChannels();
  }

  async function loadUsers() {
    state.users = await api('/users');
    const sel = $('#filter-assignee');
    sel.innerHTML = '<option value="">Все операторы</option>';
    for (const u of state.users) sel.append(el('option', { value: u.id }, u.name));
    renderTeam();
  }

  /* ---------------- inbox ---------------- */
  async function loadConversations() {
    const p = new URLSearchParams();
    if (state.filters.status) p.set('status', state.filters.status);
    if (state.filters.channel_id) p.set('channel_id', state.filters.channel_id);
    if (state.filters.assignee_id) p.set('assignee_id', state.filters.assignee_id);
    if (state.filters.q) p.set('q', state.filters.q);
    p.set('page_size', '50');
    const data = await api('/conversations?' + p.toString());
    state.conversations = data.items;
    state.convTotal = data.total;
    renderConvList();
    const badge = $('#rail-badge');
    const openCount = data.items.filter((c) => c.status === 'open' || c.status === 'pending').length;
    if (state.filters.status === 'open' && openCount) {
      badge.hidden = false; badge.textContent = openCount > 99 ? '99+' : openCount;
    } else { badge.hidden = true; }
    $('#inbox-count').textContent = `${data.total} диалог(ов)`;
  }

  async function refreshInbox() {
    try {
      await loadConversations();
      if (state.activeId) {
        const conv = state.conversations.find((c) => c.id === state.activeId);
        if (conv) updateThreadHeader(conv);
        await loadMessages(state.activeId, false);
      }
    } catch (e) { /* keep silent on poll */ }
  }

  function renderConvList() {
    const root = $('#conv-list');
    root.innerHTML = '';
    if (!state.conversations.length) {
      root.append(el('div', { class: 'empty-state', style: 'padding:40px 16px' },
        el('div', { class: 'empty-icon' }, '📭'),
        el('h2', {}, 'Нет обращений'),
        el('p', {}, 'Измените фильтры или подождите новых сообщений.')));
      return;
    }
    for (const c of state.conversations) {
      const name = c.contact?.name || c.contact?.email || c.contact?.phone || c.contact?.external_id || 'Клиент';
      const item = el('div', {
        class: 'conv-item' + (c.id === state.activeId ? ' is-active' : ''),
        onclick: () => openConversation(c.id),
      },
        el('div', { class: 'avatar', style: `background:${colorFor(name)}` }, initials(name)),
        el('div', { class: 'conv-main' },
          el('div', { class: 'conv-row' },
            el('div', { class: 'conv-name' }, name),
            el('div', { class: 'conv-time' }, fmtRel(c.last_message_at))),
          el('div', { class: 'conv-preview' },
            (c.last_message_direction === 'outbound' ? '↩ ' : '') + (c.last_message_preview || 'Нет сообщений')),
          el('div', { class: 'conv-tags' },
            el('span', { class: 'chip chip-channel', dataset: { channel: c.channel?.type } },
              CHANNEL_LABELS[c.channel?.type] || c.channel?.type || '—'),
            el('span', { class: 'chip chip-status', dataset: { status: c.status } }, STATUS_LABELS[c.status] || c.status),
            (c.priority !== 'normal' ? el('span', { class: 'chip chip-prio', dataset: { prio: c.priority } }, PRIORITY_LABELS[c.priority]) : null),
            (c.first_response_seconds !== null && c.first_response_seconds !== undefined
              ? el('span', { class: 'chip chip-frt' }, 'FRT ' + fmtDuration(c.first_response_seconds)) : null),
            (c.assignee ? el('span', { class: 'chip chip-status' }, '👤 ' + c.assignee.name) : null)
          )));
      root.append(item);
    }
  }

  /* ---------------- thread ---------------- */
  const isMobile = () => window.matchMedia('(max-width: 900px)').matches;

  function closeThread() {
    $('#app').classList.remove('thread-open');
    state.activeId = null;
    state.messages = [];
    $('#thread').hidden = true;
    $('#thread-empty').hidden = false;
    $('#messages').innerHTML = '';
    renderConvList();
  }

  async function openConversation(id) {
    state.activeId = id;
    renderConvList();
    const conv = state.conversations.find((c) => c.id === id) || await api(`/conversations/${id}`);
    state.activeConv = conv;
    $('#thread-empty').hidden = true;
    $('#thread').hidden = false;
    updateThreadHeader(conv);
    await loadMessages(id, true);
    // on a phone the thread covers the list; on desktop this class is inert
    $('#app').classList.add('thread-open');
    // do not pop the keyboard automatically on touch devices
    if (!isMobile()) $('#composer-input').focus();
  }

  function updateThreadHeader(conv) {
    const name = conv.contact?.name || conv.contact?.email || conv.contact?.phone || conv.contact?.external_id || 'Клиент';
    const av = $('#thread-avatar');
    av.textContent = initials(name);
    av.style.background = colorFor(name);
    $('#thread-name').textContent = name;
    const bits = [];
    if (conv.contact?.email) bits.push(conv.contact.email);
    if (conv.contact?.phone) bits.push(conv.contact.phone);
    if (!bits.length) bits.push(conv.contact?.external_id || '');
    bits.push(`создан ${fmtAbs(conv.created_at)}`);
    $('#thread-sub').textContent = bits.filter(Boolean).join(' · ');

    // the thread header carries the only link to a client's profile
    $('#thread-name').onclick = openContactModal;
    $('#thread-name').title = 'Изменить данные клиента';

    const ch = $('#thread-channel');
    ch.textContent = `${CHANNEL_LABELS[conv.channel?.type] || conv.channel?.type} · ${conv.channel?.name || ''}`;
    ch.dataset.channel = conv.channel?.type || '';

    const frt = $('#thread-frt');
    if (conv.first_response_seconds !== null && conv.first_response_seconds !== undefined) {
      frt.hidden = false;
      frt.textContent = 'Первый ответ: ' + fmtDuration(conv.first_response_seconds);
      frt.className = 'chip chip-frt ' + (conv.first_response_seconds <= 900 ? 'good' : conv.first_response_seconds <= 3600 ? '' : 'bad');
    } else {
      frt.hidden = false;
      frt.textContent = 'Ожидает ответа';
      frt.className = 'chip chip-frt bad';
    }

    const st = $('#thread-status');
    st.innerHTML = '';
    for (const [k, v] of Object.entries(STATUS_LABELS)) st.append(el('option', { value: k, selected: conv.status === k }, v));

    const pr = $('#thread-priority');
    pr.innerHTML = '';
    for (const [k, v] of Object.entries(PRIORITY_LABELS)) pr.append(el('option', { value: k, selected: conv.priority === k }, v));

    const as = $('#thread-assignee');
    as.innerHTML = '<option value="">Не назначен</option>';
    for (const u of state.users) as.append(el('option', { value: u.id, selected: conv.assignee_id === u.id }, u.name));
  }

  async function loadMessages(id, scroll) {
    const data = await api(`/conversations/${id}/messages?limit=300`);
    const prevCount = state.messages.length;
    state.messages = data.items;
    renderMessages(scroll || data.items.length !== prevCount);
  }

  function renderMessages(scroll) {
    const root = $('#messages');
    root.innerHTML = '';
    let lastDay = null;
    for (const m of state.messages) {
      const day = fmtDay(m.created_at);
      if (day !== lastDay) {
        lastDay = day;
        root.append(el('div', { class: 'day-divider' }, `${day}, ${new Date(m.created_at).toLocaleDateString('ru-RU')}`));
      }
      const out = m.direction === 'outbound';
      const author = m.author_name || (out ? 'Оператор' : 'Клиент');
      const bubble = el('div', { class: 'msg-bubble' + (m.status === 'failed' ? ' is-failed' : '') }, m.body || '');
      const meta = el('div', { class: 'msg-meta' },
        el('span', { class: 'msg-author' }, author),
        el('span', {}, fmtTime(m.created_at))
      );
      if (out && m.status) {
        const label = { sent: '✓ отправлено', delivered: '✓✓ доставлено', read: '✓✓ прочитано', failed: '⚠ не отправлено', queued: '… в очереди' }[m.status] || m.status;
        meta.append(el('span', { class: 'msg-status' + (m.status === 'failed' ? ' is-failed' : '') }, label));
      }
      if (m.error) meta.append(el('span', { class: 'msg-status is-failed', title: m.error }, '⚠'));
      root.append(el('div', { class: 'msg ' + (out ? 'msg-out' : 'msg-in') },
        el('div', { class: 'msg-avatar', style: `background:${colorFor(author)}` }, initials(author)),
        el('div', { class: 'msg-bubblewrap' }, meta, bubble)));
    }
    if (scroll) root.scrollTop = root.scrollHeight;
  }

  async function sendMessage() {
    const input = $('#composer-input');
    const text = input.value.trim();
    if (!text || !state.activeId) return;
    const btn = $('#send-btn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';
    try {
      const mark = $('#composer-status').value || null;
      const sent = await api(`/conversations/${state.activeId}/messages`, {
        method: 'POST',
        body: { body: text, mark_status: mark },
      });
      input.value = '';
      if (sent && sent.notice) toast(sent.notice, 'err');
      await loadMessages(state.activeId, true);
      await loadConversations();
      const conv = state.conversations.find((c) => c.id === state.activeId);
      if (conv) updateThreadHeader(conv);
    } catch (e) {
      toast('Не удалось отправить: ' + e.message, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Отправить';
      input.focus();
    }
  }

  async function patchConversation(patch) {
    if (!state.activeId) return;
    try {
      const updated = await api(`/conversations/${state.activeId}`, { method: 'PATCH', body: patch });
      updateThreadHeader(updated);
      await loadConversations();
    } catch (e) { toast(e.message, 'err'); }
  }

  /* ---------------- metrics ---------------- */
  async function loadMetrics() {
    const days = $('#metrics-days').value;
    const sla = $('#metrics-sla').value;
    $('#metrics-sub').textContent = `Период: последние ${days} дн. · Целевое FRT: ${fmtDuration(Number(sla))}`;
    try {
      const [ov, agents, chans, ts] = await Promise.all([
        api(`/metrics/overview?days=${days}&sla_seconds=${sla}`),
        api(`/metrics/agents?days=${days}`),
        api(`/metrics/channels?days=${days}`),
        api(`/metrics/timeseries?days=${days}&interval=day`),
      ]);
      renderMetricCards(ov);
      renderFrtChart(ov.frt_buckets);
      renderTimeseries(ts.points);
      renderAgentsTable(agents);
      renderChannelsTable(chans);
    } catch (e) { toast('Метрики: ' + e.message, 'err'); }
  }

  function metricCard(label, value, hint) {
    return el('div', { class: 'metric-card' },
      el('div', { class: 'label' }, label),
      el('div', { class: 'value', html: value }),
      hint ? el('div', { class: 'hint' }, hint) : null);
  }

  function renderMetricCards(m) {
    const root = $('#metrics-cards');
    root.innerHTML = '';
    root.append(
      metricCard('Обращений за период', String(m.conversations_created), `Всего в системе: ${m.conversations_total}`),
      metricCard('Решено за период', String(m.conversations_resolved), `Открытых сейчас: ${m.conversations_open}`),
      metricCard('Среднее время 1-го ответа', fmtDuration(m.avg_first_response_seconds), `Медиана: ${fmtDuration(m.median_first_response_seconds)}`),
      metricCard('Среднее время решения', fmtDuration(m.avg_resolution_seconds), `Медиана: ${fmtDuration(m.median_resolution_seconds)}`),
      metricCard('SLA FRT', m.sla_compliance_pct === null ? '—' : m.sla_compliance_pct.toFixed(1) + '%',
        `${m.sla_met} из ${m.sla_total} в норме (≤ ${fmtDuration(m.sla_target_seconds)})`),
      metricCard('Бэклог', String(m.backlog), `Без ответственного: ${m.conversations_unassigned}`),
      metricCard('Сообщений', `<small>вх </small>${m.messages_inbound}<small> / исх </small>${m.messages_outbound}`,
        m.avg_messages_per_conversation ? `~${m.avg_messages_per_conversation.toFixed(1)} на диалог` : null),
      metricCard('В ожидании', String(m.conversations_pending), 'Требуют внимания')
    );
  }

  function renderFrtChart(buckets) {
    const root = $('#chart-frt');
    root.innerHTML = '';
    const entries = Object.entries(buckets || {});
    const max = Math.max(1, ...entries.map(([, v]) => v));
    if (!entries.some(([, v]) => v > 0)) {
      root.append(el('div', { class: 'muted' }, 'Нет данных за период'));
      return;
    }
    for (const [label, value] of entries) {
      root.append(el('div', { class: 'bar-row' },
        el('div', { class: 'bar-label' }, label),
        el('div', { class: 'bar-track' }, el('div', { class: 'bar-fill', style: `width:${(value / max) * 100}%` })),
        el('div', { class: 'bar-value' }, String(value))));
    }
  }

  function renderTimeseries(points) {
    const root = $('#chart-timeseries');
    root.innerHTML = '';
    if (!points.length) { root.append(el('div', { class: 'muted' }, 'Нет данных за период')); return; }
    const max = Math.max(1, ...points.map((p) => Math.max(p.created, p.resolved)));
    const spark = el('div', { class: 'spark' });
    for (const p of points) {
      spark.append(el('div', { class: 'spark-col', title: `${p.date}: создано ${p.created}, решено ${p.resolved}` },
        el('div', { class: 'spark-bar created', style: `height:${(p.created / max) * 100}%` }),
        el('div', { class: 'spark-bar resolved', style: `height:${(p.resolved / max) * 100}%` })));
    }
    root.append(spark,
      el('div', { class: 'spark-legend' },
        el('span', { html: '<i style="background:#4d6bfe"></i>Создано' }),
        el('span', { html: '<i style="background:#b8c4ff"></i>Решено' })));
  }

  function renderAgentsTable(agents) {
    const root = $('#table-agents');
    root.innerHTML = '';
    const table = el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, 'Оператор'),
        el('th', { class: 'num' }, 'Активных'),
        el('th', { class: 'num' }, 'Решено'),
        el('th', { class: 'num' }, 'Ответов'),
        el('th', { class: 'num' }, 'Ср. FRT'),
        el('th', { class: 'num' }, 'Ср. ответ'))),
      el('tbody', {}, ...agents.map((a) => el('tr', {},
        el('td', {}, el('span', { class: 'avatar avatar-sm', style: `background:${colorFor(a.name)};display:inline-grid;vertical-align:middle;margin-right:8px;width:24px;height:24px;font-size:10px` }, initials(a.name)), a.name),
        el('td', { class: 'num' }, String(a.active_conversations)),
        el('td', { class: 'num' }, String(a.conversations_resolved)),
        el('td', { class: 'num' }, String(a.messages_outbound)),
        el('td', { class: 'num' }, fmtDuration(a.avg_first_response_seconds)),
        el('td', { class: 'num' }, fmtDuration(a.avg_response_seconds))))));
    root.append(table);
  }

  function renderChannelsTable(channels) {
    const root = $('#table-channels');
    root.innerHTML = '';
    const table = el('table', {},
      el('thead', {}, el('tr', {},
        el('th', {}, 'Канал'),
        el('th', { class: 'num' }, 'Диалогов'),
        el('th', { class: 'num' }, 'Входящих'),
        el('th', { class: 'num' }, 'Исходящих'),
        el('th', { class: 'num' }, 'Ср. FRT'))),
      el('tbody', {}, ...channels.map((c) => el('tr', {},
        el('td', {}, el('span', { class: 'chip chip-channel', dataset: { channel: c.channel_type } }, CHANNEL_LABELS[c.channel_type] || c.channel_type)),
        el('td', { class: 'num' }, String(c.conversations)),
        el('td', { class: 'num' }, String(c.messages_inbound)),
        el('td', { class: 'num' }, String(c.messages_outbound)),
        el('td', { class: 'num' }, fmtDuration(c.avg_first_response_seconds))))));
    root.append(table);
  }

  /* ---------------- channels page ---------------- */
  function renderChannels() {
    const root = $('#channels-list');
    if (!root) return;
    root.innerHTML = '';
    for (const ch of state.channels) {
      const toggle = el('div', { class: 'toggle' + (ch.enabled ? ' on' : ''), onclick: () => toggleChannel(ch) });
      const webhook = `${location.origin.startsWith('http') ? location.origin : 'http://localhost:8091'}/api/v1/webhooks/${ch.type}/${ch.id}`;
      root.append(el('div', { class: 'row-card' },
        el('div', { style: 'flex:1;min-width:240px' },
          el('div', { class: 'title' },
            el('span', { class: 'chip chip-channel', dataset: { channel: ch.type } }, CHANNEL_LABELS[ch.type] || ch.type),
            ch.name),
          el('div', { class: 'sub' }, 'Вебхук: ', el('span', { class: 'code' }, webhook)),
          el('div', { class: 'sub' }, ch.type === 'whatsapp' ? 'Входящие: webhook · Исходящие: sendMessage'
            : ch.type === 'vk' ? 'Входящие: Callback API / Long Poll · Исходящие: messages.send'
            : 'Входящие: IMAP-поллер · Исходящие: SMTP'),
          (ch.type === 'vk' && ch.config && ch.config.needs_send_token)
            ? el('div', { class: 'sub warn-line' },
                'Ответы не уходят: нужен ключ группы ',
                el('button', {
                  class: 'btn btn-ghost btn-sm',
                  onclick: () => openSendTokenModal(ch.id,
                    'VK не принимает токен этого канала для отправки. Вставьте ключ группы.'),
                }, 'Добавить ключ'))
            : null),
        el('div', { style: 'display:flex;align-items:center;gap:12px' },
          el('span', { class: 'muted' }, ch.enabled ? 'включён' : 'выключен'),
          toggle,
          el('button', {
            class: 'btn btn-ghost btn-sm',
            title: 'Удалить канал',
            onclick: () => openChannelDelete(ch),
          }, 'Удалить'))));
    }
  }

  function openChannelDelete(ch) {
    confirmAction({
      title: 'Удалить канал?',
      text: `Канал «${ch.name}» перестанет принимать сообщения. `
          + 'Если в нём есть переписка, потребуется отдельное подтверждение.',
      confirmLabel: 'Удалить',
      danger: true,
      onConfirm: async () => {
        try {
          await api(`/channels/${ch.id}`, { method: 'DELETE' });
          closeModal();
          await loadChannels();
          toast('Канал удалён');
        } catch (err) {
          // 409 means it still holds history -- offer to remove that too
          if (String(err.message).includes('диалогов')) {
            closeModal();
            confirmAction({
              title: 'Удалить вместе с перепиской?',
              text: err.message + ' Вся история переписки будет удалена безвозвратно.',
              confirmLabel: 'Удалить всё',
              danger: true,
              onConfirm: async () => {
                try {
                  await api(`/channels/${ch.id}?force=true`, { method: 'DELETE' });
                  closeModal();
                  await loadChannels();
                  await loadConversations();
                  toast('Канал и переписка удалены');
                } catch (e2) {
                  toast(e2.message, 'err');
                }
              },
            });
          } else {
            toast(err.message, 'err');
          }
        }
      },
    });
  }

  async function toggleChannel(ch) {
    try {
      await api(`/channels/${ch.id}`, { method: 'PATCH', body: { enabled: !ch.enabled } });
      await loadChannels();
      toast(`Канал ${ch.name} ${!ch.enabled ? 'включён' : 'выключен'}`);
    } catch (e) { toast(e.message, 'err'); }
  }

  /* ---------------- team page ---------------- */
  function renderTeam() {
    const root = $('#users-list');
    if (!root) return;
    root.innerHTML = '';
    for (const u of state.users) {
      root.append(el('div', { class: 'row-card' },
        el('div', { style: 'display:flex;align-items:center;gap:12px' },
          el('span', { class: 'avatar', style: `background:${u.avatar_color || colorFor(u.name)}` }, initials(u.name)),
          el('div', {},
            el('div', { class: 'title' }, u.name, u.is_admin ? el('span', { class: 'chip chip-status' }, 'admin') : null),
            el('div', { class: 'sub' }, u.email))),
        el('span', { class: 'chip chip-status', dataset: { status: u.is_active ? 'resolved' : 'closed' } }, u.is_active ? 'активен' : 'отключён')));
    }
  }

  function isSuperadmin() {
    return state.user && state.user.role === 'superadmin';
  }

  async function loadWorkspaces() {
    if (!isSuperadmin()) return;
    state.workspaces = await api('/workspaces');
    renderCompanies();
  }

  function renderCompanies() {
    const root = $('#companies-list');
    if (!root) return;
    root.innerHTML = '';
    if (!state.workspaces.length) {
      root.append(el('p', { class: 'muted' }, 'Компаний пока нет. Создайте первую.'));
      return;
    }
    for (const w of state.workspaces) {
      root.append(el('div', { class: 'row-card' },
        el('div', { style: 'flex:1;min-width:240px' },
          el('div', { class: 'title' }, w.name,
            el('span', { class: 'chip chip-status', dataset: { status: w.is_active ? 'resolved' : 'closed' } },
              w.is_active ? 'активна' : 'отключена')),
          el('div', { class: 'sub' }, 'Код: ', el('span', { class: 'code' }, w.slug),
            ' · создана ', fmtAbs(w.created_at))),
        el('div', { style: 'display:flex;gap:8px' },
          el('button', {
            class: 'btn btn-ghost btn-sm',
            onclick: () => openCompanyUsers(w),
          }, 'Сотрудники'),
          el('button', {
            class: 'btn btn-ghost btn-sm',
            onclick: () => openCompanyDelete(w),
          }, 'Удалить'))));
    }
  }

  function openUserDelete(u, ws) {
    confirmAction({
      title: 'Удалить сотрудника?',
      text: `${u.name} (${u.email}) потеряет доступ. Диалоги, которые он вёл, `
          + 'останутся в системе без ответственного.',
      confirmLabel: 'Удалить',
      danger: true,
      onConfirm: async () => {
        try {
          await api(`/users/${u.id}`, { method: 'DELETE' });
          closeModal();
          await loadUsers();
          toast('Сотрудник удалён');
        } catch (err) {
          toast(err.message, 'err');
        }
      },
    });
  }

  function openCompanyDelete(ws) {
    const form = el('form', { class: 'modal-form' },
      el('h2', {}, 'Удалить компанию?'),
      el('p', { class: 'muted', style: 'font-size:13.5px;line-height:1.55' },
        `Компания «${ws.name}» будет удалена вместе со всеми сотрудниками, `
        + 'каналами и перепиской. Это необратимо.'),
      el('label', { class: 'field' },
        el('span', {}, `Введите код компании для подтверждения: ${ws.slug}`),
        el('input', { name: 'confirm', required: true, placeholder: ws.slug })),
      el('div', { class: 'modal-actions' },
        el('button', { type: 'button', class: 'btn btn-ghost', 'data-close': '' }, 'Отмена'),
        el('button', { type: 'submit', class: 'btn btn-danger' }, 'Удалить')));

    showModal(form);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const typed = new FormData(form).get('confirm');
      const btn = form.querySelector('button[type=submit]');
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>';
      try {
        await api(`/workspaces/${ws.id}?confirm=${encodeURIComponent(typed)}`,
                  { method: 'DELETE' });
        closeModal();
        await loadWorkspaces();
        toast('Компания удалена');
      } catch (err) {
        toast(err.message, 'err');
      } finally {
        btn.disabled = false;
        btn.textContent = 'Удалить';
      }
    });
  }

  async function openCompanyUsers(ws) {
    let users = [];
    try {
      users = await api(`/workspaces/${ws.id}/users`);
    } catch (e) {
      toast(e.message, 'err');
      return;
    }
    const list = el('div', { class: 'stack' },
      ...users.map((u) => el('div', {
        style: 'display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)',
      },
        el('span', { class: 'avatar avatar-sm', style: `background:${colorFor(u.name)}` }, initials(u.name)),
        el('div', { style: 'flex:1' },
          el('div', { style: 'font-weight:600;font-size:13.5px' }, u.name),
          el('div', { class: 'muted', style: 'font-size:12px' }, `${u.email} · ${u.role}`)),
        el('button', {
          class: 'btn btn-ghost btn-sm',
          onclick: () => openUserDelete(u, ws),
        }, 'Удалить'))));

    const form = el('form', { class: 'modal-form' },
      el('h2', {}, `Сотрудники: ${ws.name}`),
      users.length ? list : el('p', { class: 'muted' }, 'Сотрудников нет'),
      el('hr', { style: 'border:none;border-top:1px solid var(--border);margin:16px 0' }),
      el('p', { class: 'muted', style: 'font-size:13px;margin-bottom:12px' }, 'Добавить сотрудника'),
      el('label', { class: 'field' }, el('span', {}, 'Имя'),
        el('input', { name: 'name', required: true })),
      el('label', { class: 'field' }, el('span', {}, 'Email'),
        el('input', { name: 'email', type: 'email', required: true })),
      el('label', { class: 'field' }, el('span', {}, 'Пароль'),
        el('input', { name: 'password', type: 'password', minlength: 6, required: true,
                      value: 'operator12345' })),
      el('label', { class: 'field' }, el('span', {}, 'Роль'),
        el('select', { name: 'role' },
          el('option', { value: 'agent' }, 'Оператор'),
          el('option', { value: 'admin' }, 'Администратор компании'))),
      el('div', { class: 'modal-actions' },
        el('button', { type: 'button', class: 'btn btn-ghost', 'data-close': '' }, 'Закрыть'),
        el('button', { type: 'submit', class: 'btn btn-primary' }, 'Добавить')));

    showModal(form);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await api('/users', {
          method: 'POST',
          body: {
            name: fd.get('name'), email: fd.get('email'),
            password: fd.get('password'), role: fd.get('role'),
            workspace_id: ws.id,
          },
        });
        toast('Сотрудник добавлен');
        openCompanyUsers(ws);
      } catch (err) {
        toast(err.message, 'err');
      }
    });
  }

  function openCompanyModal() {
    const form = el('form', { class: 'modal-form' },
      el('h2', {}, 'Новая компания'),
      el('label', { class: 'field' },
        el('span', {}, 'Название'),
        el('input', { name: 'name', required: true, placeholder: 'ООО «Ромашка»' })),
      el('label', { class: 'field' },
        el('span', {}, 'Короткий код (латиницей)'),
        el('input', { name: 'slug', required: true, pattern: '[a-z0-9_-]+',
                      placeholder: 'romashka' }),
        el('span', { class: 'field-hint' }, 'Используется в ссылках, только строчные латинские буквы, цифры, дефис')),
      el('p', { class: 'muted', style: 'font-size:13px;margin:10px 0 8px' },
        'Администратор компании (сможет создавать сотрудников и подключать каналы)'),
      el('label', { class: 'field' }, el('span', {}, 'Имя'),
        el('input', { name: 'admin_name', placeholder: 'Иван Петров' })),
      el('label', { class: 'field' }, el('span', {}, 'Email'),
        el('input', { name: 'admin_email', type: 'email', required: true })),
      el('label', { class: 'field' }, el('span', {}, 'Пароль'),
        el('input', { name: 'admin_password', type: 'text', minlength: 8,
                      value: Math.random().toString(36).slice(-12) }),
        el('span', { class: 'field-hint' }, 'Покажите его администратору компании')),
      el('div', { class: 'modal-actions' },
        el('button', { type: 'button', class: 'btn btn-ghost', 'data-close': '' }, 'Отмена'),
        el('button', { type: 'submit', class: 'btn btn-primary' }, 'Создать')));

    showModal(form);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const btn = form.querySelector('button[type=submit]');
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>';
      try {
        await api('/workspaces', {
          method: 'POST',
          body: {
            slug: fd.get('slug'),
            name: fd.get('name'),
            admin_name: fd.get('admin_name') || null,
            admin_email: fd.get('admin_email'),
            admin_password: fd.get('admin_password'),
          },
        });
        closeModal();
        await loadWorkspaces();
        toast('Компания создана');
      } catch (err) {
        toast(err.message, 'err');
      } finally {
        btn.disabled = false;
        btn.textContent = 'Создать';
      }
    });
  }

  async function openWebhookLog() {
    let rows = [];
    try {
      rows = await api('/webhooks/logs?limit=50');
    } catch (e) {
      toast(e.message, 'err');
      return;
    }

    const body = rows.length
      ? el('div', { style: 'max-height:52vh;overflow-y:auto' },
          ...rows.map((r) => el('div', {
            style: 'padding:9px 0;border-bottom:1px solid var(--border);font-size:12.5px',
          },
            el('div', { style: 'display:flex;justify-content:space-between;gap:10px;align-items:center' },
              el('span', { class: 'chip chip-channel', dataset: { channel: r.channel_type } },
                CHANNEL_LABELS[r.channel_type] || r.channel_type),
              el('span', {
                class: 'chip chip-status',
                dataset: { status: r.processed ? 'resolved' : 'closed' },
              }, r.processed ? 'принято' : 'ошибка'),
              el('span', { class: 'muted' }, fmtAbs(r.created_at))),
            r.error ? el('div', { class: 'muted', style: 'color:#b42318;margin-top:4px' },
              '⨯ ' + r.error) : null,
            el('div', { class: 'code', style: 'display:block;margin-top:5px;max-height:60px;overflow:hidden' },
              JSON.stringify(r.payload).slice(0, 300)))))
      : el('p', { class: 'muted' }, 'Входящих вебхуков пока не было.');

    showModal(el('div', {},
      el('h2', {}, 'Журнал входящих вебхуков'),
      el('p', { class: 'muted', style: 'font-size:12.5px;margin-bottom:12px' },
        'Сырые запросы от провайдеров. Помогает понять, почему канал молчит.'),
      body,
      el('div', { class: 'modal-actions' },
        el('button', { type: 'button', class: 'btn btn-ghost', 'data-close': '' }, 'Закрыть'))));
  }

  function openContactModal() {
    const conv = state.activeConv;
    if (!conv || !conv.contact) return;
    const c = conv.contact;

    const form = el('form', { class: 'modal-form' },
      el('h2', {}, 'Данные клиента'),
      el('p', { class: 'muted', style: 'font-size:12.5px;margin-bottom:14px' },
        `Канал: ${CHANNEL_LABELS[conv.channel?.type] || conv.channel?.type} · ID: ${c.external_id}`),
      el('label', { class: 'field' },
        el('span', {}, 'Имя'),
        el('input', { name: 'name', value: c.name || '', placeholder: 'Иван Петров' })),
      el('label', { class: 'field' },
        el('span', {}, 'Email'),
        el('input', { name: 'email', value: c.email || '', placeholder: 'client@example.ru' })),
      el('label', { class: 'field' },
        el('span', {}, 'Телефон'),
        el('input', { name: 'phone', value: c.phone || '', placeholder: '+7 999 123-45-67' })),
      el('div', { id: 'ct-result', class: 'modal-result', hidden: true }),
      el('div', { class: 'modal-actions' },
        el('button', { type: 'button', class: 'btn btn-ghost', 'data-close': '' }, 'Отмена'),
        el('button', { type: 'submit', class: 'btn btn-primary' }, 'Сохранить')));

    showModal(form);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const result = $('#ct-result');
      const btn = form.querySelector('button[type=submit]');
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>';
      try {
        const updated = await api(`/contacts/${c.id}`, {
          method: 'PATCH',
          body: {
            name: fd.get('name') || null,
            email: fd.get('email') || null,
            phone: fd.get('phone') || null,
          },
        });
        closeModal();
        // refresh both the thread header and the list
        state.activeConv = { ...conv, contact: { ...c, ...updated } };
        updateThreadHeader(state.activeConv);
        await loadConversations();
        toast('Данные клиента обновлены');
      } catch (err) {
        result.hidden = false;
        result.className = 'modal-result err';
        result.textContent = err.message;
      } finally {
        btn.disabled = false;
        btn.textContent = 'Сохранить';
      }
    });
  }

  function openPasswordModal() {
    const form = el('form', { class: 'modal-form' },
      el('h2', {}, 'Смена пароля'),
      el('label', { class: 'field' },
        el('span', {}, 'Текущий пароль'),
        el('input', { name: 'current', type: 'password', required: true })),
      el('label', { class: 'field' },
        el('span', {}, 'Новый пароль'),
        el('input', { name: 'next', type: 'password', minlength: 8, required: true }),
        el('span', { class: 'field-hint' }, 'Минимум 8 символов')),
      el('label', { class: 'field' },
        el('span', {}, 'Повторите новый пароль'),
        el('input', { name: 'again', type: 'password', minlength: 8, required: true })),
      el('div', { id: 'pw-result', class: 'modal-result', hidden: true }),
      el('div', { class: 'modal-actions' },
        el('button', { type: 'button', class: 'btn btn-ghost', 'data-close': '' }, 'Отмена'),
        el('button', { type: 'submit', class: 'btn btn-primary' }, 'Сменить')));

    showModal(form);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const result = $('#pw-result');
      if (fd.get('next') !== fd.get('again')) {
        result.hidden = false;
        result.className = 'modal-result err';
        result.textContent = 'Новые пароли не совпадают';
        return;
      }
      const btn = form.querySelector('button[type=submit]');
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>';
      try {
        await api('/auth/change-password', {
          method: 'POST',
          body: { current_password: fd.get('current'), new_password: fd.get('next') },
        });
        closeModal();
        toast('Пароль изменён');
      } catch (err) {
        result.hidden = false;
        result.className = 'modal-result err';
        result.textContent = err.message;
      } finally {
        btn.disabled = false;
        btn.textContent = 'Сменить';
      }
    });
  }

  function openChannelModal() {
    const types = [
      { id: 'vk', label: 'VK — сообщения группы' },
      { id: 'whatsapp', label: 'WhatsApp — номерной провайдер' },
      { id: 'email', label: 'Email — ящик поддержки' },
    ];

    const form = el('form', { class: 'modal-form' },
      el('h2', {}, 'Подключить канал'),
      el('label', { class: 'field' },
        el('span', {}, 'Тип канала'),
        el('select', { name: 'type', id: 'ch-type', onchange: () => renderChannelFields() },
          ...types.map((t) => el('option', { value: t.id }, t.label)))),
      el('label', { class: 'field' },
        el('span', {}, 'Название'),
        el('input', { name: 'name', id: 'ch-name', required: true,
                     placeholder: 'Например, Группа поддержки' })),
      el('div', { id: 'ch-fields' }),
      el('div', { id: 'ch-result', class: 'modal-result', hidden: true }),
      el('div', { class: 'modal-actions' },
        el('button', { type: 'button', class: 'btn btn-ghost', id: 'ch-validate' }, 'Проверить'),
        el('button', { type: 'button', class: 'btn btn-ghost', 'data-close': '' }, 'Отмена'),
        el('button', { type: 'submit', class: 'btn btn-primary', id: 'ch-submit' }, 'Подключить')));

    showModal(form);
    renderChannelFields();

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const type = $('#ch-type').value;
      const cfg = collectChannelConfig(type);
      const result = $('#ch-result');
      const btn = $('#ch-submit');
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>';
      try {
        await api('/channels', {
          method: 'POST',
          body: { type, name: $('#ch-name').value, enabled: true, config: cfg },
        });
        closeModal();
        await loadChannels();
        toast('Канал подключён');
      } catch (err) {
        result.hidden = false;
        result.className = 'modal-result err';
        result.textContent = err.message;
      } finally {
        btn.disabled = false;
        btn.textContent = 'Подключить';
      }
    });

    $('#ch-validate').addEventListener('click', async () => {
      const type = $('#ch-type').value;
      const cfg = collectChannelConfig(type);
      const result = $('#ch-result');
      result.hidden = false;
      result.className = 'modal-result';
      result.textContent = 'Проверяю…';
      try {
        const r = await api('/channels/validate', {
          method: 'POST',
          body: { type, name: $('#ch-name').value || 'probe', enabled: true, config: cfg },
        });
        result.className = 'modal-result ' + (r.ok ? 'ok' : 'err');
        result.textContent = r.detail || (r.ok ? 'Всё в порядке' : 'Не удалось проверить');
      } catch (err) {
        result.className = 'modal-result err';
        result.textContent = err.message;
      }
    });
  }

  /* ---------------- VK OAuth (Authorization Code Flow) ----------------
     VK returns a community token straight to the callback, which saves the
     channel server side. The admin only points at a community here: a link,
     a club123 handle or a numeric id. The manual token form below stays as
     the fallback. */
  async function startVkOAuth() {
    const groupInput = $('#ch-vk-group');
    const group = groupInput ? groupInput.value.trim() : '';
    const result = $('#ch-result');
    if (!group) {
      if (result) {
        result.hidden = false;
        result.className = 'modal-result err';
        result.textContent = 'Укажите ссылку на группу ВК';
      }
      if (groupInput) groupInput.focus();
      return;
    }
    const btn = $('#ch-vk-oauth');
    if (btn) { btn.disabled = true; btn.textContent = 'Перехожу в VK…'; }
    try {
      const r = await api('/channels/vk/oauth/start', {
        method: 'POST',
        body: { group, name: ($('#ch-name').value || '').trim() || null },
      });
      window.location.href = r.authorize_url;
    } catch (e) {
      if (btn) { btn.disabled = false; btn.textContent = 'Подключить через ВК'; }
      if (result) {
        result.hidden = false;
        result.className = 'modal-result err';
        result.textContent = e.message;
      } else {
        toast(e.message, 'err');
      }
    }
  }

  function handleVkOauthReturn() {
    const params = new URLSearchParams(location.search);
    const status = params.get('vk_oauth');
    if (!status) return;
    const reason = params.get('reason');
    const warn = params.get('warn');
    const channelId = params.get('channel_id');
    // drop the query so an accidental refresh does not repeat the action
    history.replaceState(null, '', location.pathname);
    if (status === 'error') {
      toast(reason || 'Не удалось подключиться через ВК', 'err');
      return;
    }
    loadChannels();
    if (warn && channelId) {
      // Receiving works, sending does not: VK ID community tokens are
      // refused on the messages namespace. Ask for a group key to send with.
      openSendTokenModal(channelId, warn);
    } else {
      toast('Группа подключена через ВК');
    }
  }

  function openSendTokenModal(channelId, warn) {
    const form = el('form', { class: 'modal-form' },
      el('h2', {}, 'Ключ для ответов'),
      el('div', { class: 'modal-result err' }, warn),
      el('label', { class: 'field' },
        el('span', {}, 'Ключ доступа группы'),
        el('input', { id: 'vk-send-token', placeholder: 'начинается на vk1.a.…' }),
        el('span', { class: 'field-hint' },
          'Создаётся в самой группе: Управление → Работа с API → Создать ключ. '
          + 'Отметьте «Сообщения сообщества» и «Управление сообществом». '
          + 'Приём сообщений уже работает, ключ нужен только для отправки.')),
      el('div', { class: 'modal-actions' },
        el('button', { type: 'button', class: 'btn btn-ghost', 'data-close': '' }, 'Позже'),
        el('button', { type: 'submit', class: 'btn btn-primary', id: 'vk-send-save' }, 'Сохранить')));
    showModal(form);

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('#vk-send-save');
      const token = ($('#vk-send-token').value || '').trim();
      if (!token) { toast('Вставьте ключ', 'err'); return; }
      btn.disabled = true; btn.textContent = 'Проверяю…';
      try {
        await api('/channels/vk/oauth/send-token', {
          method: 'POST',
          body: { channel_id: Number(channelId), access_token: token },
        });
        closeModal();
        toast('Ключ сохранён, ответы будут уходить от имени группы');
        loadChannels();
      } catch (err) {
        btn.disabled = false; btn.textContent = 'Сохранить';
        toast(err.message, 'err');
      }
    });
  }

  function channelField(name, label, placeholder, hint, type = 'text') {
    return el('label', { class: 'field' },
      el('span', {}, label),
      el('input', { name, placeholder, type }),
      hint ? el('span', { class: 'field-hint' }, hint) : null);
  }

  function renderChannelFields() {
    const type = $('#ch-type').value;
    const root = $('#ch-fields');
    root.innerHTML = '';

    if (type === 'vk') {
      root.append(
        el('div', { class: 'oauth-block' },
          el('label', { class: 'field' },
            el('span', {}, 'Ссылка на группу ВК'),
            el('input', { id: 'ch-vk-group', placeholder: 'vk.com/club123456789' }),
            el('span', { class: 'field-hint' },
              'Вставьте ссылку на сообщество или его числовой ID. В окне VK '
              + 'подтвердите доступ — токен группы сохранится автоматически.')),
          el('div', { class: 'oauth-row' },
            el('button', { type: 'button', class: 'btn btn-primary', id: 'ch-vk-oauth' },
              'Подключить через ВК'),
            el('span', { class: 'oauth-note' },
              'Понадобятся права администратора группы')),
          el('div', { class: 'oauth-divider' }, el('span', {}, 'или подключить вручную'))),
        channelField('group_id', 'ID группы VK', 'например, 123456789',
          'Числа из адреса группы: vk.com/club123456789 → 123456789. ' +
          'Либо Управление → Настройки → Адрес.'),
        channelField('access_token', 'Токен доступа', 'начинается на vk1.a.…',
          'Управление группой → Работа с API → Создать ключ.'),
        el('div', { class: 'modal-hint' },
          el('strong', {}, 'Обязательно отметьте при создании ключа:'),
          el('br'),
          '• «Сообщения сообщества» (messages) — без него бот не увидит обращения',
          el('br'),
          '• «Управление сообществом» (manage) — нужно, чтобы включить приём сообщений',
          el('br'), el('br'),
          'Также в самой группе должно быть включено: ',
          el('strong', {}, 'Управление → Сообщения → Сообщения сообщества'),
          el('br'), el('br'),
          'Приём сообщений (Long Poll) включится автоматически при подключении. ' +
          'Если прав manage не будет — форма скажет об этом, и можно включить вручную: ' +
          'Управление → Работа с API → Long Poll API.',
        ),
      );
      $('#ch-vk-oauth').addEventListener('click', startVkOAuth);
    } else if (type === 'whatsapp') {
      root.append(
        channelField('base_url', 'Адрес API провайдера', 'https://api.ваш-провайдер.ru'),
        channelField('token', 'Токен провайдера', 'из личного кабинета провайдера'),
        channelField('send_path', 'Путь отправки', '/sendMessage'),
        el('div', { class: 'modal-hint' },
          'Входящие придут на URL вебхука со страницы каналов. Провайдер должен отправлять туда POST с сообщениями.'),
      );
    } else {
      root.append(
        channelField('smtp_host', 'SMTP сервер (исходящие)', 'smtp.вашдомен.ru'),
        channelField('smtp_port', 'SMTP порт', '465', null, 'number'),
        channelField('smtp_user', 'Логин почты', 'support@вашдомен.ru'),
        channelField('smtp_password', 'Пароль почты', '', null, 'password'),
        channelField('imap_host', 'IMAP сервер (входящие)', 'imap.example.com'),
        channelField('imap_port', 'IMAP порт', '993', null, 'number'),
      );
    }
  }

  function collectChannelConfig(type) {
    const cfg = {};
    const names = type === 'vk'
      ? ['group_id', 'access_token']
      : type === 'whatsapp'
        ? ['base_url', 'token', 'send_path']
        : ['smtp_host', 'smtp_port', 'smtp_user', 'smtp_password',
           'imap_host', 'imap_port'];
    for (const n of names) {
      const inp = document.querySelector(`#ch-fields [name="${n}"]`);
      if (inp && inp.value.trim()) {
        cfg[n] = n.endsWith('_port') ? Number(inp.value) : inp.value.trim();
      }
    }
    return cfg;
  }

  function openUserModal() {
    const form = el('form', { class: 'modal-form' },
      el('h2', {}, 'Новый оператор'),
      el('label', { class: 'field' }, el('span', {}, 'Имя'), el('input', { name: 'name', required: true })),
      el('label', { class: 'field' }, el('span', {}, 'Email'), el('input', { name: 'email', type: 'email', required: true })),
      el('label', { class: 'field' }, el('span', {}, 'Пароль'), el('input', { name: 'password', type: 'password', minlength: 6, required: true, value: 'agent12345' })),
      el('div', { class: 'modal-actions' },
        el('button', { type: 'button', class: 'btn btn-ghost', 'data-close': '' }, 'Отмена'),
        el('button', { type: 'submit', class: 'btn btn-primary' }, 'Создать')));
    showModal(form);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await api('/users', { method: 'POST', body: { name: fd.get('name'), email: fd.get('email'), password: fd.get('password') } });
        closeModal();
        await loadUsers();
        toast('Оператор добавлен');
      } catch (err) { toast(err.message, 'err'); }
    });
  }

  function confirmAction({ title, text, confirmLabel, danger, onConfirm }) {
    const form = el('form', { class: 'modal-form' },
      el('h2', {}, title),
      el('p', { class: 'muted', style: 'font-size:13.5px;line-height:1.55' }, text),
      el('div', { class: 'modal-actions' },
        el('button', { type: 'button', class: 'btn btn-ghost', 'data-close': '' }, 'Отмена'),
        el('button', {
          type: 'submit',
          class: 'btn ' + (danger ? 'btn-danger' : 'btn-primary'),
        }, confirmLabel)));
    showModal(form);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = form.querySelector('button[type=submit]');
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>';
      try {
        await onConfirm();
      } finally {
        btn.disabled = false;
        btn.textContent = confirmLabel;
      }
    });
  }

  function showModal(node) {
    const root = $('#modal-root');
    $('#modal-body').innerHTML = '';
    $('#modal-body').append(node);
    root.hidden = false;
  }
  function closeModal() { $('#modal-root').hidden = true; }

  /* ---------------- navigation ---------------- */
  function switchView(view) {
    if (view !== 'inbox' && $('#app').classList.contains('thread-open')) {
      closeThread();
    }
    state.view = view;
    for (const v of ['inbox', 'metrics', 'channels', 'team', 'companies']) {
      $(`#view-${v}`).hidden = v !== view;
    }
    document.querySelectorAll('.rail-btn[data-view]').forEach((b) => {
      b.classList.toggle('is-active', b.dataset.view === view);
    });
    if (view === 'metrics') loadMetrics();
    if (view === 'channels') loadChannels();
    if (view === 'team') loadUsers();
    if (view === 'companies') loadWorkspaces();
  }

  /* ---------------- events ---------------- */
  function bindEvents() {
    $('#login-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('#login-btn');
      const err = $('#login-error');
      err.hidden = true;
      btn.disabled = true; btn.textContent = 'Вход…';
      try {
        await login($('#login-email').value, $('#login-password').value);
        enterApp();
      } catch (e2) {
        err.textContent = e2.message || 'Ошибка входа';
        err.hidden = false;
      } finally {
        btn.disabled = false; btn.textContent = 'Войти';
      }
    });

    $('#logout-btn').addEventListener('click', () => logout());
    document.querySelectorAll('.rail-btn[data-view]').forEach((b) => {
      b.addEventListener('click', () => switchView(b.dataset.view));
    });

    let searchTimer = null;
    $('#search-input').addEventListener('input', (e) => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { state.filters.q = e.target.value.trim(); loadConversations(); }, 300);
    });
    $('#filter-status').addEventListener('change', (e) => { state.filters.status = e.target.value; loadConversations(); });
    $('#filter-channel').addEventListener('change', (e) => { state.filters.channel_id = e.target.value; loadConversations(); });
    $('#filter-assignee').addEventListener('change', (e) => { state.filters.assignee_id = e.target.value; loadConversations(); });

    $('#thread-status').addEventListener('change', (e) => patchConversation({ status: e.target.value }));
    $('#thread-priority').addEventListener('change', (e) => patchConversation({ priority: e.target.value }));
    $('#thread-assignee').addEventListener('change', (e) => patchConversation({ assignee_id: e.target.value ? Number(e.target.value) : null }));
    $('#assign-me').addEventListener('click', () => patchConversation({ assignee_id: state.user.id }));

    $('#thread-back').addEventListener('click', closeThread);
    $('#send-btn').addEventListener('click', sendMessage);
    $('#composer-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });

    $('#metrics-days').addEventListener('change', loadMetrics);
    $('#metrics-sla').addEventListener('change', loadMetrics);
    $('#add-user-btn').addEventListener('click', openUserModal);
    $('#add-channel-btn').addEventListener('click', openChannelModal);
    $('#webhook-log-btn').addEventListener('click', openWebhookLog);
    $('#add-company-btn').addEventListener('click', openCompanyModal);

    $('#modal-root').addEventListener('click', (e) => {
      if (e.target.dataset.close !== undefined) closeModal();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        closeModal();
        if (isMobile() && $('#app').classList.contains('thread-open')) closeThread();
      }
    });
  }

  /* ---------------- start ---------------- */
  async function start() {
    bindEvents();
    $('#filter-status').value = state.filters.status;
    const ok = await restoreSession();
    if (ok) enterApp();
    else showLogin();
  }

  document.addEventListener('DOMContentLoaded', start);
})();
