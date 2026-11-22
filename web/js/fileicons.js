// Small (16 px) icons for the shared folder's list view.  The user's own Mac OS icons come
// from the Platinum bundle when it has them (folder, generic document, disks); the rest are
// RetroBoot's original pixel art in the same palette (see icons.js).

import { bundleIcon } from './platinum.js';

const PALETTE = { '#': 'ink', g: 'mid', l: 'light', w: 'white', b: 'blue', d: 'navy', y: 'yellow', r: 'red', G: 'green' };

const PIXELS = {
  folder: [
    '................',
    '................',
    '..#####.........',
    '.#bbbbb#........',
    '#############...',
    '#wwwwwwwwwwwb#..',
    '#wbbbbbbbbbbb#..',
    '#wbbbbbbbbbbb#..',
    '#wbbbbbbbbbbb#..',
    '#wbbbbbbbbbbb#..',
    '#wbbbbbbbbbbb#..',
    '#wbbbbbbbbbbb#..',
    '#bdddddddddddd#.',
    '.##############.',
    '................',
    '................',
  ],
  document: [
    '...########.....',
    '...#wwwwww##....',
    '...#wwwwww#w#...',
    '...#wwwwww#ww#..',
    '...#wwwwww####..',
    '...#wwwwwwwwl#..',
    '...#wwwwwwwwl#..',
    '...#wwwwwwwwl#..',
    '...#wwwwwwwwl#..',
    '...#wwwwwwwwl#..',
    '...#wwwwwwwwl#..',
    '...#wwwwwwwwl#..',
    '...#wwwwwwwwl#..',
    '...#wllllllll#..',
    '...###########..',
    '................',
  ],
  text: [
    '...########.....',
    '...#wwwwww##....',
    '...#w####w#w#...',
    '...#wwwwww#ww#..',
    '...#w#####w###..',
    '...#wwwwwwwwl#..',
    '...#w######wl#..',
    '...#wwwwwwwwl#..',
    '...#w#####wwl#..',
    '...#wwwwwwwwl#..',
    '...#w######wl#..',
    '...#wwwwwwwwl#..',
    '...#w####wwwl#..',
    '...#wllllllll#..',
    '...###########..',
    '................',
  ],
  picture: [
    '...########.....',
    '...#wwwwww##....',
    '...#wwwwww#w#...',
    '...#wwwwww#ww#..',
    '...#wwwwww####..',
    '...#w#######l#..',
    '...#w#bbbyb#l#..',
    '...#w#bbbbb#l#..',
    '...#w#bGbbb#l#..',
    '...#w#GGGbG#l#..',
    '...#w#GGGGG#l#..',
    '...#w#######l#..',
    '...#wwwwwwwwl#..',
    '...#wllllllll#..',
    '...###########..',
    '................',
  ],
  application: [
    '.......##.......',
    '......#ww#......',
    '.....#wwww#.....',
    '....#wwbbww#....',
    '...#wwbddbww#...',
    '..#wwbd##dbww#..',
    '.#wwbd#..#dbww#.',
    '#wwbd#....#dbww#',
    '#llbd#....#dbll#',
    '.#llbd#..#dbll#.',
    '..#llbd##dbll#..',
    '...#llbddbll#...',
    '....#llbbll#....',
    '.....#llll#.....',
    '......#ll#......',
    '.......##.......',
  ],
  archive: [
    '...########.....',
    '...#wwwwww##....',
    '...#wwwwww#w#...',
    '...#wwwwww#ww#..',
    '...#wwwwww####..',
    '...#wwwwwwwwl#..',
    '..############..',
    '..#yyyyyyyyyy#..',
    '..#y########y#..',
    '..#yyyyyyyyyy#..',
    '..############..',
    '...#wwwwwwwwl#..',
    '...#wwwwwwwwl#..',
    '...#wllllllll#..',
    '...###########..',
    '................',
  ],
  disk: [
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
  cd: [
    '.....######.....',
    '...##wwwwww##...',
    '..#wwwwllwwww#..',
    '.#wwwwwllwwwww#.',
    '.#wwwwwllwwwww#.',
    '#wwwwww##wwwwww#',
    '#llllw#..#wllll#',
    '#llllw#..#wllll#',
    '#wwwwww##wwwwww#',
    '.#wwwwwllwwwww#.',
    '.#wwwwwllwwwww#.',
    '..#wwwwllwwww#..',
    '...##wwwwww##...',
    '.....######.....',
    '................',
    '................',
  ],
  movie: [
    '...########.....',
    '...#wwwwww##....',
    '...#wwwwww#w#...',
    '...#wwwwww#ww#..',
    '...#wwwwww####..',
    '...#w#######l#..',
    '...#w#w#w#w#l#..',
    '...#w#######l#..',
    '...#w#ddddd#l#..',
    '...#w#######l#..',
    '...#w#w#w#w#l#..',
    '...#w#######l#..',
    '...#wwwwwwwwl#..',
    '...#wllllllll#..',
    '...###########..',
    '................',
  ],
  sound: [
    '...########.....',
    '...#wwwwww##....',
    '...#wwwwww#w#...',
    '...#wwwwww#ww#..',
    '...#wwwwww####..',
    '...#wwwww#wwl#..',
    '...#wwwww##wl#..',
    '...#wwwww#w#l#..',
    '...#wwwww#wwl#..',
    '...#www###wwl#..',
    '...#ww####wwl#..',
    '...#www##wwwl#..',
    '...#wwwwwwwwl#..',
    '...#wllllllll#..',
    '...###########..',
    '................',
  ],
};

// Our icon names -> the Platinum bundle's (the user's own Mac OS icons) where one fits.
const BUNDLE = { folder: 'folder', document: 'document', disk: 'disk', cd: 'cd', application: 'application', text: 'text' };

const cache = new Map();

function pixelSvg(rows, size) {
  const groups = new Map();
  rows.forEach((row, y) => {
    let x = 0;
    while (x < row.length) {
      const c = row[x];
      if (c === '.') {
        x += 1;
        continue;
      }
      let n = 1;
      while (x + n < row.length && row[x + n] === c) n += 1;
      const key = PALETTE[c] || 'ink';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(`M${x} ${y}h${n}v1h-${n}z`);
      x += n;
    }
  });
  let out = `<svg class="px" viewBox="0 0 16 16" width="${size}" height="${size}" shape-rendering="crispEdges" aria-hidden="true" focusable="false">`;
  for (const [key, d] of groups) out += `<path class="px-${key}" d="${d.join('')}"/>`;
  return `${out}</svg>`;
}

/** Markup for a list-view icon (16 px): the user's Mac OS icon when the bundle has it. */
export function fileIcon(name, size = 16) {
  const kind = PIXELS[name] ? name : 'document';
  const b = BUNDLE[kind] ? bundleIcon(BUNDLE[kind], size) : null;
  if (b) return `<img class="pt-icon" src="${b.src}" width="16" height="16" alt="" draggable="false">`;
  const key = `${kind}:${size}`;
  if (!cache.has(key)) cache.set(key, pixelSvg(PIXELS[kind], size));
  return cache.get(key);
}

/** The icon for a file the browser is about to copy (from its name only). */
export function iconForName(name, isFolder = false) {
  if (isFolder) return 'folder';
  const ext = (/\.([a-z0-9]{1,5})$/i.exec(name || '') || [])[1];
  const e = ext ? ext.toLowerCase() : '';
  if (['txt', 'text', 'md', 'csv', 'html', 'htm', 'rtf', 'log', 'c', 'h', 'py', 'js', 'json', 'xml', 'css'].includes(e)) return 'text';
  if (['jpg', 'jpeg', 'gif', 'png', 'bmp', 'tif', 'tiff', 'pict', 'pct', 'psd'].includes(e)) return 'picture';
  if (['sit', 'sea', 'hqx', 'bin', 'zip', 'cpt', 'gz', 'tgz', 'tar', 'sitx'].includes(e)) return 'archive';
  if (['iso', 'toast', 'cdr'].includes(e)) return 'cd';
  if (['img', 'dmg', 'dsk', 'smi', 'image'].includes(e)) return 'disk';
  if (['mov', 'qt', 'mp4', 'm4v', 'mpg', 'mpeg', 'avi'].includes(e)) return 'movie';
  if (['mp3', 'aif', 'aiff', 'aifc', 'wav', 'mid', 'midi', 'au', 'snd'].includes(e)) return 'sound';
  return 'document';
}
