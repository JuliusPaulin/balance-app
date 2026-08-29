import pathlib
D = pathlib.Path(__file__).parent
part = lambda n: (D / f"_{n}.part").read_text()

def build(out, title, variant, css, header, script):
    head = part("head").replace("TITLE_HERE", title)
    header = header.replace("__CONTROLS__", part("controls"))
    tail = part("tail").replace("VARIANT", variant)
    (D / out).write_text(
        f"{head}<style>\n{css}\n</style>\n</head>\n<body>\n"
        f"{part('switch')}{part('nav')}"
        f'<main class="main">\n<div class="page active" id="page-dashboard">\n'
        f"{header}\n"
        f'<div id="dash-body"></div>\n</div>\n</main>\n'
        f"<script>\n{script}\n</script>\n{tail}"
    )

# ── A ───────────────────────────────────────────────────────────────
build("a-sticky-header.html", "A · Sticky header", "a", """
/* A — the whole page header sticks: title and controls travel together.
   The most conventional, most Apple-like answer. Nothing new to learn,
   but it holds ~62px of vertical space for as long as you scroll. */
.page-header.sticky-header {
    position: sticky;
    top: 0;
    z-index: 60;
    margin: -28px -32px 20px;
    padding: 22px 32px 14px;
    background: color-mix(in srgb, var(--bg-secondary) 78%, transparent);
    -webkit-backdrop-filter: blur(20px) saturate(180%);
    backdrop-filter: blur(20px) saturate(180%);
    border-bottom: 0.5px solid transparent;
    transition: border-color var(--transition), padding var(--transition);
}
.page-header.sticky-header.stuck {
    border-bottom-color: var(--separator);
    padding-top: 13px;
    padding-bottom: 11px;
}
.page-header.sticky-header.stuck .page-title { font-size: var(--text-title3); }
.page-title { transition: font-size var(--transition); }
.header-controls { display: flex; align-items: center; gap: 8px; }
""", """<div class="page-header sticky-header" id="dash-header">
    <h1 class="page-title">Dashboard</h1>
    <div class="header-controls">
__CONTROLS__
    </div>
</div>""", """const h = document.getElementById("dash-header");
addEventListener("scroll", () => h.classList.toggle("stuck", scrollY > 4), { passive: true });""")

# ── B ───────────────────────────────────────────────────────────────
build("b-compact-bar.html", "B · Compact bar", "b", """
/* B — the big title scrolls away and a slim bar takes over. Costs ~44px
   instead of ~62px, and the "Dashboard" label only fades in once the real
   title has gone, so nothing is said twice. */
.page-header { margin-bottom: 12px; }

.control-bar {
    position: sticky;
    top: 0;
    z-index: 60;
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0 -32px 20px;
    padding: 8px 32px;
    background: color-mix(in srgb, var(--bg-secondary) 78%, transparent);
    -webkit-backdrop-filter: blur(20px) saturate(180%);
    backdrop-filter: blur(20px) saturate(180%);
    border-bottom: 0.5px solid transparent;
    transition: border-color var(--transition);
}
.control-bar.stuck { border-bottom-color: var(--separator); }

/* Compact echo of the title — only once the real one is off screen */
.control-bar-title {
    font-size: var(--text-headline);
    font-weight: 700;
    letter-spacing: -0.2px;
    opacity: 0;
    transform: translateX(-4px);
    transition: opacity var(--transition), transform var(--transition);
}
.control-bar.stuck .control-bar-title { opacity: 1; transform: none; }

/* Controls sit right; the spacer pushes them there */
.control-bar-spacer { flex: 1; }
""", """<div class="page-header">
    <h1 class="page-title">Dashboard</h1>
</div>
<div class="control-bar" id="control-bar">
    <span class="control-bar-title">Dashboard</span>
    <span class="control-bar-spacer"></span>
__CONTROLS__
</div>""", """const bar = document.getElementById("control-bar");
const barTop = bar.offsetTop;   /* NB: not `top` — that collides with window.top */
addEventListener("scroll", () => bar.classList.toggle("stuck", scrollY >= barTop), { passive: true });""")

# ── C ───────────────────────────────────────────────────────────────
build("c-floating-pill.html", "C · Floating pill", "c", """
/* C — the header scrolls away for real, and once it is gone the controls
   re-form as a floating pill over the content. Costs no permanent space
   and keeps the page edge clean; the trade is that something moves.
   The controls are MOVED, not copied, so there is only ever one of them. */
.float-dock {
    position: fixed;
    top: 14px;
    left: var(--sidebar-width);
    right: 0;
    z-index: 60;
    display: flex;
    justify-content: center;
    pointer-events: none;
}
.float-pill {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 10px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--bg) 88%, transparent);
    -webkit-backdrop-filter: blur(20px) saturate(180%);
    backdrop-filter: blur(20px) saturate(180%);
    border: 0.5px solid var(--separator);
    box-shadow: var(--shadow-lg);
    pointer-events: auto;
    opacity: 0;
    transform: translateY(-14px) scale(0.96);
    transition: opacity var(--transition-spring), transform var(--transition-spring);
}
.float-dock.shown .float-pill { opacity: 1; transform: none; }
.float-dock:not(.shown) .float-pill { pointer-events: none; }

/* The dropdown must open downward out of the pill */
.float-pill .period-dropdown { top: calc(100% + 8px); }
.header-controls { display: flex; align-items: center; gap: 8px; }
""", """<div class="float-dock" id="float-dock">
    <div class="float-pill" id="float-pill"></div>
</div>
<div class="page-header">
    <h1 class="page-title">Dashboard</h1>
    <div class="header-controls" id="header-controls">
__CONTROLS__
    </div>
</div>""", """/* Move the one set of controls between header and pill on scroll. */
const dock    = document.getElementById("float-dock");
const pill    = document.getElementById("float-pill");
const home    = document.getElementById("header-controls");
const nodes   = [...home.children];
let floating  = false;

addEventListener("scroll", () => {
    const want = scrollY > 110;
    if (want === floating) return;
    floating = want;
    nodes.forEach(n => (want ? pill : home).appendChild(n));
    dock.classList.toggle("shown", want);
}, { passive: true });""")
print("built:", *[p.name for p in sorted(D.glob("*.html"))])
