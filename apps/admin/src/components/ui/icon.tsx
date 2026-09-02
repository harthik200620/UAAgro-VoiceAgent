import type { SVGProps } from "react";

/**
 * The mockups' icon set: 20-unit strokes, drawn once, named for what they
 * mean rather than what they look like so a call to `<Icon name="inbound">`
 * reads in the component that uses it.
 */
const PATHS = {
  leaf: (
    <>
      <path d="M10 17V9" />
      <path d="M10 11c0-3 2.5-5 6-5 0 3-2.5 5-6 5z" />
      <path d="M10 9c0-3-2.5-5-6-5 0 3 2.5 5 6 5z" />
    </>
  ),
  live: <polyline points="2 10 6 10 8 4 12 16 14 10 18 10" />,
  phone: (
    <path d="M4 3h3l1.5 4-2 1.5a10 10 0 0 0 5 5L13 11.5l4 1.5v3a1 1 0 0 1-1 1A14 14 0 0 1 3 4a1 1 0 0 1 1-1z" />
  ),
  outbound: <path d="M6 14L14 6M8 6h6v6" />,
  inbound: <path d="M14 6L6 14M12 14H6V8" />,
  flows: (
    <>
      <circle cx="5" cy="5" r="2" />
      <circle cx="15" cy="15" r="2" />
      <path d="M5 7v3a3 3 0 0 0 3 3h5" />
    </>
  ),
  data: (
    <>
      <ellipse cx="10" cy="5" rx="6" ry="2.5" />
      <path d="M4 5v10c0 1.4 2.7 2.5 6 2.5s6-1.1 6-2.5V5" />
      <path d="M4 10c0 1.4 2.7 2.5 6 2.5s6-1.1 6-2.5" />
    </>
  ),
  mic: (
    <>
      <rect x="7" y="2" width="6" height="10" rx="3" />
      <path d="M4 9a6 6 0 0 0 12 0M10 15v3" />
    </>
  ),
  transfer: <path d="M3 7h11l-3-3M17 13H6l3 3" />,
  close: <path d="M5 5l10 10M15 5L5 15" />,
  clock: (
    <>
      <circle cx="10" cy="10" r="7" />
      <path d="M10 6v4l3 2" />
    </>
  ),
  search: (
    <>
      <circle cx="9" cy="9" r="5.5" />
      <path d="M13 13l4 4" />
    </>
  ),
  chevronRight: <path d="M7 4l6 6-6 6" />,
  chevronLeft: <path d="M12 4l-6 6 6 6" />,
  play: <path d="M6 4l10 6-10 6z" fill="currentColor" stroke="none" />,
  pause: <path d="M6 4v12M14 4v12" />,
  stop: <rect x="5" y="5" width="10" height="10" rx="1.5" />,
  plus: <path d="M10 4v12M4 10h12" />,
  check: <path d="M4 10l4 4 8-8" />,
  download: <path d="M10 3v10M6 9l4 4 4-4M4 17h12" />,
  upload: <path d="M10 14V4M6 8l4-4 4 4M4 16h12" />,
  lock: (
    <>
      <rect x="4" y="9" width="12" height="8" rx="1.5" />
      <path d="M7 9V6a3 3 0 0 1 6 0v3" />
    </>
  ),
  keypad: (
    <>
      {[5, 10, 15].flatMap((cy) =>
        [5, 10, 15].map((cx) => <circle key={`${cx}-${cy}`} cx={cx} cy={cy} r="1.4" />),
      )}
    </>
  ),
  whatsapp: (
    <>
      <path d="M4 16l1-3.2A6.5 6.5 0 1 1 7.4 15z" />
      <path d="M8 8.5c.3 1.6 1.9 3.2 3.5 3.5l1-.9-1.2-.9-.6.5c-.6-.3-1.1-.8-1.4-1.4l.5-.6-.9-1.2z" />
    </>
  ),
  pin: (
    <>
      <path d="M10 18s-6-5.2-6-10a6 6 0 0 1 12 0c0 4.8-6 10-6 10z" />
      <circle cx="10" cy="8" r="2" />
    </>
  ),
  shield: <path d="M10 2l6 2.5V9c0 4-2.6 7-6 9-3.4-2-6-5-6-9V4.5z" />,
  file: (
    <>
      <path d="M5 2h7l4 4v12H5z" />
      <path d="M12 2v4h4" />
    </>
  ),
  link: <path d="M8 12l4-4M7 9l-2 2a3 3 0 0 0 4 4l2-2M13 11l2-2a3 3 0 0 0-4-4l-2 2" />,
  trash: <path d="M4 6h12M8 6V4h4v2M6 6l1 11h6l1-11" />,
  power: (
    <>
      <path d="M10 3v7" />
      <path d="M6 6a6 6 0 1 0 8 0" />
    </>
  ),
};

export type IconName = keyof typeof PATHS;

type Props = Omit<SVGProps<SVGSVGElement>, "name"> & { name: IconName; size?: number };

export function Icon({ name, size = 16, className, ...rest }: Props) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className ? `shrink-0 ${className}` : "shrink-0"}
      {...rest}
    >
      {PATHS[name]}
    </svg>
  );
}
