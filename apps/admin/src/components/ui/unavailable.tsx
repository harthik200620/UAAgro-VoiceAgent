import { buttonClass } from "./button";
import { Icon, type IconName } from "./icon";

/**
 * A control the telephony layer does not back yet -- Listen, Hand to manager,
 * End, Call again. Drawn as the design draws it, named, and honest about why
 * it does nothing: the tooltip says so, and it is not a button pretending to
 * work. `aria-disabled` rather than `disabled` so the tooltip still shows.
 */
export function Unavailable({
  icon,
  label,
  reason,
  danger = false,
  size = "sm",
}: {
  icon: IconName;
  label: string;
  reason: string;
  danger?: boolean;
  size?: "sm" | "md";
}) {
  return (
    <span
      role="button"
      aria-disabled="true"
      tabIndex={0}
      title={reason}
      className={buttonClass(danger ? "danger" : "secondary", size, "cursor-not-allowed opacity-50")}
    >
      <Icon name={icon} />
      <span>{label}</span>
    </span>
  );
}
