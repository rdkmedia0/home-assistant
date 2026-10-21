// SPDX-FileCopyrightText: 2026 Meshtastic Home Assistant contributors
//
// SPDX-License-Identifier: MIT

// Mesh Chat panel: read and send Meshtastic channel and direct messages through a gateway.
// Plain web component without external dependencies, styled with Home Assistant theme variables.

const STORAGE_KEY = "meshtastic-chat";
const HISTORY_LIMIT = 200;
const TARGET_REFRESH_MS = 120 * 1000;

const STATUS_ICONS = {
  sending: { icon: "mdi:clock-outline", label: "Sending" },
  sent: { icon: "mdi:check", label: "Sent – heard by the mesh" },
  delivered: { icon: "mdi:check-all", label: "Delivered – acknowledged by the recipient" },
  no_ack: { icon: "mdi:help-circle-outline", label: "Sent – no acknowledgement received" },
  failed: { icon: "mdi:alert-circle-outline", label: "Failed" },
};

const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const utf8Length = (text) => new TextEncoder().encode(text).length;

const loadState = () => {
  try {
    return JSON.parse(window.localStorage.getItem(STORAGE_KEY)) || {};
  } catch (e) {
    return {};
  }
};

const saveState = (state) => {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (e) {
    // storage unavailable, the panel works without it
  }
};

const avatarHue = (seed) => {
  let hash = 0;
  for (const c of String(seed)) hash = (hash * 31 + c.charCodeAt(0)) | 0;
  return Math.abs(hash) % 360;
};

class MeshtasticChatPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._gateways = [];
    this._entryId = null;
    this._conversation = null;
    this._messages = new Map(); // `${entryId}|${conversation}` -> message[]
    this._filter = "";
    this._showThread = false;
    this._persisted = loadState();
    this._persisted.lastRead = this._persisted.lastRead || {};
    this._refreshTimer = null;
    this._unsubscribe = null;
    this._initialized = false;
    this._loadError = null;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (this._menuButton) this._menuButton.hass = hass;
    if (first) this._init();
  }

  get hass() {
    return this._hass;
  }

  set narrow(narrow) {
    this._narrow = narrow;
    if (this._menuButton) this._menuButton.narrow = narrow;
    this._applyLayout();
  }

  set panel(panel) {
    this._panel = panel;
  }

  set route(route) {
    this._route = route;
  }

  connectedCallback() {
    if (this._hass && this._initialized && !this._unsubscribe) {
      this._subscribe();
      this._loadGateways();
    }
    this._startRefresh();
  }

  disconnectedCallback() {
    this._stopRefresh();
    if (this._unsubscribe) {
      this._unsubscribe.then((unsub) => unsub()).catch(() => {});
      this._unsubscribe = null;
    }
  }

  // ---------- data ----------

  async _init() {
    this._renderSkeleton();
    this._initialized = true;
    await this._loadGateways();
    this._subscribe();
    this._startRefresh();
    this._hass.connection.addEventListener("ready", () => {
      this._loadGateways();
      this._reloadOpenConversation();
    });
  }

  _startRefresh() {
    if (this._refreshTimer || !this._initialized) return;
    this._refreshTimer = window.setInterval(() => this._loadGateways(), TARGET_REFRESH_MS);
  }

  _stopRefresh() {
    if (this._refreshTimer) window.clearInterval(this._refreshTimer);
    this._refreshTimer = null;
  }

  async _loadGateways() {
    try {
      this._gateways = await this._hass.callWS({ type: "meshtastic/chat/gateways" });
      this._loadError = null;
    } catch (err) {
      this._loadError = err.message || String(err);
      this._gateways = [];
    }

    if (!this._gateways.some((g) => g.entry_id === this._entryId)) {
      const remembered = this._persisted.entryId;
      const gateway = this._gateways.find((g) => g.entry_id === remembered) || this._gateways[0];
      this._entryId = gateway ? gateway.entry_id : null;
      this._conversation = null;
    }

    if (this._entryId && !this._conversation) {
      const gateway = this._gateway();
      const remembered = (this._persisted.conversation || {})[this._entryId];
      const known = remembered && this._conversationInfo(gateway, remembered);
      this._selectConversation(known ? remembered : this._defaultConversation(gateway), { showThread: false });
    }
    this._render();
  }

  _subscribe() {
    if (this._unsubscribe) return;
    this._unsubscribe = this._hass.connection.subscribeMessage((update) => this._onUpdate(update), {
      type: "meshtastic/chat/subscribe",
    });
    this._unsubscribe.catch(() => {
      this._unsubscribe = null;
    });
  }

  _onUpdate(update) {
    const gateway = this._gateways.find((g) => g.entry_id === update.entry_id);
    const key = `${update.entry_id}|${update.conversation}`;

    if (update.kind === "cleared") {
      this._messages.delete(key);
      if (gateway) delete gateway.conversations[update.conversation];
      this._render();
      return;
    }

    const message = update.message;
    const cached = this._messages.get(key);
    if (cached) {
      const index = cached.findIndex((m) => m.id === message.id);
      if (index >= 0) cached[index] = message;
      else cached.push(message);
    }

    if (gateway && update.kind === "message") {
      const summary = gateway.conversations[update.conversation] || { count: 0 };
      gateway.conversations[update.conversation] = {
        ts: message.ts,
        text: message.text,
        direction: message.direction,
        from_short: message.from_short,
        count: summary.count + 1,
      };
      if (!this._conversationInfo(gateway, update.conversation)) {
        // message from a node the gateway did not know when the list was loaded
        this._loadGateways();
      }
    }

    const isOpen = update.entry_id === this._entryId && update.conversation === this._conversation;
    if (isOpen && this._isThreadVisible()) this._markRead();
    this._renderList();
    if (isOpen) this._renderMessages({ stickToBottom: true });
  }

  async _loadHistory(entryId, conversation, before) {
    const key = `${entryId}|${conversation}`;
    const request = { type: "meshtastic/chat/history", entry_id: entryId, conversation, limit: HISTORY_LIMIT };
    if (before) request.before = before;
    try {
      const messages = await this._hass.callWS(request);
      const existing = before ? this._messages.get(key) || [] : [];
      this._messages.set(key, [...messages, ...existing]);
    } catch (err) {
      this._messages.set(key, this._messages.get(key) || []);
      this._showError(err.message || String(err));
    }
  }

  async _reloadOpenConversation() {
    if (!this._entryId || !this._conversation) return;
    await this._loadHistory(this._entryId, this._conversation);
    this._renderMessages({ stickToBottom: true });
  }

  // ---------- helpers ----------

  _gateway() {
    return this._gateways.find((g) => g.entry_id === this._entryId);
  }

  _defaultConversation(gateway) {
    if (!gateway) return null;
    const primary = gateway.channels.find((c) => c.primary) || gateway.channels[0];
    return primary ? primary.conversation : null;
  }

  _conversationInfo(gateway, conversation) {
    if (!gateway || !conversation) return null;
    const channel = gateway.channels.find((c) => c.conversation === conversation);
    if (channel) return { kind: "channel", ...channel };
    const node = gateway.nodes.find((n) => n.conversation === conversation);
    if (node) return { kind: "node", ...node };
    if (conversation.startsWith("node:")) {
      const num = Number(conversation.slice(5));
      const userId = `!${num.toString(16).padStart(8, "0")}`;
      return { kind: "node", num, user_id: userId, long_name: userId, short_name: userId.slice(-4), conversation };
    }
    return null;
  }

  _isUnread(gateway, conversation) {
    const summary = gateway.conversations[conversation];
    if (!summary || summary.direction !== "in") return false;
    const lastRead = (this._persisted.lastRead[gateway.entry_id] || {})[conversation] || 0;
    return summary.ts > lastRead;
  }

  _markRead() {
    const gateway = this._gateway();
    if (!gateway || !this._conversation) return;
    const summary = gateway.conversations[this._conversation];
    const lastRead = (this._persisted.lastRead[gateway.entry_id] = this._persisted.lastRead[gateway.entry_id] || {});
    lastRead[this._conversation] = summary ? summary.ts : Date.now() / 1000;
    saveState(this._persisted);
  }

  _isThreadVisible() {
    return !this._narrow || this._showThread;
  }

  _language() {
    return (this._hass && this._hass.locale && this._hass.locale.language) || navigator.language;
  }

  _formatTime(ts) {
    return new Date(ts * 1000).toLocaleTimeString(this._language(), { hour: "2-digit", minute: "2-digit" });
  }

  _formatDay(ts) {
    const date = new Date(ts * 1000);
    const today = new Date();
    const yesterday = new Date();
    yesterday.setDate(today.getDate() - 1);
    if (date.toDateString() === today.toDateString()) return "Today";
    if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
    return date.toLocaleDateString(this._language(), { weekday: "long", day: "numeric", month: "long" });
  }

  _formatAgo(ts) {
    if (!ts) return "";
    const seconds = Math.max(0, Date.now() / 1000 - ts);
    if (seconds < 60) return "now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
    return `${Math.floor(seconds / 86400)}d`;
  }

  _avatar(seed, label, extraClass = "") {
    const hue = avatarHue(seed);
    return `<span class="avatar ${extraClass}" style="--avatar-hue:${hue}" aria-hidden="true">${escapeHtml(
      String(label || "?").slice(0, 4)
    )}</span>`;
  }

  // ---------- actions ----------

  async _selectConversation(conversation, { showThread = true } = {}) {
    this._conversation = conversation;
    this._showThread = showThread;
    if (this._entryId && conversation) {
      this._persisted.entryId = this._entryId;
      this._persisted.conversation = this._persisted.conversation || {};
      this._persisted.conversation[this._entryId] = conversation;
      saveState(this._persisted);
    }
    this._applyLayout();
    this._renderList();
    this._renderThread();
    if (!this._entryId || !conversation) return;

    const key = `${this._entryId}|${conversation}`;
    if (!this._messages.has(key)) {
      await this._loadHistory(this._entryId, conversation);
    }
    if (this._conversation === conversation) {
      if (this._isThreadVisible()) this._markRead();
      this._renderList();
      this._renderMessages({ stickToBottom: true });
      if (showThread && !this._narrow) this._textarea.focus();
    }
  }

  async _send() {
    const gateway = this._gateway();
    const text = this._textarea.value.trim();
    if (!gateway || !this._conversation || !text || this._sending) return;
    if (this._composedLength(text) > gateway.max_bytes) return;

    this._sending = true;
    this._updateComposer();
    try {
      const message = await this._hass.callWS({
        type: "meshtastic/chat/send",
        entry_id: gateway.entry_id,
        conversation: this._conversation,
        text,
      });
      // normally the subscription delivers the message first; this covers a missing subscription
      const cached = this._messages.get(`${gateway.entry_id}|${this._conversation}`);
      if (cached && !cached.some((m) => m.id === message.id)) {
        cached.push(message);
        this._renderMessages({ stickToBottom: true });
      }
      this._textarea.value = "";
      this._autoGrow();
    } catch (err) {
      this._showError(err.message || String(err));
    } finally {
      this._sending = false;
      this._updateComposer();
      this._textarea.focus();
    }
  }

  async _clearConversation() {
    const gateway = this._gateway();
    const info = this._conversationInfo(gateway, this._conversation);
    if (!gateway || !info) return;
    const name = info.kind === "channel" ? info.name : info.long_name;
    // eslint-disable-next-line no-alert
    if (!window.confirm(`Delete the stored history of "${name}" in Home Assistant? Messages on devices are not affected.`)) {
      return;
    }
    try {
      await this._hass.callWS({ type: "meshtastic/chat/clear", entry_id: gateway.entry_id, conversation: this._conversation });
    } catch (err) {
      this._showError(err.message || String(err));
    }
  }

  async _loadOlder() {
    const key = `${this._entryId}|${this._conversation}`;
    const messages = this._messages.get(key) || [];
    if (!messages.length) return;
    const scroller = this._messagesEl;
    const previousHeight = scroller.scrollHeight;
    await this._loadHistory(this._entryId, this._conversation, messages[0].ts);
    this._renderMessages();
    scroller.scrollTop = scroller.scrollHeight - previousHeight;
  }

  _composedLength(text) {
    const gateway = this._gateway();
    const prefix = gateway && gateway.prefix ? `${gateway.prefix}: ` : "";
    return utf8Length(prefix + text);
  }

  _showError(message) {
    this._errorEl.textContent = message;
    this._errorEl.hidden = false;
    window.clearTimeout(this._errorTimer);
    this._errorTimer = window.setTimeout(() => {
      this._errorEl.hidden = true;
    }, 8000);
  }

  _autoGrow() {
    const textarea = this._textarea;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`;
  }

  // ---------- rendering ----------

  _renderSkeleton() {
    this.shadowRoot.innerHTML = `
      <style>${MeshtasticChatPanel.styles}</style>
      <div class="toolbar">
        <ha-menu-button></ha-menu-button>
        <div class="title">Mesh Chat</div>
        <select class="gateway-select" aria-label="Gateway" hidden></select>
      </div>
      <div class="layout">
        <aside class="sidebar">
          <div class="search">
            <ha-icon icon="mdi:magnify"></ha-icon>
            <input type="search" placeholder="Search channels and nodes" aria-label="Search channels and nodes" />
          </div>
          <nav class="conversations" aria-label="Conversations"></nav>
        </aside>
        <main class="thread">
          <header class="thread-header"></header>
          <div class="messages" role="log" aria-live="polite"></div>
          <div class="error" role="alert" hidden></div>
          <form class="composer">
            <div class="composer-field">
              <textarea rows="1" aria-label="Message"></textarea>
              <span class="counter"></span>
            </div>
            <button type="submit" class="send" aria-label="Send">
              <ha-icon icon="mdi:send"></ha-icon>
            </button>
          </form>
        </main>
      </div>
    `;

    const root = this.shadowRoot;
    this._menuButton = root.querySelector("ha-menu-button");
    this._menuButton.hass = this._hass;
    this._menuButton.narrow = this._narrow;
    this._gatewaySelect = root.querySelector(".gateway-select");
    this._layoutEl = root.querySelector(".layout");
    this._listEl = root.querySelector(".conversations");
    this._headerEl = root.querySelector(".thread-header");
    this._messagesEl = root.querySelector(".messages");
    this._errorEl = root.querySelector(".error");
    this._form = root.querySelector(".composer");
    this._textarea = root.querySelector("textarea");
    this._counter = root.querySelector(".counter");
    this._sendButton = root.querySelector(".send");
    const search = root.querySelector("input[type=search]");

    search.addEventListener("input", () => {
      this._filter = search.value.trim().toLowerCase();
      this._renderList();
    });

    this._gatewaySelect.addEventListener("change", () => {
      this._entryId = this._gatewaySelect.value;
      this._conversation = null;
      const gateway = this._gateway();
      const remembered = (this._persisted.conversation || {})[this._entryId];
      this._selectConversation(
        remembered && this._conversationInfo(gateway, remembered) ? remembered : this._defaultConversation(gateway),
        { showThread: false }
      );
    });

    this._listEl.addEventListener("click", (ev) => {
      const item = ev.target.closest("[data-conversation]");
      if (item) this._selectConversation(item.dataset.conversation);
    });

    this._headerEl.addEventListener("click", (ev) => {
      const action = ev.target.closest("[data-action]");
      if (!action) return;
      if (action.dataset.action === "back") {
        this._showThread = false;
        this._applyLayout();
      } else if (action.dataset.action === "clear") {
        this._clearConversation();
      }
    });

    this._messagesEl.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-action=older]")) this._loadOlder();
    });

    this._form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      this._send();
    });

    this._textarea.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        this._send();
      }
    });

    this._textarea.addEventListener("input", () => {
      this._autoGrow();
      this._updateComposer();
    });
  }

  _applyLayout() {
    if (!this._layoutEl) return;
    this._layoutEl.classList.toggle("narrow", !!this._narrow);
    this._layoutEl.classList.toggle("show-thread", !!this._showThread);
  }

  _render() {
    if (!this._layoutEl) return;
    this._renderGatewaySelect();
    this._renderList();
    this._renderThread();
  }

  _renderGatewaySelect() {
    const select = this._gatewaySelect;
    select.hidden = this._gateways.length < 2;
    select.innerHTML = this._gateways
      .map(
        (g) =>
          `<option value="${escapeHtml(g.entry_id)}" ${g.entry_id === this._entryId ? "selected" : ""}>${escapeHtml(
            g.gateway.long_name || g.title
          )}</option>`
      )
      .join("");
  }

  _renderList() {
    if (!this._listEl) return;
    const gateway = this._gateway();
    if (!gateway) {
      this._listEl.innerHTML = this._loadError
        ? `<p class="empty">${escapeHtml(this._loadError)}</p>`
        : `<p class="empty">No gateway has Mesh Chat enabled. Turn it on in the Meshtastic integration options.</p>`;
      return;
    }

    const filter = this._filter;
    const matches = (...values) => !filter || values.some((v) => String(v || "").toLowerCase().includes(filter));

    const channelItems = gateway.channels
      .filter((c) => matches(c.name, `#${c.name}`))
      .map((c) => this._listItem(gateway, c.conversation, {
        avatar: `<span class="avatar channel ${c.public ? "public" : ""}" aria-hidden="true"><ha-icon icon="${
          c.public ? "mdi:earth" : "mdi:lock"
        }"></ha-icon></span>`,
        title: c.name,
        badge: c.public ? "public" : "",
      }))
      .join("");

    const lastTs = (n) => (gateway.conversations[n.conversation] || {}).ts || 0;
    const nodes = gateway.nodes
      .filter((n) => matches(n.long_name, n.short_name, n.user_id))
      .sort(
        (a, b) =>
          (b.favorite === true) - (a.favorite === true) ||
          lastTs(b) - lastTs(a) ||
          (b.last_heard || 0) - (a.last_heard || 0)
      );

    // conversations with nodes that are no longer in the node database
    const knownNodes = new Set(gateway.nodes.map((n) => n.conversation));
    const orphaned = Object.keys(gateway.conversations)
      .filter((c) => c.startsWith("node:") && !knownNodes.has(c))
      .map((c) => this._conversationInfo(gateway, c))
      .filter((n) => matches(n.long_name, n.user_id));

    const nodeItems = [...nodes, ...orphaned]
      .map((n) => this._listItem(gateway, n.conversation, {
        avatar: this._avatar(n.num, n.short_name),
        title: n.long_name,
        badge: n.favorite ? "★" : "",
        meta: n.last_heard ? `heard ${this._formatAgo(n.last_heard)} ago` : "",
      }))
      .join("");

    const unavailable = gateway.available
      ? ""
      : `<p class="empty warning">The gateway is not connected. History is still available.</p>`;

    this._listEl.innerHTML = `
      ${unavailable}
      <h2>Channels</h2>
      ${channelItems || `<p class="empty">No matching channels</p>`}
      <h2>Direct messages</h2>
      ${nodeItems || `<p class="empty">No matching nodes</p>`}
    `;
  }

  _listItem(gateway, conversation, { avatar, title, badge = "", meta = "" }) {
    const summary = gateway.conversations[conversation];
    const unread = this._isUnread(gateway, conversation) && conversation !== this._conversation;
    const preview = summary
      ? `${summary.direction === "out" ? "You: " : ""}${summary.text}`
      : meta;
    const time = summary ? this._formatAgo(summary.ts) : "";
    const selected = conversation === this._conversation;
    return `
      <button class="item ${selected ? "selected" : ""} ${unread ? "unread" : ""}" data-conversation="${escapeHtml(
        conversation
      )}" ${selected ? 'aria-current="true"' : ""}>
        ${avatar}
        <span class="item-text">
          <span class="item-title">
            <span class="name">${escapeHtml(title)}</span>
            ${badge ? `<span class="badge">${escapeHtml(badge)}</span>` : ""}
            <span class="time">${escapeHtml(time)}</span>
          </span>
          <span class="preview">${escapeHtml(preview)}</span>
        </span>
        ${unread ? '<span class="dot" aria-label="unread"></span>' : ""}
      </button>
    `;
  }

  _renderThread() {
    if (!this._headerEl) return;
    const gateway = this._gateway();
    const info = this._conversationInfo(gateway, this._conversation);

    if (!gateway || !info) {
      this._headerEl.innerHTML = "";
      this._messagesEl.innerHTML = `<p class="empty center">Select a channel or node to start chatting.</p>`;
      this._updateComposer();
      return;
    }

    let avatar;
    let title;
    let subtitle;
    if (info.kind === "channel") {
      avatar = `<span class="avatar channel ${info.public ? "public" : ""}" aria-hidden="true"><ha-icon icon="${
        info.public ? "mdi:earth" : "mdi:lock"
      }"></ha-icon></span>`;
      title = info.name;
      subtitle = info.public
        ? `Channel ${info.index} · public – anyone on the mesh can read it`
        : `Channel ${info.index} · encrypted`;
    } else {
      avatar = this._avatar(info.num, info.short_name);
      title = info.long_name;
      const parts = [info.user_id];
      if (info.last_heard) parts.push(`heard ${this._formatAgo(info.last_heard)} ago`);
      if (info.hops_away !== undefined && info.hops_away !== null) parts.push(`${info.hops_away} hops`);
      parts.push(info.has_public_key ? "end-to-end encrypted" : "no public key yet");
      subtitle = parts.join(" · ");
    }

    this._headerEl.innerHTML = `
      <button class="icon-button back" data-action="back" aria-label="Back to conversations">
        <ha-icon icon="mdi:arrow-left"></ha-icon>
      </button>
      ${avatar}
      <div class="thread-title">
        <div class="name">${escapeHtml(title)}</div>
        <div class="subtitle">${escapeHtml(subtitle)}</div>
      </div>
      ${
        gateway.can_clear
          ? `<button class="icon-button" data-action="clear" aria-label="Delete stored history" title="Delete stored history">
              <ha-icon icon="mdi:delete-sweep-outline"></ha-icon>
            </button>`
          : ""
      }
    `;

    this._textarea.placeholder =
      info.kind === "channel"
        ? `Message ${info.name}${info.public ? " (public)" : ""}`
        : `Message ${info.long_name}`;
    this._renderMessages({ stickToBottom: true });
    this._updateComposer();
  }

  _renderMessages({ stickToBottom = false } = {}) {
    if (!this._messagesEl) return;
    const gateway = this._gateway();
    if (!gateway || !this._conversation) return;
    const key = `${this._entryId}|${this._conversation}`;
    const messages = this._messages.get(key);
    const scroller = this._messagesEl;
    const atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;

    if (!messages) {
      scroller.innerHTML = `<p class="empty center">Loading…</p>`;
      return;
    }
    if (!messages.length) {
      scroller.innerHTML = `<p class="empty center">No messages yet. Messages received or sent from here will appear in this conversation.</p>`;
      return;
    }

    const summary = gateway.conversations[this._conversation];
    const hasOlder = summary && summary.count > messages.length;
    let html = hasOlder ? `<button class="older" data-action="older">Load older messages</button>` : "";
    let lastDay = null;
    let lastSender = null;

    for (const m of messages) {
      const day = this._formatDay(m.ts);
      if (day !== lastDay) {
        html += `<div class="day"><span>${escapeHtml(day)}</span></div>`;
        lastDay = day;
        lastSender = null;
      }
      const outgoing = m.direction === "out";
      const continued = lastSender === `${m.direction}:${m.from}`;
      lastSender = `${m.direction}:${m.from}`;

      let footer = `<span class="time">${escapeHtml(this._formatTime(m.ts))}</span>`;
      if (outgoing) {
        const status = STATUS_ICONS[m.status] || STATUS_ICONS.sending;
        const label = m.error ? `${status.label}: ${m.error}` : status.label;
        footer += `<ha-icon class="status ${escapeHtml(m.status)}" icon="${status.icon}" title="${escapeHtml(
          label
        )}" aria-label="${escapeHtml(label)}"></ha-icon>`;
      }

      const sentBy = outgoing && m.sent_by ? `<span class="via">${escapeHtml(m.sent_by)} via Home Assistant</span>` : "";
      const author =
        !outgoing && !continued
          ? `<div class="author">${escapeHtml(m.from_name)} <span>${escapeHtml(m.from_user_id || "")}</span></div>`
          : "";

      html += `
        <div class="row ${outgoing ? "out" : "in"} ${continued ? "continued" : ""}">
          ${!outgoing ? (continued ? '<span class="avatar spacer"></span>' : this._avatar(m.from, m.from_short)) : ""}
          <div class="bubble ${m.status === "failed" ? "failed" : ""}">
            ${author}
            <div class="text">${escapeHtml(m.text)}</div>
            <div class="footer">${sentBy}${footer}</div>
          </div>
        </div>`;
    }

    scroller.innerHTML = html;
    if (stickToBottom || atBottom) scroller.scrollTop = scroller.scrollHeight;
  }

  _updateComposer() {
    if (!this._textarea) return;
    const gateway = this._gateway();
    const enabled = !!(gateway && this._conversation && gateway.available);
    const text = this._textarea.value.trim();
    const length = gateway ? this._composedLength(text) : 0;
    const max = gateway ? gateway.max_bytes : 200;
    const tooLong = length > max;

    this._textarea.disabled = !enabled;
    this._sendButton.disabled = !enabled || !text || tooLong || !!this._sending;
    this._counter.textContent = text ? `${length}/${max}` : "";
    this._counter.classList.toggle("over", tooLong);
    this._counter.title = gateway && gateway.prefix ? `Includes the prefix "${gateway.prefix}: "` : "";
  }

  static get styles() {
    return `
      :host {
        display: block;
        /* the panel container has no explicit height, so size against the viewport */
        height: 100vh;
        height: 100dvh;
        overflow: hidden;
        background: var(--primary-background-color);
        color: var(--primary-text-color);
        font-family: var(--ha-font-family-body, Roboto, sans-serif);
        --chat-radius: 16px;
        --chat-sidebar-width: 320px;
      }
      * { box-sizing: border-box; }
      button { font: inherit; color: inherit; }

      .toolbar {
        display: flex;
        align-items: center;
        gap: 8px;
        height: var(--header-height, 56px);
        padding: 0 12px 0 4px;
        background: var(--app-header-background-color, var(--primary-color));
        color: var(--app-header-text-color, var(--text-primary-color, #fff));
        border-bottom: var(--app-header-border-bottom, none);
      }
      .toolbar .title { flex: 1; font-size: 20px; margin-left: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .gateway-select {
        max-width: 50%;
        padding: 6px 8px;
        border-radius: 8px;
        border: 1px solid currentColor;
        background: transparent;
        color: inherit;
      }
      .gateway-select option { color: var(--primary-text-color); background: var(--card-background-color); }

      .layout {
        display: flex;
        height: calc(100% - var(--header-height, 56px));
        min-height: 0;
      }

      .sidebar {
        width: var(--chat-sidebar-width);
        flex-shrink: 0;
        display: flex;
        flex-direction: column;
        border-right: 1px solid var(--divider-color);
        background: var(--card-background-color);
        min-height: 0;
      }
      .search {
        display: flex;
        align-items: center;
        gap: 8px;
        margin: 12px;
        padding: 0 12px;
        height: 40px;
        border-radius: 20px;
        background: var(--secondary-background-color);
        color: var(--secondary-text-color);
      }
      .search input {
        flex: 1;
        min-width: 0;
        border: none;
        outline: none;
        background: transparent;
        color: var(--primary-text-color);
        font: inherit;
      }
      .conversations { flex: 1; overflow-y: auto; padding-bottom: 12px; }
      .conversations h2 {
        margin: 16px 16px 4px;
        font-size: 12px;
        font-weight: 500;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--secondary-text-color);
      }

      .item {
        display: flex;
        align-items: center;
        gap: 12px;
        width: 100%;
        padding: 8px 16px;
        border: none;
        background: none;
        text-align: left;
        cursor: pointer;
      }
      .item:hover { background: var(--secondary-background-color); }
      .item.selected { background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.12); }
      .item:focus-visible { outline: 2px solid var(--primary-color); outline-offset: -2px; }
      .item-text { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
      .item-title { display: flex; align-items: baseline; gap: 6px; }
      .item-title .name { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .item-title .time { margin-left: auto; font-size: 12px; color: var(--secondary-text-color); flex-shrink: 0; }
      .item.unread .name, .item.unread .preview { font-weight: 700; color: var(--primary-text-color); }
      .preview { font-size: 13px; color: var(--secondary-text-color); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .badge {
        font-size: 11px;
        padding: 0 6px;
        border-radius: 8px;
        background: var(--secondary-background-color);
        color: var(--secondary-text-color);
        flex-shrink: 0;
      }
      .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--primary-color); flex-shrink: 0; }

      .avatar {
        width: 40px;
        height: 40px;
        border-radius: 50%;
        flex-shrink: 0;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-size: 12px;
        font-weight: 600;
        color: #fff;
        background: hsl(var(--avatar-hue, 200), 45%, 45%);
        overflow: hidden;
        white-space: nowrap;
      }
      .avatar.channel { background: var(--primary-color); color: var(--text-primary-color, #fff); }
      .avatar.channel.public { background: var(--warning-color, #ffa600); }
      .avatar.spacer { visibility: hidden; height: 1px; }
      .avatar ha-icon { --mdc-icon-size: 20px; }

      .thread {
        flex: 1;
        min-width: 0;
        display: flex;
        flex-direction: column;
        min-height: 0;
      }
      .thread-header {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 8px 16px;
        min-height: 64px;
        border-bottom: 1px solid var(--divider-color);
        background: var(--card-background-color);
      }
      .thread-title { flex: 1; min-width: 0; }
      .thread-title .name { font-size: 16px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .thread-title .subtitle { font-size: 12px; color: var(--secondary-text-color); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .icon-button {
        width: 40px;
        height: 40px;
        border: none;
        border-radius: 50%;
        background: none;
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        color: var(--secondary-text-color);
        flex-shrink: 0;
      }
      .icon-button:hover { background: var(--secondary-background-color); }
      .back { display: none; }

      .messages { flex: 1; overflow-y: auto; padding: 8px 16px 16px; }
      .day { display: flex; justify-content: center; margin: 16px 0 8px; }
      .day span {
        font-size: 12px;
        padding: 2px 10px;
        border-radius: 10px;
        background: var(--secondary-background-color);
        color: var(--secondary-text-color);
      }
      .row { display: flex; align-items: flex-end; gap: 8px; margin-top: 10px; }
      .row.continued { margin-top: 2px; }
      .row.out { justify-content: flex-end; }
      .row .avatar { width: 32px; height: 32px; font-size: 10px; }
      .bubble {
        max-width: min(75%, 560px);
        padding: 8px 12px 6px;
        border-radius: var(--chat-radius);
        background: var(--card-background-color);
        box-shadow: var(--ha-card-box-shadow, 0 1px 2px rgba(0, 0, 0, 0.12));
        border: 1px solid var(--divider-color);
      }
      .row.in .bubble { border-bottom-left-radius: 4px; }
      .row.out .bubble {
        background: var(--primary-color);
        color: var(--text-primary-color, #fff);
        border-color: transparent;
        border-bottom-right-radius: 4px;
      }
      .bubble.failed { background: var(--error-color, #db4437) !important; }
      .author { font-size: 12px; font-weight: 600; margin-bottom: 2px; color: var(--primary-color); }
      .author span { font-weight: 400; color: var(--secondary-text-color); }
      .text { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.4; }
      .footer {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        gap: 4px;
        margin-top: 2px;
        font-size: 11px;
        opacity: 0.75;
      }
      .footer .via { margin-right: auto; padding-right: 8px; }
      .footer .status { --mdc-icon-size: 14px; }
      .older {
        display: block;
        margin: 8px auto;
        padding: 6px 14px;
        border-radius: 16px;
        border: 1px solid var(--divider-color);
        background: var(--card-background-color);
        cursor: pointer;
      }

      .error {
        margin: 0 16px 8px;
        padding: 8px 12px;
        border-radius: 8px;
        background: var(--error-color, #db4437);
        color: #fff;
        font-size: 14px;
      }

      .composer {
        display: flex;
        align-items: flex-end;
        gap: 8px;
        padding: 8px 16px 12px;
        border-top: 1px solid var(--divider-color);
        background: var(--card-background-color);
      }
      .composer-field {
        flex: 1;
        min-width: 0;
        display: flex;
        align-items: flex-end;
        border-radius: 20px;
        background: var(--secondary-background-color);
        padding: 8px 14px;
      }
      textarea {
        flex: 1;
        min-width: 0;
        border: none;
        outline: none;
        resize: none;
        background: transparent;
        color: var(--primary-text-color);
        font: inherit;
        line-height: 1.4;
        max-height: 160px;
      }
      .counter { font-size: 11px; color: var(--secondary-text-color); margin-left: 8px; flex-shrink: 0; }
      .counter.over { color: var(--error-color, #db4437); font-weight: 600; }
      .send {
        width: 44px;
        height: 44px;
        border: none;
        border-radius: 50%;
        background: var(--primary-color);
        color: var(--text-primary-color, #fff);
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
      }
      .send:disabled { opacity: 0.4; cursor: default; }

      .empty { padding: 8px 16px; margin: 0; color: var(--secondary-text-color); font-size: 14px; }
      .empty.center { text-align: center; padding: 48px 16px; }
      .empty.warning { color: var(--warning-color, #ffa600); }

      /* narrow: one pane at a time */
      .layout.narrow .sidebar { width: 100%; border-right: none; }
      .layout.narrow .thread { display: none; }
      .layout.narrow.show-thread .sidebar { display: none; }
      .layout.narrow.show-thread .thread { display: flex; }
      .layout.narrow .back { display: inline-flex; }
      .layout.narrow .bubble { max-width: 85%; }
    `;
  }
}

customElements.define("meshtastic-chat-panel", MeshtasticChatPanel);
