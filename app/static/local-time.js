/* Render server-provided UTC times in the viewer's own timezone and locale. */
(function () {
  function render() {
    var formatter = new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    });
    document.querySelectorAll("time[data-local-time]").forEach(function (element) {
      var value = element.getAttribute("datetime");
      var date = value ? new Date(value) : null;
      if (!date || Number.isNaN(date.getTime())) {
        return;
      }
      element.textContent = formatter.format(date);
      element.removeAttribute("data-local-time");
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
