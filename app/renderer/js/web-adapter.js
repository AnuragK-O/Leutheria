// Web browser fallback adapter when not running inside Electron
(function () {
  if (window.leutheria) return; // Running inside Electron, nothing to do

  const AGENT_WS_URL = "ws://127.0.0.1:8765";
  let ws = null;
  const listeners = {
    log: [],
    chat: [],
    timeline: [],
    ghost: [],
    confirmRequest: [],
    confirmResolved: [],
    speech: [],
    skillProposed: [],
    status: [],
    inventoryChanged: [],
  };

  const pendingRequests = new Map();
  let nextRequestId = 1;
  let pendingIsChat = false;

  function emit(event, data) {
    (listeners[event] || []).forEach((fn) => {
      try {
        fn(data);
      } catch (e) {
        console.error(e);
      }
    });
  }

  function sendControl(payload) {
    return new Promise((resolve) => {
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        resolve({ ok: false, error: "not connected to agent" });
        return;
      }
      const requestId = String(nextRequestId++);
      pendingRequests.set(requestId, resolve);
      ws.send(JSON.stringify({ ...payload, request_id: requestId }));
      setTimeout(() => {
        if (pendingRequests.delete(requestId)) resolve({ ok: false, error: "agent timed out" });
      }, 10000);
    });
  }

  function connect() {
    emit("status", "connecting");
    try {
      ws = new WebSocket(AGENT_WS_URL);
    } catch (e) {
      emit("status", "error");
      setTimeout(connect, 2000);
      return;
    }

    ws.onopen = () => {
      emit("status", "connected");
      emit("log", "[bridge] connected to agent via browser WebSocket");
    };

    ws.onmessage = (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (err) {
        emit("log", `[bridge] received: ${event.data}`);
        if (pendingIsChat) {
          pendingIsChat = false;
          emit("chat", { role: "assistant", text: event.data });
        }
        return;
      }

      emit("log", `[bridge] received: ${JSON.stringify(payload)}`);

      if (payload.type === "control_result") {
        const resolve = pendingRequests.get(payload.request_id);
        if (resolve) {
          pendingRequests.delete(payload.request_id);
          resolve(payload);
        }
        return;
      }

      if (payload.type === "timeline_event") {
        emit("timeline", payload);
        return;
      }

      if (payload.type === "ghost_guidance" || payload.tool === "ghost_guidance") {
        const guidance = payload.guidance || payload.args || {};
        emit("ghost", guidance);
        return;
      }

      if (payload.type === "confirmation_required") {
        emit("confirmRequest", payload);
        return;
      }

      if (payload.type === "confirmation_resolved") {
        emit("confirmResolved", payload);
        return;
      }

      if (payload.type === "transcript") {
        emit("chat", { role: "user", text: payload.text, voice: true });
        return;
      }

      if (payload.type === "speech") {
        emit("speech", payload.data);
        return;
      }

      if (payload.type === "skill_proposed") {
        emit("skillProposed", payload);
        return;
      }

      if (payload.type === "skill_saved") {
        emit("chat", { role: "assistant", text: `✅ Saved "${payload.name}" as a reusable skill.` });
        emit("inventoryChanged");
        return;
      }

      if (payload.type === "response") {
        emit("chat", {
          role: "assistant",
          text: payload.text,
          trace: payload.trace || [],
          skill_used: payload.skill_used,
          task_id: payload.task_id,
          mode: payload.mode,
        });
        return;
      }

      if (payload.type === "tool_result") {
        emit("chat", {
          role: "assistant",
          text: "",
          trace: [{ tool: payload.tool, args: {}, result: payload.result }],
        });
        return;
      }

      if (pendingIsChat) {
        pendingIsChat = false;
        emit("chat", { role: "assistant", text: payload.text || JSON.stringify(payload) });
      }
    };

    ws.onerror = () => {
      emit("status", "error");
    };

    ws.onclose = () => {
      emit("status", "disconnected");
      setTimeout(connect, 2000);
    };
  }

  window.leutheria = {
    onLog: (cb) => listeners.log.push(cb),
    onChat: (cb) => listeners.chat.push(cb),
    onTimelineEvent: (cb) => listeners.timeline.push(cb),
    onGhostTarget: (cb) => listeners.ghost.push(cb),
    onConfirmRequest: (cb) => listeners.confirmRequest.push(cb),
    onConfirmResolved: (cb) => listeners.confirmResolved.push(cb),
    onSpeech: (cb) => listeners.speech.push(cb),
    onSkillProposed: (cb) => listeners.skillProposed.push(cb),
    onStatus: (cb) => listeners.status.push(cb),
    onInventoryChanged: (cb) => listeners.inventoryChanged.push(cb),

    sendCommand: (text, mode = "copilot") => {
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        emit("chat", { role: "assistant", text: "⚠️ not connected to agent yet" });
        return;
      }
      emit("chat", { role: "user", text, mode });
      pendingIsChat = true;
      ws.send(JSON.stringify({ type: "command", text, mode }));
    },

    sendConfirmResponse: (id, approved, remember, correction) => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(JSON.stringify({ type: "confirm", id, approved, remember, correction }));
    },

    sendAudio: (data, format, mode = "copilot") => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      pendingIsChat = true;
      ws.send(JSON.stringify({ type: "audio", data, format, mode }));
    },

    sendSkillResponse: (id, approved, resolutions) => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(JSON.stringify({ type: "skill_response", id, approved, resolutions }));
    },

    getInventory: () => sendControl({ type: "inventory" }),
    getMetrics: () => sendControl({ type: "get_metrics" }),
    exportSkill: (name) => sendControl({ type: "export_skill", name }),
    importSkill: (packageStr) => sendControl({ type: "import_skill", package: packageStr }),
    getTrace: (taskId) => sendControl({ type: "get_trace", task_id: taskId }),
    setPreference: (name, changes) => sendControl({ type: "set_preference", name, ...changes }),
    deleteSkill: (name) => sendControl({ type: "delete_skill", name }),
    revokeTrust: (key) => sendControl({ type: "revoke_trust", key }),
  };

  connect();
})();
