// Inline stroke icons (no icon font, no network).
const P: Record<string, string> = {
  home: "M3 11.5 12 4l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
  chart: "M4 20V10M10 20V4M16 20v-8M22 20H2",
  bell: "M6 16V11a6 6 0 1 1 12 0v5l2 2H4zM10 21h4",
  note: "M6 3h9l4 4v14H6zM14 3v5h5M9 12h7M9 16h7",
  chev: "M9 6l6 6-6 6",
  back: "M15 6l-6 6 6 6",
  info: "M12 8h.01M11 12h1v5h1M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z",
  plus: "M12 5v14M5 12h14",
  x: "M6 6l12 12M18 6 6 18",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
  camera: "M4 8h3l2-3h6l2 3h3v12H4zM12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
  download: "M12 4v11M7 10l5 5 5-5M5 20h14",
  bars: "M5 20v-6M10 20V8M15 20v-9M20 20V5",
  layers: "M12 3 2 8l10 5 10-5zM2 13l10 5 10-5",
  wall: "M3 6h18v4H3zM3 14h8v4H3zM13 14h8v4h-8z",
  db: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  pulse: "M3 12h4l3-8 4 16 3-8h4",
  shield: "M12 3 4 6v6c0 5 3.4 8 8 9 4.6-1 8-4 8-9V6z",
  send: "M22 2 11 13M22 2l-7 20-4-9-9-4z",
  phone: "M8 2h8a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zM11 18h2",
  check: "M5 12l5 5L20 7",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
};

export function Icon({ name, title }: { name: keyof typeof P | string; title?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden={title ? undefined : true} role={title ? "img" : undefined}>
      {title && <title>{title}</title>}
      <path d={P[name] ?? P.info} />
    </svg>
  );
}
