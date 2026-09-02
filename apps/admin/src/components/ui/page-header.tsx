import type { ReactNode } from "react";

/** The serif title every screen opens with, its one-line explanation, and the actions on the right. */
export function PageHeader({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="flex items-end justify-between gap-6">
      <div>
        <h1 className="font-serif text-title font-normal">{title}</h1>
        {subtitle ? <p className="mt-1.5 max-w-[760px] text-ui text-muted">{subtitle}</p> : null}
      </div>
      {children ? <div className="flex items-center gap-2.5">{children}</div> : null}
    </div>
  );
}
