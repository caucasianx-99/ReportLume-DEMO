// Clients page: choosing which clients stay active within the plan's
// capacity. Loaded as a file because the CSP blocks inline scripts.
"use strict";

document.querySelectorAll("[data-capacity-selection]").forEach((form) => {
  const capacity = Number(form.dataset.limit);
  const checks = Array.from(
    form.querySelectorAll("[data-capacity-checkbox]")
  );
  const count = form.querySelector("[data-capacity-count]");
  const submit = form.querySelector("[data-capacity-submit]");
  const search = form.querySelector("[data-capacity-search]");

  if (
    !Number.isSafeInteger(capacity) ||
    capacity < 1 ||
    capacity > 200
  ) return;

  const refresh = () => {
    const selected = checks.filter((box) => box.checked).length;
    if (count) count.textContent = String(selected);
    if (submit) submit.disabled = selected !== capacity;
  };

  for (const checkbox of checks) {
    checkbox.addEventListener("change", refresh);
  }

  if (search) {
    search.addEventListener("input", () => {
      const term = search.value.trim().toLowerCase();

      form.querySelectorAll("[data-capacity-client]").forEach((row) => {
        row.hidden = !row.dataset.searchName.includes(term);
      });
    });
  }

  const selectVisible = form.querySelector(
    "[data-capacity-select-visible]"
  );
  const clearVisible = form.querySelector(
    "[data-capacity-clear-visible]"
  );

  if (selectVisible) {
    selectVisible.addEventListener("click", () => {
      let selected = checks.filter((box) => box.checked).length;

      for (const checkbox of checks) {
        if (selected >= capacity) break;

        const row = checkbox.closest("[data-capacity-client]");
        if (!checkbox.checked && row && !row.hidden) {
          checkbox.checked = true;
          selected += 1;
        }
      }
      refresh();
    });
  }

  if (clearVisible) {
    clearVisible.addEventListener("click", () => {
      for (const checkbox of checks) {
        const row = checkbox.closest("[data-capacity-client]");
        if (row && !row.hidden) checkbox.checked = false;
      }
      refresh();
    });
  }

  form.addEventListener("submit", (event) => {
    if (
      checks.filter((box) => box.checked).length !== capacity
    ) {
      event.preventDefault();
    }
  });

  refresh();
});
