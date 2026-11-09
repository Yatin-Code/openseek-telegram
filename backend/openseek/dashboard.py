"""Shared browser dashboard, using the original OpenSeek desktop visual system.

Palette, layout and brand geometry derive from the AGPL OpenSeek desktop source
at 9691f9a; see UPSTREAMS.md and DESIGN.md. CSS/JS are self-contained so every
supported deployment packages the same UI. Admin credentials stay in the
existing fragment/session/header flow; thumbnail requests use that header too.
"""
from __future__ import annotations

ADMIN_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>OpenSeek admin</title><style>
:root{color-scheme:dark;
--bg:#0d0e12;--surface:#13151b;--card:#181b23;--card-hi:#1f2330;--border:#252a36;--text:#e9eaf0;--muted:#8b91a1;
--accent:#8b7bff;--accent-2:#5ad1c1;--good:#4fd68a;--warn:#f0b44c;--bad:#ef6a6a;--bar:#0d0e12;--thumb:#2b3040;
}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--text);font-family:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;font-size:14px;line-height:1.45}
::selection{background:var(--accent);color:#0d0e12}
::placeholder{color:var(--muted);opacity:1}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
::-webkit-scrollbar{width:12px;height:12px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--thumb);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:#3a4155}
a{color:var(--accent-2);text-underline-offset:3px}
code{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:.92em;overflow-wrap:anywhere}
.skip{position:absolute;left:-9999px;top:0;background:var(--card-hi);color:var(--text);padding:10px 14px;z-index:50}
.skip:focus{left:8px;top:8px}
#app{display:flex;height:100dvh;overflow:hidden}
#sidebar{overflow-y:auto}
/* ---- sidebar (original) ---- */
#sidebar{width:250px;flex:0 0 250px;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;padding:18px 14px 14px;gap:4px}
.brand{display:flex;align-items:center;gap:9px;padding:0 6px 16px}
.brand svg{display:block;flex:0 0 auto}
.brand span{font-size:15px;font-weight:600;letter-spacing:.4px}
nav.primary{display:flex;flex-direction:column;gap:4px}
nav.primary button{width:100%;text-align:left;padding:9px 14px;min-height:44px;border:0;border-radius:9px;color:var(--muted);background:transparent;font:inherit;font-size:13px;cursor:pointer}
nav.primary button:hover{background:var(--card);color:var(--text)}
nav.primary button[aria-current="page"]{background:var(--card-hi);color:var(--text)}
.side-foot{margin-top:auto;padding-top:16px}
.pill{display:inline-flex;align-items:center;gap:6px;border-radius:10px;padding:3px 10px;font-size:12px;font-weight:600;background:var(--card-hi);color:var(--muted)}
.pill .dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex:0 0 auto}
.pill-good{background:rgba(79,214,138,.14);color:var(--good)}
.pill-accent{background:rgba(139,123,255,.16);color:var(--accent)}
.pill-teal{background:rgba(90,209,193,.14);color:var(--accent-2)}
.pill-warn{background:rgba(240,180,76,.14);color:var(--warn)}
.pill-bad{background:rgba(239,106,106,.14);color:var(--bad)}
/* ---- main ---- */
main{flex:1;min-width:0;padding:28px 32px 24px;overflow-y:auto}
.panel{max-width:1280px}
.panel[hidden]{display:none}
.page-head{display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap;margin-bottom:18px}
.page-head h1{font-size:22px;font-weight:600;margin:0;line-height:1.2}
.page-head .sub{color:var(--muted);margin:3px 0 0}
.page-head .spacer{flex:1}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px 18px;margin-bottom:14px}
.jobs-wrap{margin-bottom:14px}
.jobs-wrap h2{font-size:14px;font-weight:600;margin:0 0 10px}
.card h2{font-size:14px;font-weight:600;margin:0 0 10px}
.card h3{font-size:13px;font-weight:600;margin:16px 0 8px}
.muted{color:var(--muted)}
.faint-src{color:var(--muted)}
.note{color:var(--muted);font-size:13px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.row-end{margin-left:auto}
/* stats (original) */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:14px}
.stat{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px 18px;min-width:0}
.stat .value{font-size:26px;font-weight:600;line-height:1.15;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.stat .caption{color:var(--muted);font-size:12px;margin-top:2px}
.stat .sub{color:var(--muted);font-size:12px;margin-top:2px}
.bar{height:8px;border-radius:4px;background:var(--surface);border:0;margin-top:10px;overflow:hidden}
.bar-fill{height:100%;background:var(--accent);border-radius:4px;width:0%}
.bar-fill.good{background:var(--good)}
.bar-fill.warn{background:var(--warn)}
/* health row */
.health{display:flex;gap:26px;flex-wrap:wrap;background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px 18px;margin-bottom:14px}
.health .item{display:flex;flex-direction:column;gap:5px;min-width:110px}
.health .caption{color:var(--muted);font-size:12px}
/* buttons (original) */
button.btn{font:inherit;min-height:44px;padding:10px 16px;border-radius:9px;border:1px solid var(--border);background:var(--card-hi);color:var(--text);cursor:pointer;white-space:normal}
button.btn:hover{border-color:#3a4155;background:#232838}
button.btn:disabled{color:var(--muted);background:var(--card);cursor:not-allowed}
button.btn.primary{background:var(--accent);border:0;color:#0d0e12;font-weight:600}
button.btn.primary:hover{background:#9d8fff}
button.btn.primary:disabled{background:#2b2846;color:var(--muted)}
button.btn.danger{color:var(--bad);background:var(--card-hi)}
button.btn.danger:hover{background:#2a1d22;border-color:#5a2c33}
button.btn.ghost{background:transparent;border:0;color:var(--muted);min-height:44px}
button.btn.ghost:hover{color:var(--text)}
button.btn.small{min-height:44px;padding:8px 12px}
button.btn.armed{background:#2a1d22;border-color:var(--bad);color:var(--bad)}
/* forms */
form .field{margin:0 0 12px}
label{display:block;color:var(--muted);font-size:13px;margin-bottom:5px}
input,select,textarea{font:inherit;color:var(--text);background:var(--surface);border:1px solid var(--border);border-radius:9px;padding:7px 10px;min-height:44px;width:100%;caret-color:var(--accent)}
input:focus,select:focus{border-color:var(--accent)}
input[type="checkbox"].switch{appearance:none;-webkit-appearance:none;width:38px;height:20px;border-radius:10px;background:#2b3040;border:0;padding:0;min-height:20px;position:relative;cursor:pointer;flex:0 0 auto;vertical-align:middle}
input[type="checkbox"].switch::after{content:"";position:absolute;top:3px;left:3px;width:14px;height:14px;border-radius:50%;background:#fff}
input[type="checkbox"].switch:checked{background:var(--accent)}
input[type="checkbox"].switch:checked::after{left:21px}
.switch-row{display:flex;align-items:center;gap:10px;min-height:44px;flex-wrap:wrap}
.switch-row label{display:inline;margin:0;color:var(--text)}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-grid .full{grid-column:1/-1}
.hint{color:var(--muted);font-size:12px;margin:4px 0 0}
.feedback{margin:10px 0 0}
.feedback:empty{display:none}
.errorbox{border:1px solid #5a2c33;background:#2a1d22;border-radius:10px;padding:10px 14px;margin:10px 0}
.errorbox:empty{display:none}
details.err-details{margin-top:8px}
details.err-details summary{cursor:pointer;color:var(--muted);min-height:44px;display:flex;align-items:center}
.banner{border-radius:10px;padding:10px 14px;margin-bottom:14px}
.banner.warn{background:rgba(240,180,76,.12);color:var(--warn)}
.banner.bad{background:rgba(239,106,106,.12);color:var(--bad)}
.banner:empty{display:none}
/* tables */
table{border-collapse:collapse;width:100%}
th{color:var(--muted);font-weight:500;text-align:left;padding:6px 8px;font-size:12px}
td{padding:8px;border-top:1px solid var(--border);vertical-align:top;overflow-wrap:anywhere}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
/* lists */
ul.clean{list-style:none;margin:0;padding:0}
.job{border:1px solid var(--border);border-radius:14px;padding:16px 18px;margin-bottom:12px;background:var(--card)}
.job-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
.job-head h3{margin:0;font-size:14px;flex:1;min-width:180px}
.job .meta{color:var(--muted);font-size:13px;margin:8px 0 0;overflow-wrap:anywhere}
.recent li{display:flex;gap:10px;align-items:baseline;padding:5px 0}
.recent .dot{color:var(--accent-2);font-size:10px;flex:0 0 auto}
.recent .when{margin-left:auto;color:var(--muted);font-size:12px;flex:0 0 auto}
.recent .what{min-width:0}
/* library */
.lib-controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.lib-controls input[type="search"]{flex:1;min-width:200px}
.lib-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(176px,1fr));gap:16px;list-style:none;margin:0;padding:0}
.lib-card{margin:0}
.lib-thumb{aspect-ratio:16/9;border-radius:12px;overflow:hidden;background:var(--surface);border:1px solid var(--border);position:relative;display:flex;align-items:center;justify-content:center}
.lib-thumb img{width:100%;height:100%;object-fit:contain;display:block}
.lib-thumb .fallback{color:var(--muted);font-size:12px;text-align:center;padding:12px}
.lib-title{font-weight:600;font-size:13px;margin:9px 0 3px;overflow-wrap:anywhere}
.lib-sub{color:var(--muted);font-size:12px;overflow-wrap:anywhere}
.lib-chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.chip{font-size:11px;border:1px solid var(--border);border-radius:8px;padding:2px 8px;color:var(--muted);background:var(--surface)}
.pagination{display:flex;gap:10px;align-items:center;margin-top:14px;flex-wrap:wrap}
dl.kv{display:grid;grid-template-columns:220px 1fr;gap:6px 16px;margin:0}
dl.kv dt{color:var(--muted);font-size:13px}
dl.kv dd{margin:0;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.addr-list{list-style:none;margin:8px 0;padding:0}
.addr-list li{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:6px 0;border-top:1px solid var(--border)}
.addr-list li:first-child{border-top:0}
.mono-code{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:13px;overflow-wrap:anywhere}
.big-code{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:24px;font-weight:600;letter-spacing:2px;background:var(--surface);border-radius:12px;padding:16px;text-align:center;color:var(--accent-2);overflow-wrap:anywhere}
.empty{text-align:center;color:var(--muted);padding:24px 12px}
.skeleton{height:14px;border-radius:4px;background:var(--card-hi);margin:8px 0}
#authgate{max-width:640px;margin:40px auto;padding:28px 24px;background:var(--card);border:1px solid var(--border);border-radius:14px}
#authgate h1{font-size:22px;margin:0 0 10px}
[hidden]{display:none !important}
/* responsive */
@media (max-width:900px){
  #app{flex-direction:column;height:auto;min-height:100dvh;overflow:visible}
  #sidebar{width:100%;flex:none;border-right:0;border-bottom:1px solid var(--border);padding:12px 12px 10px;overflow:visible}
  main{overflow:visible;width:100%}
  .brand{padding-bottom:10px}
  nav.primary{flex-direction:row;overflow-x:auto;padding-bottom:2px}
  nav.primary button{width:auto;white-space:nowrap;flex:0 0 auto}
  .side-foot{margin-top:0;padding-top:10px}
  main{padding:16px 14px 24px}
  .stats{grid-template-columns:1fr 1fr}
  .form-grid{grid-template-columns:1fr}
  dl.kv{grid-template-columns:1fr;gap:2px 0}
  dl.kv dd{margin-bottom:8px}
  /* A block clipping box keeps intrinsic header widths out of mobile scroll bounds. */
  table.data-table thead{display:block;position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap;border:0}
  table.data-table tbody,table.data-table tbody tr{display:block;width:100%}
  table.data-table tbody tr{border:1px solid var(--border);border-radius:10px;margin:0 0 10px;padding:6px 10px}
  table.data-table td{display:grid;grid-template-columns:9rem minmax(0,1fr);align-items:start;gap:.6rem;border:0;padding:6px 0}
  table.data-table td::before{font-weight:600;color:var(--muted)}
  table.data-table td::before{content:attr(data-label)}
  table.data-table .empty-row td{display:block}
  table.data-table .empty-row td::before{content:none}
  .health{gap:14px}
  .lib-grid{grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
}
@media (max-width:520px){.stats{grid-template-columns:1fr}.page-head h1{font-size:20px}}
@media (prefers-reduced-motion:reduce){*{transition:none !important}}
</style></head><body>
<a class="skip" href="#main">Skip to main content</a>
<div id="app">
<aside id="sidebar">
  <div class="brand"><svg width="26" height="26" viewBox="0 0 64 64" aria-hidden="true"><defs><linearGradient id="bg1" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#9b8cff"/><stop offset="1" stop-color="#4fd1c1"/></linearGradient></defs><rect x="3" y="3" width="58" height="58" rx="15" fill="url(#bg1)"/><rect x="13" y="33" width="38" height="5" rx="2.5" fill="#fff" opacity=".94"/><circle cx="36" cy="35.5" r="5.5" fill="#0d0e12"/><rect x="26" y="14" width="20" height="13" rx="3" fill="#fff" opacity=".94"/></svg><span>OpenSeek</span></div>
  <nav class="primary" aria-label="Primary">
    <button type="button" data-panel="dashboard" aria-current="page">Dashboard</button>
    <button type="button" data-panel="library">Library</button>
    <button type="button" data-panel="activity">Live activity</button>
    <button type="button" data-panel="diagnostics">Diagnostics</button>
    <button type="button" data-panel="sharing">Sharing</button>
    <button type="button" data-panel="settings">Settings</button>
  </nav>
  <div class="side-foot"><span id="srv-pill" class="pill"><span class="dot" aria-hidden="true"></span><span id="srv-pill-text">Checking…</span></span></div>
</aside>
<main id="main">
<div id="authgate" hidden role="alert" aria-live="assertive" tabindex="-1"><h1 id="authgate-title">Admin access required</h1><p id="authgate-text" class="muted"></p></div>
<div id="panels" hidden>

<section class="panel" id="panel-dashboard" aria-labelledby="h-dashboard">
  <div class="page-head"><div><h1 id="h-dashboard" tabindex="-1">Dashboard</h1><p class="sub">Shared-pool status for this server.</p></div><div class="spacer"></div><span id="dash-online" class="pill"><span class="dot" aria-hidden="true"></span><span id="dash-online-text">Checking…</span></span><button type="button" class="btn primary" id="dash-generate">Generate package</button><button type="button" class="btn" id="dash-connect">Connect a device</button></div>
  <p id="dash-banner" class="banner" role="alert" hidden></p>
  <div class="stats" aria-label="Pool summary">
    <div class="stat"><div class="value" id="stat-entries">–</div><div class="caption" id="stat-entries-caption">Pool packages</div><div class="sub" id="stat-entries-sub">packages in pool</div></div>
    <div class="stat"><div class="value" id="stat-tiles">–</div><div class="caption" id="stat-tiles-caption">Pool tiles</div><div class="sub" id="stat-tiles-sub">tiles in pool</div></div>
    <div class="stat"><div class="value" id="stat-bytes">–</div><div class="caption">Cache storage</div><div class="sub" id="stat-bytes-sub">of – limit</div><div class="bar" role="progressbar" aria-label="Cache storage" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" id="bytes-bar"><div class="bar-fill" id="bytes-fill"></div></div></div>
    <div class="stat"><div class="value" id="stat-jobs">–</div><div class="caption">Active jobs</div><div class="sub" id="stat-jobs-sub">–</div></div>
  </div>
  <div class="health" aria-label="Sharing and pool health">
    <div class="item"><span class="caption">Library sharing</span><span class="pill" id="health-sharing"><span class="dot" aria-hidden="true"></span><span>–</span></span></div>
    <div class="item"><span class="caption">Guest uploads</span><span class="pill" id="health-contrib"><span class="dot" aria-hidden="true"></span><span>–</span></span></div>
    <div class="item"><span class="caption">Pool</span><span class="pill" id="health-pool"><span class="dot" aria-hidden="true"></span><span>–</span></span></div>
    <div class="item"><span class="caption">Public endpoint</span><span class="pill" id="health-public"><span class="dot" aria-hidden="true"></span><span>–</span></span></div>
    <div class="item"><span class="caption">Pairing</span><span class="pill" id="health-pairing"><span class="dot" aria-hidden="true"></span><span>–</span></span></div>
  </div>
  <div class="card"><h2>Recent activity</h2><p class="note" id="recent-note">Generation jobs reported by the server.</p><ul class="clean recent" id="recent-list"></ul><p class="empty" id="recent-empty" hidden>Nothing yet. Start a generation from Live activity and it will appear here.</p><div class="row" style="margin-top:12px"><button type="button" class="btn" id="dash-open-activity">Open Live activity</button></div></div>
</section>

<section class="panel" id="panel-library" aria-labelledby="h-library" hidden>
  <div class="page-head"><div><h1 id="h-library" tabindex="-1">Library</h1><p class="sub">Pool catalogue from the server.</p></div><div class="spacer"></div><span class="muted" id="lib-count"></span></div>
  <div class="lib-controls"><label for="lib-search" class="sr-only" style="position:absolute;left:-9999px">Search this page</label><input type="search" id="lib-search" placeholder="Search this page…" autocomplete="off"><button type="button" class="btn" id="lib-refresh">Refresh</button></div>
  <p class="feedback" id="lib-feedback" role="status" aria-live="polite"></p>
  <div class="errorbox" id="lib-error" role="alert" hidden></div>
  <ul class="lib-grid" id="lib-grid"></ul>
  <p class="empty" id="lib-empty" hidden>No packages in the pool yet.<br>Generate one from Live activity and it will appear here.</p>
  <p class="empty" id="lib-unsupported" hidden>Library preview is not available on this deployment. The server did not report a supported pool catalogue. Pool sharing and generation elsewhere in this dashboard keep working.</p>
  <div class="pagination"><button type="button" class="btn" id="lib-prev">Previous</button><span id="lib-page" aria-live="polite">Page 1</span><button type="button" class="btn" id="lib-next">Next</button></div>
</section>

<section class="panel" id="panel-activity" aria-labelledby="h-activity" hidden>
  <div class="page-head"><div><h1 id="h-activity" tabindex="-1">Live activity</h1><p class="sub">Real figures from the running server.</p></div></div>
  <div class="card">
    <h2>Generate NVMT package</h2>
    <p class="note">This runs only when you start it here. It reads the media URL and publishes the generated package to the pool. The URL is used for this generation only and is not saved or logged.</p>
    <form id="gen-form" novalidate>
      <div class="field"><label for="gen-source">Media URL</label><input id="gen-source" name="source_url" type="url" required maxlength="4096" autocomplete="off" inputmode="url"></div>
      <div class="form-grid">
        <div class="field"><label for="gen-profile">Profile</label><select id="gen-profile" name="profile"><option value="nvmt-v1-g10">nvmt-v1-g10 (10 s tiles)</option><option value="nvmt-v1-g5">nvmt-v1-g5 (5 s tiles)</option></select></div>
        <div class="field"><label for="gen-imdb">IMDb ID (optional)</label><input id="gen-imdb" name="imdb" placeholder="tt1234567" autocomplete="off" maxlength="16"></div>
        <div class="field"><label for="gen-season">Season (optional)</label><input id="gen-season" name="season" type="number" min="0" max="9999" inputmode="numeric"></div>
        <div class="field"><label for="gen-episode">Episode (optional)</label><input id="gen-episode" name="episode" type="number" min="0" max="9999" inputmode="numeric"></div>
        <div class="field full"><label for="gen-filename">Filename hint (optional)</label><input id="gen-filename" name="filename" maxlength="1024" autocomplete="off"></div>
      </div>
      <div class="row"><button type="submit" class="btn primary" id="gen-submit">Generate and publish</button></div>
    </form>
    <p class="feedback" id="gen-feedback" role="status" aria-live="polite"></p>
    <div class="errorbox" id="gen-error" role="alert" hidden></div>
  </div>
  <div class="jobs-wrap"><h2>Jobs</h2><ul class="clean" id="jobs-list"></ul><p class="empty" id="jobs-empty" hidden>No generation jobs yet.</p><p class="feedback" id="jobs-feedback" role="status" aria-live="polite"></p><div class="errorbox" id="jobs-error" role="alert" hidden></div></div>
</section>

<section class="panel" id="panel-diagnostics" aria-labelledby="h-diagnostics" hidden>
  <div class="page-head"><div><h1 id="h-diagnostics" tabindex="-1">Diagnostics</h1><p class="sub">What the server reports right now. No runtime log feed is available here.</p></div></div>
  <div class="card"><h2>Channel, librarian and contribution path</h2><dl class="kv" id="diag-channel"></dl><p class="feedback" id="diag-feedback" role="status" aria-live="polite"></p></div>
  <div class="card"><h2>Queue</h2><dl class="kv" id="diag-queue"></dl></div>
  <div class="card"><h2>Public budgets and blocks</h2><dl class="kv" id="diag-public"></dl></div>

</section>

<section class="panel" id="panel-sharing" aria-labelledby="h-sharing" hidden>
  <div class="page-head"><div><h1 id="h-sharing" tabindex="-1">Sharing &amp; devices</h1><p class="sub">Manage device access and guest contributions to your shared pool.</p></div></div>
  <div class="card">
    <h2>Sharing</h2>
    <div class="row">
      <span class="switch-row"><input type="checkbox" class="switch" id="sharing-enabled"><label for="sharing-enabled">Library sharing</label></span>
      <span class="switch-row"><input type="checkbox" class="switch" id="sharing-contrib"><label for="sharing-contrib">Allow guest contributions</label></span>
    </div>
    <p class="note" id="sharing-endpoint"></p>
    <div class="row"><button type="button" class="btn primary" id="btn-new-invite">New invitation…</button></div>
    <p class="feedback" id="sharing-feedback" role="status" aria-live="polite"></p>
    <div class="errorbox" id="sharing-error" role="alert" hidden></div>
  </div>
  <div class="card">
    <h2>Connect a device</h2>
    <p class="note" id="pairing-summary">Pairing is closed.</p>
    <div id="pairing-open" hidden>
      <p class="note">Enter this code on the device:</p>
      <p class="big-code" id="pairing-code"></p>
      <p class="note" id="pairing-meta"></p>
      <p class="note" id="pairing-address"></p>
      <p class="note" id="pairing-pin"></p>
      <p class="note" id="pairing-join-wrap" hidden>Join link: <code class="mono-code" id="pairing-join"></code></p>
      <div class="row"><button type="button" class="btn small" id="btn-copy-pair-join" hidden>Copy join link</button><button type="button" class="btn small" id="btn-copy-pair-code">Copy code</button></div>
    </div>
    <div class="row" style="margin-top:12px"><button type="button" class="btn primary" id="btn-pair-open">Open pairing window</button><button type="button" class="btn" id="btn-pair-close">Close pairing</button></div>
    <p class="feedback" id="pairing-feedback" role="status" aria-live="polite"></p>
    <div class="errorbox" id="pairing-error" role="alert" hidden></div>
  </div>
  <div class="card">
    <h2>Devices</h2>
    <table class="data-table"><thead><tr><th scope="col">Device</th><th scope="col">Role</th><th scope="col">Contributor</th><th scope="col">Paired</th><th scope="col">Last seen</th><th scope="col">Expires</th><th scope="col">Actions</th></tr></thead><tbody id="devices-body"></tbody></table>
    <p class="empty" id="devices-empty" hidden>No paired devices yet. Open a pairing window above.</p>
    <p class="feedback" id="devices-feedback" role="status" aria-live="polite"></p>
    <div class="errorbox" id="devices-error" role="alert" hidden></div>
  </div>
  <div class="card" id="invite-card">
    <h2>Invitations</h2>
    <form id="invite-form" novalidate>
      <div class="form-grid">
        <div class="field"><label for="invite-label">Label</label><input id="invite-label" name="label" maxlength="64" autocomplete="off" placeholder="Weekend guest"></div>
        <div class="field"><label for="invite-ttl">Expiry</label><select id="invite-ttl" name="ttl_s"><option value="3600">1 hour</option><option value="86400" selected>1 day</option><option value="259200">3 days</option><option value="604800">7 days</option></select></div>
        <div class="field"><label for="invite-device-days">Device lasts (days, optional)</label><input id="invite-device-days" name="device_days" type="number" min="1" max="3650" inputmode="numeric" placeholder="Leave blank for default"></div>
        <div class="field"><span class="switch-row"><input type="checkbox" class="switch" id="invite-readonly" name="read_only"><label for="invite-readonly">Read-only (no contributions)</label></span></div>
        <div class="field full"><span class="switch-row"><input type="checkbox" class="switch" id="invite-public" name="public"><label for="invite-public">Public endpoint</label></span><p class="hint" id="invite-public-note">Public invites are only available when a public host is configured.</p></div>
      </div>
      <div class="row"><button type="submit" class="btn primary" id="invite-submit">Create invitation</button></div>
    </form>
    <div id="invite-result" hidden>
      <h3>Invitation ready — send it privately; it will not be shown again</h3>
      <p class="note">Code: <code class="mono-code" id="invite-code"></code></p>
      <p class="note">Server: <code class="mono-code" id="invite-address"></code></p>
      <p class="note">Valid for <span id="invite-expires"></span>, one use.</p>
      <p class="note">Join link: <code class="mono-code" id="invite-join"></code></p>
      <div class="row"><button type="button" class="btn small" id="btn-copy-invite-code">Copy code</button><button type="button" class="btn small" id="btn-copy-invite-join">Copy join link</button></div>
    </div>
    <p class="feedback" id="invite-feedback" role="status" aria-live="polite"></p>
    <div class="errorbox" id="invite-error" role="alert" hidden></div>
    <table class="data-table" style="margin-top:14px"><thead><tr><th scope="col">Label</th><th scope="col">State</th><th scope="col">Expires in</th><th scope="col">Attempts left</th><th scope="col">Actions</th></tr></thead><tbody id="invites-body"></tbody></table>
    <p class="empty" id="invites-empty" hidden>No invitations.</p>
  </div>
</section>

<section class="panel" id="panel-settings" aria-labelledby="h-settings" hidden>
  <div class="page-head"><div><h1 id="h-settings" tabindex="-1">Settings</h1></div></div>
  <div class="card"><h2>Deployment &amp; server</h2><dl class="kv" id="deploy-list"></dl><ul class="addr-list" id="addresses-list"></ul><p class="note">Addresses and pin come from the running server. Copy them into Nuvio; the pin is checked automatically by the client.</p></div>
  <div class="card" id="setup-card">
    <h2>Telegram pool</h2>
    <div id="setup-wrap">
      <p class="note">OpenSeek stores packages in Telegram. Enter the channel and Telegram API details. Provide exactly one identity: a bot token or a StringSession. The channel must allow history access; posting permission enables uploads. Credentials are saved in the per-install private <code>.env</code> file and are never returned or logged. The form is cleared after successful setup.</p>
      <form id="setup-form" novalidate>
        <div class="field"><label for="setup-channel">Channel ID or username</label><input id="setup-channel" name="telegram_channel_id" required maxlength="128" autocomplete="off"></div>
        <div class="field"><label for="setup-api-id">Telegram API ID</label><input id="setup-api-id" name="telegram_api_id" type="number" min="1" required inputmode="numeric" autocomplete="off"></div>
        <div class="field"><label for="setup-api-hash">Telegram API hash</label><input id="setup-api-hash" name="telegram_api_hash" type="password" autocomplete="off" required minlength="8" maxlength="256"></div>
        <div class="field"><label for="setup-bot-token">Bot token (leave blank when using a StringSession)</label><input id="setup-bot-token" name="telegram_bot_token" type="password" autocomplete="off" maxlength="256"></div>
        <div class="field"><label for="setup-session">StringSession (leave blank when using a bot token)</label><input id="setup-session" name="telegram_session" type="password" autocomplete="off" maxlength="8192"></div>
        <div class="row"><button type="submit" class="btn primary" id="setup-submit">Verify channel and start scan</button></div>
      </form>
      <p class="feedback" id="setup-feedback" role="status" aria-live="polite"></p>
      <div class="errorbox" id="setup-error" role="alert" hidden></div>
    </div>
    <p class="note" id="setup-done" hidden>Telegram pool is already configured for this deployment.</p>
  </div>
  <div class="card"><h2>Channel</h2><dl class="kv" id="channel-list"></dl>
    <div class="row" style="margin-top:12px"><button type="button" class="btn" id="btn-scan">Scan channel</button></div>
    <p class="note">Rebuild clears the local channel index and rescans the whole channel. It does not delete Telegram messages or NVMT packages, but matching can miss titles until the rescan finishes.</p>
    <div class="row"><span class="switch-row"><input type="checkbox" class="switch" id="rebuild-confirm"><label for="rebuild-confirm">I understand rebuild clears the local index</label></span><button type="button" class="btn danger" id="btn-rebuild" disabled>Rebuild index</button></div>
    <p class="feedback" id="channel-feedback" role="status" aria-live="polite"></p>
    <div class="errorbox" id="channel-error" role="alert" hidden></div>
  </div>
  <div class="card" id="purge-card"><h2>Host maintenance</h2><div id="purge-wrap"></div><p class="feedback" id="purge-feedback" role="status" aria-live="polite"></p><div class="errorbox" id="purge-error" role="alert" hidden></div><div id="purge-result"></div></div>
  <div class="card"><h2>Public blocks</h2><dl class="kv" id="public-list"></dl>
    <p class="note">Clearing lifts every per-address block, strike and rate budget on the public listener. It does not disable the public listener itself.</p>
    <div class="row"><span class="switch-row"><input type="checkbox" class="switch" id="clear-confirm"><label for="clear-confirm">I understand this unblocks every public address</label></span><button type="button" class="btn danger" id="btn-clear-blocks" disabled>Clear blocked addresses</button></div>
    <p class="feedback" id="public-feedback" role="status" aria-live="polite"></p>
    <div class="errorbox" id="public-error" role="alert" hidden></div>
  </div>
  <div class="card"><h2>Librarian contributors</h2>
    <div id="contrib-wrap">
      <p class="note">Moderate Telegram librarian senders by numeric sender ID. Blocking stops their work; unblocking restores it. This is configured for local deployments only.</p>
      <form id="contrib-form" novalidate>
        <div class="field"><label for="contrib-sender">Sender ID (numeric)</label><input id="contrib-sender" name="sender_id" type="number" min="1" inputmode="numeric" autocomplete="off" required></div>
        <div class="row"><button type="button" class="btn danger" id="btn-contrib-block">Block sender</button><button type="button" class="btn" id="btn-contrib-unblock">Unblock sender</button></div>
      </form>
      <table class="data-table" style="margin-top:14px"><thead><tr><th scope="col">Sender ID</th><th scope="col">Work key</th><th scope="col">Day</th><th scope="col" class="num">Count</th></tr></thead><tbody id="contrib-quotas-body"></tbody></table>
      <table class="data-table" style="margin-top:14px"><thead><tr><th scope="col">Blocked sender ID</th><th scope="col">Blocked at</th></tr></thead><tbody id="contrib-blocked-body"></tbody></table>
      <p class="empty" id="contrib-empty" hidden>No contributor activity or blocks on this page.</p>
      <div class="pagination"><button type="button" class="btn" id="contrib-prev" disabled>Previous contributor records</button><span class="note" id="contrib-page"></span><button type="button" class="btn" id="contrib-next" disabled>Next contributor records</button></div>
    </div>
    <p class="note" id="contrib-unsupported" hidden>Contributor moderation is not available on this deployment (the server did not report it as supported). Librarian work itself is unaffected.</p>
    <p class="feedback" id="contrib-feedback" role="status" aria-live="polite"></p>
    <div class="errorbox" id="contrib-error" role="alert" hidden></div>
  </div>
</section>

</div>
</main>
</div>
<script>
"use strict";
const PROFILE='__PROFILE__';
let T=(location.hash||'').slice(1)||sessionStorage.getItem('osk')||'';if(T)sessionStorage.setItem('osk',T);history.replaceState(null,'',location.pathname);
const H={'X-OpenSeek-Admin':T};
const PAIR_KEY='oskPairing';
let authFailed=false;
let lastPairing='';
let lastPairCode='';
let lastInvite=null;
let lastPairJoin='';
const LIB_LIMIT=24;
let libOffset=0;
let libItems=[];
let libHasMore=false;
let libSupported=null;
let libQuery='';
const thumbCache=new Map();
let thumbnailQueue=Promise.resolve();
let lastDevicesSig='';
let lastInvitesSig='';
let lastPairingOpen=false;
let lastPoolMode='';
let purgeReady=false;
let previewOptions='';
let connectionSignature='';
let contributorOffset=0;
const CONTRIBUTOR_LIMIT=50;

function $(id){return document.getElementById(id);}
function el(tag,attrs,text){const n=document.createElement(tag);if(attrs){for(const k in attrs){if(k==='text')continue;n.setAttribute(k,attrs[k]);}}if(text!=null)n.textContent=String(text);return n;}
function setText(id,v){const n=$(id);if(n)n.textContent=(v==null?'':String(v));}
function isRecord(v){return v!==null&&typeof v==='object'&&!Array.isArray(v);}
function failure(kind,code){const e=new Error(kind);e.kind=kind;e.code=code||kind;return e;}
function clearBox(id){const n=$(id);if(!n)return;n.textContent='';n.hidden=true;}
function showError(boxId,code,fallback){const box=$(boxId);if(!box)return;box.textContent='';box.hidden=false;const p=el('p',{},humanError(code,fallback));p.style.margin='0';box.appendChild(p);const d=el('details',{class:'err-details'});const s=el('summary',{},'Error code');const c=el('code',{},String(code||'request_failed'));s.appendChild(el('span',{},' '));s.appendChild(c);d.appendChild(s);const det=el('p',{class:'note'},'The code above is for support. No secret or media URL is included.');d.appendChild(det);box.appendChild(d);}
function setFeedback(id,msg){setText(id,msg||'');}
function humanError(code,fallback){
  const m={
    telegram_access_denied:'OpenSeek cannot read this channel. Add the bot or account to the channel and grant access to its message history.',
    telegram_login_required:'Telegram rejected these credentials. Check the bot token or StringSession and Telegram API ID/hash.',
    telegram_unavailable:'Telegram could not be reached. Check the network and try again.',
    telegram_error:'Telegram could not verify this channel. Check the channel ID and account permissions.',
    bad_telegram_settings:'These Telegram settings were rejected. Check the channel ID, API ID/hash, and provide exactly one of bot token or StringSession.',
    setup_failed:'Telegram setup failed. Check the channel ID, Telegram credentials, and channel permissions.',
    already_configured:'Telegram is already configured for this deployment.',
    set_environment_variables_in_vercel:'Telegram setup must be done with environment variables in Vercel for this deployment.',
    pool_unavailable:'The pool is not available on this server, so generation cannot publish. Check the pool status in Settings.',
    generation_busy:'Another generation is already running. Wait for it to finish or cancel it.',
    busy:'Another generation is already running. Wait for it to finish or cancel it.',
    source_forbidden:'This media URL is not allowed by the destination policy. Use a URL this server is allowed to fetch.',
    generation_failed:'Generation failed. Check the media URL and try again.',
    generation_interrupted:'This generation stopped before it finished (the server may have restarted). Start it again.',
    publication_failed:'The package was generated but could not be published to the pool. Check the channel or host pool status.',
    not_running:'This generation is not running, so it cannot be cancelled.',
    not_found:'That item was not found on the server. It may have already been used, revoked or cleaned up.',
    sharing_disabled:'Library sharing is off. Turn it on before creating invitations.',
    public_endpoint_not_configured:'A public host is not configured, so a public invitation cannot be created. Create a LAN invitation instead.',
    bad_host:'The server could not determine its public host for this request. Try again.',
    pairing_already_open:'A pairing window is already open. Close it first or reuse the open code.',
    bad_purge_options:'Those purge options were rejected. Check the numbers and try a preview first.',
    bad_request:'The server rejected those values. Check the form fields and try again.',
    forbidden:'This action is not supported on this deployment.',
    unsupported:'This feature is not available on this deployment.',
    not_supported:'This feature is not available on this deployment.',
    pool_backend_unavailable:'The pool backend is temporarily unavailable. Wait a few seconds and retry.',
    request_failed:'The server reported a problem. Check the status below and retry.',
    network:'The server could not be reached. Check that OpenSeek is running and try again.',
    malformed:'The server returned an unexpected response. Try again; if it persists, restart OpenSeek.',
    load:'OpenSeek could not load a valid dashboard response. Check that the server is reachable, then retry.',
    auth:'Admin access denied.'
  };
  if(fallback&&m[code])return m[code];
  return m[code]||(fallback||('Something went wrong ('+String(code||'request_failed')+'). Check the status and try again.'));
}
function setupErrorText(code){return humanError(code);}
async function adminRequest(url,options){
  let response;
  try{response=await fetch(url,Object.assign({},options||{},{referrerPolicy:'no-referrer',headers:Object.assign({},(options&&options.headers)||{})}));}
  catch(_){throw failure('network','network');}
  if(response.status===204)return {response:response,data:null};
  let data=null;
  try{data=await response.json();}catch(_){if(response.ok)throw failure('malformed','malformed');}
  if(response.status===403&&isRecord(data)&&data.error==='admin_auth')throw failure('auth','admin_auth');
  if(response.ok&&!isRecord(data))throw failure('malformed','malformed');
  return {response:response,data:data};
}
async function runBusy(controls,fn){
  const list=(controls||[]).filter(Boolean);
  list.forEach(function(c){c.disabled=true;});
  try{return await fn();}
  finally{
    list.forEach(function(c){c.disabled=false;});
    const ids=list.map(function(c){return c.id;});
    if(ids.indexOf('lib-prev')!==-1||ids.indexOf('lib-next')!==-1)renderLibraryMeta();
    if(ids.indexOf('btn-pair-open')!==-1||ids.indexOf('btn-pair-close')!==-1){$('btn-pair-open').disabled=lastPairingOpen;$('btn-pair-close').disabled=!lastPairingOpen;}
    if(ids.indexOf('btn-scan')!==-1)$('btn-scan').disabled=(lastPoolMode!=='channel');
    if(ids.indexOf('btn-rebuild')!==-1||ids.indexOf('rebuild-confirm')!==-1)$('btn-rebuild').disabled=lastPoolMode!=='channel'||!$('rebuild-confirm').checked;
    if(ids.indexOf('btn-clear-blocks')!==-1||ids.indexOf('clear-confirm')!==-1)$('btn-clear-blocks').disabled=!$('clear-confirm').checked;
    if(ids.indexOf('btn-purge-preview')!==-1||ids.indexOf('btn-purge-confirm')!==-1){const pc=$('btn-purge-confirm');if(pc)pc.disabled=!purgeReady;}
  }
}
function addCopyButton(container,text,feedbackId){
  const b=el('button',{type:'button',class:'btn small'},'Copy');
  b.addEventListener('click',async function(){
    try{await navigator.clipboard.writeText(text);setFeedback(feedbackId,'Copied.');}
    catch(_){
      const ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);ta.select();
      try{document.execCommand('copy');setFeedback(feedbackId,'Copied.');}catch(__){setFeedback(feedbackId,'Copy failed. Select the text and copy it manually.');}
      ta.remove();
    }
  });
  container.appendChild(b);
}
function fmtBytes(n){if(!Number.isFinite(n))return '–';if(n<1024)return String(n)+' B';const u=['KiB','MiB','GiB','TiB'];let v=n;let i=-1;while(v>=1024&&i<u.length-1){v/=1024;i++;}return v.toFixed(v>=100?0:v>=10?1:2)+' '+u[i];}
function fmtDuration(ms){if(!Number.isFinite(ms)||ms<=0)return '–';const s=Math.round(ms/1000);if(s<60)return s+'s';const m=Math.floor(s/60);const r=s-m*60;if(m<60)return m+'m '+String(r).padStart(2,'0')+'s';const h=Math.floor(m/60);return h+'h '+String(m-h*60).padStart(2,'0')+'m';}
function fmtAgo(ts){if(!Number.isFinite(ts)||ts<=0)return '–';const d=(Date.now()/1000)-ts;if(d<60)return 'just now';if(d<3600)return Math.floor(d/60)+' min ago';if(d<86400)return Math.floor(d/3600)+' h ago';const days=Math.floor(d/86400);return days+' d ago';}
function fmtDate(ts){if(!Number.isFinite(ts)||ts<=0)return 'never';try{return new Date(ts*1000).toLocaleDateString();}catch(_){return '–';}}
function fmtExpiresIn(s){if(!Number.isFinite(s))return '–';if(s<=0)return 'expired';if(s<3600)return Math.floor(s/60)+' min';const h=Math.floor(s/3600);return h+' h';}
function fmtClock(ts){if(!Number.isFinite(ts)||ts<=0)return '';try{const d=new Date(ts*1000);return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');}catch(_){return '';}}
function pillUpdate(id,cls,text){const p=$(id);if(!p)return;p.className='pill '+cls;const s=p.querySelector('span:last-child');if(s)s.textContent=text;else p.textContent=text;}
function kvClear(id){const n=$(id);if(n)n.textContent='';}
function kvAdd(id,dt,dd){const n=$(id);if(!n)return;n.appendChild(el('dt',{},dt));n.appendChild(el('dd',{},dd));}
function jobTitle(j){
  if(j&&typeof j.fp==='string'&&j.fp)return 'Generation · '+j.fp.slice(0,8)+(j.profile?' · '+j.profile:'');
  if(j&&j.profile)return 'Generation · '+j.profile;
  return 'Generation';
}
function jobStateText(s){return s?String(s):'unknown';}
function restoreFocus(listEl,focusKey){
  if(!focusKey)return;
  const t=listEl.querySelector('[data-key="'+focusKey+'"]');
  if(t)t.focus();
}
function captureFocus(listEl){
  const a=document.activeElement;
  if(a&&listEl.contains(a)&&a.dataset&&a.dataset.key)return a.dataset.key;
  return '';
}
function parsePairCode(stored){
  if(typeof stored!=='string')return '';
  const prefix='Nuvio code: ';
  if(stored.indexOf(prefix)!==0)return '';
  const rest=stored.slice(prefix.length);
  const cut=rest.indexOf(' |');
  return cut===-1?rest:rest.slice(0,cut);
}
function parsePairJoin(stored){
  if(typeof stored!=='string')return '';
  const marker=' | Join link: ';
  const idx=stored.indexOf(marker);
  return idx===-1?'':stored.slice(idx+marker.length);
}

/* ---- navigation ---- */
let currentPanel='dashboard';
function showPanel(name){
  currentPanel=name;
  const btns=document.querySelectorAll('nav.primary button[data-panel]');
  btns.forEach(function(b){if(b.dataset.panel===name)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
  const panels=['dashboard','library','activity','diagnostics','sharing','settings'];
  panels.forEach(function(p){const sec=$('panel-'+p);if(sec)sec.hidden=(p!==name);});
  const h=$('h-'+name);
  if(h&&document.activeElement!==h){h.focus({preventScroll:true});}
  const main=$('main');if(main&&h){main.scrollTop=0;}
  if(name==='library'&&libSupported===null){loadLibrary();}
}

/* ---- auth gate ---- */
function showGate(denied){
  authFailed=denied;
  const gate=$('authgate'),panels=$('panels');
  if(gate)gate.hidden=false;
  if(panels)panels.hidden=true;
  if(denied){
    setText('authgate-title','Admin access denied');
    setText('authgate-text',PROFILE==='vercel'?'The admin secret was not accepted. Reopen the dashboard with the correct admin link from your deployment.':'The admin secret was not accepted. On the server, run python -m openseek dashboard to get a fresh link, then reopen it.');
  }else{
    setText('authgate-title','Admin access required');
    setText('authgate-text',PROFILE==='vercel'?'Open this page with the admin secret in the URL fragment: https://'+location.host+'/#<OPENSEEK_ADMIN_TOKEN>':'Open this page with: python -m openseek dashboard');
  }
  if(gate)gate.focus();
}
function hideGate(){$('authgate').hidden=true;$('panels').hidden=false;}

/* ---- poll + update ---- */
let polling=false;
async function poll(){
  if(polling)return;
  if(!T){showGate(false);return;}
  if(authFailed)return;
  polling=true;
  try{
    const cfgRes=await adminRequest('/admin/setup',{headers:H});
    if(!cfgRes.response.ok)throw failure('load',isRecord(cfgRes.data)&&cfgRes.data.error?cfgRes.data.error:'load');
    const cfg=cfgRes.data;
    if(typeof cfg.setup_required!=='boolean')throw failure('malformed','malformed');
    updateSetupVisibility(cfg);
    const stRes=await adminRequest('/admin/status',{headers:H});
    if(!stRes.response.ok)throw failure('load',isRecord(stRes.data)&&stRes.data.error?stRes.data.error:'load');
    const st=stRes.data;
    if(!isRecord(st.pairing)||typeof st.pairing.open!=='boolean'||!isRecord(st.server)||!Array.isArray(st.server.addresses)||!Array.isArray(st.devices)||!st.devices.every(isRecord)||!isRecord(st.cache)||!['entries','tiles','bytes','max_bytes'].every(function(k){return Number.isFinite(st.cache[k]);}))throw failure('malformed','malformed');
    if(!st.pairing.open){try{sessionStorage.removeItem(PAIR_KEY);}catch(_){}lastPairing='';lastPairCode='';lastPairJoin='';}else if(!lastPairCode){try{lastPairing=sessionStorage.getItem(PAIR_KEY)||'';lastPairCode=parsePairCode(lastPairing);if(!lastPairJoin)lastPairJoin=parsePairJoin(lastPairing);}catch(_){}}
    updateOnline(true);
    const shRes=await adminRequest('/admin/sharing',{headers:H});
    if(!shRes.response.ok)throw failure('load',isRecord(shRes.data)&&shRes.data.error?shRes.data.error:'load');
    const sh=shRes.data;
    if(typeof sh.enabled!=='boolean'||!Array.isArray(sh.invites)||!sh.invites.every(function(i){return isRecord(i)&&typeof i.invite_id==='string';}))throw failure('malformed','malformed');
    const genRes=await adminRequest('/admin/generations',{headers:H});
    if(!genRes.response.ok)throw failure('load',isRecord(genRes.data)&&genRes.data.error?genRes.data.error:'load');
    const gen=genRes.data;
    if(!Array.isArray(gen.jobs)||!gen.jobs.every(isRecord))throw failure('malformed','malformed');
    const poolRes=await adminRequest('/admin/pool/status',{headers:H});
    if(poolRes.response.status===503&&poolRes.data&&poolRes.data.error==='telegram_session_busy')return;
    if(!poolRes.response.ok)throw failure('load',isRecord(poolRes.data)&&poolRes.data.error?poolRes.data.error:'load');
    const pool=poolRes.data;
    if(!isRecord(pool)||typeof pool.mode!=='string')throw failure('malformed','malformed');
    let contrib=null,contribUnsupported=false;
    try{
      const cRes=await adminRequest('/admin/contributors?limit='+CONTRIBUTOR_LIMIT+'&offset='+contributorOffset,{headers:H});
      if(!cRes.response.ok||!isRecord(cRes.data)||typeof cRes.data.supported!=='boolean')contribUnsupported=true;
      else contrib=cRes.data;
    }catch(e){if(e&&e.kind==='auth')throw e;contribUnsupported=true;}
    updateDashboard(st,sh,pool,gen);
    updateSharing(st,sh);
    updateActivityJobs(gen);
    updateSettings(st,sh,pool,cfg);
    updateDiagnostics(st,sh,pool,gen);
    updateContributors(contrib,contribUnsupported);
    hideGate();
    setOnlinePill(true);
  }catch(e){
    if(e&&e.kind==='auth'){
      try{sessionStorage.removeItem('osk');sessionStorage.removeItem(PAIR_KEY);}catch(_){}
      lastPairing='';lastPairCode='';lastInvite=null;lastPairJoin='';T='';delete H['X-OpenSeek-Admin'];
      showGate(true);setOnlinePill(false);return;
    }
    setOnlinePill(false);
    const b=$('dash-banner');if(b){b.hidden=false;b.textContent='Dashboard status unavailable. OpenSeek could not load a valid dashboard response. Check that the server is reachable, then retry. Existing form entries are kept.';}
  }finally{polling=false;}
}
function setOnlinePill(on){
  pillUpdate('srv-pill',on?'pill-good':'pill-bad',on?'Server online':'Server offline');
  pillUpdate('dash-online',on?'pill-good':'pill-bad',on?'Online':'Offline');
}
function updateOnline(on){if(on){const b=$('dash-banner');if(b)b.hidden=true;}}
function updateSetupVisibility(cfg){
  const wrap=$('setup-wrap'),done=$('setup-done');
  if(cfg.setup_required){if(wrap)wrap.hidden=false;if(done)done.hidden=true;}
  else{if(wrap)wrap.hidden=true;if(done)done.hidden=false;}
}
function updateDashboard(st,sh,pool,gen){
  const c=st.cache;
  if(pool.mode==='host'&&Number.isFinite(pool.packages)){
    setText('stat-entries',Number(pool.packages).toLocaleString());
    setText('stat-entries-caption','Pool packages');
    setText('stat-entries-sub',pool.packages===1?'package in pool':'packages in pool');
  }else if(pool.mode==='channel'&&Number.isFinite(pool.hints)){
    setText('stat-entries',Number(pool.hints).toLocaleString());
    setText('stat-entries-caption','Pool hints');
    setText('stat-entries-sub',pool.hints===1?'hint in pool':'hints in pool');
  }else{
    setText('stat-entries','–');
    setText('stat-entries-caption','Pool packages');
    setText('stat-entries-sub','no pool in local mode');
  }
  if((pool.mode==='host'||pool.mode==='channel')&&Number.isFinite(pool.tiles)){
    setText('stat-tiles',Number(pool.tiles).toLocaleString());
    setText('stat-tiles-caption','Pool tiles');
    setText('stat-tiles-sub','tiles in pool');
  }else{
    setText('stat-tiles','–');
    setText('stat-tiles-caption','Pool tiles');
    setText('stat-tiles-sub','no pool in local mode');
  }
  setText('stat-bytes',fmtBytes(c.bytes));
  setText('stat-bytes-sub','of '+fmtBytes(c.max_bytes)+' limit');
  const pct=c.max_bytes>0?Math.max(0,Math.min(1,c.bytes/c.max_bytes)):0;
  $('bytes-fill').style.width=(pct*100).toFixed(1)+'%';
  $('bytes-fill').className='bar-fill'+(pct>0.9?' warn':'');
  $('bytes-bar').setAttribute('aria-valuenow',String(Math.round(pct*100)));
  const runningGen=(gen.jobs||[]).filter(function(j){return j.state==='generating'||j.state==='queued'||j.state==='publishing';}).length;
  setText('stat-jobs',st.active_sources==null?'–':st.active_sources);
  setText('stat-jobs-sub',(st.active_sources===0?'idle':'active source'+(st.active_sources===1?'':'s'))+(runningGen?(' · '+runningGen+' generation'+(runningGen===1?'':'s')+' running'):' · no generation running'));
  pillUpdate('health-sharing',sh.enabled?'pill-good':'',sh.enabled?'On':'Off');
  pillUpdate('health-contrib',sh.pool_contributions_enabled?'pill-good':'',sh.pool_contributions_enabled?'On':'Off');
  const mode=pool.mode||'local';
  pillUpdate('health-pool',mode==='channel'?'pill-teal':(mode==='host'?'pill-accent':''),mode);
  const pub=sh.public||{};
  pillUpdate('health-public',pub.listening?'pill-teal':'',pub.listening?(pub.host||('port '+pub.port)):'Not listening');
  pillUpdate('health-pairing',st.pairing.open?'pill-accent':'',st.pairing.open?'Open':'Closed');
  renderRecent(gen);
}
function renderRecent(gen){
  const list=$('recent-list');if(!list)return;
  const focusKey=captureFocus(list);
  list.textContent='';
  const jobs=(gen.jobs||[]).slice(0,6);
  $('recent-empty').hidden=jobs.length>0;
  jobs.forEach(function(j){
    const li=el('li');
    const dot=el('span',{class:'dot'},'●');
    const what=el('span',{class:'what'});
    let txt=jobTitle(j)+' · '+jobStateText(j.state);
    if(Number.isFinite(j.tiles_done)&&Number.isFinite(j.tiles_total))txt+=' · '+j.tiles_done+'/'+j.tiles_total+' tiles';
    else if(Number.isFinite(j.tiles_done))txt+=' · '+j.tiles_done+' tiles done';
    if(j.error)txt+=' · '+humanError(j.error);
    what.textContent=txt;
    li.appendChild(dot);li.appendChild(what);
    li.appendChild(el('span',{class:'when'},fmtClock(j.updated||j.created)));
    list.appendChild(li);
  });
  restoreFocus(list,focusKey);
}
function updateSharing(st,sh){
  const en=$('sharing-enabled'),co=$('sharing-contrib');
  if(document.activeElement!==en)en.checked=!!sh.enabled;
  if(document.activeElement!==co)co.checked=!!sh.pool_contributions_enabled;
  const pub=sh.public||{};
  let txt;
  if(pub.listening)txt='Public endpoint: '+(pub.host||'(no public host set)')+' → listening on port '+pub.port+' · '+(pub.blocked||0)+' address(es) blocked · '+(pub.refused_total||0)+' request(s) rate-limited. Guests can only pair with an invitation and read shared thumbnails.';
  else txt=(PROFILE==='vercel'?'Invitations use this deployment’s HTTPS address.':'The public endpoint is not started. Invitations work on your local network only.');
  setText('sharing-endpoint',txt);
  const pubCheck=$('invite-public'),note=$('invite-public-note');
  const pubReady=!!(pub.listening&&pub.host);
  if(pubCheck)pubCheck.disabled=!pubReady;
  if(note)note.textContent=pubReady?'Public invites use '+pub.host+'.':'Public invites are only available when a public host is configured.';
  const pairOpen=!!st.pairing.open;
  setText('pairing-summary',pairOpen?'Pairing is open. Enter the code below on the device.':'Pairing is closed.');
  $('pairing-open').hidden=!pairOpen;
  $('btn-pair-open').disabled=pairOpen;
  $('btn-pair-close').disabled=!pairOpen;
  lastPairingOpen=pairOpen;
  if(pairOpen){
    const statusCode=typeof st.pairing.code==='string'?st.pairing.code:'';
    if(statusCode)lastPairCode=statusCode;
    setText('pairing-code',statusCode||lastPairCode||'(code not reported)');
    setText('pairing-meta','Expires in '+fmtExpiresIn(st.pairing.expires_in_s)+(' · attempts left '+st.pairing.attempts_left));
    setText('pairing-address','Server: '+(PROFILE==='vercel'?location.host:st.server.addresses.map(function(a){return a+':'+st.server.port;}).join(', ')));
    setText('pairing-pin','TLS pin: '+(st.server.spki_pin||'–'));
    const statusJoin=typeof st.pairing.join==='string'&&st.pairing.join?st.pairing.join:'';
    if(statusJoin)lastPairJoin=statusJoin;
    const hasJoin=!!lastPairJoin;
    $('pairing-join-wrap').hidden=!hasJoin;$('btn-copy-pair-join').hidden=!hasJoin;
    if(hasJoin)setText('pairing-join',lastPairJoin);
  }else if(st.pairing.burned){setText('pairing-summary','Pairing is closed. The last window burned after too many wrong codes.');}
  renderDevices(st.devices||[]);
  renderInvites(sh.invites||[]);
}
function renderDevices(devices){
  const body=$('devices-body');if(!body)return;
  const sig=JSON.stringify(devices.map(function(d){return [d.device_id,d.name,d.role,d.contribute,d.expires];}));
  if(sig===lastDevicesSig){devices.forEach(function(d,i){const row=body.children[i];if(row){row.children[3].textContent=fmtAgo(d.created);row.children[4].textContent=fmtAgo(d.last_seen);}});return;}
  lastDevicesSig=sig;
  const focusKey=captureFocus(body);
  body.textContent='';
  $('devices-empty').hidden=devices.length>0;
  devices.forEach(function(d){
    const tr=el('tr');
    tr.appendChild(el('td',{'data-label':'Device'},d.name||'Unnamed device'));
    tr.appendChild(el('td',{'data-label':'Role'},d.role||'owner'));
    const contribCell=el('td',{'data-label':'Contributor'},d.role==='guest'?(d.contribute?'Contributor':'Reader'):'–');
    tr.appendChild(contribCell);
    tr.appendChild(el('td',{'data-label':'Paired'},fmtAgo(d.created)));
    tr.appendChild(el('td',{'data-label':'Last seen'},fmtAgo(d.last_seen)));
    tr.appendChild(el('td',{'data-label':'Expires'},fmtDate(d.expires)));
    const actions=el('td',{'data-label':'Actions'});
    if(d.role==='guest'){
      const b=el('button',{type:'button',class:'btn small','data-key':'contrib-'+d.device_id},d.contribute?'Remove contributor':'Make contributor');
      b.addEventListener('click',async function(){await setContributor(d,b);});
      actions.appendChild(b);
    }
    const rev=el('button',{type:'button',class:'btn small danger','data-key':'revoke-'+d.device_id},'Revoke');
    rev.addEventListener('click',async function(){if(!armConfirm(rev,'Revoke'))return;await revokeDevice(d,rev);});
    actions.appendChild(rev);
    tr.appendChild(actions);
    body.appendChild(tr);
  });
  restoreFocus(body,focusKey);
}
function renderInvites(invites){
  const body=$('invites-body');if(!body)return;
  const sig=JSON.stringify(invites.map(function(i){return [i.invite_id,i.label,i.state,i.attempts_left];}));
  if(sig===lastInvitesSig){invites.forEach(function(inv,i){const row=body.children[i];if(row)row.children[2].textContent=fmtExpiresIn(inv.expires_in_s);});return;}
  lastInvitesSig=sig;
  const focusKey=captureFocus(body);
  body.textContent='';
  $('invites-empty').hidden=invites.length>0;
  invites.forEach(function(inv){
    const tr=el('tr');
    tr.appendChild(el('td',{'data-label':'Label'},inv.label||'(no label)'));
    tr.appendChild(el('td',{'data-label':'State'},inv.state||'?'));
    tr.appendChild(el('td',{'data-label':'Expires in'},fmtExpiresIn(inv.expires_in_s)));
    tr.appendChild(el('td',{'data-label':'Attempts left'},String(inv.attempts_left==null?'–':inv.attempts_left)));
    const actions=el('td',{'data-label':'Actions'});
    if(inv.state==='open'){
      const b=el('button',{type:'button',class:'btn small danger','data-key':'invite-'+inv.invite_id},'Revoke');
      b.addEventListener('click',async function(){if(!armConfirm(b,'Revoke'))return;await revokeInvite(inv,b);});
      actions.appendChild(b);
    }
    tr.appendChild(actions);
    body.appendChild(tr);
  });
  restoreFocus(body,focusKey);
}
function updateActivityJobs(gen){
  const list=$('jobs-list');if(!list)return;
  const focusKey=captureFocus(list);
  const openByGen={};
  list.querySelectorAll('[data-gen]').forEach(function(card){openByGen[card.dataset.gen]=Array.prototype.map.call(card.querySelectorAll('details'),function(d){return d.open;});});
  list.textContent='';
  const jobs=gen.jobs||[];
  $('jobs-empty').hidden=jobs.length>0;
  jobs.forEach(function(j){
    const card=el('div',{class:'job'});card.dataset.gen=String(j.generation_id||'');
    const head=el('div',{class:'job-head'});
    head.appendChild(el('h3',{},jobTitle(j)));
    const state=j.state||'unknown';
    const cls=state==='complete'?'pill-good':(state==='failed'||state==='cancelled'?'pill-bad':(state==='generating'||state==='queued'||state==='publishing'?'pill-accent':''));
    const pill=el('span',{class:'pill '+cls});pill.appendChild(el('span',{class:'dot'},''));const st=el('span',{},state);pill.appendChild(st);
    head.appendChild(pill);
    if(state==='queued'||state==='generating'){
      const cb=el('button',{type:'button',class:'btn small danger','data-key':'cancel-'+j.generation_id},'Cancel');
      cb.addEventListener('click',async function(){await cancelGeneration(j,cb);});
      head.appendChild(cb);
    }
    card.appendChild(head);
    const total=Number.isFinite(j.tiles_total)?j.tiles_total:null;
    const done=Number.isFinite(j.tiles_done)?j.tiles_done:0;
    const barWrap=el('div',{class:'bar',role:'progressbar','aria-label':'Generation progress','aria-valuemin':'0','aria-valuemax':'100','aria-valuenow':String(total?Math.round(done/total*100):0)});
    const fill=el('div',{class:'bar-fill'+(state==='complete'?' good':'')});
    fill.style.width=(total?Math.round(done/total*100):(state==='complete'?100:0))+'%';
    barWrap.appendChild(fill);card.appendChild(barWrap);
    let meta=done+'/'+(total==null?'?':total)+' tiles';
    if(Number.isFinite(j.duration_ms)&&j.duration_ms)meta+=' · '+fmtDuration(j.duration_ms);
    if(j.profile)meta+=' · '+j.profile;
    if(j.updated)meta+=' · updated '+fmtAgo(j.updated);
    if(j.fp)meta+=' · package '+j.fp.slice(0,12)+'…';
    card.appendChild(el('p',{class:'meta'},meta));
    if(j.error){
      card.appendChild(el('p',{class:'meta'},'Error: '+humanError(j.error)));
      const d=el('details',{class:'err-details'});const s=el('summary',{},'Error code');s.appendChild(el('span',{},' '));s.appendChild(el('code',{},String(j.error)));d.appendChild(s);card.appendChild(d);
    }
    const det=el('details',{class:'err-details'});const sum=el('summary',{},'Details');det.appendChild(sum);
    const dl=el('dl',{class:'kv'});
    dl.appendChild(el('dt',{},'Generation ID'));dl.appendChild(el('dd',{},String(j.generation_id||'–')));
    if(j.fp){dl.appendChild(el('dt',{},'Package'));dl.appendChild(el('dd',{},String(j.fp)));}
    if(j.upload_id){dl.appendChild(el('dt',{},'Upload'));dl.appendChild(el('dd',{},String(j.upload_id)));}
    if(j.package_sha256){dl.appendChild(el('dt',{},'Package hash'));dl.appendChild(el('dd',{},String(j.package_sha256)));}
    det.appendChild(dl);card.appendChild(det);
    const openArr=openByGen[String(j.generation_id||'')]||[];
    card.querySelectorAll('details').forEach(function(d,i){if(openArr[i])d.open=true;});
    list.appendChild(card);
  });
  restoreFocus(list,focusKey);
}
function updateSettings(st,sh,pool,cfg){
  lastPoolMode=pool.mode||'';
  kvClear('deploy-list');
  kvAdd('deploy-list','Deployment profile',String(st.server.profile||PROFILE||'–'));
  kvAdd('deploy-list','API port',String(st.server.port==null?'–':st.server.port));
  kvAdd('deploy-list','Cache',st.cache.entries+' entries · '+st.cache.tiles+' tiles · '+fmtBytes(st.cache.bytes)+' of '+fmtBytes(st.cache.max_bytes));
  kvAdd('deploy-list','Sharing',(sh.enabled?'On':'Off')+' · guest uploads '+(sh.pool_contributions_enabled?'On':'Off'));
  kvAdd('deploy-list','Pool mode',String(pool.mode||'local'));
  kvAdd('deploy-list','TLS pin',String(st.server.spki_pin||'–'));
  const nextConnectionSignature=JSON.stringify(st.server);
  if(nextConnectionSignature!==connectionSignature){
    connectionSignature=nextConnectionSignature;
    const ul=$('addresses-list');ul.textContent='';
    const addrs=(st.server.profile==='vercel')?[location.host]:((st.server.addresses||[]).map(function(a){return a+':'+st.server.port;}));
    if(!addrs.length)addrs.push('–');
    addrs.forEach(function(a){
      const li=el('li');
      li.appendChild(el('code',{class:'mono-code'},String(a)));
      addCopyButton(li,String(a),'deploy-feedback');
      ul.appendChild(li);
    });
    const pinLi=el('li');
    pinLi.appendChild(el('span',{class:'note'},'TLS pin: '));
    pinLi.appendChild(el('code',{class:'mono-code'},String(st.server.spki_pin||'–')));
    addCopyButton(pinLi,String(st.server.spki_pin||''),'deploy-feedback');
    ul.appendChild(pinLi);
    const fb=el('p',{class:'feedback',id:'deploy-feedback',role:'status','aria-live':'polite'});
    if(!$('deploy-feedback'))ul.after(fb);
  }
  kvClear('channel-list');
  kvAdd('channel-list','Mode',String(pool.mode||'local'));
  if(pool.mode==='channel'){
    kvAdd('channel-list','Channel',String(pool.channel_id||'–'));
    kvAdd('channel-list','Tiles / hints',String(pool.tiles==null?'–':pool.tiles)+' / '+String(pool.hints==null?'–':pool.hints));
    kvAdd('channel-list','Last message',String(pool.last_message_id==null?'–':pool.last_message_id));
    kvAdd('channel-list','History readable',pool.history_readable==null?'unknown':(pool.history_readable?'yes':'no'));
    kvAdd('channel-list','Posting',pool.channel_writable==null?'unknown':(pool.channel_writable?'available':'read-only'));
    kvAdd('channel-list','Pending uploads',String(pool.outbox==null?'–':pool.outbox));
    kvAdd('channel-list','Last error',pool.last_error?humanError(pool.last_error):'none');
    kvAdd('channel-list','Librarian',String(pool.librarian_status||'–'));
    kvAdd('channel-list','Contribution path',String(pool.contribution_path||'–'));
    $('btn-scan').disabled=false;$('btn-rebuild').disabled=lastPoolMode!=='channel'||!$('rebuild-confirm').checked;
  }else if(pool.mode==='host'){
    kvAdd('channel-list','API',pool.api_enabled?'enabled':'disabled');
    if(pool.api_url)kvAdd('channel-list','API URL',String(pool.api_url));
    kvAdd('channel-list','Tiles / packages',String(pool.tiles==null?'–':pool.tiles)+' / '+String(pool.packages==null?'–':pool.packages));
    kvAdd('channel-list','Pending uploads',String(pool.outbox==null?'–':pool.outbox));
    kvAdd('channel-list','Last error',pool.last_error?humanError(pool.last_error):'none');
    kvAdd('channel-list','Note','Channel scan and rebuild apply to channel mode only.');
    $('btn-scan').disabled=true;$('btn-rebuild').disabled=true;
  }else{
    kvAdd('channel-list','Note','No shared pool connections in this mode.');$('btn-scan').disabled=true;$('btn-rebuild').disabled=true;
  }
  kvClear('public-list');
  const pub=sh.public||{};
  kvAdd('public-list','Public listener',pub.listening?'listening':'not listening');
  kvAdd('public-list','Host / port',String(pub.host||'–')+' / '+String(pub.port==null?'–':pub.port));
  kvAdd('public-list','Blocked addresses',String(pub.blocked==null?'–':pub.blocked));
  kvAdd('public-list','Rate-limited requests',String(pub.refused_total==null?'–':pub.refused_total));
  kvAdd('public-list','Tracked addresses',String(pub.tracked==null?'–':pub.tracked));
  renderPurgeCard(pool);
}
function renderPurgeCard(pool){
  const wrap=$('purge-wrap');if(!wrap)return;
  const unavailableMode='none:'+(pool.mode||'local');
  if(pool.mode!=='host'&&wrap.dataset.rendered!==unavailableMode){
    wrap.textContent='';wrap.dataset.rendered=unavailableMode;
    wrap.appendChild(el('p',{class:'note'},'Host purge is only available in host mode. This deployment is in '+(pool.mode||'local')+' mode, so there is no purge to preview here.'));
    return;
  }
  if(pool.mode==='host'&&wrap.dataset.rendered!=='host'){
    wrap.textContent='';wrap.dataset.rendered='host';
    const form=el('form',{id:'purge-form',novalidate:''});
    const grid=el('div',{class:'form-grid'});
    const f1=el('div',{class:'field'});f1.appendChild(el('label',{for:'purge-days'},'Outbox older than (days)'));const dIn=el('input',{id:'purge-days',type:'number',min:'1',max:'3650',value:'30',inputmode:'numeric'});f1.appendChild(dIn);
    const f2=el('div',{class:'field'});f2.appendChild(el('label',{for:'purge-attempts'},'Outbox attempts'));const aIn=el('input',{id:'purge-attempts',type:'number',min:'0',max:'100',value:'5',inputmode:'numeric'});f2.appendChild(aIn);
    grid.appendChild(f1);grid.appendChild(f2);
    const f3=el('div',{class:'field full'});const sw=el('span',{class:'switch-row'});const vac=el('input',{id:'purge-vacuum',type:'checkbox',class:'switch'});sw.appendChild(vac);sw.appendChild(el('label',{for:'purge-vacuum'},'Vacuum database after purge'));f3.appendChild(sw);
    grid.appendChild(f3);
    form.appendChild(grid);
    const row=el('div',{class:'row'});
    const prev=el('button',{type:'button',class:'btn',id:'btn-purge-preview'},'Preview purge');row.appendChild(prev);
    const conf=el('button',{type:'button',class:'btn danger',id:'btn-purge-confirm',disabled:''},'Purge now');conf.disabled=true;row.appendChild(conf);
    form.appendChild(row);
    const hint=el('p',{class:'hint'},'Preview first. Purge removes dead Telegram references and stale failed outbox work; it never deletes healthy packages.');
    form.appendChild(hint);
    wrap.appendChild(form);
    form.addEventListener('input',function(){purgeReady=false;conf.disabled=true;setFeedback('purge-feedback','Options changed. Preview again before purging.');});
    prev.addEventListener('click',function(){purgePreview(false);});
    conf.addEventListener('click',function(){purgePreview(true);});
  }
}
async function purgePreview(confirm){
  const dEl=$('purge-days'),aEl=$('purge-attempts'),vEl=$('purge-vacuum');
  const days=dEl?Number(dEl.value):30,attempts=aEl?Number(aEl.value):5,vacuum=vEl?!!vEl.checked:false;
  if(!Number.isInteger(days)||days<1||days>3650||!Number.isInteger(attempts)||attempts<0||attempts>100){
    clearBox('purge-error');showError('purge-error','bad_purge_options');setFeedback('purge-feedback','');return;
  }
  const options=JSON.stringify([days,attempts,vacuum]);
  if(confirm&&(!purgeReady||previewOptions!==options)){setFeedback('purge-feedback','Preview these options before purging.');return;}
  const btns=[$('btn-purge-preview'),$('btn-purge-confirm'),dEl,aEl,vEl].filter(Boolean);
  await runBusy(btns,async function(){
    clearBox('purge-error');setFeedback('purge-feedback','Working…');
    try{
      const x=await adminRequest('/admin/pool/purge',{method:'POST',headers:Object.assign({'Content-Type':'application/json'},H),body:JSON.stringify({dry_run:!confirm,outbox_older_than_days:days,outbox_attempts:attempts,vacuum:vacuum})});
      if(!x.response.ok)throw failure('load',isRecord(x.data)&&x.data.error?x.data.error:'request_failed');
      if(!isRecord(x.data))throw failure('malformed','malformed');
      const r=x.data;
      const result=$('purge-result');result.textContent='';
      const dl=el('dl',{class:'kv'});
      [['Dry run',r.dry_run?'yes (preview only)':'no (purged)'],['Pool tiles',String(r.pool_tiles==null?'–':r.pool_tiles)],['Pool packages',String(r.pool_packages==null?'–':r.pool_packages)],['Episode packages',String(r.pool_episode_packages==null?'–':r.pool_episode_packages)],['Hints',String(r.pool_hints==null?'–':r.pool_hints)],['Outbox',String(r.pool_outbox==null?'–':r.pool_outbox)],['Uploads',String(r.pool_uploads==null?'–':r.pool_uploads)],['Publication queue',String(r.pool_publication_queue==null?'–':r.pool_publication_queue)],['Telegram deletes attempted',String(r.telegram_messages_delete_attempted==null?'–':r.telegram_messages_delete_attempted)],['Database before',fmtBytes(r.database_bytes_before)],['Database after',fmtBytes(r.database_bytes_after)],['Vacuumed',r.vacuumed?'yes':'no']].forEach(function(kv){dl.appendChild(el('dt',{},kv[0]));dl.appendChild(el('dd',{},kv[1]));});
      result.appendChild(dl);
      if(confirm){purgeReady=false;setFeedback('purge-feedback','Purge complete. Counts above are the server report.');const c=$('btn-purge-confirm');if(c)c.disabled=true;}
      else{purgeReady=true;previewOptions=options;setFeedback('purge-feedback','Preview complete. Nothing was deleted. Review the counts, then press “Purge now” to confirm.');const c=$('btn-purge-confirm');if(c)c.disabled=false;}
    }catch(e){
      if(e&&e.kind==='auth'){showGate(true);return;}
      showError('purge-error',e&&e.code?e.code:'request_failed');setFeedback('purge-feedback','');
    }
  });
}
function updateDiagnostics(st,sh,pool,gen){
  kvClear('diag-channel');
  kvAdd('diag-channel','Pool mode',String(pool.mode||'local'));
  if(pool.channel_id)kvAdd('diag-channel','Channel',String(pool.channel_id));
  if(pool.api_url)kvAdd('diag-channel','Host API URL',String(pool.api_url));
  kvAdd('diag-channel','Librarian',String(pool.librarian_status||'–'));
  kvAdd('diag-channel','Contribution path',String(pool.contribution_path||'–'));
  kvAdd('diag-channel','Last scan (message)',String(pool.last_message_id==null?'–':pool.last_message_id));
  kvAdd('diag-channel','Last error',pool.last_error?humanError(pool.last_error):'none');
  kvClear('diag-queue');
  kvAdd('diag-queue','Pending uploads (outbox)',String(pool.outbox==null?'–':pool.outbox));
  kvAdd('diag-queue','Active sources',String(st.active_sources==null?'–':st.active_sources));
  const counts={};(gen.jobs||[]).forEach(function(j){counts[j.state||'unknown']=(counts[j.state||'unknown']||0)+1;});
  kvAdd('diag-queue','Generation jobs',Object.keys(counts).length?Object.keys(counts).map(function(k){return k+' '+counts[k];}).join(' · '):'none');
  kvAdd('diag-queue','Cache','entries '+st.cache.entries+' · tiles '+st.cache.tiles);
  kvClear('diag-public');
  const pub=sh.public||{};
  kvAdd('diag-public','Public listener',pub.listening?('listening on port '+pub.port):'not listening');
  kvAdd('diag-public','Host',String(pub.host||'–'));
  kvAdd('diag-public','Blocked addresses',String(pub.blocked==null?'–':pub.blocked));
  kvAdd('diag-public','Rate-limited requests',String(pub.refused_total==null?'–':pub.refused_total));
  kvAdd('diag-public','Tracked addresses',String(pub.tracked==null?'–':pub.tracked));

}
function updateContributors(data,unsupported){
  const wrap=$('contrib-wrap'),note=$('contrib-unsupported'),empty=$('contrib-empty');
  if(unsupported||!data||data.supported===false){
    if(wrap)wrap.hidden=true;if(note)note.hidden=false;if(empty)empty.hidden=true;
    return;
  }
  if(wrap)wrap.hidden=false;if(note)note.hidden=true;
  $('contrib-prev').disabled=contributorOffset===0;
  $('contrib-next').disabled=!(data.has_more_quotas||data.has_more_blocked);
  setText('contrib-page','Page '+(Math.floor(contributorOffset/CONTRIBUTOR_LIMIT)+1)+' · up to '+CONTRIBUTOR_LIMIT+' activity records and blocks per page');
  const qb=$('contrib-quotas-body');qb.textContent='';
  (data.quotas||[]).forEach(function(q){
    const tr=el('tr');
    tr.appendChild(el('td',{'data-label':'Sender ID'},String(q.sender_id==null?'–':q.sender_id)));
    tr.appendChild(el('td',{'data-label':'Work key'},String(q.work_key||'–')));
    tr.appendChild(el('td',{'data-label':'Day'},String(q.day||'–')));
    tr.appendChild(el('td',{'data-label':'Count',class:'num'},String(q.count==null?'–':q.count)));
    qb.appendChild(tr);
  });
  const bb=$('contrib-blocked-body');bb.textContent='';
  (data.blocked||[]).forEach(function(b){
    const tr=el('tr');
    tr.appendChild(el('td',{'data-label':'Blocked sender ID'},String(b.sender_id==null?'–':b.sender_id)));
    const created=b.created;
    tr.appendChild(el('td',{'data-label':'Blocked at'},Number.isFinite(created)?fmtAgo(created):String(created||'–')));
    bb.appendChild(tr);
  });
  const has=(data.quotas||[]).length>0||(data.blocked||[]).length>0;
  if(empty)empty.hidden=has;
}

/* ---- library ---- */
function clearThumbCache(){
  thumbCache.forEach(function(url){try{URL.revokeObjectURL(url);}catch(_){}});
  thumbCache.clear();
}
async function loadLibrary(){
  const grid=$('lib-grid'),errBox=$('lib-error'),empty=$('lib-empty'),unsup=$('lib-unsupported'),feed=$('lib-feedback');
  clearBox('lib-error');setFeedback('lib-feedback','Loading…');
  grid.setAttribute('aria-busy','true');
  const btns=[$('lib-refresh'),$('lib-prev'),$('lib-next')].filter(Boolean);
  try{
    await runBusy(btns,async function(){
      const x=await adminRequest('/admin/library?limit='+LIB_LIMIT+'&offset='+libOffset,{headers:H});
      if(x.response.status===404){libSupported=false;libItems=[];libHasMore=false;renderLibrary();return;}
      if(!x.response.ok)throw failure('load',isRecord(x.data)&&x.data.error?x.data.error:'request_failed');
      if(!isRecord(x.data)||typeof x.data.supported!=='boolean'||!Array.isArray(x.data.items)||typeof x.data.has_more!=='boolean')throw failure('malformed','malformed');
      if(!x.data.supported){libSupported=false;libItems=[];libHasMore=false;renderLibrary();return;}
      libSupported=true;libItems=x.data.items;libHasMore=x.data.has_more;
      renderLibrary();
    });
    setFeedback('lib-feedback','');
  }catch(e){
    if(e&&e.kind==='auth'){showGate(true);return;}
    if(e&&e.code==='not_found'){libSupported=false;renderLibrary();return;}
    grid.textContent='';libItems=[];libSupported=libSupported===false?false:null;
    showError('lib-error',e&&e.code?e.code:'request_failed');
    errBox.hidden=false;
    const retry=el('button',{type:'button',class:'btn small'},'Retry');
    retry.addEventListener('click',function(){loadLibrary();});
    errBox.appendChild(el('div'));errBox.lastChild.appendChild(retry);
    renderLibraryMeta();
    setFeedback('lib-feedback','');
  }finally{grid.setAttribute('aria-busy','false');}
}
function renderLibrary(){
  const grid=$('lib-grid');
  const focusKey=captureFocus(grid);
  grid.textContent='';
  clearThumbCache();
  const q=libQuery.trim().toLowerCase();
  const items=q?libItems.filter(function(it){const hay=((it.filename||'')+' '+(it.imdb||'')+' '+(it.fp||'')+' '+(it.profile||'')).toLowerCase();return hay.indexOf(q)!==-1;}):libItems;
  $('lib-unsupported').hidden=libSupported!==false;
  $('lib-empty').hidden=libSupported!==true||items.length>0;
  if(libSupported&&q&&items.length===0)$('lib-empty').hidden=false,$('lib-empty').textContent='Nothing on this page matches “'+libQuery+'”.';
  else if(libSupported&&!q)$('lib-empty').textContent='No packages in the pool yet. Generate one from Live activity and it will appear here.';
  items.forEach(function(it){
    const li=el('li',{class:'lib-card'});
    const thumb=el('div',{class:'lib-thumb'});
    const img=el('img',{alt:''});
    img.style.display='none';
    thumb.appendChild(img);
    thumb.appendChild(el('div',{class:'fallback'},it.imdb?'':(it.filename?it.filename.slice(0,40):'No preview')));
    if(it.thumbnail_available&&typeof it.fp==='string'&&typeof it.profile==='string')fetchThumb(it,img,thumb);
    li.appendChild(thumb);
    const title=it.filename||((it.imdb?String(it.imdb):'')+((it.season||it.episode)?(' S'+(it.season||0)+' E'+(it.episode||0)):'')||'Unidentified');
    li.appendChild(el('p',{class:'lib-title'},title||'Unidentified'));
    let sub=fmtDuration(it.duration_ms)+' · '+(Number.isFinite(it.n_tiles)?it.n_tiles+' tiles':'tiles unknown');
    if(it.created)sub+=' · '+fmtAgo(it.created);
    li.appendChild(el('p',{class:'lib-sub'},sub));
    const chips=el('div',{class:'lib-chips'});
    if(it.imdb)chips.appendChild(el('span',{class:'chip'},String(it.imdb)));
    if(it.season||it.episode)chips.appendChild(el('span',{class:'chip'},'S'+(it.season||0)+' E'+(it.episode||0)));
    if(it.profile)chips.appendChild(el('span',{class:'chip'},String(it.profile)));
    if(typeof it.fp==='string'&&it.fp)chips.appendChild(el('span',{class:'chip'},it.fp.slice(0,8)));
    li.appendChild(chips);
    grid.appendChild(li);
  });
  renderLibraryMeta();
  restoreFocus(grid,focusKey);
}
function renderLibraryMeta(){
  const pageNum=Math.floor(libOffset/LIB_LIMIT)+1;
  setText('lib-page','Page '+pageNum);
  $('lib-prev').disabled=libOffset<=0;
  $('lib-next').disabled=!libHasMore;
  const q=libQuery.trim();
  const shown=q?libItems.filter(function(it){const hay=((it.filename||'')+' '+(it.imdb||'')+' '+(it.fp||'')+' '+(it.profile||'')).toLowerCase();return hay.indexOf(q.toLowerCase())!==-1;}).length:libItems.length;
  setText('lib-count',libSupported?'Showing '+shown+(q?' of '+libItems.length:'')+' on page '+pageNum:'');
}
function fetchThumb(it,img,thumb){
  thumbnailQueue=thumbnailQueue.then(function(){return fetchThumbnail(it,img,thumb);});
}
async function fetchThumbnail(it,img,thumb){
  if(!thumb.isConnected)return;
  const key=it.fp+'|'+it.profile;
  if(thumbCache.has(key)){img.src=thumbCache.get(key);img.style.display='block';const fb=thumb.querySelector('.fallback');if(fb)fb.style.display='none';return;}
  try{
    const path='/admin/library/'+encodeURIComponent(it.fp)+'/thumbnail?profile='+encodeURIComponent(it.profile);
    let response;
    for(let attempt=0;attempt<3;attempt++){
      if(!thumb.isConnected)return;
      response=await fetch(path,{headers:H,referrerPolicy:'no-referrer'});
      if(response.status!==503)break;
      await new Promise(function(resolve){setTimeout(resolve,1000);});
    }
    if(!response.ok)return;
    const blob=await response.blob();
    if(!blob||blob.type!=='image/jpeg')return;
    const url=URL.createObjectURL(blob);
    if(document.body.contains(thumb)){thumbCache.set(key,url);img.src=url;img.style.display='block';const fb=thumb.querySelector('.fallback');if(fb)fb.style.display='none';}
    else{try{URL.revokeObjectURL(url);}catch(_){}}
  }catch(_){/* thumbnails are optional; keep fallback */}
}

/* ---- actions ---- */
function armConfirm(btn,label){
  if(btn.dataset.armed==='1'){btn.dataset.armed='';btn.textContent=label;btn.classList.remove('armed');return true;}
  btn.dataset.armed='1';btn.textContent='Confirm? Click again';btn.classList.add('armed');
  setTimeout(function(){if(btn.dataset.armed==='1'){btn.dataset.armed='';btn.textContent=label;btn.classList.remove('armed');}},5000);
  /* armed state only; the caller reports the actual result */
  return false;
}
async function revokeDevice(d,btn){
  await runBusy([btn],async function(){
    clearBox('devices-error');setFeedback('devices-feedback','Revoking…');
    try{
      const x=await adminRequest('/admin/devices/'+encodeURIComponent(d.device_id),{method:'DELETE',headers:H});
      if(!x.response.ok)throw failure('load',isRecord(x.data)&&x.data.error?x.data.error:'request_failed');
      setFeedback('devices-feedback','Device revoked.');
      await poll();
    }catch(e){if(e&&e.kind==='auth'){showGate(true);return;}showError('devices-error',e&&e.code?e.code:'request_failed');setFeedback('devices-feedback','');}
  });
}
async function setContributor(d,btn){
  const next=!d.contribute;
  await runBusy([btn],async function(){
    clearBox('devices-error');setFeedback('devices-feedback','Saving…');
    try{
      const x=await adminRequest('/admin/devices/'+encodeURIComponent(d.device_id)+'/permissions',{method:'PUT',headers:Object.assign({'Content-Type':'application/json'},H),body:JSON.stringify({contribute:next})});
      if(!x.response.ok)throw failure('load',isRecord(x.data)&&x.data.error?x.data.error:'request_failed');
      if(!isRecord(x.data)||x.data.contribute!==next)throw failure('malformed','malformed');
      setFeedback('devices-feedback',next?'Contributor permission granted.':'Contributor permission removed.');
      await poll();
    }catch(e){if(e&&e.kind==='auth'){showGate(true);return;}showError('devices-error',e&&e.code?e.code:'request_failed');setFeedback('devices-feedback','');}
  });
}
async function revokeInvite(inv,btn){
  await runBusy([btn],async function(){
    clearBox('invite-error');setFeedback('invite-feedback','Revoking…');
    try{
      const x=await adminRequest('/admin/invites/'+encodeURIComponent(inv.invite_id),{method:'DELETE',headers:H});
      if(!x.response.ok)throw failure('load',isRecord(x.data)&&x.data.error?x.data.error:'request_failed');
      setFeedback('invite-feedback','Invitation revoked.');
      await poll();
    }catch(e){if(e&&e.kind==='auth'){showGate(true);return;}showError('invite-error',e&&e.code?e.code:'request_failed');setFeedback('invite-feedback','');}
  });
}
async function cancelGeneration(j,btn){
  await runBusy([btn],async function(){
    clearBox('jobs-error');setFeedback('jobs-feedback','Cancelling…');
    try{
      const x=await adminRequest('/admin/generations/'+encodeURIComponent(j.generation_id),{method:'DELETE',headers:H});
      if(!x.response.ok)throw failure('load',isRecord(x.data)&&x.data.error?x.data.error:'request_failed');
      setFeedback('jobs-feedback','Cancellation requested.');
      await poll();
    }catch(e){if(e&&e.kind==='auth'){showGate(true);return;}showError('jobs-error',e&&e.code?e.code:'request_failed');setFeedback('jobs-feedback','');}
  });
}

/* ---- wiring ---- */
function wire(){
  $('contrib-prev').addEventListener('click',function(){contributorOffset=Math.max(0,contributorOffset-CONTRIBUTOR_LIMIT);poll();});
  $('contrib-next').addEventListener('click',function(){contributorOffset+=CONTRIBUTOR_LIMIT;poll();});
  document.querySelectorAll('nav.primary button[data-panel]').forEach(function(b){b.addEventListener('click',function(){showPanel(b.dataset.panel);});});
  $('dash-generate').addEventListener('click',function(){showPanel('activity');const s=$('gen-source');if(s)s.focus();});
  $('dash-connect').addEventListener('click',function(){showPanel('sharing');const b=$('btn-pair-open');if(b)b.focus();});
  $('dash-open-activity').addEventListener('click',function(){showPanel('activity');});
  $('lib-refresh').addEventListener('click',function(){loadLibrary();});
  $('lib-prev').addEventListener('click',function(){if(libOffset>0){libOffset=Math.max(0,libOffset-LIB_LIMIT);loadLibrary();}});
  $('lib-next').addEventListener('click',function(){if(libHasMore){libOffset+=LIB_LIMIT;loadLibrary();}});
  $('lib-search').addEventListener('input',function(){libQuery=this.value;renderLibrary();});
  $('gen-form').addEventListener('submit',async function(ev){
    ev.preventDefault();
    clearBox('gen-error');setFeedback('gen-feedback','');
    const source=$('gen-source').value.trim();
    if(!source){showError('gen-error','bad_request');setFeedback('gen-feedback','Enter a media URL.');return;}
    const imdbRaw=$('gen-imdb').value.trim();
    const seasonRaw=$('gen-season').value.trim();
    const episodeRaw=$('gen-episode').value.trim();
    const filenameRaw=$('gen-filename').value.trim();
    const payload={source_url:source,profile:$('gen-profile').value,imdb:imdbRaw?imdbRaw:null,season:seasonRaw?Number(seasonRaw):0,episode:episodeRaw?Number(episodeRaw):0,filename:filenameRaw?filenameRaw:null};
    await runBusy([$('gen-submit')],async function(){
      setFeedback('gen-feedback','Starting generation…');
      try{
        const x=await adminRequest('/admin/generations',{method:'POST',headers:Object.assign({'Content-Type':'application/json'},H),body:JSON.stringify(payload)});
        if(!x.response.ok)throw failure('load',isRecord(x.data)&&x.data.error?x.data.error:'request_failed');
        if(!isRecord(x.data)||typeof x.data.generation_id!=='string')throw failure('malformed','malformed');
        setFeedback('gen-feedback','Generation started. It will appear in Jobs below with its progress.');
        await poll();
      }catch(e){if(e&&e.kind==='auth'){showGate(true);return;}showError('gen-error',e&&e.code?e.code:'request_failed');setFeedback('gen-feedback','');}
    });
  });
  $('sharing-enabled').addEventListener('change',async function(){
    const on=this.checked,box=this;
    await runBusy([box],async function(){
      clearBox('sharing-error');setFeedback('sharing-feedback','Saving…');
      try{
        const x=await adminRequest('/admin/sharing',{method:'PUT',headers:Object.assign({'Content-Type':'application/json'},H),body:JSON.stringify({enabled:on})});
        if(!x.response.ok)throw failure('load',isRecord(x.data)&&x.data.error?x.data.error:'request_failed');
        if(!isRecord(x.data)||x.data.enabled!==on)throw failure('malformed','malformed');
        setFeedback('sharing-feedback',on?'Library sharing is on.':'Library sharing is off. Existing devices stay paired but new guests cannot join.');
        await poll();
      }catch(e){if(e&&e.kind==='auth'){showGate(true);return;}showError('sharing-error',e&&e.code?e.code:'request_failed');setFeedback('sharing-feedback','');}
    });
  });
  $('sharing-contrib').addEventListener('change',async function(){
    const on=this.checked,box=this;
    await runBusy([box],async function(){
      clearBox('sharing-error');setFeedback('sharing-feedback','Saving…');
      try{
        const x=await adminRequest('/admin/pool/contributions',{method:'PUT',headers:Object.assign({'Content-Type':'application/json'},H),body:JSON.stringify({enabled:on})});
        if(!x.response.ok)throw failure('load',isRecord(x.data)&&x.data.error?x.data.error:'request_failed');
        if(!isRecord(x.data)||x.data.enabled!==on)throw failure('malformed','malformed');
        setFeedback('sharing-feedback',on?'Guest uploads are on. Authorised guests can contribute.':'Guest uploads are off.');
        await poll();
      }catch(e){if(e&&e.kind==='auth'){showGate(true);return;}showError('sharing-error',e&&e.code?e.code:'request_failed');setFeedback('sharing-feedback','');}
    });
  });
  $('btn-new-invite').addEventListener('click',function(){$('invite-card').scrollIntoView({behavior:'smooth',block:'start'});$('invite-label').focus();});
  $('btn-pair-open').addEventListener('click',async function(){
    await runBusy([$('btn-pair-open')],async function(){
      clearBox('pairing-error');setFeedback('pairing-feedback','Opening pairing…');
      try{
        const x=await adminRequest('/admin/pair',{method:'POST',headers:H});
        if(!x.response.ok)throw failure('load',isRecord(x.data)&&x.data.error?x.data.error:'request_failed');
        if(!isRecord(x.data)||typeof x.data.code!=='string'||typeof x.data.spki_pin!=='string')throw failure('malformed','malformed');
        lastPairCode=x.data.code;
        lastPairJoin=typeof x.data.join==='string'?x.data.join:'';
        lastPairing='Nuvio code: '+lastPairCode+' | TLS pin: '+x.data.spki_pin+(lastPairJoin?' | Join link: '+lastPairJoin:'');
        try{sessionStorage.setItem(PAIR_KEY,lastPairing);}catch(_){}
        setFeedback('pairing-feedback','Pairing window is open. Enter the code on the device.');
        await poll();
      }catch(e){if(e&&e.kind==='auth'){showGate(true);return;}showError('pairing-error',e&&e.code?e.code:'request_failed');setFeedback('pairing-feedback','');}
    });
  });
  $('btn-pair-close').addEventListener('click',async function(){
    await runBusy([$('btn-pair-close')],async function(){
      clearBox('pairing-error');setFeedback('pairing-feedback','Closing…');
      try{
        const x=await adminRequest('/admin/pair',{method:'DELETE',headers:H});
        if(!x.response.ok)throw failure('load',isRecord(x.data)&&x.data.error?x.data.error:'request_failed');
        try{sessionStorage.removeItem(PAIR_KEY);}catch(_){}
        lastPairing='';lastPairCode='';lastPairJoin='';
        setFeedback('pairing-feedback','Pairing is closed.');
        await poll();
      }catch(e){if(e&&e.kind==='auth'){showGate(true);return;}showError('pairing-error',e&&e.code?e.code:'request_failed');setFeedback('pairing-feedback','');}
    });
  });
  $('btn-copy-pair-code').addEventListener('click',async function(){await copyPairText(lastPairCode,'pairing-feedback');});
  $('btn-copy-pair-join').addEventListener('click',async function(){await copyPairText(lastPairJoin,'pairing-feedback');});
  $('invite-form').addEventListener('submit',async function(ev){
    ev.preventDefault();
    clearBox('invite-error');setFeedback('invite-feedback','');
    const label=$('invite-label').value.slice(0,64);
    const ttl=Number($('invite-ttl').value);
    const daysRaw=$('invite-device-days').value.trim();
    const deviceDays=daysRaw?Number(daysRaw):null;
    const readOnly=$('invite-readonly').checked;
    const pub=$('invite-public').checked;
    if(pub&&$('invite-public').disabled){showError('invite-error','public_endpoint_not_configured');return;}
    const payload={label:label,ttl_s:ttl,device_days:deviceDays,public:pub,read_only:readOnly};
    await runBusy([$('invite-submit')],async function(){
      setFeedback('invite-feedback','Creating invitation…');
      try{
        const x=await adminRequest('/admin/invites',{method:'POST',headers:Object.assign({'Content-Type':'application/json'},H),body:JSON.stringify(payload)});
        if(!x.response.ok)throw failure('load',isRecord(x.data)&&x.data.error?x.data.error:'request_failed');
        if(!isRecord(x.data)||typeof x.data.address!=='string'||typeof x.data.code!=='string'||typeof x.data.join!=='string'||!Number.isFinite(x.data.expires_in_s))throw failure('malformed','malformed');
        lastInvite=x.data;
        $('invite-result').hidden=false;
        setText('invite-code',x.data.code);
        setText('invite-address',x.data.address);
        setText('invite-expires',fmtExpiresIn(x.data.expires_in_s));
        setText('invite-join',x.data.join);
        setFeedback('invite-feedback','Invitation created. Copy the code or join link now; the code will not be shown again.');
        await poll();
      }catch(e){if(e&&e.kind==='auth'){showGate(true);return;}showError('invite-error',e&&e.code?e.code:'request_failed');setFeedback('invite-feedback','');}
    });
  });
  $('btn-copy-invite-code').addEventListener('click',async function(){await copyPairText(lastInvite?lastInvite.code:'','invite-feedback');});
  $('btn-copy-invite-join').addEventListener('click',async function(){await copyPairText(lastInvite?lastInvite.join:'','invite-feedback');});
  $('setup-form').addEventListener('submit',async function(ev){
    ev.preventDefault();
    clearBox('setup-error');setFeedback('setup-feedback','');
    const d={telegram_channel_id:$('setup-channel').value.trim(),telegram_api_id:Number($('setup-api-id').value),telegram_api_hash:$('setup-api-hash').value.trim(),telegram_bot_token:$('setup-bot-token').value.trim()||null,telegram_session:$('setup-session').value.trim()||null};
    const hasBot=!!d.telegram_bot_token,hasSession=!!d.telegram_session;
    if(!d.telegram_channel_id||!(d.telegram_api_id>0)||!d.telegram_api_hash||hasBot===hasSession){
      showError('setup-error','bad_telegram_settings');setFeedback('setup-feedback','Provide the channel, API ID/hash, and exactly one of bot token or StringSession.');return;
    }
    await runBusy([$('setup-submit')],async function(){
      setFeedback('setup-feedback','Verifying channel…');
      try{
        const x=await adminRequest('/admin/setup',{method:'POST',headers:Object.assign({'Content-Type':'application/json'},H),body:JSON.stringify(d)});
        if(!x.response.ok){showError('setup-error',isRecord(x.data)&&x.data.error?x.data.error:'setup_failed');setFeedback('setup-feedback','');return;}
        if(!isRecord(x.data)||!isRecord(x.data.initial_scan)||!Number.isFinite(x.data.initial_scan.indexed)||typeof x.data.channel_writable!=='boolean')throw failure('malformed','malformed');
        const msg='Telegram history is readable. Initial scan indexed '+x.data.initial_scan.indexed+' tiles; '+(x.data.channel_writable?'channel posting is available.':'this channel is read-only, so uploads are disabled. Grant the bot Post Messages permission to enable uploads.');
        setFeedback('setup-feedback',msg);
        $('setup-form').reset();
        ['setup-api-hash','setup-bot-token','setup-session'].forEach(function(id){const n=$(id);if(n)n.value='';});
        await poll();
      }catch(e){if(e&&e.kind==='auth'){showGate(true);return;}showError('setup-error',e&&e.code?e.code:'setup_failed');setFeedback('setup-feedback','');}
    });
  });
  $('btn-scan').addEventListener('click',async function(){
    await runBusy([$('btn-scan')],async function(){
      clearBox('channel-error');setFeedback('channel-feedback','Scanning…');
      try{
        const x=await adminRequest('/admin/pool/scan',{method:'POST',headers:H});
        if(!x.response.ok)throw failure('load',isRecord(x.data)&&x.data.error?x.data.error:'request_failed');
        if(!isRecord(x.data)||!Number.isFinite(x.data.scanned)||!Number.isFinite(x.data.indexed))throw failure('malformed','malformed');
        setFeedback('channel-feedback','Scan complete: scanned '+x.data.scanned+', indexed '+x.data.indexed+', last message '+x.data.last_message_id+'.');
        await poll();
      }catch(e){if(e&&e.kind==='auth'){showGate(true);return;}showError('channel-error',e&&e.code?e.code:'request_failed');setFeedback('channel-feedback','');}
    });
  });
  $('rebuild-confirm').addEventListener('change',function(){$('btn-rebuild').disabled=!this.checked;});
  $('btn-rebuild').addEventListener('click',async function(){
    if(!$('rebuild-confirm').checked)return;
    await runBusy([$('btn-rebuild'),$('rebuild-confirm')],async function(){
      clearBox('channel-error');setFeedback('channel-feedback','Rebuilding…');
      try{
        const x=await adminRequest('/admin/pool/rebuild',{method:'POST',headers:H});
        if(!x.response.ok)throw failure('load',isRecord(x.data)&&x.data.error?x.data.error:'request_failed');
        if(!isRecord(x.data)||!Number.isFinite(x.data.scanned)||!Number.isFinite(x.data.indexed))throw failure('malformed','malformed');
        $('rebuild-confirm').checked=false;$('btn-rebuild').disabled=true;
        setFeedback('channel-feedback','Rebuild complete: scanned '+x.data.scanned+', indexed '+x.data.indexed+'.');
        await poll();
      }catch(e){if(e&&e.kind==='auth'){showGate(true);return;}showError('channel-error',e&&e.code?e.code:'request_failed');setFeedback('channel-feedback','');}
    });
  });
  $('clear-confirm').addEventListener('change',function(){$('btn-clear-blocks').disabled=!this.checked;});
  $('btn-clear-blocks').addEventListener('click',async function(){
    if(!$('clear-confirm').checked)return;
    await runBusy([$('btn-clear-blocks'),$('clear-confirm')],async function(){
      clearBox('public-error');setFeedback('public-feedback','Clearing…');
      try{
        const x=await adminRequest('/admin/public/blocks',{method:'DELETE',headers:H});
        if(!x.response.ok)throw failure('load',isRecord(x.data)&&x.data.error?x.data.error:'request_failed');
        $('clear-confirm').checked=false;$('btn-clear-blocks').disabled=true;
        setFeedback('public-feedback','Blocks cleared. Every public address can request again within its rate budget.');
        await poll();
      }catch(e){if(e&&e.kind==='auth'){showGate(true);return;}showError('public-error',e&&e.code?e.code:'request_failed');setFeedback('public-feedback','');}
    });
  });
  async function contribAction(block){
    const raw=$('contrib-sender').value.trim();
    if(!/^[0-9]+$/.test(raw)){clearBox('contrib-error');showError('contrib-error','bad_request');setFeedback('contrib-feedback','Enter a numeric sender ID.');return;}
    await runBusy([$('btn-contrib-block'),$('btn-contrib-unblock')],async function(){
      clearBox('contrib-error');setFeedback('contrib-feedback','Saving…');
      try{
        const x=await adminRequest('/admin/contributors/'+encodeURIComponent(raw)+'/block',{method:block?'PUT':'DELETE',headers:H});
        if(!x.response.ok)throw failure('load',isRecord(x.data)&&x.data.error?x.data.error:'request_failed');
        contributorOffset=0;
        setFeedback('contrib-feedback',block?'Sender '+raw+' is blocked.':'Sender '+raw+' is unblocked.');
        await poll();
      }catch(e){if(e&&e.kind==='auth'){showGate(true);return;}showError('contrib-error',e&&e.code?e.code:'request_failed');setFeedback('contrib-feedback','');}
    });
  }
  $('btn-contrib-block').addEventListener('click',function(){contribAction(true);});
  $('btn-contrib-unblock').addEventListener('click',function(){contribAction(false);});
}
async function copyPairText(text,feedbackId){
  if(!text){setFeedback(feedbackId,'Nothing to copy yet.');return;}
  try{await navigator.clipboard.writeText(text);setFeedback(feedbackId,'Copied.');}
  catch(_){
    const ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy');setFeedback(feedbackId,'Copied.');}catch(__){setFeedback(feedbackId,'Copy failed. Select the text and copy it manually.');}
    ta.remove();
  }
}
wire();
showPanel('dashboard');
poll();
setInterval(poll,5000);
</script></body></html>"""
