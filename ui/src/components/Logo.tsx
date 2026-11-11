/** Satchel mark: outline bag body, flap and buckle with a top handle. Inherits `currentColor`. */
export function SatchelMark({ size = 20, strokeWidth = 1.7, className = "" }: { size?: number; strokeWidth?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`shrink-0 ${className}`}
      aria-hidden
    >
      <path d="M8.5 7V5.6A1.6 1.6 0 0 1 10.1 4h3.8a1.6 1.6 0 0 1 1.6 1.6V7" />
      <path d="M3.5 9.2A2.2 2.2 0 0 1 5.7 7h12.6a2.2 2.2 0 0 1 2.2 2.2v8.6a2.2 2.2 0 0 1-2.2 2.2H5.7a2.2 2.2 0 0 1-2.2-2.2z" />
      <path d="M3.5 9.5c0 2.2 1.6 3.7 3.8 3.7h9.4c2.2 0 3.8-1.5 3.8-3.7" />
      <path d="M10.6 12.2h2.8v3.2h-2.8z" />
    </svg>
  );
}

/** Icon plus the uppercase, letterspaced SATCHEL wordmark. */
export function Logo() {
  return (
    <span className="flex items-center gap-2 text-ink">
      <SatchelMark size={22} className="text-accent" />
      <span className="text-[15px] font-semibold uppercase tracking-[0.16em]">Satchel</span>
    </span>
  );
}
