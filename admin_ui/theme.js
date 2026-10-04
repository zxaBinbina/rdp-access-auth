"use strict";
// Run before styles load so a saved theme is also used on the first paint.
(() => {
  const systemTheme = matchMedia("(prefers-color-scheme: dark)");
  let preference;
  try {
    preference = localStorage.getItem("rdp-management-theme");
  } catch (_) {}
  if (!["dark", "light"].includes(preference)) preference = null;
  const apply = () => {
    const theme = preference || (systemTheme.matches ? "dark" : "light");
    document.documentElement.dataset.theme = theme;
    const button = document.getElementById("theme");
    if (button) {
      const label = theme === "dark" ? "切换浅色主题" : "切换深色主题";
      button.setAttribute("aria-label", label);
      button.title = label;
    }
  };
  apply();
  systemTheme.addEventListener("change", apply);
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    document.getElementById("theme").addEventListener("click", () => {
      preference =
        document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      try {
        localStorage.setItem("rdp-management-theme", preference);
      } catch (_) {}
      apply();
    });
  });
})();
