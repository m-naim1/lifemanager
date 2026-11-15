/* LifeManager client behaviour: Alpine components, icons and htmx glue. */

(function () {
  "use strict";

  var WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  document.addEventListener("alpine:init", registerComponents);
  // In case the script is loaded after Alpine has already booted.
  registerComponents();

  function registerComponents() {
    if (!window.Alpine || typeof window.Alpine.data !== "function") return;

    if (!window.__lmIntervalPickerRegistered) {
      window.__lmIntervalPickerRegistered = true;
      window.Alpine.data("intervalPicker", function (config) {
        config = config || {};
        return {
          mode: config.mode === "routine" ? "routine" : "task",
          count: Number(config.count) || 1,
          unit: config.unit || "week",
          days: Array.isArray(config.days) ? config.days.map(Number) : [],
          weekdayLabels: WEEKDAY_LABELS,

          get hasDays() {
            return this.unit === "week" || this.unit === "month";
          },

          changeUnit: function (value) {
            if (value === this.unit) return;
            this.unit = value;
            // Weekdays and month days mean different things; start fresh.
            this.days = [];
          },

          toggleDay: function (n) {
            n = Number(n);
            var index = this.days.indexOf(n);
            var next;
            if (index === -1) {
              next = this.days.concat([n]).sort(function (a, b) {
                return a - b;
              });
            } else {
              next = this.days.filter(function (value) {
                return value !== n;
              });
            }
            this.days = next;
            if (next.length) this.count = next.length;
          },

          isSelected: function (n) {
            return this.days.indexOf(Number(n)) !== -1;
          },
        };
      });
    }

    if (!window.__lmThemePickerRegistered) {
      window.__lmThemePickerRegistered = true;
      window.Alpine.data("themePicker", function () {
        var root = document.documentElement;
        var storedMode =
          localStorage.getItem("lm-theme-mode") ||
          localStorage.getItem("theme") ||
          "system";
        if (["light", "dark", "system"].indexOf(storedMode) === -1) {
          storedMode = "system";
        }
        return {
          menu: false,
          mode: storedMode,
          accent: root.dataset.accent || "blue",
          accents: [
            { id: "blue", label: "Ocean", color: "#2563eb" },
            { id: "violet", label: "Iris", color: "#7c3aed" },
            { id: "emerald", label: "Forest", color: "#059669" },
            { id: "rose", label: "Rose", color: "#e11d48" },
            { id: "amber", label: "Sunset", color: "#d97706" },
            { id: "slate", label: "Graphite", color: "#475569" },
          ],

          init: function () {
            var self = this;
            if (window.matchMedia) {
              window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
                if (self.mode === "system") self.applyMode();
              });
            }
          },

          applyMode: function () {
            var dark =
              this.mode === "dark" ||
              (this.mode === "system" &&
                window.matchMedia &&
                window.matchMedia("(prefers-color-scheme: dark)").matches);
            root.classList.toggle("dark", dark);
            root.style.colorScheme = dark ? "dark" : "light";
          },

          setMode: function (value) {
            this.mode = value;
            localStorage.setItem("lm-theme-mode", value);
            // Keep the legacy key in sync for older tabs.
            localStorage.setItem(
              "theme",
              value === "dark" || (value === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches)
                ? "dark"
                : "light"
            );
            this.applyMode();
          },

          setAccent: function (value) {
            this.accent = value;
            root.dataset.accent = value;
            localStorage.setItem("lm-accent", value);
          },
        };
      });
    }

    if (!window.__lmGoalBuilderRegistered) {
      window.__lmGoalBuilderRegistered = true;
      window.Alpine.data("goalBuilder", function (config) {
        config = config || {};
        var nextKey = 0;
        function normalize(field) {
          field = field || {};
          return {
            key: nextKey++,
            id: field.id || null,
            name: field.name || "",
            unit: field.unit || "",
            target: field.target === null || field.target === undefined ? "" : field.target,
            compare: field.compare || "latest",
            direction: field.direction === "lower" ? "lower" : "higher",
          };
        }
        function normalizeStage(stage) {
          stage = stage || {};
          return {
            key: nextKey++,
            id: stage.id || null,
            title: stage.title || "",
            done: !!stage.done,
          };
        }
        var fields = Array.isArray(config.fields) ? config.fields.map(normalize) : [];
        var stages = Array.isArray(config.stages) ? config.stages.map(normalizeStage) : [];
        return {
          kind: ["percent", "staged"].indexOf(config.kind) !== -1 ? config.kind : "measured",
          showChart: config.showChart !== false,
          fields: fields,
          stages: stages,

          setKind: function (value) {
            if (this.kind === value) return;
            this.kind = value;
            if (value === "staged" && !this.stages.length) this.addStage();
            if (value === "measured" && !this.fields.length) this.addField();
          },

          addField: function () {
            this.fields.push(normalize({}));
          },

          removeField: function (index) {
            if (this.fields.length > 1) this.fields.splice(index, 1);
          },

          addStage: function () {
            this.stages.push(normalizeStage({}));
          },

          removeStage: function (index) {
            if (this.stages.length > 1) this.stages.splice(index, 1);
          },

          toggleStage: function (stage) {
            stage.done = !stage.done;
          },
        };
      });
    }
  }

  function renderIcons() {
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      window.lucide.createIcons();
    }
  }

  document.addEventListener("DOMContentLoaded", renderIcons);
  document.addEventListener("htmx:afterSwap", renderIcons);
  document.addEventListener("htmx:afterSettle", renderIcons);

  // Escape closes whatever modal is currently mounted.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var root = document.getElementById("modal-root");
    if (root && root.innerHTML.trim()) {
      root.innerHTML = "";
    }
  });
})();
