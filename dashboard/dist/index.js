// Precog — web dashboard tab. Plain IIFE, no build step.
(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const h = React.createElement;
  const { Card, CardHeader, CardTitle, CardContent } = SDK.components;
  const API = "/api/plugins/precog";

  async function call(path, opts) {
    const fetcher = SDK.fetchJSON || (async (url, o) => {
      const res = await fetch(url, Object.assign({ credentials: "same-origin" }, o || {}));
      if (!res.ok) throw new Error(res.status + " " + res.statusText);
      return res.json();
    });
    return fetcher(API + path, opts);
  }

  function Stat(props) {
    return h("div", { className: "rounded-md border p-3" },
      h("div", { className: "text-2xl font-semibold" }, String(props.value)),
      h("div", { className: "text-xs text-muted-foreground" }, props.label));
  }

  function PrecogPage() {
    const [summary, setSummary] = React.useState(null);
    const [lessons, setLessons] = React.useState([]);
    const [error, setError] = React.useState("");

    const load = React.useCallback(function () {
      Promise.all([call("/summary"), call("/lessons")]).then(function (r) {
        setSummary(r[0]); setLessons(r[1].lessons || []); setError("");
      }).catch(function (e) { setError(String(e && e.message || e)); });
    }, []);
    React.useEffect(function () { load(); const t = setInterval(load, 15000); return function () { clearInterval(t); }; }, [load]);

    return h("div", { className: "flex flex-col gap-4" },
      h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "Precog — Hermes stops repeating its own mistakes")),
        h(CardContent, null,
          error ? h("p", { className: "text-sm text-destructive" }, "Backend unavailable: " + error) : null,
          summary ? h("div", { className: "grid grid-cols-3 gap-3" },
            h(Stat, { label: "mistakes prevented", value: summary.saves }),
            h(Stat, { label: "lessons on", value: summary.enabled + "/" + summary.lessons }),
            h(Stat, { label: "failed commands this week", value: summary.week_failures })) : null)),
      h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "Lessons")),
        h(CardContent, null,
          lessons.length === 0
            ? h("p", { className: "text-sm text-muted-foreground" },
                "No lessons yet. When a command fails and a later one fixes it, Precog keeps the fix and applies it next time.")
            : lessons.map(function (l) {
                return h("div", { key: l.id, className: "border-b py-2 text-sm", style: { opacity: l.enabled ? 1 : 0.55 } },
                  h("div", { className: "flex items-center gap-2" },
                    h("span", { className: "text-muted-foreground" }, "#" + l.id),
                    h("span", { className: "flex-1" }, l.text),
                    h("span", { className: "text-xs text-muted-foreground" }, l.project + " · saved " + l.saves + "×"),
                    h("button", { className: "text-xs underline", onClick: function () {
                      call("/lessons/" + l.id + "/enabled", { method: "POST", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ enabled: !l.enabled }) }).then(load);
                    } }, l.enabled ? "turn off" : "turn on"),
                    h("button", { className: "text-xs underline", onClick: function () {
                      call("/lessons/" + l.id + "/forget", { method: "POST" }).then(load);
                    } }, "forget")),
                  l.fail_cmd ? h("pre", { className: "mt-1 whitespace-pre-wrap text-xs text-muted-foreground" },
                    "✗ " + l.fail_cmd.split("\n")[0] + (l.error ? "   (" + l.error + ")" : "") + "\n✓ " + l.fix_cmd.split("\n")[0]) : null);
              }))));
  }

  window.__HERMES_PLUGINS__.register("precog", PrecogPage);
})();
