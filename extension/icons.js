// Vienkāršas līniju ikonas (stroke: currentColor) — emoji vietā.
const _I = (inner, fill) =>
  `<svg viewBox="0 0 24 24" width="16" height="16" ${
    fill ? 'fill="currentColor" stroke="none"'
         : 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
  } aria-hidden="true">${inner}</svg>`;

const DVT_ICONS = {
  mic: _I('<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/>'),
  folder: _I('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'),
  search: _I('<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/>'),
  trash: _I('<path d="M4 7h16"/><path d="M9 7V5h6v2"/><path d="M6 7l1 13h10l1-13"/><path d="M10 11v6M14 11v6"/>'),
  pencil: _I('<path d="M4 20l1-4L16 5l3 3L8 19l-4 1z"/><path d="M14 7l3 3"/>'),
  x: _I('<path d="M6 6l12 12M18 6L6 18"/>'),
  play: _I('<path d="M8 5l12 7-12 7z"/>', true),
  pause: _I('<path d="M7 5h4v14H7zM13 5h4v14h-4z"/>', true),
};
