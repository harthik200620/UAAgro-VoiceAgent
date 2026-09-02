import { clsx } from "clsx";
import type { HTMLAttributes, ReactNode } from "react";

/** White, one hairline, 12px corners, the faintest shadow. Every panel on every screen. */
export function Card({ className, children, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={clsx("rounded-card border border-line bg-surface shadow-card", className)}
      {...rest}
    >
      {children}
    </div>
  );
}

/** The bold line that opens a card, with the muted aside the mockups put after a dot. */
export function CardTitle({
  children,
  aside,
  className,
}: {
  children: ReactNode;
  aside?: ReactNode;
  className?: string;
}) {
  return (
    <div className={clsx("flex items-baseline justify-between gap-3", className)}>
      <div className="font-semibold">{children}</div>
      {aside ? <div className="text-small text-muted">{aside}</div> : null}
    </div>
  );
}
