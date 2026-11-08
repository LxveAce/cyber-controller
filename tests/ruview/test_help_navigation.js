(function (global) {
  "use strict";

  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }
  function equal(actual, expected, message) {
    assert(Object.is(actual, expected), message);
  }
  function copy(value) {
    return JSON.parse(JSON.stringify(value));
  }
  function role(root, name) {
    return root.querySelector('[data-ruview-role="' + name + '"]');
  }
  function action(root, name) {
    return root.querySelector('[data-ruview-help-action="' + name + '"]');
  }
  function key(node, value) {
    const win = node.ownerDocument.defaultView;
    node.dispatchEvent(new win.KeyboardEvent("keydown", { key: value, bubbles: true, cancelable: true }));
  }
  function reportedState(provider, options) {
    const config = options || {};
    const mode = config.mode || "live";
    return {
      connection: {
        provider, instance_id: "source-1", state: config.state || "connected",
        source_mode: mode, age_ms: config.age === undefined ? 0 : config.age
      },
      capabilities: {
        "ruview.recording": {
          supported: config.supported !== false, available: config.available !== false,
          evidence: {
            provider, instance_id: "source-1", source_mode: mode, stale: false
          }
        }
      }
    };
  }
  function makeWorkspace(doc, parent) {
    const root = doc.createElement("div");
    root.className = "cc-ruview";
    root.dataset.ruviewRole = "workspace";
    root.dataset.ruviewProvider = "ruview";
    root.dataset.ruviewState = "connected";
    root.dataset.ruviewMode = "live";
    const header = doc.createElement("header");
    const title = doc.createElement("h1");
    title.textContent = "RuView workspace";
    header.append(title);
    const status = doc.createElement("div");
    status.className = "cc-ruview__status";
    const label = doc.createElement("strong");
    label.dataset.ruviewRole = "status";
    label.textContent = "Connected";
    status.append(label);
    root.append(header, status);
    for (const name of ["controls", "capabilities", "views", "warnings", "nodes"]) {
      const section = doc.createElement(name === "nodes" ? "aside" : "section");
      const heading = doc.createElement("h2");
      heading.textContent = name;
      const host = doc.createElement(name === "nodes" ? "ul" : "div");
      host.dataset.ruviewRole = name;
      section.append(heading, host);
      root.append(section);
    }
    let operations = 0;
    const productButton = doc.createElement("button");
    productButton.type = "button";
    productButton.textContent = "Start local recording";
    productButton.addEventListener("click", () => { operations += 1; });
    role(root, "controls").append(productButton);
    parent.append(root);
    return { root, productButton, operations: () => operations };
  }

  const cases = [
    ["mount starts closed and retains the workspace DOM", f => {
      equal(f.controller.getState().open, false, "Help must start closed.");
      assert(role(f.root, "help-panel").hidden, "The panel must be hidden.");
      assert(f.root.contains(f.productButton), "Product controls must remain owned by the workspace.");
      equal(f.root.querySelectorAll('[data-ruview-role="help-host"]').length, 1, "One host is required.");
    }],
    ["opening and Escape restore the invoking control", f => {
      const opener = action(f.root, "open");
      opener.focus();
      opener.click();
      equal(f.controller.getState().open, true, "Help must open.");
      equal(f.doc.activeElement, role(f.root, "help-article"), "The readable article must receive focus.");
      equal(opener.getAttribute("aria-expanded"), "true", "Expanded state must match visibility.");
      key(role(f.root, "help-article"), "Escape");
      equal(f.controller.getState().open, false, "Escape must close help.");
      equal(f.doc.activeElement, opener, "Focus must return to the invoking control.");
      equal(opener.getAttribute("aria-expanded"), "false", "Expanded state must reset.");
    }],
    ["walkthrough reaches every authored step without product actions", f => {
      action(f.root, "start").click();
      for (let index = 0; index < f.content.journey.length; index += 1) {
        equal(f.controller.getState().topic, f.content.journey[index], "Walkthrough order must match content.");
        equal(f.controller.getState().position, index + 1, "Step position must match.");
        assert(role(f.root, "help-progress").textContent.includes("of " + f.content.journey.length),
          "The full step count must be visible.");
        if (index < f.content.journey.length - 1) action(f.root, "next").click();
      }
      assert(!action(f.root, "close").hidden, "A close action remains present.");
      const finish = Array.from(f.root.querySelectorAll("button")).find(node => node.textContent === "Finish walkthrough");
      assert(finish && !finish.hidden, "The final step must offer Finish.");
      finish.click();
      equal(f.controller.getState().open, false, "Finish must close help.");
      equal(f.operations(), 0, "The walkthrough must not invoke product actions.");
    }],
    ["previous and next stop at the journey boundaries", f => {
      f.controller.start();
      assert(action(f.root, "previous").disabled, "Previous must be disabled at the start.");
      action(f.root, "next").click();
      action(f.root, "previous").click();
      equal(f.controller.getState().position, 1, "Previous must return to the first step.");
      for (let index = 1; index < f.content.journey.length; index += 1) action(f.root, "next").click();
      assert(action(f.root, "next").disabled, "Next must be disabled at the end.");
    }],
    ["topic navigation and related links switch to browse mode", f => {
      f.controller.start();
      const navigation = role(f.root, "help-navigation");
      navigation.querySelector('[data-ruview-help-topic="recording-stop"]').click();
      equal(f.controller.getState().mode, "browse", "A direct topic choice must leave walkthrough mode.");
      equal(f.controller.getState().topic, "recording-stop", "The selected topic must open.");
      const selected = navigation.querySelector('[aria-current="page"]');
      equal(selected.dataset.ruviewHelpTopic, "recording-stop", "Current navigation state must match.");
      role(f.root, "help-related").querySelector('[data-ruview-help-topic="recording-list"]').click();
      equal(f.controller.getState().topic, "recording-list", "Related help must navigate.");
    }],
    ["topic arrows Home and End move focus without opening a topic", f => {
      f.controller.open();
      const buttons = Array.from(role(f.root, "help-topic-list").querySelectorAll("button"));
      buttons[0].focus();
      key(buttons[0], "End");
      equal(f.doc.activeElement, buttons[buttons.length - 1], "End must focus the last topic.");
      key(buttons[buttons.length - 1], "ArrowDown");
      equal(f.doc.activeElement, buttons[0], "ArrowDown must wrap.");
      key(buttons[0], "ArrowUp");
      equal(f.doc.activeElement, buttons[buttons.length - 1], "ArrowUp must wrap.");
      key(buttons[buttons.length - 1], "Home");
      equal(f.doc.activeElement, buttons[0], "Home must focus the first topic.");
      equal(f.controller.getState().topic, f.content.journey[0], "Arrow navigation alone must not activate a topic.");
    }],
    ["search narrows the list and recovers from no results", f => {
      f.controller.open();
      const search = role(f.root, "help-search");
      search.focus();
      search.value = "zzzz-no-such-topic";
      search.dispatchEvent(new f.win.Event("input", { bubbles: true }));
      equal(role(f.root, "help-topic-list").querySelectorAll("button").length, 0, "No results must be explicit.");
      assert(role(f.root, "help-topic-list").textContent.includes("No matching"), "Empty results need guidance.");
      equal(f.doc.activeElement, search, "Search must retain focus.");
      equal(search.maxLength, 80, "Search must have a finite input limit.");
      search.value = "";
      search.dispatchEvent(new f.win.Event("input", { bubbles: true }));
      equal(role(f.root, "help-topic-list").querySelectorAll("button").length, f.content.topics.length,
        "Clearing search must restore the whole topic list.");
    }],
    ["context help survives a workspace payload repaint", f => {
      const payload = role(f.root, "capabilities");
      const trigger = payload.closest("section").querySelector('[data-ruview-help-topic="capabilities"]');
      assert(trigger, "The stable heading must have contextual help.");
      payload.replaceChildren(f.doc.createTextNode("Updated capabilities"));
      assert(trigger.isConnected, "Workspace payload replacement must not remove the heading trigger.");
      trigger.click();
      equal(f.controller.getState().topic, "capabilities", "The contextual topic must open.");
    }],
    ["disabled recording forms still have usable contextual help", f => {
      const form = f.doc.createElement("form");
      form.dataset.ruviewAction = "recording.stop";
      const fields = f.doc.createElement("fieldset");
      fields.disabled = true;
      form.append(fields);
      role(f.root, "controls").append(form);
      let submissions = 0;
      form.addEventListener("submit", event => { event.preventDefault(); submissions += 1; });
      f.controller.update(reportedState("ruview"));
      const trigger = form.nextElementSibling;
      equal(trigger.dataset.ruviewHelpTopic, "recording-stop", "The actual command identity must select Stop help.");
      equal(trigger.type, "button", "Context help cannot submit a product form.");
      trigger.click();
      equal(f.controller.getState().topic, "recording-stop", "Help must remain usable beside disabled fields.");
      equal(submissions, 0, "Help cannot submit the disabled command.");
    }],
    ["updates rebind contextual help after controls remount", f => {
      const host = role(f.root, "controls");
      function modelForm() {
        const form = f.doc.createElement("form");
        form.dataset.ruviewAction = "models.load";
        return form;
      }
      const old = modelForm();
      host.append(old);
      f.controller.update(reportedState("ruview"));
      const oldHelp = old.nextElementSibling;
      const next = modelForm();
      host.replaceChildren(next);
      f.controller.update(reportedState("ruview"));
      assert(!oldHelp.isConnected, "An old owner subtree must not remain bound.");
      equal(next.nextElementSibling.dataset.ruviewHelpTopic, "models", "The replacement must receive contextual help.");
      f.controller.update(reportedState("ruview"));
      equal(host.querySelectorAll('[data-ruview-help-topic="models"]').length, 1,
        "Repeated updates cannot duplicate contextual buttons.");
      for (let index = 0; index < 40; index += 1) host.prepend(modelForm());
      f.controller.update(reportedState("ruview"));
      equal(host.querySelectorAll('[data-ruview-help-topic="models"]').length, 32,
        "Connected forms outside the current bound must release their old help buttons.");
    }],
    ["show section moves focus and restores temporary tabindex", f => {
      f.controller.open("connect");
      const heading = role(f.root, "controls").closest("section").querySelector("h2");
      equal(heading.getAttribute("tabindex"), null, "The fixture heading starts unmodified.");
      action(f.root, "area").click();
      equal(f.controller.getState().open, false, "Section navigation closes help.");
      equal(f.doc.activeElement, heading, "Section navigation must move keyboard focus.");
      action(f.root, "open").focus();
      equal(heading.getAttribute("tabindex"), null, "Temporary focus attributes must be restored on blur.");
    }],
    ["missing target stays unavailable without invoking another control", f => {
      role(f.root, "controls").closest("section").remove();
      f.controller.open("connect");
      assert(action(f.root, "area").disabled, "A missing section cannot be activated.");
      equal(f.operations(), 0, "No replacement product action may be invoked.");
    }],
    ["state update preserves the focused help input", f => {
      f.controller.open("recording-start");
      const search = role(f.root, "help-search");
      search.focus();
      equal(f.controller.update(reportedState("ruview")), true, "A projected state must be accepted.");
      equal(f.doc.activeElement, search, "Status refresh must not rebuild focused content.");
      assert(role(f.root, "help-availability").textContent.includes("Use only controls that are enabled"),
        "Reported support must not override disabled controls.");
    }],
    ["stale evidence never remains current", f => {
      f.controller.open("recording-start");
      f.controller.update(reportedState("ruview"));
      f.controller.update(reportedState("ruview", { age: 15000 }));
      assert(role(f.root, "help-availability").textContent.includes("not established"), "The stale boundary must withhold current availability.");
    }],
    ["DOM transport failure overrides an older connected update", f => {
      f.controller.update(reportedState("ruview"));
      f.root.dataset.ruviewState = "offline";
      f.controller.open("recording-start");
      assert(role(f.root, "help-availability").textContent.includes("not established"),
        "An older capability cannot survive a newer transport failure.");
    }],
    ["source mode change does not reuse live capability evidence", f => {
      f.controller.update(reportedState("ruview"));
      f.root.dataset.ruviewMode = "replay";
      f.controller.open("recording-start");
      assert(role(f.root, "help-context").textContent.includes("Replay"), "The displayed source mode must change.");
      assert(!role(f.root, "help-availability").textContent.includes("capability available."),
        "A live capability must not become replay evidence.");
    }],
    ["native provider does not borrow upstream control availability", f => {
      f.root.dataset.ruviewProvider = "cc_csi";
      f.controller.update(reportedState("cc_csi"));
      f.controller.open("recording-start");
      assert(role(f.root, "help-availability").textContent.includes("belong to RuView upstream"),
        "Native CSI must retain its separate policy.");
    }],
    ["unsupported and unavailable are distinct", f => {
      f.controller.open("recording-start");
      f.controller.update(reportedState("ruview", { supported: false, available: false }));
      assert(role(f.root, "help-availability").textContent.includes("unsupported"), "Unsupported must remain explicit.");
      f.controller.update(reportedState("ruview", { available: false }));
      assert(role(f.root, "help-availability").textContent.includes("unavailable right now"), "Temporary unavailability must stay distinct.");
    }],
    ["mismatched capability evidence does not establish availability", f => {
      const state = reportedState("ruview");
      state.capabilities["ruview.recording"].evidence.instance_id = "other-source";
      f.controller.update(state);
      f.controller.open("recording-start");
      assert(role(f.root, "help-availability").textContent.includes("not reported"), "Other-instance evidence must be ignored.");
    }],
    ["invalid update clears prior capability projection", f => {
      f.controller.update(reportedState("ruview"));
      f.controller.open("recording-start");
      equal(f.controller.update({ upstream: reportedState("ruview") }), false,
        "An envelope is not the documented normalized-state input.");
      assert(!role(f.root, "help-availability").textContent.includes("capability available."),
        "Invalid input must remove old availability.");
    }],
    ["private state fields and reasons never enter help text", f => {
      const secret = "PRIVATE_SENTINEL_TOKEN_123";
      const state = reportedState("ruview");
      state.connection.endpoint = "https://" + secret;
      state.connection.profile_id = secret;
      state.capabilities["ruview.recording"].reason = secret;
      state.capabilities["ruview.recording"].evidence.endpoint = secret;
      f.controller.update(state);
      f.controller.open("recording-start");
      assert(!role(f.root, "help-host").textContent.includes(secret), "Raw paths, profiles, reasons and credentials must not render.");
      assert(!JSON.stringify(f.controller.getState()).includes(secret), "Help navigation state must contain no product identity.");
    }],
    ["content markup is text rather than executable DOM", f => {
      const changed = copy(f.content);
      changed.topics[0].summary = '<img src=x onerror="throw 1"><script>throw 1</script>';
      const controller = f.help.mount(f.root, { content: changed });
      controller.open();
      assert(role(f.root, "help-article").textContent.includes("<img"), "Markup characters must be visible text.");
      assert(!role(f.root, "help-host").querySelector("img,script"), "Content cannot create executable DOM.");
    }],
    ["duplicate topics or unknown links are refused before replacing a good mount", f => {
      const bad = copy(f.content);
      bad.topics[1].id = bad.topics[0].id;
      let refused = false;
      try { f.help.mount(f.root, { content: bad }); } catch (_error) { refused = true; }
      assert(refused, "Duplicate topics must be rejected.");
      equal(f.controller.open(), true, "A failed replacement must retain the good mount.");
      const badLink = copy(f.content);
      badLink.topics[0].related.push("not-a-real-topic");
      refused = false;
      try { f.help.mount(f.root, { content: badLink }); } catch (_error) { refused = true; }
      assert(refused, "Unknown internal links must be rejected.");
    }],
    ["content is copied at the boundary", f => {
      const original = copy(f.content);
      const controller = f.help.mount(f.root, { content: original });
      const expected = original.topics[0].title;
      original.topics[0].title = "Changed after mount";
      controller.open();
      assert(role(f.root, "help-article").textContent.includes(expected), "Later caller mutation must not rewrite help.");
      assert(!role(f.root, "help-article").textContent.includes("Changed after mount"), "The content snapshot must be owned.");
    }],
    ["unknown topics and oversized content are bounded refusals", f => {
      equal(f.controller.open("missing-topic"), false, "Unknown topics must not fall through to another action.");
      const huge = copy(f.content);
      huge.topics[0].summary = "x".repeat(401);
      let refused = false;
      try { f.help.mount(f.root, { content: huge }); } catch (_error) { refused = true; }
      assert(refused, "Oversized content must be refused.");
    }],
    ["remount removes old listeners and leaves one current controller", f => {
      const next = f.help.mount(f.root, { content: f.content });
      equal(f.controller.open(), false, "The old controller must be inert.");
      equal(f.root.querySelectorAll('[data-ruview-role="help-host"]').length, 1, "Remount must leave one host.");
      action(f.root, "start").click();
      equal(next.getState().mode, "walkthrough", "The new listener must handle the action once.");
      f.controller.destroy();
      equal(next.getState().open, true, "A late old destroy cannot tear down a replacement.");
    }],
    ["two workspace mounts have independent IDs and navigation", f => {
      const other = makeWorkspace(f.doc, f.doc.body);
      const controller = f.help.mount(other.root, { content: f.content });
      try {
        assert(role(other.root, "help-panel").id !== role(f.root, "help-panel").id, "Panel IDs must be unique.");
        controller.open("privacy");
        equal(f.controller.getState().open, false, "One mount cannot open another.");
        equal(controller.getState().topic, "privacy", "The second mount owns its navigation.");
      } finally { controller.destroy(); other.root.remove(); }
    }],
    ["nested workspace events do not navigate the outer help", f => {
      const inner = makeWorkspace(f.doc, role(f.root, "views"));
      const controller = f.help.mount(inner.root, { content: f.content });
      try {
        action(inner.root, "start").click();
        equal(controller.getState().open, true, "The nested help must open.");
        equal(f.controller.getState().open, false, "The outer mount must ignore nested workspace controls.");
      } finally { controller.destroy(); inner.root.remove(); }
    }],
    ["destroy restores DOM and makes late calls inert", f => {
      const host = role(f.root, "controls");
      const before = host.textContent;
      f.controller.open("privacy");
      f.controller.destroy();
      f.controller.destroy();
      equal(f.root.querySelectorAll('[data-ruview-help-action]').length, 0, "Owned triggers and DOM must be removed.");
      equal(host.textContent, before, "Destroy cannot replace the product's content.");
      equal(f.controller.open(), false, "Late open must be inert.");
      equal(f.controller.update(reportedState("ruview")), false, "Late updates must be inert.");
      equal(f.controller.close(), false, "Late close must be inert.");
      equal(f.controller.getState().mode, "destroyed", "Lifetime must be explicit.");
    }],
    ["pagehide tears down owned help without product commands", f => {
      f.controller.start();
      f.win.dispatchEvent(new f.win.Event("pagehide"));
      equal(f.controller.getState().mode, "destroyed", "Page exit must destroy help.");
      equal(role(f.root, "help-host"), null, "Page exit must remove help DOM.");
      equal(f.operations(), 0, "Page exit cannot become a recording stop or deletion.");
    }]
  ];

  function run(options) {
    if (!options || !options.document || !options.document.body ||
        !options.help || typeof options.help.mount !== "function" || !options.content) {
      throw new TypeError("Provide an isolated browser document, the real help module and its content.");
    }
    const doc = options.document;
    const results = [];
    for (const [name, test] of cases) {
      const fixture = makeWorkspace(doc, doc.body);
      let controller = null;
      try {
        controller = options.help.mount(fixture.root, { content: options.content });
        test(Object.assign({}, fixture, {
          doc, win: doc.defaultView, help: options.help, content: options.content, controller
        }));
        results.push({ name, status: "PASS" });
      } catch (error) {
        results.push({ name, status: "FAIL", message: error instanceof Error ? error.message : "Test threw." });
      } finally {
        if (controller) controller.destroy();
        const mounted = fixture.root.querySelector('[data-ruview-role="help-host"]');
        if (mounted) {
          // Remount cases own a replacement controller that the fixture must also retire.
          options.help.mount(fixture.root, { content: options.content }).destroy();
        }
        fixture.root.remove();
      }
    }
    const report = Object.freeze({
      cases: results.length, passed: results.filter(item => item.status === "PASS").length,
      failed: results.filter(item => item.status === "FAIL").length, results
    });
    if (report.failed) {
      const error = new Error("RuView help navigation cases failed.");
      error.report = report;
      throw error;
    }
    return report;
  }

  global.CCRuViewHelpTests = Object.freeze({ run, caseNames: Object.freeze(cases.map(item => item[0])) });
})(window);

