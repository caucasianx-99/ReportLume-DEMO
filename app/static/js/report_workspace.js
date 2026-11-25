"use strict";

/*
 * Owner-only report workspace helpers.
 *
 * Report data and permissions remain server-owned.
 * Draft text stays only in the active form until submitted to the server.
 */
(() => {
    const priorPlan = document.getElementById(
        "previous-plan-text"
    );

    const copyButton = document.getElementById(
        "copy-previous-plan"
    );

    const summary = document.getElementById(
        "executive-summary"
    );

    if (priorPlan && copyButton && summary) {
        copyButton.addEventListener("click", () => {
            const suggestion = priorPlan.value.trim();

            if (!suggestion) {
                return;
            }

            if (
                summary.value.trim() &&
                !window.confirm(
                    "Replace the current summary with the previous month's plan?"
                )
            ) {
                return;
            }

            const maximum = summary.maxLength > 0
                ? summary.maxLength
                : 2000;

            summary.value = suggestion.slice(0, maximum);
            summary.focus();
        });
    }

    const message = document.querySelector(
        ".report-form-error[role='alert'], " +
        ".report-action-notice[role='status']"
    );

    if (message) {
        message.focus({
            preventScroll: true,
        });

        message.scrollIntoView({
            block: "start",
            behavior: "auto",
        });
    }

    for (
        const publishForm of document.querySelectorAll(
            "form[data-report-publish-confirm]"
        )
    ) {
        publishForm.addEventListener("submit", (event) => {
            const requiredChecks = publishForm.querySelectorAll(
                "input[type='checkbox'][required]"
            );

            for (const checkbox of requiredChecks) {
                if (!checkbox.checked) {
                    event.preventDefault();
                    checkbox.focus();
                    return;
                }
            }

            const period = (
                publishForm.dataset.reportPeriod || "this month"
            );

            const trial = (
                publishForm.dataset.trialReport === "true"
            );

            const unfinished = (
                publishForm.dataset.unfinishedMonth === "true"
            );

            const messages = [
                `Publish the saved report for ${period}?`,
                "Existing private links will continue to open their original versions."
            ];

            if (unfinished) {
                messages.push(
                    "This month has not ended; metrics may still change."
                );
            }

            if (trial) {
                messages.push(
                    "This uses your one free-trial report and the report will show a trial mark."
                );
            }

            if (
                !window.confirm(
                    messages.join("\n\n")
                )
            ) {
                event.preventDefault();
            }
        });
    }

    for (
        const copyForm of document.querySelectorAll(
            "form[data-paid-copy-confirm]"
        )
    ) {
        copyForm.addEventListener("submit", (event) => {
            const approved = copyForm.querySelector(
                "input[name='confirm_paid_copy']"
            );

            if (
                approved &&
                !approved.checked
            ) {
                event.preventDefault();
                approved.focus();
                return;
            }

            if (
                !window.confirm(
                    "Publish a new version without the trial mark, " +
                    "copied from an eligible frozen trial-marked version? " +
                    "No data will be recalculated. " +
                    "Existing links and saved drafts will not change."
                )
            ) {
                event.preventDefault();
            }
        });
    }

    for (
        const revokeForm of document.querySelectorAll(
            "form[data-revoke-share]"
        )
    ) {
        revokeForm.addEventListener("submit", (event) => {
            if (
                !window.confirm(
                    "Revoke this private link? " +
                    "People using it will immediately lose access."
                )
            ) {
                event.preventDefault();
            }
        });
    }

    /*
     * The current report template identifies case unpublishing
     * with a hidden action field. Preserve compatibility with
     * forms using the explicit data-unpublish-case attribute.
     */
    for (const form of document.querySelectorAll("form")) {
        const isCaseUnpublish = (
            form.hasAttribute("data-unpublish-case") ||
            form.querySelector(
                "input[name='action'][value='case_unpublish']"
            ) !== null
        );

        if (!isCaseUnpublish) {
            continue;
        }

        form.addEventListener("submit", (event) => {
            if (
                !window.confirm(
                    "Unpublish this public case study? " +
                    "The report itself will stay published."
                )
            ) {
                event.preventDefault();
            }
        });
    }
})();
