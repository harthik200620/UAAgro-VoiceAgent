import { clsx } from "clsx";
import type { ReactNode } from "react";

/**
 * The design's table: 12px muted headers over a hairline, 13.5px cells over a
 * fainter one, 11px of vertical padding so a row is a comfortable hit target.
 */
type Align = "left" | "right" | "center";

const ALIGN: Record<Align, string> = {
  left: "text-left",
  right: "text-right",
  center: "text-center",
};

export function Table({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className="overflow-x-auto">
      <table className={clsx("w-full border-collapse text-ui", className)}>{children}</table>
    </div>
  );
}

export function Th({
  children,
  align = "left",
  className,
}: {
  children?: ReactNode;
  align?: Align;
  className?: string;
}) {
  return (
    <th
      scope="col"
      className={clsx(
        "whitespace-nowrap border-b border-line px-3 py-2.5 text-label font-medium text-muted",
        ALIGN[align],
        className,
      )}
    >
      {children}
    </th>
  );
}

export function Td({
  children,
  align = "left",
  className,
  colSpan,
}: {
  children?: ReactNode;
  align?: Align;
  className?: string;
  colSpan?: number;
}) {
  return (
    <td
      colSpan={colSpan}
      className={clsx("border-b border-inset px-3 py-[11px] align-middle", ALIGN[align], className)}
    >
      {children}
    </td>
  );
}
