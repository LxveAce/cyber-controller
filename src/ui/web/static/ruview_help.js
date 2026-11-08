(function (global) {
  "use strict";

  const mounts = new WeakMap();
  const AREAS = Object.freeze({
    controls: '[data-ruview-role="controls"]',
    capabilities: '[data-ruview-role="capabilities"]',
    views: '[data-ruview-role="views"]',
    warnings: '[data-ruview-role="warnings"]',
    nodes: '[data-ruview-role="nodes"]'
  });
  const CONTEXT_TOPICS = Object.freeze({
    controls: "connect", capabilities: "capabilities", views: "sensing",
    warnings: "recovery", nodes: "sensing"
  });
  const ACTION_TOPICS = Object.freeze({
    "pose.calibrate": "calibration", "calibration.start": "calibration",
    "calibration.stop": "calibration", "calibration.cancel": "calibration",
    "calibration.reset": "calibration", "models.load": "models",
    "models.unload": "models", "model.sona.activate": "models",
    "models.lora.activate": "models", "models.delete": "models",
    "adaptive.train": "models", "adaptive.unload": "models",
    "recording.start": "recording-start", "recording.stop": "recording-stop",
    "recording.delete": "recording-delete", "stream.ws_ticket": "privacy",
    "training.jobs": "models"
  });
  const PROVIDERS = Object.freeze({ ruview: "RuView upstream", cc_csi: "CC-native CSI" });
  const MODES = new Set(["live", "replay", "simulation", "unknown"]);
  const STATES = new Set([
    "loading", "connected", "connecting", "disconnected", "stale",
    "auth", "auth_error", "offline", "timeout", "protocol", "error", "unknown"
  ]);
  const CAPABILITIES = new Set(["ruview.models", "ruview.calibration", "ruview.recording"]);
  let serial = 0;

  function record(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function hasOwn(value, key) {
    return Object.prototype.hasOwnProperty.call(value, key);
  }

  function parseContent(raw) {
    const invalid = () => { throw new TypeError("RuView help content is invalid."); };
    let budget = 60000;
    function text(value, limit) {
      if (typeof value !== "string" || !value.trim() || value.length > limit) invalid();
      budget -= value.length;
      if (budget < 0) invalid();
      return value;
    }
    function id(value) {
      if (typeof value !== "string" || !/^[a-z][a-z0-9-]{0,47}$/.test(value)) invalid();
      return value;
    }
    function list(value, limit, parse) {
      if (!Array.isArray(value) || value.length > limit) invalid();
      return Object.freeze(value.map(parse));
    }
    if (!record(raw) || raw.schema_version !== 1 || raw.locale !== "en") invalid();
    const title = text(raw.title, 100);
    const summary = text(raw.summary, 400);
    const topics = list(raw.topics, 24, item => {
      if (!record(item) || !hasOwn(AREAS, item.area)) invalid();
      if (item.capability !== undefined && !CAPABILITIES.has(item.capability)) invalid();
      const sections = list(item.sections, 6, section => {
        if (!record(section)) invalid();
        const paragraphs = list(section.paragraphs === undefined ? [] : section.paragraphs,
          8, value => text(value, 800));
        const steps = list(section.steps === undefined ? [] : section.steps,
          8, value => text(value, 400));
        const bullets = list(section.bullets === undefined ? [] : section.bullets,
          8, value => text(value, 400));
        if (!paragraphs.length && !steps.length && !bullets.length) invalid();
        return Object.freeze({ heading: text(section.heading, 120), paragraphs, steps, bullets });
      });
      if (!sections.length) invalid();
      return Object.freeze({
        id: id(item.id), title: text(item.title, 100), summary: text(item.summary, 400),
        area: item.area, capability: item.capability || null, sections,
        related: list(item.related, 6, id)
      });
    });
    const index = new Map(topics.map(topic => [topic.id, topic]));
    if (!topics.length || index.size !== topics.length) invalid();
    const journey = list(raw.journey, 24, id);
    if (!journey.length || new Set(journey).size !== journey.length ||
        journey.some(key => !index.has(key))) invalid();
    for (const topic of topics) {
      if (new Set(topic.related).size !== topic.related.length ||
          topic.related.some(key => key === topic.id || !index.has(key))) invalid();
    }
    return Object.freeze({ title, summary, topics, index, journey });
  }

  function unknownContext() {
    return { provider: "unknown", state: "unknown", mode: "unknown", capabilities: new Map() };
  }

  function projectState(raw) {
    if (!record(raw) || !record(raw.connection)) return null;
    const c = raw.connection;
    if (!hasOwn(PROVIDERS, c.provider) || !STATES.has(c.state) ||
        !MODES.has(c.source_mode) || typeof c.instance_id !== "string" ||
        !c.instance_id || c.instance_id.length > 128 ||
        !(c.age_ms === null || (typeof c.age_ms === "number" &&
          Number.isFinite(c.age_ms) && c.age_ms >= 0)) ||
        !record(raw.capabilities) || Object.keys(raw.capabilities).length > 128) return null;
    const capabilities = new Map();
    for (const key of CAPABILITIES) {
      if (!hasOwn(raw.capabilities, key)) continue;
      const cap = raw.capabilities[key];
      if (!record(cap) || typeof cap.supported !== "boolean" ||
          typeof cap.available !== "boolean" || (cap.available && !cap.supported)) continue;
      const e = cap.evidence;
      if (!record(e) || e.provider !== c.provider || e.instance_id !== c.instance_id ||
          e.source_mode !== c.source_mode || typeof e.stale !== "boolean") continue;
      capabilities.set(key, {
        supported: cap.supported,
        available: cap.available && !e.stale
      });
    }
    return {
      provider: c.provider,
      state: c.age_ms !== null && c.age_ms >= 15000 && c.state === "connected" ? "stale" : c.state,
      mode: c.source_mode, capabilities
    };
  }

  function mount(root, options) {
    if (!root || root.nodeType !== 1 || !root.ownerDocument) {
      throw new TypeError("A RuView workspace element is required.");
    }
    if (!record(options)) throw new TypeError("RuView help content is required.");
    const content = parseContent(options.content);
    const previous = mounts.get(root);
    if (previous) previous.destroy();
    const doc = root.ownerDocument;
    const win = doc.defaultView;
    const prefix = "ruview-help-" + (++serial) + "-";
    const triggers = new Set();
    const anchors = new Map();
    const temporaryFocus = new Map();
    let state = { phase: "closed", topic: content.journey[0], query: "" };
    let context = unknownContext();
    let returnFocus = null;
    let destroyed = false;

    function element(tag, value, role) {
      const node = doc.createElement(tag);
      if (value !== undefined) node.textContent = value;
      if (role) node.dataset.ruviewRole = role;
      return node;
    }
    function button(label, action, topic) {
      const node = element("button", label);
      node.type = "button";
      node.dataset.ruviewHelpAction = action;
      if (topic) node.dataset.ruviewHelpTopic = topic;
      return node;
    }

    const shell = element("section", undefined, "help-host");
    shell.className = "cc-ruview__help";
    shell.setAttribute("aria-label", content.title);
    const toolbar = element("div", undefined, "help-toolbar");
    const launcher = button("Help", "open");
    const walkthrough = button("Start walkthrough", "start");
    const panel = element("aside", undefined, "help-panel");
    panel.id = prefix + "panel";
    panel.hidden = true;
    panel.setAttribute("role", "region");
    panel.setAttribute("aria-labelledby", prefix + "title");
    for (const opener of [launcher, walkthrough]) {
      opener.setAttribute("aria-controls", panel.id);
      opener.setAttribute("aria-expanded", "false");
      triggers.add(opener);
    }
    toolbar.append(launcher, walkthrough);
    const title = element("h2", content.title);
    title.id = prefix + "title";
    const closeButton = button("Close help", "close");
    const summary = element("p", content.summary);
    const progress = element("p", "", "help-progress");
    const contextText = element("p", "", "help-context");
    const availability = element("p", "", "help-availability");
    const searchLabel = element("label", "Find a help topic");
    searchLabel.htmlFor = prefix + "search";
    const search = element("input", undefined, "help-search");
    search.id = prefix + "search";
    search.type = "search";
    search.maxLength = 80;
    search.autocomplete = "off";
    search.spellcheck = false;
    const nav = element("nav", undefined, "help-navigation");
    nav.setAttribute("aria-label", "Help topics");
    const topicList = element("ul", undefined, "help-topic-list");
    nav.append(topicList);
    const article = element("article", undefined, "help-article");
    article.tabIndex = -1;
    article.setAttribute("aria-labelledby", prefix + "topic-title");
    const related = element("nav", undefined, "help-related");
    related.setAttribute("aria-label", "Related help");
    const footer = element("div", undefined, "help-footer");
    const previousButton = button("Previous", "previous");
    const nextButton = button("Next", "next");
    const areaButton = button("Show workspace section", "area");
    const finishButton = button("Finish walkthrough", "close");
    footer.append(previousButton, nextButton, areaButton, finishButton);
    const announcement = element("p", "", "help-announcement");
    announcement.setAttribute("role", "status");
    announcement.setAttribute("aria-live", "polite");
    announcement.setAttribute("aria-atomic", "true");
    panel.append(title, closeButton, summary, progress, contextText, availability,
      searchLabel, search, nav, article, related, footer, announcement);
    shell.append(toolbar, panel);
    const status = root.querySelector('[data-ruview-role="status"]');
    const statusHost = status && status.closest(".cc-ruview__status");
    root.insertBefore(shell, statusHost && statusHost.parentNode === root ? statusHost : null);

    function domContext() {
      const data = root.dataset;
      return {
        provider: hasOwn(PROVIDERS, data.ruviewProvider) ? data.ruviewProvider : "unknown",
        state: STATES.has(data.ruviewState) ? data.ruviewState : "unknown",
        mode: MODES.has(data.ruviewMode) ? data.ruviewMode : "unknown"
      };
    }
    function currentContext() {
      const dom = domContext();
      const value = Object.assign({}, context);
      if (dom.provider !== "unknown") {
        if (dom.provider !== value.provider) value.capabilities = new Map();
        value.provider = dom.provider;
      }
      // DOM transport failure must not leave an older capability marked current.
      if (dom.state !== "unknown" && dom.state !== "connected") value.state = dom.state;
      if (dom.mode !== "unknown") {
        if (dom.mode !== value.mode) value.capabilities = new Map();
        value.mode = dom.mode;
      }
      return value;
    }
    function safeFocus(node) {
      if (node && node.isConnected && typeof node.focus === "function") {
        node.focus({ preventScroll: true });
      }
    }
    function sequence() {
      return state.phase === "walkthrough" ? content.journey : content.topics.map(topic => topic.id);
    }
    function refreshContext() {
      const value = currentContext();
      const provider = PROVIDERS[value.provider] || "Provider not reported";
      const mode = {
        live: "Live data.", replay: "Replay of recorded data.",
        simulation: "Simulated data.", unknown: "Source mode not reported."
      }[value.mode];
      contextText.textContent = provider + ". " + mode;
      const topic = content.index.get(state.topic);
      if (value.state === "auth" || value.state === "auth_error") {
        availability.textContent = value.state === "auth" ?
          "Sign in to Cyber Controller again, then reload the page." :
          "The source reports an authentication error. Check the configured profile.";
      } else if (value.state !== "connected" || value.mode === "unknown") {
        availability.textContent = "Current availability is not established. Check the workspace status and leave affected actions unavailable.";
      } else if (topic.capability && value.provider === "cc_csi") {
        availability.textContent = "These controls belong to RuView upstream. CC-native CSI has a separate capability policy.";
      } else if (topic.capability) {
        const cap = value.capabilities.get(topic.capability);
        availability.textContent = !cap ?
          "Availability is not reported here. Check the capability label and enabled controls." :
          !cap.supported ? "The source reports this capability unsupported." :
          !cap.available ? "The source reports this capability unavailable right now." :
          "The source reports this capability available. Use only controls that are enabled.";
      } else {
        availability.textContent = "Check the current capability labels before acting. Help does not change the source.";
      }
      areaButton.disabled = !root.querySelector(AREAS[topic.area]);
    }
    function renderNavigation() {
      const query = state.query.toLocaleLowerCase("en");
      const topics = content.topics.filter(topic =>
        (topic.title + " " + topic.summary).toLocaleLowerCase("en").includes(query));
      const rows = topics.map(topic => {
        const row = element("li");
        const choice = button(topic.title, "topic", topic.id);
        if (topic.id === state.topic) choice.setAttribute("aria-current", "page");
        choice.setAttribute("aria-controls", prefix + "article");
        row.append(choice);
        return row;
      });
      if (!rows.length) rows.push(element("li", "No matching help topics. Clear the search to browse all topics."));
      topicList.replaceChildren(...rows);
      search.setAttribute("aria-describedby", prefix + "results");
      results.textContent = topics.length + (topics.length === 1 ? " topic" : " topics");
    }
    const results = element("p", "", "help-search-results");
    results.id = prefix + "results";
    nav.before(results);
    article.id = prefix + "article";

    function renderTopic() {
      const topic = content.index.get(state.topic);
      const heading = element("h3", topic.title);
      heading.id = prefix + "topic-title";
      const rows = [heading, element("p", topic.summary)];
      for (const section of topic.sections) {
        rows.push(element("h4", section.heading));
        for (const paragraph of section.paragraphs) rows.push(element("p", paragraph));
        for (const [tag, items] of [["ol", section.steps], ["ul", section.bullets]]) {
          if (items.length) {
            const list = element(tag);
            list.append(...items.map(item => element("li", item)));
            rows.push(list);
          }
        }
      }
      article.replaceChildren(...rows);
      related.replaceChildren(...topic.related.map(key => button(content.index.get(key).title, "topic", key)));
      const order = sequence();
      const position = order.indexOf(state.topic);
      previousButton.disabled = position <= 0;
      nextButton.disabled = position >= order.length - 1;
      finishButton.hidden = state.phase !== "walkthrough" || position !== order.length - 1;
      progress.textContent = state.phase === "walkthrough" ?
        "Step " + (position + 1) + " of " + order.length : "Browse help";
      for (const trigger of triggers) trigger.setAttribute("aria-expanded", String(!panel.hidden));
      renderNavigation();
      refreshContext();
    }
    function show(topic, phase) {
      if (destroyed || !content.index.has(topic)) return false;
      refreshAnchors();
      if (state.phase === "closed") returnFocus = doc.activeElement;
      state = { phase, topic, query: "" };
      search.value = "";
      panel.hidden = false;
      renderTopic();
      announcement.textContent = content.index.get(topic).title;
      safeFocus(article);
      return true;
    }
    function open(topic) {
      return show(topic === undefined ? state.topic : topic, "browse");
    }
    function start() {
      return show(content.journey[0], "walkthrough");
    }
    function close(restore) {
      if (destroyed || state.phase === "closed") return false;
      state.phase = "closed";
      panel.hidden = true;
      for (const trigger of triggers) trigger.setAttribute("aria-expanded", "false");
      if (restore !== false) safeFocus(returnFocus);
      returnFocus = null;
      return true;
    }
    function releaseFocus(node) {
      const entry = temporaryFocus.get(node);
      if (!entry) return;
      node.removeEventListener("blur", entry.restore);
      if (node.getAttribute("tabindex") === "-1") {
        if (entry.previous === null) node.removeAttribute("tabindex");
        else node.setAttribute("tabindex", entry.previous);
      }
      temporaryFocus.delete(node);
    }
    function focusArea() {
      const topic = content.index.get(state.topic);
      const host = root.querySelector(AREAS[topic.area]);
      if (!host) {
        announcement.textContent = "This workspace section is not available yet.";
        areaButton.disabled = true;
        return false;
      }
      const section = host.closest("section, aside") || host;
      const target = section.querySelector("h2") || section;
      if (!temporaryFocus.has(target)) {
        const entry = { previous: target.getAttribute("tabindex"), restore: () => releaseFocus(target) };
        temporaryFocus.set(target, entry);
        target.setAttribute("tabindex", "-1");
        target.addEventListener("blur", entry.restore);
      }
      close(false);
      safeFocus(target);
      return true;
    }
    function onClick(event) {
      const target = event.target && event.target.closest ? event.target.closest("button") : null;
      if (!target || !root.contains(target) || target.disabled) return;
      if (root.matches('[data-ruview-role="workspace"]') &&
          target.closest('[data-ruview-role="workspace"]') !== root) return;
      const action = target.dataset.ruviewHelpAction;
      const topic = target.dataset.ruviewHelpTopic;
      if (!action && !topic) return;
      if (!shell.contains(target) && !triggers.has(target) &&
          (!topic || !content.index.has(topic))) return;
      if (action === "close") { event.preventDefault(); close(); }
      else if (action === "start") { event.preventDefault(); start(); }
      else if (action === "open" || action === "topic" || (!action && topic)) {
        if (topic !== undefined && !content.index.has(topic)) return;
        event.preventDefault();
        open(topic);
      } else if (action === "previous" || action === "next") {
        const order = sequence();
        const position = order.indexOf(state.topic) + (action === "next" ? 1 : -1);
        if (position >= 0 && position < order.length) {
          event.preventDefault();
          show(order[position], state.phase);
        }
      } else if (action === "area") { event.preventDefault(); focusArea(); }
    }
    function onInput(event) {
      if (event.target !== search || destroyed) return;
      state.query = search.value.slice(0, 80);
      renderNavigation();
      announcement.textContent = results.textContent;
    }
    function onKeydown(event) {
      if (destroyed || panel.hidden || !panel.contains(event.target)) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        close();
        return;
      }
      if (!topicList.contains(event.target) ||
          !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
      const buttons = Array.from(topicList.querySelectorAll("button"));
      const index = buttons.indexOf(event.target);
      if (index < 0 || !buttons.length) return;
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 :
        (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
      event.preventDefault();
      safeFocus(buttons[next]);
    }
    function update(raw) {
      if (destroyed) return false;
      refreshAnchors();
      let next = null;
      try { next = projectState(raw); } catch (_error) {}
      context = next || unknownContext();
      if (state.phase !== "closed") refreshContext();
      return next !== null;
    }
    function destroy() {
      if (destroyed) return;
      destroyed = true;
      root.removeEventListener("click", onClick);
      root.removeEventListener("input", onInput);
      root.removeEventListener("keydown", onKeydown);
      if (win) win.removeEventListener("pagehide", destroy);
      for (const node of Array.from(temporaryFocus.keys())) releaseFocus(node);
      for (const trigger of triggers) {
        if (!shell.contains(trigger)) trigger.remove();
      }
      triggers.clear();
      anchors.clear();
      shell.remove();
      returnFocus = null;
      context = unknownContext();
      if (mounts.get(root) === controller) mounts.delete(root);
    }

    function refreshAnchors() {
      const desired = new Map();
      function include(host, topic) {
        if (!host || !content.index.has(topic)) return;
        if (root.matches('[data-ruview-role="workspace"]') &&
            host.closest('[data-ruview-role="workspace"]') !== root) return;
        desired.set(host, topic);
      }
      for (const [area, topic] of Object.entries(CONTEXT_TOPICS)) {
        const host = root.querySelector(AREAS[area]);
        const section = host && (host.closest("section, aside") || host);
        include(section && section.querySelector("h2"), topic);
      }
      include(root.querySelector('[data-ruview-role="connection-form"]'), "connect");
      const forms = root.querySelectorAll("form[data-ruview-action]");
      for (let index = 0; index < Math.min(forms.length, 32); index += 1) {
        const form = forms[index];
        const action = form.dataset.ruviewAction;
        if (hasOwn(ACTION_TOPICS, action)) include(form, ACTION_TOPICS[action]);
      }
      for (const [host, trigger] of anchors) {
        if (desired.get(host) !== trigger.dataset.ruviewHelpTopic || !root.contains(trigger)) {
          trigger.remove();
          triggers.delete(trigger);
          anchors.delete(host);
        }
      }
      for (const [host, topic] of desired) {
        if (anchors.has(host)) continue;
        const trigger = button("Help", "open", topic);
        trigger.className = "cc-ruview__help-trigger";
        trigger.setAttribute("aria-label", "Help. " + content.index.get(topic).title + ".");
        trigger.setAttribute("aria-controls", panel.id);
        trigger.setAttribute("aria-expanded", String(!panel.hidden));
        host.after(trigger);
        triggers.add(trigger);
        anchors.set(host, trigger);
      }
    }
    refreshAnchors();
    root.addEventListener("click", onClick);
    root.addEventListener("input", onInput);
    root.addEventListener("keydown", onKeydown);
    if (win) win.addEventListener("pagehide", destroy);
    const controller = Object.freeze({
      open, close, start, update, destroy,
      getState() {
        const order = sequence();
        return Object.freeze({
          open: !destroyed && state.phase !== "closed",
          topic: state.topic, mode: destroyed ? "destroyed" : state.phase,
          position: order.indexOf(state.topic) + 1, total: order.length
        });
      }
    });
    mounts.set(root, controller);
    renderTopic();
    return controller;
  }

  const namespace = global.CCRuView || (global.CCRuView = {});
  namespace.help = Object.freeze({ mount });
})(window);

