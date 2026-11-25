"use strict";

/*
 * Accessible navigation for the read-only example workspace.
 *
 * The demonstration does not write data, create a client,
 * begin a trial or contact the reporting APIs.
 */

(() => {
  const root = document.getElementById("sample-workspace");

  if (!root) {
    return;
  }

  const panels = Array.from(
    root.querySelectorAll("[data-demo-panel]")
  );

  const buttons = Array.from(
    root.querySelectorAll("[data-demo-step]")
  );

  const availableSteps = new Set(
    panels.map((panel) => panel.dataset.demoPanel)
  );

  if (!panels.length || !buttons.length) {
    return;
  }

  function show(step, focusButton = false) {
    if (!availableSteps.has(step)) {
      return;
    }

    for (const panel of panels) {
      panel.hidden = panel.dataset.demoPanel !== step;
    }

    for (const button of buttons) {
      const active = button.dataset.demoStep === step;

      button.setAttribute(
        "aria-pressed",
        String(active)
      );

      if (active && focusButton) {
        button.focus();
      }
    }
  }

  buttons.forEach((button, index) => {
    button.addEventListener("click", () => {
      show(button.dataset.demoStep);
    });

    button.addEventListener("keydown", (event) => {
      const keys = {
        ArrowRight: 1,
        ArrowLeft: -1
      };

      let nextIndex = null;

      if (event.key in keys) {
        nextIndex = (
          index + keys[event.key] + buttons.length
        ) % buttons.length;

      } else if (event.key === "Home") {
        nextIndex = 0;

      } else if (event.key === "End") {
        nextIndex = buttons.length - 1;
      }

      if (nextIndex === null) {
        return;
      }

      event.preventDefault();

      const nextButton = buttons[nextIndex];

      show(
        nextButton.dataset.demoStep,
        true
      );
    });
  });

  const initialStep = buttons.some(
    (button) => button.dataset.demoStep === "import"
  )
    ? "import"
    : buttons[0].dataset.demoStep;

  show(initialStep);
})();
