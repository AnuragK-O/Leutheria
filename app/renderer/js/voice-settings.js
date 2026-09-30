/* The Voice view: the always-listening session's settings (wake word,
   timeouts, dismiss phrases, microphone). Edits collect in a local draft and
   are sent together as one set_voice_settings carrying only the changed keys.
   The agent owns the ranges -- it validates the whole update and rejects it on
   the first bad key, and that error is shown against the field it names. The
   client only checks that a value is well-formed (a number, a non-empty
   phrase, at least one wake model). */

(function (LX) {
  const { el, clear, icon, toast } = LX;

  const bodyEl = document.getElementById("voice-body");

  const KEYS = [
    "enabled",
    "wake_models",
    "wake_threshold",
    "silence_timeout_s",
    "endpoint_silence_ms",
    "dismiss_phrases",
    "input_device",
  ];

  // Two of openWakeWord's pretrained models aren't wake words at all -- they
  // fire on a whole request -- which is worth saying before someone picks one.
  const MODEL_NOTES = {
    timer: "fires on requests like “set a timer”",
    weather: "fires on requests like “what's the weather”",
  };

  // voice_state -> label, matching the header pill's wording.
  const STALLED_HINT =
    "Enabled but not listening. If this doesn't change in a few seconds, the microphone or wake model failed to start — saving any change retries.";

  const STATE_LABELS = {
    off: "Off",
    idle: "Idle",
    listening: "Listening",
    hearing: "Listening",
    transcribing: "Thinking",
    thinking: "Thinking",
    speaking: "Speaking",
    awaiting_confirmation: "Needs approval",
  };

  let saved = null; // last settings the agent reported
  let draft = null; // what the form shows
  let raw = {}; // numeric fields as typed, so a half-typed value survives a re-render
  let loadError = null;
  let loading = false;
  let saving = false;
  let saveError = null; // { key, message } from the agent
  let clientErrors = {}; // key -> message
  let devices = null;
  let devicesError = null;
  let pretrained = null;
  let modelsError = null;
  let customStatus = {}; // custom model path -> exists on the agent's disk
  let addModelError = null;
  let voice = { state: "off", session: false, connected: false };

  const copy = (value) => JSON.parse(JSON.stringify(value));
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  // --- data --------------------------------------------------------------

  async function load() {
    if (loading) return;
    loading = true;
    const [settings, deviceList, models] = await Promise.all([
      window.leutheria.getVoiceSettings(),
      window.leutheria.listInputDevices(),
      window.leutheria.listWakeModels([]),
    ]);
    loading = false;

    if (!settings || !settings.ok) {
      loadError = (settings && settings.error) || "Couldn't read voice settings";
    } else {
      loadError = null;
      // Dirty is judged against the previous copy: an edit in progress is
      // kept, otherwise the form follows whatever the agent now holds.
      const keepDraft = isDirty();
      saved = settings.settings;
      if (!keepDraft) resetDraft();
    }

    devices = deviceList && deviceList.ok ? deviceList.devices : null;
    devicesError = deviceList && !deviceList.ok ? deviceList.error : null;
    pretrained = models && models.ok ? models.pretrained : null;
    modelsError = models && !models.ok ? models.error : null;

    if (saved) await checkCustomModels(customModels(saved.wake_models));
    render();
  }

  async function checkCustomModels(paths) {
    const unknown = paths.filter((path) => !(path in customStatus));
    if (!unknown.length) return;
    const result = await window.leutheria.listWakeModels(unknown);
    if (result && result.ok) {
      for (const entry of result.paths) customStatus[entry.path] = entry.exists;
    }
  }

  function resetDraft() {
    draft = copy(saved);
    raw = {
      silence_timeout_s: saved.silence_timeout_s === null ? "180" : String(saved.silence_timeout_s),
      endpoint_silence_ms: String(saved.endpoint_silence_ms),
    };
    clientErrors = {};
    saveError = null;
    addModelError = null;
  }

  function pretrainedNames() {
    return (pretrained || []).map((model) => model.name);
  }

  function customModels(models) {
    const names = pretrainedNames();
    return models.filter((model) => !names.includes(model));
  }

  function changedKeys() {
    if (!saved || !draft) return [];
    return KEYS.filter((key) => !same(draft[key], saved[key]));
  }

  function isDirty() {
    return changedKeys().length > 0;
  }

  // --- validation (well-formedness only; ranges are the agent's call) -----

  function validate() {
    const errors = {};
    for (const key of ["silence_timeout_s", "endpoint_silence_ms"]) {
      if (key === "silence_timeout_s" && draft[key] === null) continue; // "never"
      if (!Number.isFinite(draft[key])) errors[key] = "Enter a number.";
    }
    if (!draft.wake_models.length) errors.wake_models = "Pick at least one wake word.";
    if (!draft.dismiss_phrases.length) errors.dismiss_phrases = "Keep at least one phrase, or a session only ends on the timeout.";
    return errors;
  }

  // The agent's messages lead with the setting's key ("wake_threshold must be
  // between 0 and 1", "unknown voice setting: foo"), so that's how an error is
  // matched back to its field.
  function errorKey(message) {
    return KEYS.find((key) => message.includes(key)) || null;
  }

  async function save() {
    const keys = changedKeys();
    if (!keys.length || saving) return;
    clientErrors = validate();
    if (Object.keys(clientErrors).length) {
      render();
      return;
    }
    const partial = Object.fromEntries(keys.map((key) => [key, draft[key]]));
    saving = true;
    saveError = null;
    LX.app.renderActions();
    const result = await window.leutheria.setVoiceSettings(partial);
    saving = false;
    if (result && result.ok) {
      saved = result.settings;
      resetDraft();
      toast(`Voice settings saved`);
    } else {
      const message = (result && result.error) || "Couldn't save voice settings";
      saveError = { key: errorKey(message), message };
    }
    render();
  }

  function revert() {
    if (saved) resetDraft();
    render();
  }

  // Called after any edit that doesn't need the form rebuilt (typing, the
  // slider): keeps the topbar's Save/Revert and subtitle in step.
  function touched(key) {
    const hadError = Boolean(clientErrors[key] || (saveError && saveError.key === key));
    delete clientErrors[key];
    if (saveError && saveError.key === key) saveError = null;
    if (hadError) {
      // Clear the stale error in place; a full render would drop focus from
      // the field being corrected.
      const fieldRow = bodyEl.querySelector(`[data-key="${key}"]`);
      if (fieldRow) {
        fieldRow.classList.remove("has-error");
        const message = fieldRow.querySelector(":scope > .field-error");
        if (message) message.remove();
      }
      if (!saveError && !Object.keys(clientErrors).length) {
        const banner = bodyEl.querySelector(".callout-danger");
        if (banner) banner.remove();
      }
    }
    LX.app.renderActions();
  }

  // --- pieces ------------------------------------------------------------

  function fieldError(key) {
    const message = clientErrors[key] || (saveError && saveError.key === key ? saveError.message : null);
    return message ? el("div.field-error", {}, icon("warning"), el("span", { text: message })) : null;
  }

  function row(key, title, description, control, below) {
    const hasError = Boolean(clientErrors[key] || (saveError && saveError.key === key));
    return el(
      "div.setting.setting-stack",
      { class: hasError ? "has-error" : "", "data-key": key },
      el(
        "div.setting-main",
        {},
        el("div.setting-text", {}, el("strong", { text: title }), description && el("span", { text: description })),
        control
      ),
      below,
      fieldError(key)
    );
  }

  function card(title, description, ...rows) {
    return el(
      "div.card",
      {},
      el("div.card-header", {}, el("h2", { text: title }), el("p", { text: description })),
      ...rows
    );
  }

  function humanModel(name) {
    return name
      .split("_")
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(" ");
  }

  function renderEnabled() {
    const toggle = el("button.switch", {
      role: "switch",
      "aria-checked": String(draft.enabled),
      onclick: () => {
        draft.enabled = !draft.enabled;
        touched("enabled");
        render();
      },
    });
    return row(
      "enabled",
      "Listen for the wake word",
      draft.enabled
        ? "The microphone stays open and a session starts when you say a wake word."
        : "The microphone is closed. ⌥Space or the header button still start a one-off session.",
      el("div.inline-controls", {}, el("span.badge", { id: "voice-live-state" }), toggle),
      el("div.setting-hint", { id: "voice-stalled", hidden: true, text: STALLED_HINT })
    );
  }

  // Patched in place on every voice_state rather than re-rendering the form,
  // which would steal focus from whatever field is being typed in.
  function renderLiveState() {
    const badge = document.getElementById("voice-live-state");
    const hint = document.getElementById("voice-stalled");
    if (!badge || !saved) return;
    const label = voice.connected ? STATE_LABELS[voice.state] || "Off" : "Off";
    badge.className = label === "Off" ? "badge" : "badge badge-success";
    badge.textContent = voice.session ? `${label} · in a session` : label;
    hint.hidden = !(voice.connected && saved.enabled && voice.state === "off" && !voice.session);
    // The agent says why when it knows (a mic that wouldn't open, a wake
    // model that wouldn't load); otherwise keep the generic hint.
    hint.textContent = voice.error
      ? `Couldn't start listening (${voice.error.stage}): ${voice.error.message}. Saving any change retries.`
      : STALLED_HINT;
  }

  function renderWakeModels() {
    const choices = el("div.choice-row.wrap");
    for (const model of pretrained || []) {
      const selected = draft.wake_models.includes(model.name);
      choices.appendChild(
        el(
          "button.choice",
          {
            class: selected ? "selected" : "",
            title: [
              model.name,
              MODEL_NOTES[model.name],
              model.downloaded ? null : "downloads the first time it's used",
            ]
              .filter(Boolean)
              .join(" — "),
            onclick: () => {
              draft.wake_models = selected
                ? draft.wake_models.filter((name) => name !== model.name)
                : [...draft.wake_models, model.name];
              touched("wake_models");
              render();
            },
          },
          humanModel(model.name),
          model.downloaded ? null : el("span.choice-note", { text: "download" })
        )
      );
    }

    const custom = customModels(draft.wake_models);
    const customList = el("div.custom-models");
    for (const path of custom) {
      const exists = customStatus[path];
      customList.appendChild(
        el(
          "div.custom-model",
          {},
          // The file name is the part worth seeing, so a long path loses its head.
          el("span.custom-model-path", { text: path.length > 64 ? `…${path.slice(-63)}` : path, title: path }),
          exists === false ? el("span.badge.badge-danger", { text: "not found" }) : null,
          el("button.btn.btn-sm", {
            text: "Remove",
            onclick: () => {
              draft.wake_models = draft.wake_models.filter((name) => name !== path);
              touched("wake_models");
              render();
            },
          })
        )
      );
    }

    const input = el("input.field-input.grow", {
      type: "text",
      placeholder: "/path/to/hey_leutheria.onnx",
      spellcheck: "false",
    });
    const add = async () => {
      const path = input.value.trim();
      addModelError = null;
      if (!path) return;
      if (!path.toLowerCase().endsWith(".onnx")) addModelError = "A custom wake word is a trained .onnx file.";
      else if (draft.wake_models.includes(path)) addModelError = "Already in the list.";
      else {
        delete customStatus[path];
        await checkCustomModels([path]);
        if (customStatus[path] === false) addModelError = "No file at that path on this Mac.";
      }
      if (!addModelError) {
        draft.wake_models = [...draft.wake_models, path];
        touched("wake_models");
      }
      render();
      if (addModelError) {
        const again = bodyEl.querySelector('[data-key="wake_models"] input');
        if (again) {
          again.value = path;
          again.focus();
        }
      }
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") add();
    });

    return row(
      "wake_models",
      "Wake words",
      "Any selected phrase starts a session. Models not yet on this Mac download the first time they're used.",
      null,
      el(
        "div.setting-body",
        {},
        pretrained
          ? choices
          : el("div.setting-hint", { text: modelsError || "Loading the available wake words…" }),
        custom.length ? customList : null,
        el("div.add-row", {}, input, el("button.btn.btn-sm", { text: "Add model", onclick: add })),
        addModelError ? el("div.field-error", {}, icon("warning"), el("span", { text: addModelError })) : null
      )
    );
  }

  function renderThreshold() {
    const readout = el("span.range-value", { text: Number(draft.wake_threshold).toFixed(2) });
    const slider = el("input.range", {
      type: "range",
      min: "0.1",
      max: "0.95",
      step: "0.05",
      value: String(draft.wake_threshold),
      oninput: (event) => {
        draft.wake_threshold = Math.round(Number(event.target.value) * 100) / 100;
        readout.textContent = draft.wake_threshold.toFixed(2);
        touched("wake_threshold");
      },
    });
    return row(
      "wake_threshold",
      "Wake sensitivity",
      "How sure the detector must be before it wakes. Lower wakes more easily, and more often by mistake.",
      el("div.inline-controls", {}, slider, readout)
    );
  }

  function numberField(key, unit, min, max, step) {
    const input = el("input.field-input.number", {
      type: "number",
      min: String(min),
      max: max === null ? null : String(max),
      step: String(step),
      value: raw[key],
      oninput: (event) => {
        raw[key] = event.target.value;
        draft[key] = event.target.value.trim() === "" ? NaN : Number(event.target.value);
        touched(key);
      },
    });
    return el("div.inline-controls", {}, input, el("span.unit", { text: unit }));
  }

  // The number field plus a "Never" box. Never saves null; unticking restores
  // whatever number was last typed, so toggling doesn't lose it.
  function timeoutField() {
    const never = draft.silence_timeout_s === null;
    const field = numberField("silence_timeout_s", "seconds", 5, null, 5);
    const input = field.querySelector("input");
    input.disabled = never;
    const box = el("input", {
      type: "checkbox",
      checked: never,
      onchange: (event) => {
        draft.silence_timeout_s = event.target.checked
          ? null
          : raw.silence_timeout_s.trim() === "" ? NaN : Number(raw.silence_timeout_s);
        touched("silence_timeout_s");
        render();
      },
    });
    field.appendChild(el("label.inline-check", {}, box, el("span", { text: "Never" })));
    return field;
  }

  function renderPhrases() {
    const chips = el("div.chips");
    for (const phrase of draft.dismiss_phrases) {
      chips.appendChild(
        el(
          "span.chip",
          {},
          el("span", { text: phrase }),
          el(
            "button.chip-remove",
            {
              title: `Remove “${phrase}”`,
              onclick: () => {
                draft.dismiss_phrases = draft.dismiss_phrases.filter((entry) => entry !== phrase);
                touched("dismiss_phrases");
                render();
              },
            },
            icon("x")
          )
        )
      );
    }
    const input = el("input.field-input.grow", { type: "text", placeholder: "Add a phrase, then press Enter" });
    const add = () => {
      const phrase = input.value.trim().replace(/\s+/g, " ");
      if (!phrase) return;
      if (!draft.dismiss_phrases.some((entry) => entry.toLowerCase() === phrase.toLowerCase())) {
        draft.dismiss_phrases = [...draft.dismiss_phrases, phrase];
        touched("dismiss_phrases");
      }
      render();
      const again = bodyEl.querySelector('[data-key="dismiss_phrases"] input');
      if (again) again.focus();
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") add();
    });
    return row(
      "dismiss_phrases",
      "Dismiss phrases",
      "Saying one of these on its own (a few words at most) ends the session.",
      null,
      el(
        "div.setting-body",
        {},
        draft.dismiss_phrases.length ? chips : null,
        el("div.add-row", {}, input, el("button.btn.btn-sm", { text: "Add", onclick: add }))
      )
    );
  }

  function renderDevice() {
    const current = draft.input_device;
    const select = el("select.field-input.select", {
      onchange: (event) => {
        draft.input_device = JSON.parse(event.target.value);
        touched("input_device");
        render();
      },
    });
    const defaultDevice = (devices || []).find((device) => device.default);
    const options = [
      [null, defaultDevice ? `System default (${defaultDevice.name})` : "System default"],
    ];
    let matched = current === null;
    for (const device of devices || []) {
      // The setting may be an index (hand-edited) or a name; a new pick is
      // stored by name, which survives devices being added and renumbered.
      const isCurrent = current === device.index || current === device.name;
      matched = matched || isCurrent;
      options.push([isCurrent ? current : device.name, device.name]);
    }
    if (!matched) options.push([current, `${current} (not connected)`]);
    for (const [value, label] of options) {
      const option = el("option", { value: JSON.stringify(value), text: label });
      if (same(value, current)) option.selected = true;
      select.appendChild(option);
    }

    return row(
      "input_device",
      "Microphone",
      "Listing devices never opens one. A device plugged in after Leutheria started appears after a restart.",
      el(
        "div.inline-controls",
        {},
        el("div.select-wrap", {}, select),
        el(
          "button.icon-button",
          {
            title: "Reload the device list",
            onclick: async () => {
              const result = await window.leutheria.listInputDevices();
              devices = result && result.ok ? result.devices : devices;
              devicesError = result && !result.ok ? result.error : null;
              render();
            },
          },
          icon("refresh")
        )
      ),
      devicesError ? el("div.setting-hint", { text: devicesError }) : null
    );
  }

  // --- entry points ------------------------------------------------------

  function render() {
    clear(bodyEl);
    LX.app.renderActions();

    if (!saved) {
      bodyEl.appendChild(
        el("div.empty", { text: loadError && voice.connected ? loadError : "Waiting for the agent…" })
      );
      return;
    }

    const errorCount = Object.keys(clientErrors).length;
    // Node.append would render a null as the text "null".
    const banner =
      saveError
        ? el(
            "div.callout.callout-danger",
            {},
            icon("warning"),
            el("span", {
              text: `Nothing was saved — ${saveError.message}. One invalid value refuses the whole update, so your other changes are still here, unsaved.`,
            })
          )
        : errorCount
          ? el(
              "div.callout.callout-danger",
              {},
              icon("warning"),
              el("span", { text: "Fix the highlighted field before saving." })
            )
          : null;
    if (banner) bodyEl.appendChild(banner);
    bodyEl.append(
      card(
        "Wake word",
        "What starts a hands-free session.",
        renderEnabled(),
        renderWakeModels(),
        renderThreshold()
      ),
      card(
        "Conversation",
        "How a session listens, and how it ends.",
        row(
          "endpoint_silence_ms",
          "End of utterance",
          "The pause after you stop talking before Leutheria treats it as finished. Shorter replies faster; too short cuts you off mid-sentence.",
          numberField("endpoint_silence_ms", "ms", 240, 5000, 50)
        ),
        row(
          "silence_timeout_s",
          "Silence timeout",
          "A session with nothing said for this long ends on its own. With Never, only a dismiss phrase or Stop listening ends it.",
          timeoutField()
        ),
        renderPhrases()
      ),
      card("Input", "The microphone the session listens on.", renderDevice())
    );
    renderLiveState();
  }

  function actions() {
    if (!saved) return [];
    const dirty = isDirty();
    return [
      el("button.btn", { text: "Revert", disabled: !dirty || saving, onclick: revert }),
      el("button.btn.btn-primary", {
        text: saving ? "Saving…" : "Save changes",
        disabled: !dirty || saving,
        onclick: save,
      }),
    ];
  }

  function subtitle() {
    if (!saved) return "Wake word, timeouts and microphone";
    const count = changedKeys().length;
    return count ? `${count} unsaved ${count === 1 ? "change" : "changes"}` : "Wake word, timeouts and microphone";
  }

  // Re-read from the agent each time the view is opened, unless there's an
  // edit in progress -- another window may have changed the settings.
  function show() {
    if (!draft || !isDirty()) load();
  }

  window.leutheria.onVoiceEvent((event) => {
    if (event.type !== "voice_state") return;
    voice = {
      state: event.state,
      session: Boolean(event.session),
      connected: event.connected !== false,
      error: event.error || null,
    };
    renderLiveState();
  });

  window.leutheria.onStatus((status) => {
    voice.connected = status === "connected";
    if (status === "connected" && LX.app && LX.app.currentView() === "voice") show();
  });

  window.leutheria.getVoiceState().then((state) => {
    if (state) voice = state;
    renderLiveState();
  });

  LX.voice = { render, actions, subtitle, show };
})(window.LX);
