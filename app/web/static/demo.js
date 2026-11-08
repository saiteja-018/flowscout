(function () {
  "use strict";

  document.documentElement.classList.add("js");

  var root = document.querySelector("[data-replay]");
  if (!root) return;

  var toggle = root.querySelector("[data-replay-toggle]");
  var restart = root.querySelector("[data-replay-restart]");
  var speedButton = root.querySelector("[data-replay-speed]");
  var skip = root.querySelector("[data-replay-skip]");
  var status = root.querySelector("[data-replay-status]");
  var progressText = root.querySelector("[data-replay-progress-text]");
  var progressBar = root.querySelector("[data-replay-progress]");
  var steps = Array.prototype.slice.call(root.querySelectorAll("[data-step]"));
  var shots = Array.prototype.slice.call(root.querySelectorAll("[data-shot-step]"));
  var placeholder = root.querySelector("[data-stage-placeholder]");
  var firstShotStep = shots.length ? Number(shots[0].getAttribute("data-shot-step")) : 0;
  var totals = Array.prototype.slice.call(root.querySelectorAll("[data-total-step]"));
  var resultCard = root.querySelector("[data-reveal-at='10']");
  var actionRows = Array.prototype.slice.call(document.querySelectorAll("[data-action-step]"));
  var totalSteps = Number(root.getAttribute("data-total-steps")) || steps.length || 10;

  var currentStep = 1;
  var playing = false;
  var speed = 1;
  var timer = null;
  var reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function setStatus(text) {
    if (status) status.textContent = text;
  }

  function setProgress(step) {
    if (progressText) progressText.textContent = step + " / " + totalSteps;
    if (progressBar) progressBar.style.width = (step / totalSteps) * 100 + "%";
  }

  function activeShotFor(step) {
    if (!shots.length || step < firstShotStep) return null;
    var active = shots[0];
    for (var i = 0; i < shots.length; i += 1) {
      var shotStep = Number(shots[i].getAttribute("data-shot-step"));
      if (shotStep <= step) active = shots[i];
    }
    return active;
  }

  function reveal(step) {
    steps.forEach(function (card) {
      var number = Number(card.getAttribute("data-step"));
      card.classList.toggle("is-revealed", number <= step);
      card.classList.toggle("is-current", number === step);
    });

    var active = activeShotFor(step);
    shots.forEach(function (shot) {
      shot.classList.toggle("is-active", shot === active);
    });
    if (placeholder) {
      placeholder.classList.toggle("is-hidden", step >= firstShotStep);
    }

    totals.forEach(function (row) {
      row.classList.toggle("is-revealed", Number(row.getAttribute("data-total-step")) <= step);
    });

    if (resultCard) resultCard.classList.toggle("is-revealed", step >= totalSteps);

    actionRows.forEach(function (row) {
      row.classList.toggle("is-current", Number(row.getAttribute("data-action-step")) === step);
    });

    setProgress(step);

    if (step >= totalSteps) {
      setStatus("Replay complete — all ten steps and the result are visible.");
      if (toggle) {
        toggle.textContent = "Replay again";
        toggle.setAttribute("aria-pressed", "false");
      }
    } else if (step <= 1) {
      setStatus("Ready to replay");
      if (toggle) {
        toggle.textContent = "Play replay";
        toggle.setAttribute("aria-pressed", "false");
      }
    } else {
      var current = steps.filter(function (card) {
        return Number(card.getAttribute("data-step")) === step;
      })[0];
      var title = current ? current.querySelector("h3").textContent : "step " + step;
      setStatus("Step " + step + " of " + totalSteps + " · " + title);
    }
  }

  function stopTimer() {
    if (timer) {
      window.clearTimeout(timer);
      timer = null;
    }
  }

  function pause() {
    playing = false;
    stopTimer();
    if (currentStep > 0 && currentStep < totalSteps) {
      setStatus("Paused at step " + currentStep + " of " + totalSteps + ".");
    }
    if (toggle) {
      toggle.textContent = currentStep >= totalSteps ? "Replay again" : "Resume replay";
      toggle.setAttribute("aria-pressed", "false");
    }
  }

  function tick() {
    if (!playing) return;
    if (currentStep >= totalSteps) {
      playing = false;
      return;
    }
    currentStep += 1;
    reveal(currentStep);
    if (currentStep >= totalSteps) {
      playing = false;
      return;
    }
    timer = window.setTimeout(tick, 1500 / speed);
  }

  function play() {
    if (reducedMotion) {
      currentStep = totalSteps;
      reveal(currentStep);
      setStatus("Reduced motion is on — showing the complete run.");
      return;
    }
    if (currentStep >= totalSteps) {
      currentStep = 1;
      reveal(1);
    }
    playing = true;
    if (toggle) {
      toggle.textContent = "Pause replay";
      toggle.setAttribute("aria-pressed", "true");
    }
    setStatus("Playing · step " + Math.max(currentStep, 1) + " of " + totalSteps);
    stopTimer();
    timer = window.setTimeout(tick, 500 / speed);
  }

  if (toggle) {
    toggle.addEventListener("click", function () {
      if (playing) {
        pause();
      } else {
        play();
      }
    });
  }

  if (restart) {
    restart.addEventListener("click", function () {
      playing = false;
      stopTimer();
      currentStep = 1;
      reveal(1);
    });
  }

  if (skip) {
    skip.addEventListener("click", function () {
      playing = false;
      stopTimer();
      currentStep = totalSteps;
      reveal(totalSteps);
    });
  }

  if (speedButton) {
    speedButton.addEventListener("click", function () {
      speed = speed === 1 ? 2 : 1;
      speedButton.textContent = speed + "×";
      speedButton.setAttribute("aria-label", "Replay speed " + speed + "×");
      speedButton.setAttribute("aria-pressed", speed === 2 ? "true" : "false");
      if (playing) {
        stopTimer();
        timer = window.setTimeout(tick, 1500 / speed);
      }
    });
  }

  reveal(1);

  if (reducedMotion) {
    currentStep = totalSteps;
    reveal(currentStep);
    setStatus("Reduced motion is on — showing the complete run.");
  }
})();
