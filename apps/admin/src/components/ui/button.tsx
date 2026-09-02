import { clsx } from "clsx";
import type { ButtonHTMLAttributes, ReactNode } from "react";

import { Link } from "@/i18n/routing";

import { Icon, type IconName } from "./icon";

/**
 * The three buttons of the design -- ink on paper, white with a hairline,
 * and bare -- plus the red-text variant for stopping things.
 *
 * The mockups' small buttons are 30px tall. The invisible `::after` box on
 * that size lifts the hit target to 40px without changing what is drawn, so
 * a manager on a laptop trackpad does not have to aim.
 */
type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md" | "lg";

const VARIANT: Record<Variant, string> = {
  primary: "border-ink bg-ink text-paper hover:bg-ink/90",
  secondary: "border-line bg-surface text-ink hover:bg-inset",
  ghost: "border-transparent bg-transparent text-ink hover:bg-inset",
  danger: "border-line bg-surface text-red-text hover:bg-red-bg",
};

const SIZE: Record<Size, string> = {
  sm: "px-3 py-1.5 text-small after:absolute after:inset-x-0 after:-inset-y-[5px] after:content-['']",
  md: "px-4 py-[9px] text-ui",
  lg: "w-full justify-center rounded-panel px-3.5 py-3.5 text-prose font-semibold",
};

export function buttonClass(variant: Variant, size: Size, className?: string): string {
  return clsx(
    "relative inline-flex items-center gap-2 whitespace-nowrap rounded-btn border font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50",
    VARIANT[variant],
    SIZE[size],
    className,
  );
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: Size;
  icon?: IconName;
  children: ReactNode;
};

export function Button({
  variant = "secondary",
  size = "sm",
  icon,
  className,
  children,
  type = "button",
  ...rest
}: ButtonProps) {
  return (
    <button type={type} className={buttonClass(variant, size, className)} {...rest}>
      {icon ? <Icon name={icon} /> : null}
      <span>{children}</span>
    </button>
  );
}

export function ButtonLink({
  href,
  variant = "secondary",
  size = "sm",
  icon,
  className,
  children,
}: {
  href: string;
  variant?: Variant;
  size?: Size;
  icon?: IconName;
  className?: string;
  children: ReactNode;
}) {
  return (
    <Link href={href} className={buttonClass(variant, size, className)}>
      {icon ? <Icon name={icon} /> : null}
      <span>{children}</span>
    </Link>
  );
}
