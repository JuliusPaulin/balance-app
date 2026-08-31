/* Ten colourways for Balance.
   Each one is a complete token set for BOTH themes — never give a colour its
   only value inside the dark block. Everything the app tints (accent-soft,
   the row hover, the heatmap ramp) is derived from the accent here, so a
   colourway can never leave a stray rgba() of the old accent behind. */

const PALETTES = {
  evergreen: {
    name: "Evergreen", note: "What ships today — the control.",
    light: { canvas:"#f6f7f9", surface:"#ffffff", tertiary:"#f0f2f5", sidebar:"#ffffff",
             p:"#0d1320", s:"#5e6678", t:"#9aa1b1", q:"#b6bcc9",
             accent:"#00a06b", accentHover:"#008f5f", onAccent:"#ffffff",
             green:"#00a06b", red:"#e0445b", sep:"#e6e9ee", sepOpaque:"#dce0e7", stripe:"#f6f7f9" },
    dark:  { canvas:"#0b0e14", surface:"#121722", tertiary:"#171d2a", sidebar:"#0c1019",
             p:"#e7ebf2", s:"#8a93a6", t:"#5d6677", q:"#454d5e",
             accent:"#00e599", accentHover:"#00c886", onAccent:"#04231a",
             green:"#00e599", red:"#ff5c72", sep:"rgba(255,255,255,0.08)", sepOpaque:"#222a38", stripe:"rgba(255,255,255,0.02)" },
  },

  nordic: {
    name: "Nordic Blue", note: "Cool grey-blue canvas, deep blue accent. The Nordic banking idiom.",
    light: { canvas:"#f1f4f9", surface:"#ffffff", tertiary:"#e8edf5", sidebar:"#ffffff",
             p:"#0f172a", s:"#55637a", t:"#94a1b5", q:"#b4bece",
             accent:"#1d4ed8", accentHover:"#1a45bd", onAccent:"#ffffff",
             green:"#0e8a5f", red:"#d84358", sep:"#e2e8f1", sepOpaque:"#d5deea", stripe:"#f5f8fc" },
    dark:  { canvas:"#080c14", surface:"#101827", tertiary:"#161f31", sidebar:"#0a0f1a",
             p:"#e8edf7", s:"#8c9ab2", t:"#5d6b83", q:"#465268",
             accent:"#5b8dff", accentHover:"#4278f0", onAccent:"#05132e",
             green:"#2fcf95", red:"#ff6478", sep:"rgba(255,255,255,0.08)", sepOpaque:"#1f2a3d", stripe:"rgba(255,255,255,0.02)" },
  },

  graphite: {
    name: "Graphite", note: "No brand hue at all. Green and red are left to mean income and overspend, nothing else.",
    light: { canvas:"#f4f4f3", surface:"#ffffff", tertiary:"#ebebe9", sidebar:"#fafaf9",
             p:"#16181a", s:"#5c6166", t:"#96999e", q:"#b8bbbf",
             accent:"#23262a", accentHover:"#101214", onAccent:"#ffffff",
             green:"#0e8a5f", red:"#c0392b", sep:"#e5e5e3", sepOpaque:"#d8d8d5", stripe:"#f6f6f5" },
    dark:  { canvas:"#0d0e0f", surface:"#17191b", tertiary:"#1e2124", sidebar:"#101112",
             p:"#eff0f1", s:"#9298a0", t:"#666c74", q:"#4b5158",
             accent:"#e9eaec", accentHover:"#ffffff", onAccent:"#16181a",
             green:"#2fcf95", red:"#ff6b5e", sep:"rgba(255,255,255,0.08)", sepOpaque:"#262a2e", stripe:"rgba(255,255,255,0.02)" },
  },

  sand: {
    name: "Sand & Olive", note: "Warm paper instead of white. Olive accent, softer on the eyes for long sessions.",
    light: { canvas:"#f4f1ea", surface:"#fffdf9", tertiary:"#ece7dc", sidebar:"#fbf8f2",
             p:"#1d1b16", s:"#635e52", t:"#9b9587", q:"#bcb6a8",
             accent:"#5e7a3c", accentHover:"#4d6631", onAccent:"#ffffff",
             green:"#2f7d55", red:"#b0452f", sep:"#e6e0d4", sepOpaque:"#dcd5c6", stripe:"#f7f4ed" },
    dark:  { canvas:"#15140f", surface:"#1e1c16", tertiary:"#26241c", sidebar:"#131209",
             p:"#f0ebdf", s:"#a49d8c", t:"#736d5f", q:"#544f44",
             accent:"#a8c07a", accentHover:"#b8ce8e", onAccent:"#1d1b16",
             green:"#6fd196", red:"#ff7a5e", sep:"rgba(255,255,255,0.08)", sepOpaque:"#2e2b22", stripe:"rgba(255,255,255,0.02)" },
  },

  terracotta: {
    name: "Terracotta", note: "Warm clay accent against a cool crimson, so income and spending never share a hue.",
    light: { canvas:"#f7f2ef", surface:"#ffffff", tertiary:"#efe6e1", sidebar:"#fffcfa",
             p:"#211a17", s:"#6a5c56", t:"#a3948d", q:"#c2b5af",
             accent:"#9c4a2a", accentHover:"#833d22", onAccent:"#ffffff",
             green:"#2f7d55", red:"#cf2649", sep:"#ebe1dc", sepOpaque:"#ded1cb", stripe:"#f9f4f1" },
    dark:  { canvas:"#14100e", surface:"#1e1815", tertiary:"#26201c", sidebar:"#120e0c",
             p:"#f2eae6", s:"#a8968e", t:"#776760", q:"#564a45",
             accent:"#e08a5f", accentHover:"#ef9d72", onAccent:"#1a0f0a",
             green:"#3fd398", red:"#ff5d86", sep:"rgba(255,255,255,0.08)", sepOpaque:"#2e2621", stripe:"rgba(255,255,255,0.02)" },
  },

  teal: {
    name: "Deep Teal", note: "Keeps the green family but pulls it colder and deeper — less mint, more slate-green.",
    light: { canvas:"#f0f5f5", surface:"#ffffff", tertiary:"#e4eeee", sidebar:"#ffffff",
             p:"#0d1b1b", s:"#54666a", t:"#93a5a8", q:"#b5c3c5",
             accent:"#0f766e", accentHover:"#0c5f59", onAccent:"#ffffff",
             green:"#0e8a5f", red:"#d1435b", sep:"#dfebeb", sepOpaque:"#d0e0e0", stripe:"#f5faf9" },
    dark:  { canvas:"#071110", surface:"#10201f", tertiary:"#162928", sidebar:"#091715",
             p:"#e4f2f0", s:"#87a09d", t:"#5c7370", q:"#455856",
             accent:"#2dd4bf", accentHover:"#5ce0d0", onAccent:"#06201d",
             green:"#34d399", red:"#ff6478", sep:"rgba(255,255,255,0.08)", sepOpaque:"#1c3230", stripe:"rgba(255,255,255,0.02)" },
  },

  plum: {
    name: "Plum", note: "The furthest from money-green. Nothing about the accent competes with income or overspend.",
    light: { canvas:"#f5f2f7", surface:"#ffffff", tertiary:"#ece5f0", sidebar:"#ffffff",
             p:"#1b1420", s:"#5f5568", t:"#9a90a3", q:"#bcb3c2",
             accent:"#7a3b96", accentHover:"#663080", onAccent:"#ffffff",
             green:"#0e8a5f", red:"#d1435b", sep:"#e8e0ec", sepOpaque:"#dbd1e0", stripe:"#f8f5fa" },
    dark:  { canvas:"#100c15", surface:"#1a1420", tertiary:"#221a2a", sidebar:"#0e0a12",
             p:"#efe9f4", s:"#9d90a8", t:"#6d6178", q:"#514858",
             accent:"#c084fc", accentHover:"#cd9dfd", onAccent:"#1b0f26",
             green:"#3fd398", red:"#ff6478", sep:"rgba(255,255,255,0.08)", sepOpaque:"#2a2033", stripe:"rgba(255,255,255,0.02)" },
  },

  amber: {
    name: "Amber", note: "Honey accent on a neutral warm canvas. The brightest of the ten in daylight.",
    light: { canvas:"#f7f4ee", surface:"#ffffff", tertiary:"#efe9de", sidebar:"#fffdf9",
             p:"#1c1913", s:"#655e50", t:"#9c9484", q:"#bcb5a5",
             accent:"#b57314", accentHover:"#965f0f", onAccent:"#ffffff",
             green:"#2f7d55", red:"#c33b3b", sep:"#eae3d7", sepOpaque:"#ded5c6", stripe:"#faf7f1" },
    dark:  { canvas:"#131110", surface:"#1d1a17", tertiary:"#25211d", sidebar:"#110f0d",
             p:"#f3ede4", s:"#a89d8e", t:"#786e60", q:"#575044",
             accent:"#f0b429", accentHover:"#f6c451", onAccent:"#241a06",
             green:"#3fd398", red:"#ff6f61", sep:"rgba(255,255,255,0.08)", sepOpaque:"#2d2822", stripe:"rgba(255,255,255,0.02)" },
  },

  sky: {
    name: "Slate & Sky", note: "Cool slate surfaces, bright sky accent. Reads as software rather than as a bank.",
    light: { canvas:"#eef1f5", surface:"#ffffff", tertiary:"#e3e8ef", sidebar:"#ffffff",
             p:"#0f1720", s:"#586474", t:"#8f9aab", q:"#b2bbc8",
             accent:"#0284c7", accentHover:"#0270a8", onAccent:"#ffffff",
             green:"#0e8a5f", red:"#dc2f45", sep:"#e2e7ee", sepOpaque:"#d3dae4", stripe:"#f4f7fa" },
    dark:  { canvas:"#0a0e13", surface:"#131a23", tertiary:"#1a222d", sidebar:"#0b1017",
             p:"#e9eef5", s:"#8b98a9", t:"#5e6a7b", q:"#48525f",
             accent:"#38bdf8", accentHover:"#63cdfa", onAccent:"#04202e",
             green:"#34d399", red:"#ff6478", sep:"rgba(255,255,255,0.08)", sepOpaque:"#212b37", stripe:"rgba(255,255,255,0.02)" },
  },

  forest: {
    name: "Forest Night", note: "Built dark-first: a green-black canvas, with the light theme derived from it rather than the other way round.",
    light: { canvas:"#eef2ee", surface:"#ffffff", tertiary:"#e3eae4", sidebar:"#ffffff",
             p:"#0c1710", s:"#54655b", t:"#8fa396", q:"#b2c0b7",
             accent:"#15803d", accentHover:"#116430", onAccent:"#ffffff",
             green:"#15803d", red:"#c02f3d", sep:"#e2e9e3", sepOpaque:"#d3ddd6", stripe:"#f4f8f5" },
    dark:  { canvas:"#070d0a", surface:"#101a14", tertiary:"#16241c", sidebar:"#08110c",
             p:"#e6f0e9", s:"#8ba396", t:"#5f7a6c", q:"#48594f",
             accent:"#4ade80", accentHover:"#6ee79a", onAccent:"#04220f",
             green:"#4ade80", red:"#ff6b6b", sep:"rgba(255,255,255,0.08)", sepOpaque:"#1c2b22", stripe:"rgba(255,255,255,0.02)" },
  },
};

/* hex → "r, g, b" so the derived tints can be written as rgba(). */
function rgbOf(hex) {
  let h = hex.replace("#", "");
  if (h.length === 3) h = h.split("").map(c => c + c).join("");
  const n = parseInt(h, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255].join(", ");
}

/* The token block for one colourway in one theme — the same variable names the
   app's stylesheet already reads, so nothing else has to change. */
function tokenBlock(key, theme) {
  const c = PALETTES[key][theme];
  const a = rgbOf(c.accent);
  const dark = theme === "dark";
  return `
  color-scheme: ${theme};

  --bg: ${c.surface};
  --bg-secondary: ${c.canvas};
  --bg-tertiary: ${c.tertiary};
  --bg-grouped: ${c.canvas};

  --text-primary: ${c.p};
  --text-secondary: ${c.s};
  --text-tertiary: ${c.t};
  --text-quaternary: ${c.q};

  --accent: ${c.accent};
  --accent-hover: ${c.accentHover};
  --on-accent: ${c.onAccent};
  --green: ${c.green};
  --red: ${c.red};

  --accent-soft: rgba(${a}, ${dark ? "0.12" : "0.08"});
  --accent-softer: rgba(${a}, ${dark ? "0.06" : "0.04"});
  --accent-ring: rgba(${a}, ${dark ? "0.22" : "0.18"});
  --hover-overlay: ${dark ? "rgba(255, 255, 255, 0.05)" : "rgba(" + rgbOf(c.p) + ", 0.04)"};
  --row-stripe: ${c.stripe};
  --row-hover: rgba(${a}, ${dark ? "0.08" : "0.07"});
  --heat-0: rgba(${a}, ${dark ? "0.10" : "0.08"});
  --heat-1: rgba(${a}, ${dark ? "0.32" : "0.30"});
  --heat-2: rgba(${a}, 0.55);
  --heat-3: rgba(${a}, ${dark ? "0.82" : "0.80"});

  --separator: ${c.sep};
  --separator-opaque: ${c.sepOpaque};
  --border: ${dark ? "rgba(255, 255, 255, 0.10)" : c.sep};

  --sidebar-bg: ${c.sidebar};`;
}

/* What you would paste into style.css to adopt a colourway. */
function styleSheetFor(key) {
  return `:root {${tokenBlock(key, "light")}\n}\n\n:root[data-theme="dark"] {${tokenBlock(key, "dark")}\n\n  --shadow-sm: none;\n  --shadow-md: none;\n  --shadow-lg: 0 10px 30px rgba(0, 0, 0, 0.45);\n  --shadow-card: none;\n}`;
}
