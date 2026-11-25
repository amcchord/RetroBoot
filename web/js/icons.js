// Original pixel-art icons and the RetroBoot wordmark, drawn for this project.
// Each icon is a 16×16 grid; characters map to theme colors (see .px-* in CSS).

import { bundleIcon, onPlatinum } from './platinum.js';

const PALETTE = {
  '#': 'ink',
  g: 'mid',
  l: 'light',
  w: 'white',
  b: 'blue',
  d: 'navy',
  y: 'yellow',
  r: 'red',
  G: 'green',
};

const PIXELS = {
  bluetooth: [
    '......####......',
    '....##bbbb##....',
    '...#bbbwbbbd#...',
    '...#bbbwwbbd#...',
    '..#bbbbwbwbbd#..',
    '..#bbwbwbbwbd#..',
    '..#bbbwwbwbbd#..',
    '..#bbbbwwbbbd#..',
    '..#bbbbwwbbbd#..',
    '..#bbbwwbwbbd#..',
    '..#bbwbwbbwbd#..',
    '..#bbbbwbwbbd#..',
    '...#bbbwwbbd#...',
    '...#bbbwbbbd#...',
    '....##dddd##....',
    '......####......',
  ],
  mac: [
    '..############..',
    '.#wwwwwwwwwwww#.',
    '.#w##########l#.',
    '.#w#xxxxxxxx#l#.',
    '.#w#xxxxxxxx#l#.',
    '.#w#xxxxxxxx#l#.',
    '.#w#xxxxxxxx#l#.',
    '.#w#xxxxxxxx#l#.',
    '.#w##########l#.',
    '.#wlllllllllll#.',
    '.#wllllll###ll#.',
    '.#wlllllllllll#.',
    '.#gggggggggggg#.',
    '..############..',
    '..#gggggggggg#..',
    '..############..',
  ],
  machine: [
    '................',
    '....#.#..#.#....',
    '....#.#..#.#....',
    '...##########...',
    '.###wwwwwwwl###.',
    '...#wllllllg#...',
    '.###wlbbbblg###.',
    '...#wlbddblg#...',
    '...#wlbddblg#...',
    '.###wlbbbblg###.',
    '...#wllllllg#...',
    '.###lggggggg###.',
    '...##########...',
    '....#.#..#.#....',
    '....#.#..#.#....',
    '................',
  ],
  display: [
    '.##############.',
    '#wwwwwwwwwwwwwl#',
    '#w############l#',
    '#w#bbbbbbbbbb#l#',
    '#w#bwwbbbbbbb#l#',
    '#w#bwbbbbbbbb#l#',
    '#w#bbbbbbbbbb#l#',
    '#w#bbbbbbbbbb#l#',
    '#w#bbbbbbbbbb#l#',
    '#w############l#',
    '#lllllllllllllg#',
    '.##############.',
    '......#gg#......',
    '...##########...',
    '...#llllllll#...',
    '...##########...',
  ],
  screen: [
    '.##############.',
    '#wwwwwwwwwwwwwl#',
    '#w############l#',
    '#w#bbbbbbbbbb#l#',
    '#w#b#bbbbbbbb#l#',
    '#w#b##bbbbbbb#l#',
    '#w#b###bbbbbb#l#',
    '#w#b####bbbbb#l#',
    '#w#b##bbbbbbb#l#',
    '#w############l#',
    '#lllllllllllllg#',
    '.##############.',
    '......#gg#......',
    '...##########...',
    '...#llllllll#...',
    '...##########...',
  ],
  sound: [
    '................',
    '........#.......',
    '.......##...#...',
    '......#w#....#..',
    '.....#ww#.....#.',
    '.####wlw#.#...#.',
    '.#wwwwlw#..#..#.',
    '.#wllllw#..#..#.',
    '.#wllllw#..#..#.',
    '.#wggglw#..#..#.',
    '.####wlw#.#...#.',
    '.....#lw#.....#.',
    '......#w#....#..',
    '.......##...#...',
    '........#.......',
    '................',
  ],
  storage: [
    '................',
    '................',
    '................',
    '.##############.',
    '#wwwwwwwwwwwwwl#',
    '#wllllllllllllg#',
    '#wl##########lg#',
    '#wllllllllllllg#',
    '#wllllllllllllg#',
    '#wllllllllGGllg#',
    '#lggggggggggggg#',
    '.##############.',
    '..##........##..',
    '................',
    '................',
    '................',
  ],
  network: [
    '.....######.....',
    '.....#wwww#.....',
    '.....#wbbl#.....',
    '.....#wbbl#.....',
    '.....######.....',
    '.......##.......',
    '.......##.......',
    '..############..',
    '..##........##..',
    '..##........##..',
    '######....######',
    '#wwww#....#wwww#',
    '#wbbl#....#wbbl#',
    '#wbbl#....#wbbl#',
    '######....######',
    '................',
  ],
  printer: [
    '....########....',
    '....#wwwwww#....',
    '....#w####w#....',
    '....#wwwwww#....',
    '..############..',
    '.#wwwwwwwwwwww#.',
    '.#wllllllllllw#.',
    '.#wl########lw#.',
    '.#wllllllllGGw#.',
    '.#gggggggggggg#.',
    '.#g##########g#.',
    '.#g#wwwwwwww#g#.',
    '.#g#w######w#g#.',
    '.#g#wwwwwwww#g#.',
    '..############..',
    '................',
  ],
  diagnostics: [
    '...######.......',
    '.##wwwwww##.....',
    '.#wwwlllll#.....',
    '#wwlllllllb#....',
    '#wllllllllb#....',
    '#wllllllllb#....',
    '#wllllllllb#....',
    '#llllllllbb#....',
    '#lllllllbbb#....',
    '.#lllllbbb#.....',
    '.##bbbbbb###....',
    '...######.###...',
    '...........###..',
    '............###.',
    '.............###',
    '..............##',
  ],
  recovery: [
    '................',
    '.....######.....',
    '.....#....#.....',
    '.##############.',
    '.#wwwwwwwwwwww#.',
    '.#wwwwwrrwwwwl#.',
    '.#wwwwwrrwwwwl#.',
    '.#wwwrrrrrrwwl#.',
    '.#wwwrrrrrrwwl#.',
    '.#wwwwwrrwwwwl#.',
    '.#wwwwwrrwwwwl#.',
    '.#llllllllllll#.',
    '.##############.',
    '................',
    '................',
    '................',
  ],
  update: [
    '................',
    '......####......',
    '......#bb#......',
    '......#bb#......',
    '......#bb#......',
    '...####bb####...',
    '....#bbbbbb#....',
    '.....#bbbb#.....',
    '......#bb#......',
    '.##...####...##.',
    '#ww#...##...#wl#',
    '#wl##########lg#',
    '#wllllllllllllg#',
    '#wllllllllGGllg#',
    '#lggggggggggggg#',
    '.##############.',
  ],
  devices: [
    '.....######.....',
    '....#wwwwwl#....',
    '....#w####l#....',
    '....#w#bb#l#....',
    '....#w#bw#l#....',
    '....#w#bb#l#....',
    '....#w#bb#l#....',
    '....#w#bb#l#....',
    '....#w#bb#l#....',
    '....#w#bb#l#....',
    '....#w####l#....',
    '....#wwwwwl#....',
    '....#ww##wl#....',
    '....#lllllg#....',
    '.....######.....',
    '................',
  ],
  caution: [
    '.......##.......',
    '......#yy#......',
    '......#yy#......',
    '.....#yyyy#.....',
    '.....#y##y#.....',
    '....#yy##yy#....',
    '....#yy##yy#....',
    '...#yyy##yyy#...',
    '...#yyy##yyy#...',
    '..#yyyy##yyyy#..',
    '..#yyyyyyyyyy#..',
    '.#yyyyy##yyyyy#.',
    '.#yyyyy##yyyyy#.',
    '#yyyyyyyyyyyyyy#',
    '################',
    '................',
  ],
  stop: [
    '.....######.....',
    '....#rrrrrr#....',
    '...#rrrrrrrr#...',
    '..#rrrrwwrrrr#..',
    '.#rrrrrwwrrrrr#.',
    '#rrrrrrwwrrrrrr#',
    '#rrrrrrwwrrrrrr#',
    '#rrrrrrwwrrrrrr#',
    '#rrrrrrwwrrrrrr#',
    '#rrrrrrrrrrrrrr#',
    '#rrrrrrwwrrrrrr#',
    '.#rrrrrwwrrrrr#.',
    '..#rrrrrrrrrr#..',
    '...#rrrrrrrr#...',
    '....#rrrrrr#....',
    '.....######.....',
  ],
  note: [
    '................',
    '.##############.',
    '#wwwwwwbbwwwwww#',
    '#wwwwwwbbwwwwww#',
    '#wwwwwwwwwwwwww#',
    '#wwwwwbbbwwwwww#',
    '#wwwwwwbbwwwwww#',
    '#wwwwwwbbwwwwww#',
    '#wwwwwwbbwwwwww#',
    '#wwwwwbbbbwwwww#',
    '#llllllllllllll#',
    '.#####.########.',
    '.....#w#........',
    '....#w#.........',
    '...##...........',
    '................',
  ],
};

const cache = new Map();

function pixelSvg(rows, size, label) {
  const groups = new Map();
  const push = (key, x, y, n) => {
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(`M${x} ${y}h${n}v1h-${n}z`);
  };
  rows.forEach((row, y) => {
    let x = 0;
    while (x < row.length) {
      const c = row[x];
      if (c === '.') {
        x += 1;
        continue;
      }
      if (c === 'x') {
        push((x + y) % 2 === 0 ? 'ink' : 'white', x, y, 1);
        x += 1;
        continue;
      }
      let n = 1;
      while (x + n < row.length && row[x + n] === c) n += 1;
      push(PALETTE[c] || 'ink', x, y, n);
      x += n;
    }
  });
  const w = rows[0].length;
  const hgt = rows.length;
  const a11y = label ? `role="img" aria-label="${label}"` : 'aria-hidden="true" focusable="false"';
  let out = `<svg class="px" viewBox="0 0 ${w} ${hgt}" width="${size}" height="${Math.round((size * hgt) / w)}" shape-rendering="crispEdges" ${a11y}>`;
  for (const [key, d] of groups) out += `<path class="px-${key}" d="${d.join('')}"/>`;
  return out + '</svg>';
}

function guestIcon(name, size) {
  const b = bundleIcon(name, size);
  if (!b) return '';
  const px = size <= 20 ? 16 : size >= 64 ? 64 : 32;
  return `<img class="pt-icon" src="${b.src}" width="${px}" height="${px}" alt="" draggable="false">`;
}

/** Use RetroBoot's pixel art until the optional guest icon becomes available. */
export function pixelIcon(name, size = 32) {
  const guest = guestIcon(name, size);
  if (guest) return guest;
  const key = `${name}:${size}`;
  if (!cache.has(key)) {
    const icon = pixelSvg(PIXELS[name] || PIXELS.mac, size);
    cache.set(key, icon.replace('<svg ', `<svg data-platinum-icon="${encodeURIComponent(name)}" `));
  }
  return cache.get(key);
}

onPlatinum(() => {
  // Update existing icons without replacing the surrounding form or page.
  for (const svg of document.querySelectorAll('svg[data-platinum-icon]')) {
    const icon = guestIcon(decodeURIComponent(svg.dataset.platinumIcon), Number(svg.getAttribute('width')));
    if (icon) svg.outerHTML = icon;
  }
});

// ---- Wordmark: a chunky 5×7 pixel face, emboldened by smearing one pixel right.

const GLYPHS = {
  R: ['####.', '#...#', '#...#', '####.', '#.#..', '#..#.', '#...#'],
  B: ['####.', '#...#', '#...#', '####.', '#...#', '#...#', '####.'],
  e: ['.....', '.....', '.###.', '#...#', '#####', '#....', '.###.'],
  t: ['.#...', '.#...', '###..', '.#...', '.#...', '.#...', '..##.'],
  r: ['.....', '.....', '#.##.', '##..#', '#....', '#....', '#....'],
  o: ['.....', '.....', '.###.', '#...#', '#...#', '#...#', '.###.'],
};

function boldGlyph(rows) {
  const bold = rows.map((r) => {
    const cells = (r + '.').split('');
    const out = cells.map((c, i) => (c === '#' || (i > 0 && cells[i - 1] === '#') ? '#' : '.'));
    return out.join('');
  });
  let width = 0;
  for (const r of bold) width = Math.max(width, r.lastIndexOf('#') + 1);
  return bold.map((r) => r.slice(0, width));
}

export function wordmark(scale = 2, text = 'RetroBoot') {
  const glyphs = [...text].map((ch) => boldGlyph(GLYPHS[ch] || GLYPHS.o));
  const rows = Array.from({ length: 7 }, () => '');
  glyphs.forEach((g, gi) => {
    for (let y = 0; y < 7; y += 1) rows[y] += g[y] + (gi < glyphs.length - 1 ? '.' : '');
  });
  const w = rows[0].length;
  let d = '';
  rows.forEach((row, y) => {
    let x = 0;
    while (x < w) {
      if (row[x] !== '#') {
        x += 1;
        continue;
      }
      let n = 1;
      while (x + n < w && row[x + n] === '#') n += 1;
      d += `M${x} ${y}h${n}v1h-${n}z`;
      x += n;
    }
  });
  return `<svg class="wordmark" viewBox="0 0 ${w} 7" width="${w * scale}" height="${7 * scale}" shape-rendering="crispEdges" role="img" aria-label="${text}"><path d="${d}"/></svg>`;
}

// ---- Small line icons for buttons (24×24, stroke = currentColor).

const LINE = {
  copy: '<rect x="8" y="8" width="12" height="12" rx="1.5"/><path d="M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8"/>',
  download: '<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v5h-5"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  up: '<path d="M6 15l6-6 6 6"/>',
  lock: '<rect x="5" y="10.5" width="14" height="10" rx="1.5"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/>',
  pause: '<path d="M9 5v14M15 5v14"/>',
  play: '<path d="M8 5l11 7-11 7z"/>',
  expand: '<path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/>',
  power: '<path d="M12 3v8"/><path d="M6.3 7.3a8 8 0 1 0 11.4 0"/>',
  restart: '<path d="M4 12a8 8 0 1 0 2.34-5.66"/><path d="M4 4v5h5"/>',
  camera: '<rect x="3" y="7" width="18" height="13" rx="2"/><circle cx="12" cy="13.5" r="3.5"/><path d="M8.5 7l1.5-3h4l1.5 3"/>',
  box: '<path d="M4 8l8-4 8 4v8l-8 4-8-4z"/><path d="M4 8l8 4 8-4M12 12v8"/>',
  wifi: '<path d="M2.5 9a14 14 0 0 1 19 0M5.5 12.5a9.5 9.5 0 0 1 13 0M8.5 16a5 5 0 0 1 7 0"/><circle cx="12" cy="19.2" r="1" fill="currentColor"/>',
  ethernet: '<rect x="4" y="5" width="16" height="12" rx="1.5"/><path d="M8 17v3M12 17v3M16 17v3M8 9h8v4H8z"/>',
  printer: '<path d="M6 9V4h12v5M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13"/>',
  signout: '<path d="M14 4h5v16h-5M10 8l-4 4 4 4M6 12h10"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
  alert: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.5"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
};

// Pixel versions of the line icons the Platinum client shows (icon-only buttons, the
// network list, the settings bar), on a 1-pixel grid so they stay crisp at every pixel size.
const LINE_PIXELS = {
  copy: [
    '............',
    '.######.....',
    '.#....#.....',
    '.#....#.....',
    '.#..#######.',
    '.#..#.....#.',
    '.####.....#.',
    '....#.....#.',
    '....#.....#.',
    '....#.....#.',
    '....#######.',
    '............',
  ],
  close: [
    '............',
    '............',
    '..##....##..',
    '...##..##...',
    '....####....',
    '.....##.....',
    '....####....',
    '...##..##...',
    '..##....##..',
    '............',
    '............',
    '............',
  ],
  lock: [
    '............',
    '....####....',
    '...#....#...',
    '...#....#...',
    '...#....#...',
    '..########..',
    '..########..',
    '..###..###..',
    '..###..###..',
    '..########..',
    '..########..',
    '............',
  ],
  down: [
    '............',
    '............',
    '............',
    '............',
    '...#######..',
    '....#####...',
    '.....###....',
    '......#.....',
    '............',
    '............',
    '............',
    '............',
  ],
  up: [
    '............',
    '............',
    '............',
    '............',
    '......#.....',
    '.....###....',
    '....#####...',
    '...#######..',
    '............',
    '............',
    '............',
    '............',
  ],
};

function linePixels(rows) {
  const w = rows[0].length;
  const hgt = rows.length;
  let d = '';
  rows.forEach((row, y) => {
    let x = 0;
    while (x < w) {
      if (row[x] !== '#') {
        x += 1;
        continue;
      }
      let n = 1;
      while (x + n < w && row[x + n] === '#') n += 1;
      d += `M${x} ${y}h${n}v1h-${n}z`;
      x += n;
    }
  });
  return `<svg class="li px-line" viewBox="0 0 ${w} ${hgt}" width="${w}" height="${hgt}" shape-rendering="crispEdges" fill="currentColor" aria-hidden="true" focusable="false"><path d="${d}"/></svg>`;
}

/** A small monochrome icon (currentColor); pixel art when there is a Platinum version. */
export function lineIcon(name, size = 18) {
  if (LINE_PIXELS[name]) return linePixels(LINE_PIXELS[name]);
  return `<svg class="li" viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${LINE[name] || ''}</svg>`;
}

/** Markup for the favicon-sized compact computer used as the app glyph. */
export const appGlyph = (size = 28) => pixelIcon('mac', size);
