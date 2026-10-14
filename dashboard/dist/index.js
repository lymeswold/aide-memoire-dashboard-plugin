(function () {
  "use strict";
  var SDK = window.__HERMES_PLUGIN_SDK__;
  var REG = window.__HERMES_PLUGINS__;
  if (!SDK || !REG) return;

  var React = SDK.React;
  var h = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useMemo = SDK.hooks.useMemo;
  var fetchJSON = SDK.fetchJSON;
  var C = SDK.components;

  function fmtDate(ts) {
    if (!ts) return "";
    try {
      return new Date(ts * 1000).toLocaleDateString("en-GB",
        { day: "2-digit", month: "short", year: "numeric" });
    } catch (e) { return ""; }
  }

  function Row(item, kind, showReasons) {
    var meta = [];
    if (item.version) meta.push("v" + item.version);
    if (kind === "plugin" && item.kinds && item.kinds.length) meta.push(item.kinds.join(" + "));
    if (item.mtime) meta.push(fmtDate(item.mtime));

    return h("div", { className: "am-row", key: kind + ":" + item.path },
      h("div", { className: "am-row-head" },
        h("span", { className: "am-name" }, item.name),
        meta.length ? h("span", { className: "am-meta" }, meta.join("  ·  ")) : null),
      item.path && item.path !== item.name
        ? h("div", { className: "am-path" }, item.path) : null,
      item.description
        ? h("div", { className: "am-desc" }, item.description)
        : h("div", { className: "am-desc am-desc-empty" }, "No description"),
      showReasons && item.reasons && item.reasons.length
        ? h("div", { className: "am-reason" }, "Excluded: " + item.reasons.join("; "))
        : null
    );
  }

  function AideMemoire() {
    var a = useState(null), data = a[0], setData = a[1];
    var b = useState(null), err = b[0], setErr = b[1];
    var c = useState(""), query = c[0], setQuery = c[1];
    var d = useState(true), loading = d[0], setLoading = d[1];
    var e = useState(false), showExcluded = e[0], setShowExcluded = e[1];

    function load() {
      setLoading(true);
      fetchJSON("/api/plugins/aide-memoire/list")
        .then(function (r) { setData(r); setErr(null); })
        .catch(function (ex) { setErr(String((ex && ex.message) || ex)); })
        .then(function () { setLoading(false); });
    }
    useEffect(function () { load(); }, []);

    var f = useMemo(function () {
      var empty = { skills: [], plugins: [], xs: [], xp: [] };
      if (!data) return empty;
      var n = query.trim().toLowerCase();
      function m(it) {
        if (!n) return true;
        return (it.name || "").toLowerCase().indexOf(n) !== -1 ||
               (it.description || "").toLowerCase().indexOf(n) !== -1 ||
               (it.path || "").toLowerCase().indexOf(n) !== -1;
      }
      return {
        skills: (data.skills || []).filter(m),
        plugins: (data.plugins || []).filter(m),
        xs: (data.excluded_skills || []).filter(m),
        xp: (data.excluded_plugins || []).filter(m)
      };
    }, [data, query]);

    if (err) {
      return h(C.Card, null,
        h(C.CardHeader, null, h(C.CardTitle, null, "Aide-Mémoire")),
        h(C.CardContent, null,
          h("p", { className: "am-muted" }, "Could not load: " + err),
          h(C.Button, { onClick: load, className: "am-mt" }, "Retry")));
    }

    var counts = (data && data.counts) || {};

    return h("div", { className: "am-wrap" },
      h(C.Card, null,
        h(C.CardHeader, null,
          h(C.CardTitle, null, "Aide-Mémoire"),
          h("p", { className: "am-sub" },
            "Skills and plugins we authored ourselves. Stock Hermes content is "
            + "excluded by checking each file against the hermes-agent git history, "
            + "bundled category names, and bundled skill names.")),
        h(C.CardContent, null,
          h("div", { className: "am-toolbar" },
            h(C.Input, {
              placeholder: "Filter by name, description or path…",
              value: query,
              onChange: function (ev) { setQuery(ev.target.value); }
            }),
            h(C.Button, { onClick: load, variant: "outline" },
              loading ? "Refreshing…" : "Refresh")),
          h("div", { className: "am-stats" },
            h(C.Badge, null, (counts.skills || 0) + " authored skills"),
            h(C.Badge, null, (counts.plugins || 0) + " authored plugins"),
            h("span", { className: "am-muted" },
              "excluded " + (counts.excluded_skills || 0) + " skills, "
              + (counts.excluded_plugins || 0) + " plugins as stock"),
            data && !data.git_history_available
              ? h("span", { className: "am-warn" },
                  "git history unavailable — provenance is weaker")
              : null))),

      loading && !data ? h("p", { className: "am-muted am-mt" }, "Scanning…") : null,

      data ? h(C.Card, { className: "am-mt" },
        h(C.CardHeader, null,
          h(C.CardTitle, null, "Authored Plugins (" + f.plugins.length + ")")),
        h(C.CardContent, null,
          f.plugins.length
            ? f.plugins.map(function (p) { return Row(p, "plugin", false); })
            : h("p", { className: "am-muted" }, "Nothing matches."))) : null,

      data ? h(C.Card, { className: "am-mt" },
        h(C.CardHeader, null,
          h(C.CardTitle, null, "Authored Skills (" + f.skills.length + ")")),
        h(C.CardContent, null,
          f.skills.length
            ? f.skills.map(function (s) { return Row(s, "skill", false); })
            : h("p", { className: "am-muted" }, "Nothing matches."))) : null,

      data ? h(C.Card, { className: "am-mt" },
        h(C.CardHeader, null,
          h("div", { className: "am-x-head" },
            h(C.CardTitle, null,
              "Excluded as Stock (" + (f.xs.length + f.xp.length) + ")"),
            h(C.Button, {
              variant: "outline",
              onClick: function () { setShowExcluded(!showExcluded); }
            }, showExcluded ? "Hide" : "Show"))),
        showExcluded ? h(C.CardContent, null,
          h("p", { className: "am-muted am-mb" },
            "Audit list. To correct a wrong call, edit overrides.json in the "
            + "plugin folder: {\"authored\": [\"path\"], \"stock\": [\"path\"]}"),
          f.xp.map(function (p) { return Row(p, "plugin", true); }),
          f.xs.map(function (s) { return Row(s, "skill", true); })
        ) : null) : null
    );
  }

  REG.register("aide-memoire", AideMemoire);
})();
