/* FlowScout QA Console interactions.
   Progressive enhancement only: every page still works without JavaScript. */
(function () {
  "use strict";

  const $ = (selector, root) => (root || document).querySelector(selector);
  const $$ = (selector, root) => Array.from((root || document).querySelectorAll(selector));

  function setBusy(form, busy) {
    form.classList.toggle("is-busy", busy);
    const button = $("[data-submit]", form);
    if (button) {
      button.disabled = busy;
      const label = $("[data-submit-label]", button);
      if (label) {
        if (busy) {
          label.dataset.idle = label.textContent;
          label.textContent = "Starting…";
        } else if (label.dataset.idle) {
          label.textContent = label.dataset.idle;
        }
      }
    }
  }

  function showError(form, message) {
    const box = $("[data-form-error]", form);
    if (!box) return;
    box.textContent = message;
    box.hidden = false;
    box.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function clearError(form) {
    const box = $("[data-form-error]", form);
    if (box) {
      box.hidden = true;
      box.textContent = "";
    }
  }

  async function postJson(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload),
    });
    let data = {};
    try {
      data = await response.json();
    } catch (error) {
      data = {};
    }
    if (!response.ok) {
      throw new Error(data.detail || "Request failed with HTTP " + response.status);
    }
    return data;
  }

  function selectedMode(form) {
    const checked = $('input[name="mode"]:checked', form);
    return checked ? checked.value : "mock";
  }

  function wireMode(form) {
    const auth = $("[data-live-auth]", form);
    const sync = () => {
      if (!auth) return;
      auth.hidden = selectedMode(form) !== "live";
    };
    $$('input[name="mode"]', form).forEach((input) => input.addEventListener("change", sync));
    sync();
  }

  // ------------------------------------------------------------- navigation
  const navToggle = $("[data-nav-toggle]");
  const mobileNav = $("#mobile-nav");
  if (navToggle && mobileNav) {
    navToggle.addEventListener("click", () => {
      const open = mobileNav.hasAttribute("hidden");
      if (open) mobileNav.removeAttribute("hidden");
      else mobileNav.setAttribute("hidden", "");
      navToggle.setAttribute("aria-expanded", String(open));
    });
  }

  // --------------------------------------------------------------- new run
  const newRunForm = $("#new-run-form");
  if (newRunForm) {
    const projectSelect = $("#project_id", newRunForm);
    const projectName = $("#project_name", newRunForm);
    const steps = $("#max_steps", newRunForm);
    const stepsOutput = $("#steps-output", newRunForm);

    const params = new URLSearchParams(window.location.search);
    const target = params.get("target");
    const urlInput = $("#target_url", newRunForm);
    if (target && urlInput && !urlInput.value) urlInput.value = target;
    $$("[data-demo-url]", newRunForm).forEach((button) => {
      button.addEventListener("click", () => {
        if (!urlInput) return;
        urlInput.value = button.dataset.demoUrl || "/shop/";
        urlInput.focus();
      });
    });

    if (steps && stepsOutput) {
      const syncSteps = () => { stepsOutput.textContent = steps.value; };
      steps.addEventListener("input", syncSteps);
      syncSteps();
    }

    if (projectSelect && projectName) {
      const syncProject = () => {
        const existing = Boolean(projectSelect.value);
        projectName.disabled = existing;
        if (existing) projectName.value = "";
      };
      projectSelect.addEventListener("change", syncProject);
      syncProject();
    }

    wireMode(newRunForm);

    newRunForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      clearError(newRunForm);
      if (!newRunForm.reportValidity()) return;
      const payload = {
        target_url: urlInput.value.trim(),
        project_id: projectSelect && projectSelect.value ? projectSelect.value : null,
        project_name: projectName && !projectName.disabled ? projectName.value.trim() || null : null,
        mode: selectedMode(newRunForm),
        authorize_live: Boolean($('input[name="authorize_live"]', newRunForm)?.checked),
        max_steps: steps ? Number(steps.value) : 8,
        use_ai: Boolean($("#use_ai", newRunForm)?.checked),
      };
      setBusy(newRunForm, true);
      try {
        const data = await postJson(newRunForm.dataset.endpoint, payload);
        window.location.assign(data.url || "/console/runs");
      } catch (error) {
        setBusy(newRunForm, false);
        showError(newRunForm, error.message);
      }
    });
  }

  // -------------------------------------------------------------- regression
  const regressionForm = $("#regression-form");
  if (regressionForm) {
    const projectSelect = $("#regression-project");
    const selectAll = $("[data-select-all]", regressionForm);
    const boxes = () => $$('input[name="test_ids"]', regressionForm);
    const countLabel = $("[data-selected-count]", regressionForm);

    const syncCount = () => {
      const total = boxes().length;
      const checked = boxes().filter((box) => box.checked).length;
      if (countLabel) countLabel.textContent = checked + " of " + total + " selected";
      if (selectAll) {
        selectAll.checked = total > 0 && checked === total;
        selectAll.indeterminate = checked > 0 && checked < total;
      }
    };
    boxes().forEach((box) => box.addEventListener("change", syncCount));
    if (selectAll) {
      selectAll.addEventListener("change", () => {
        boxes().forEach((box) => { box.checked = selectAll.checked; });
        syncCount();
      });
    }
    syncCount();

    wireMode(regressionForm);

    regressionForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      clearError(regressionForm);
      const testIds = boxes().filter((box) => box.checked).map((box) => box.value);
      if (!testIds.length) {
        showError(regressionForm, "Select at least one saved test.");
        return;
      }
      const payload = {
        project_id: projectSelect ? projectSelect.value : "",
        test_ids: testIds,
        mode: selectedMode(regressionForm),
        authorize_live: Boolean($('input[name="authorize_live"]', regressionForm)?.checked),
      };
      setBusy(regressionForm, true);
      try {
        const data = await postJson(regressionForm.dataset.endpoint, payload);
        window.location.assign(data.url || "/console/runs");
      } catch (error) {
        setBusy(regressionForm, false);
        showError(regressionForm, error.message);
      }
    });
  }

  // ------------------------------------------------------------ delete test
  $$("[data-delete-test]").forEach((button) => {
    button.addEventListener("click", async () => {
      const testId = button.dataset.deleteTest;
      if (!testId) return;
      if (!window.confirm("Delete this saved test case? This cannot be undone.")) return;
      button.disabled = true;
      try {
        const response = await fetch("/console/api/tests/" + encodeURIComponent(testId), {
          method: "DELETE",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(data.detail || "Could not delete the test");
        }
        const card = $('[data-test-card="' + testId + '"]');
        if (card) card.remove();
        const remaining = $$("[data-test-card]").length;
        const count = $(".panel-head h2");
        if (count && remaining === 0) window.location.reload();
      } catch (error) {
        button.disabled = false;
        window.alert(error.message);
      }
    });
  });

  // --------------------------------------------------------------- live run
  const liveRoot = $("[data-live-run]");
  if (liveRoot) {
    const runUrl = liveRoot.dataset.runUrl;
    let timer = null;
    let inFlight = false;

    const update = (data) => {
      const run = data.run || {};
      const value = Number(run.progress || 0);
      $$("[data-progress-value]").forEach((node) => { node.textContent = String(value); });
      const bar = $(".run-hero-progress .progress");
      if (bar) {
        bar.setAttribute("aria-valuenow", String(value));
        const fill = bar.querySelector("span");
        if (fill) fill.style.width = value + "%";
      }
      $$("[data-phase-value]").forEach((node) => {
        const text = String(run.phase || "");
        node.textContent = text ? text.charAt(0).toUpperCase() + text.slice(1).replace(/_/g, " ") : "—";
      });
      if (["succeeded", "failed", "cancelled"].includes(run.status)) {
        window.clearInterval(timer);
        window.location.reload();
      }
    };

    const poll = async () => {
      if (inFlight || document.visibilityState === "hidden") return;
      inFlight = true;
      try {
        const response = await fetch(runUrl, { headers: { Accept: "application/json" } });
        if (response.ok) update(await response.json());
      } catch (error) {
        /* keep polling; a transient network error is not a run failure */
      } finally {
        inFlight = false;
      }
    };

    timer = window.setInterval(poll, 2500);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") poll();
    });
  }

  // ------------------------------------------------------------------ print
  $$("[data-print]").forEach((button) => {
    button.addEventListener("click", () => window.print());
  });
})();
