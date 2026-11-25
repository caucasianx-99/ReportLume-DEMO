
"use strict";

/*
 * Classification changes are version-checked on the server.
 * The page may show 100 posts, but each commit contains at most 20 writes.
 * Selections and undo history are not persisted in browser storage.
 */

(() => {
  const root = document.getElementById("classification-app");

  if (!root || root.dataset.editable !== "true") {
    return;
  }

  const clientId = root.dataset.clientId;
  const byId = (id) => document.getElementById(id);

  const rows = Array.from(
    root.querySelectorAll(".classification-row")
  );

  const checkboxes = Array.from(
    root.querySelectorAll(".post-checkbox")
  );

  const saveButton = byId("save-classifications");
  const replyButton = byId("confirm-possible-replies");
  const undoButton = byId("undo-classifications");

  const message = byId("classification-message");
  const selectVisible = byId("select-visible");
  const selectedCount = byId("selected-count");

  const MAX_BATCH = 20;

  if (
    !saveButton ||
    !replyButton ||
    !undoButton ||
    !message ||
    !selectVisible ||
    !selectedCount
  ) {
    return;
  }

  let busy = false;
  let lastUndo = null;

  function notify(text, error = false) {
    message.textContent = text;

    message.className = error
      ? "import-error-text"
      : "muted";
  }

  function checkboxFor(row) {
    return row.querySelector(".post-checkbox");
  }

  function visibleRows() {
    return rows.filter((row) => !row.hidden);
  }

  function selectedRows() {
    return visibleRows().filter((row) => {
      const checkbox = checkboxFor(row);

      return Boolean(
        checkbox && checkbox.checked
      );
    });
  }

  function updateSelection() {
    const visible = visibleRows();
    const selected = selectedRows().length;

    selectedCount.textContent = (
      `${selected} ${selected === 1 ? "item" : "items"} selected`
    );

    saveButton.disabled = (
      busy || selected === 0
    );

    replyButton.disabled = (
      busy || selected === 0
    );

    undoButton.disabled = (
      busy || !lastUndo || !lastUndo.length
    );

    selectVisible.disabled = (
      busy || visible.length === 0
    );

    selectVisible.checked = (
      visible.length > 0 && selected === visible.length
    );

    selectVisible.indeterminate = (
      selected > 0 && selected < visible.length
    );
  }

  function hasPossibleReply(row) {
    return Boolean(
      row.querySelector(".possible-reply") &&
      row.dataset.contentType === "Unknown"
    );
  }

  function filterRows() {
    const search = byId("post-search").value.toLowerCase().trim();
    const author = byId("author-filter").value;
    const contentType = byId("type-filter").value;
    const suggestion = byId("suggestion-filter").value;

    for (const row of rows) {
      const content = row.querySelector(
        ".classification-post-text"
      );

      const text = content
        ? content.textContent.toLowerCase()
        : "";

      const matches = (
        (!search || text.includes(search)) &&
        (!author || row.dataset.author === author) &&
        (!contentType || row.dataset.contentType === contentType) &&
        (!suggestion || hasPossibleReply(row))
      );

      row.hidden = !matches;

      if (!matches) {
        const checkbox = checkboxFor(row);

        if (checkbox) {
          checkbox.checked = false;
        }
      }
    }

    updateSelection();
  }

  function currentChanges() {
    const changes = {};

    const author = byId("edit-authorship").value;
    const contentType = byId("edit-content-type").value;
    const format = byId("edit-format").value;
    const topic = byId("edit-topic").value.trim();

    if (author) {
      changes.authorship = author;
    }

    if (contentType) {
      changes.content_type = contentType;
    }

    if (format) {
      changes.format = format;
    }

    if (byId("clear-topic").checked) {
      changes.topic = "";
    } else if (topic) {
      changes.topic = topic;
    }

    return changes;
  }

  function buildPatches(changes, selected) {
    return selected.map((row) => ({
      post_id: row.dataset.postId,
      expected_version: row.dataset.version,
      changes: {
        ...changes
      }
    }));
  }

  async function sendPatches(patches) {
    const response = await fetch(
      `/api/clients/${encodeURIComponent(clientId)}/posts/classify`,
      {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": byId("classification-csrf").value
        },
        body: JSON.stringify({
          patches
        })
      }
    );

    let result;

    try {
      result = await response.json();
    } catch {
      throw new Error(
        "The server response could not be confirmed. " +
        "Refresh before retrying."
      );
    }

    if (!response.ok) {
      throw new Error(
        typeof result.detail === "string"
          ? result.detail
          : "Classification could not be saved."
      );
    }

    if (
      !Array.isArray(result.updated) ||
      result.updated.length !== patches.length ||
      result.updated.some((item) => (
        !item ||
        typeof item.version !== "string" ||
        typeof item.post_id !== "string"
      ))
    ) {
      throw new Error(
        "Saved document versions could not be confirmed. " +
        "Refresh before retrying."
      );
    }

    return result.updated;
  }

  function displayLabel(field, value) {
    if (field === "topic" && !value) {
      return "Unassigned";
    }

    if (
      value === "Unknown" ||
      value === null ||
      value === undefined
    ) {
      return "Not categorized";
    }

    return String(value);
  }

  function renderUpdated(updated) {
    const byPostId = new Map(
      rows.map((row) => [
        row.dataset.postId,
        row
      ])
    );

    for (const item of updated) {
      const row = byPostId.get(item.post_id);

      if (!row) {
        continue;
      }

      row.dataset.version = item.version;

      for (const [field, value] of Object.entries(
        item.after || {}
      )) {
        if (field === "content_type") {
          row.dataset.contentType = value;
        } else if (field === "authorship") {
          row.dataset.author = value;
        } else {
          row.dataset[field] = value;
        }

        const label = row.querySelector(
          `.label-${field}`
        );

        if (label) {
          label.textContent = displayLabel(
            field,
            value
          );
        }
      }

      const checkbox = checkboxFor(row);

      if (checkbox) {
        checkbox.checked = false;
      }
    }

    filterRows();
  }

  function setBusy(value) {
    busy = value;

    for (const checkbox of checkboxes) {
      checkbox.disabled = value;
    }

    updateSelection();
  }

  async function sendInBatches(patches) {
    const completed = [];

    for (
      let offset = 0;
      offset < patches.length;
      offset += MAX_BATCH
    ) {
      const group = patches.slice(
        offset,
        offset + MAX_BATCH
      );

      try {
        const updated = await sendPatches(group);

        completed.push(...updated);
        renderUpdated(updated);

      } catch (error) {
        return {
          completed,
          error
        };
      }
    }

    return {
      completed,
      error: null
    };
  }

  async function saveSelected(forcedChanges = null) {
    if (busy) {
      return;
    }

    const selected = selectedRows();

    if (!selected.length) {
      notify(
        "Select at least one post or reply on this page.",
        true
      );
      return;
    }

    const changes = forcedChanges || currentChanges();

    if (!Object.keys(changes).length) {
      notify(
        "Choose at least one classification field to change.",
        true
      );
      return;
    }

    const patches = buildPatches(
      changes,
      selected
    );

    setBusy(true);

    try {
      const result = await sendInBatches(patches);

      lastUndo = result.completed.map((item) => ({
        post_id: item.post_id,
        expected_version: item.version,
        changes: item.before
      }));

      if (result.error) {
        notify(
          `${result.completed.length} of ${patches.length} updates confirmed. ` +
          `The remaining changes were not confirmed: ${result.error.message}`,
          true
        );
      } else {
        notify(
          `${result.completed.length} items saved. ` +
          "Undo is available until those records change."
        );
      }
    } finally {
      setBusy(false);
    }
  }

  saveButton.addEventListener("click", () => {
    saveSelected();
  });

  replyButton.addEventListener("click", () => {
    if (busy) {
      return;
    }

    const chosen = selectedRows();
    const eligible = chosen.filter(hasPossibleReply);

    if (!eligible.length) {
      notify(
        "Select at least one item marked Possible reply.",
        true
      );
      return;
    }

    for (const row of chosen) {
      const checkbox = checkboxFor(row);

      if (checkbox) {
        checkbox.checked = eligible.includes(row);
      }
    }

    saveSelected({
      content_type: "Reply"
    });
  });

  undoButton.addEventListener("click", async () => {
    if (
      busy ||
      !lastUndo ||
      !lastUndo.length
    ) {
      return;
    }

    const patches = lastUndo;

    setBusy(true);

    try {
      const result = await sendInBatches(patches);

      const reversedIds = new Set(
        result.completed.map((item) => item.post_id)
      );

      lastUndo = result.error
        ? patches.filter((item) => (
            !reversedIds.has(item.post_id)
          ))
        : null;

      if (result.error) {
        notify(
          `${result.completed.length} of ${patches.length} reversals confirmed. ` +
          `Remaining changes were not confirmed: ${result.error.message}`,
          true
        );
      } else {
        notify(
          "Previous classification changes restored."
        );
      }
    } finally {
      setBusy(false);
    }
  });

  selectVisible.addEventListener("change", () => {
    for (const row of visibleRows()) {
      const checkbox = checkboxFor(row);

      if (checkbox) {
        checkbox.checked = selectVisible.checked;
      }
    }

    updateSelection();
  });

  for (const checkbox of checkboxes) {
    checkbox.addEventListener(
      "change",
      updateSelection
    );
  }

  for (const id of [
    "post-search",
    "author-filter",
    "type-filter",
    "suggestion-filter"
  ]) {
    const element = byId(id);

    element.addEventListener(
      id === "post-search" ? "input" : "change",
      filterRows
    );
  }

  updateSelection();
})();
