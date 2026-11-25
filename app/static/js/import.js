"use strict";

(() => {
  const root = document.getElementById("csv-import-app");

  if (!root) {
    return;
  }

  const clientId = root.dataset.clientId;
  const byId = (id) => document.getElementById(id);

  const fileInput = byId("content-csv");
  const message = byId("import-message");
  const previewButton = byId("preview-csv");
  const confirmButton = byId("confirm-csv");
  const details = byId("preview-details");
  const nextActions = byId("import-next-actions");

  let receipt = "";
  let selectedFile = null;
  let importCompleted = false;
  let conflicts = [];
  let dateChangeCount = 0;

  const formatter = new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC"
  });

  function displayDate(value) {
    if (
      typeof value !== "string" ||
      !/^\d{4}-\d{2}-\d{2}$/.test(value)
    ) {
      return value || "Not available";
    }

    const parsed = new Date(`${value}T12:00:00Z`);

    if (
      Number.isNaN(parsed.getTime()) ||
      parsed.toISOString().slice(0, 10) !== value
    ) {
      return value;
    }

    return formatter.format(parsed);
  }

  function displayMonth(value) {
    if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(value)) {
      return value;
    }

    return new Intl.DateTimeFormat("en-GB", {
      month: "long",
      year: "numeric",
      timeZone: "UTC"
    }).format(new Date(`${value}-01T12:00:00Z`));
  }

  const zone =
    Intl.DateTimeFormat().resolvedOptions().timeZone;

  if (zone && byId("measurement-zone")) {
    byId("measurement-zone").value = zone;
  }

  // A device date is only a suggestion. It is not proof of the
  // actual export's measurement date.
  const measurementDate = byId("measurement-date");

  if (measurementDate && !measurementDate.value) {
    const today = new Date();
    const year = today.getFullYear();
    const month = String(today.getMonth() + 1).padStart(2, "0");
    const day = String(today.getDate()).padStart(2, "0");

    measurementDate.value = `${year}-${month}-${day}`;
  }

  function say(value, isError = false) {
    message.textContent = value;
    message.className = isError
      ? "import-error-text"
      : "muted";
  }

  function addList(id, values, fallback) {
    const list = byId(id);
    list.replaceChildren();

    const texts = Array.isArray(values) && values.length
      ? values
      : [fallback];

    for (const text of texts) {
      const item = document.createElement("li");
      item.textContent = String(text);
      list.appendChild(item);
    }
  }

  function rowLabel(row) {
    const parts = [
      `Row ${row.row}`,
      displayDate(row.date),
      `${row.impressions ?? "–"} impressions`,
      `${row.engagements ?? "–"} engagements`,
      `${row.likes ?? "–"} likes`
    ];

    return `${parts.join(" · ")} — ${row.text}`;
  }

  function showDuplicates(groups) {
    const section = byId("preview-duplicates");
    const container = byId("duplicate-groups");
    container.replaceChildren();
    conflicts = Array.isArray(groups) ? groups : [];
    section.hidden = conflicts.length === 0;

    for (const group of conflicts) {
      const fieldset = document.createElement("fieldset");
      const legend = document.createElement("legend");
      legend.textContent = `Post ${group.post_id}`;
      fieldset.appendChild(legend);

      for (const row of group.rows) {
        const label = document.createElement("label");
        label.className = "consent-label";
        const input = document.createElement("input");
        input.type = "radio";
        input.name = `duplicate-${group.post_id}`;
        input.value = String(row.row);
        const text = document.createElement("span");
        text.textContent = rowLabel(row);
        label.append(input, text);
        fieldset.appendChild(label);
      }

      container.appendChild(fieldset);
    }
  }

  function showDateChanges(count, changes) {
    dateChangeCount = Number(count) || 0;
    byId("preview-date-changes").hidden = dateChangeCount === 0;
    byId("date-changes-approved").checked = false;

    const items = (changes || []).map((change) => {
      return `Post ${change.post_id}: ${displayDate(change.previous)} → ` +
        displayDate(change.new);
    });

    if (dateChangeCount > items.length) {
      items.push(`and ${dateChangeCount - items.length} more`);
    }

    addList("date-change-list", items, "");
  }

  function duplicateChoices() {
    const choices = {};

    for (const group of conflicts) {
      const picked = root.querySelector(
        `input[name="duplicate-${group.post_id}"]:checked`
      );

      if (!picked) {
        return null;
      }

      choices[group.post_id] = Number(picked.value);
    }

    return choices;
  }

  async function sendFile(url, file, extra = {}) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        "Content-Type": "text/csv",
        "X-CSRF-Token": byId("import-csrf").value,
        "X-Upload-Filename": encodeURIComponent(file.name),
        ...extra
      },
      body: file
    });

    let result;

    try {
      result = await response.json();
    } catch {
      throw new Error(
        "The server response could not be read. Please try again."
      );
    }

    if (!response.ok) {
      const detail = typeof result.detail === "string"
        ? result.detail
        : "Import request failed.";

      throw new Error(detail);
    }

    return result;
  }

  function resetPreview() {
    receipt = "";
    selectedFile = null;
    importCompleted = false;

    details.hidden = true;
    nextActions.hidden = true;

    byId("import-approved").checked = false;
    showDuplicates([]);
    showDateChanges(0, []);
    byId("preview-content-changes").hidden = true;

    confirmButton.disabled = false;
    previewButton.disabled = false;

    say("");
  }

  fileInput.addEventListener("change", resetPreview);

  previewButton.addEventListener("click", async () => {
    resetPreview();

    const file = fileInput.files[0];

    if (!file) {
      say("Choose your X Content CSV first.", true);
      return;
    }

    if (file.size === 0 || file.size > 10 * 1024 * 1024) {
      say("Choose a non-empty CSV smaller than 10 MiB.", true);
      return;
    }

    previewButton.disabled = true;
    say("Reading CSV preview...");

    try {
      const data = await sendFile(
        `/api/clients/${clientId}/imports/preview`,
        file
      );

      receipt = data.preview_token;
      selectedFile = file;

      byId("preview-count").textContent =
        String(data.row_count);

      byId("preview-dates").textContent =
        `${displayDate(data.published_start)} to ` +
        displayDate(data.published_end);

      byId("preview-months").textContent =
        Object.entries(data.months || {})
          .map(([month, count]) => {
            return `${displayMonth(month)}: ${count}`;
          })
          .join(" · ");

      byId("preview-replies").textContent =
        String(data.possible_replies);

      byId("preview-existing").textContent =
        String(data.already_imported ?? 0);

      showDuplicates(data.duplicate_conflicts);
      showDateChanges(data.date_change_count, data.date_changes);

      const contentChanges = Number(data.content_change_count) || 0;
      byId("content-change-count").textContent = String(contentChanges);
      byId("preview-content-changes").hidden = contentChanges === 0;

      addList(
        "preview-warnings",
        data.warnings,
        "No issues detected in this preview."
      );

      addList(
        "preview-sample",
        (data.sample || []).map((post) => {
          return `${displayDate(post.date)} — ${post.text}`;
        }),
        "No sample records."
      );

      if (data.suggested_export) {
        byId("export-start").value =
          data.suggested_export.start || "";

        byId("export-end").value =
          data.suggested_export.end || "";
      }

      details.hidden = false;

      say(
        "Preview ready. Check the data and confirm the dates " +
        "before importing."
      );

    } catch (error) {
      say(error.message, true);

    } finally {
      previewButton.disabled = false;
    }
  });

  confirmButton.addEventListener("click", async () => {
    if (importCompleted) {
      return;
    }

    if (
      !selectedFile ||
      !receipt ||
      fileInput.files[0] !== selectedFile
    ) {
      say("Preview the selected file first.", true);
      return;
    }

    const required = [
      "export-start",
      "export-end",
      "measurement-date",
      "measurement-zone"
    ];

    if (required.some((id) => !byId(id).value.trim())) {
      say(
        "Confirm the export dates, data date and time zone.",
        true
      );
      return;
    }

    const choices = duplicateChoices();

    if (choices === null) {
      say("Choose which row to keep for each repeated post.", true);
      return;
    }

    if (dateChangeCount && !byId("date-changes-approved").checked) {
      say("Confirm the changed publication dates before importing.", true);
      return;
    }

    if (!byId("import-approved").checked) {
      say("Confirm your review before importing.", true);
      return;
    }

    const extraHeaders = {};

    if (conflicts.length) {
      extraHeaders["X-Duplicate-Choices"] = JSON.stringify(choices);
    }

    if (dateChangeCount) {
      extraHeaders["X-Accept-Date-Changes"] = "yes";
    }

    confirmButton.disabled = true;
    previewButton.disabled = true;

    say("Saving your data. Do not close this page.");

    try {
      const result = await sendFile(
        `/api/clients/${clientId}/imports/confirm`,
        selectedFile,
        {
          "X-Preview-Token": receipt,
          "X-Confirm-Import": "yes",
          "X-Export-Start": byId("export-start").value,
          "X-Export-End": byId("export-end").value,
          "X-Measurement-Date": byId("measurement-date").value,
          "X-Measurement-Time": byId("measurement-time").value,
          "X-Measurement-Zone":
            byId("measurement-zone").value.trim(),
          ...extraHeaders
        }
      );

      receipt = "";
      importCompleted = true;
      details.hidden = true;
      nextActions.hidden = false;

      const trialNote = result.trial_started
        ? " Your 72-hour free trial has started."
        : "";

      say(
        `${result.rows} post and reply observations saved.` +
        trialNote
      );

      nextActions.scrollIntoView({
        behavior: "smooth",
        block: "nearest"
      });

    } catch (error) {
      say(
        `${error.message} You may retry the same file with ` +
        "the same confirmed dates.",
        true
      );

      confirmButton.disabled = false;
      previewButton.disabled = false;
    }
  });
})();
