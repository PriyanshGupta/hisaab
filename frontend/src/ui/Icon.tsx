// The app's icons: one hand-drawn set on a 24-unit grid, 1.75 stroke, round caps, drawn in currentColor so an
// icon always takes its control's colour. Decorative by default (aria-hidden): the control around it carries
// the name. Kept here, not in a dependency: the app runs offline and needs a couple of dozen.
const PATHS = {
  report: "M4 20V10m6 10V4m6 16v-7m4 7H3",
  review: "M9 11l2.5 2.5L16 9M5 4h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z",
  transactions: "M4 7h13m0 0-3-3m3 3-3 3M20 17H7m0 0 3-3m-3 3 3 3",
  statements: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Zm0 0v5h5M9 13h6M9 17h4",
  add: "M12 5v14M5 12h14",
  accounts: "M3 10h18M5 10v8m4.67-8v8m4.66-8v8M19 10v8M3 21h18M12 3l9 5H3l9-5Z",
  categories: "M4 5h6v6H4V5Zm10 0h6v6h-6V5ZM4 15h6v4H4v-4Zm10 0h6v4h-6v-4Z",
  settings: "M4 6h9m4 0h3M4 12h3m4 0h9M4 18h11m4 0h1M15 4v4M9 10v4M17 16v4",
  admin: "M12 3l7 3v5c0 4.5-3 8.5-7 10-4-1.5-7-5.5-7-10V6l7-3Z",
  card: "M3 7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Zm0 3h18M7 15h4",
  wallet: "M4 7v11a2 2 0 0 0 2 2h13a1 1 0 0 0 1-1v-3m0-5V8a1 1 0 0 0-1-1H6a2 2 0 0 1-2-2 2 2 0 0 1 2-2h11v4m3 4h-4a2 2 0 0 0 0 4h4v-4Z",
  edit: "M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16v4Zm9.5-13.5 4 4",
  archive: "M4 8v11a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V8M3 4h18v4H3V4Zm7 8h4",
  menu: "M4 7h16M4 12h16M4 17h16",
  close: "M6 6l12 12M18 6 6 18",
  "chevron-left": "M15 6l-6 6 6 6",
  "chevron-right": "M9 6l6 6-6 6",
  "chevron-down": "M6 9l6 6 6-6",
  download: "M12 4v11m0 0-4-4m4 4 4-4M5 20h14",
  upload: "M12 20V9m0 0-4 4m4-4 4 4M5 4h14",
  check: "M5 12.5l4.5 4.5L19 7.5",
  alert: "M12 8v5m0 3.5v.01M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20h15.4a2 2 0 0 0 1.7-2.8L13.7 3.9a2 2 0 0 0-3.4 0Z",
  info: "M12 11v6m0-9.5v.01M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z",
  search: "M20 20l-4.5-4.5M17 10.5a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0Z",
  signout: "M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 16l4-4-4-4m4 4H4",
  mail: "M4 6h16v12H4V6Zm0 0 8 7 8-7",
  refresh: "M20 11a8 8 0 0 0-14.6-4.5M4 4v3h3m-3 6a8 8 0 0 0 14.6 4.5M20 20v-3h-3",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 18, className }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg className={className ? `icon ${className}` : "icon"} viewBox="0 0 24 24" width={size} height={size}
      fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round"
      aria-hidden="true" focusable="false">
      <path d={PATHS[name]} />
    </svg>
  );
}
