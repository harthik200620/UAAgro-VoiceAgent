import { useTranslations } from "next-intl";

import { Link } from "@/i18n/routing";
import type { Contact, ContactStatus } from "@/lib/contract";
import { contactLabelKey, humanize, messageKey } from "@/lib/tones";

/**
 * One farmer on the wall. Paper while waiting, amber and pulsing during the
 * call, green when done, red when not reached, muted grey when the gate
 * removed them. The 4px band on the left carries the same state for a
 * reader who cannot see the fill. A card with a call behind it is a link.
 */
const STYLE: Record<ContactStatus, { card: string; band: string; text: string }> = {
  waiting: { card: "border-line bg-surface", band: "bg-grey", text: "text-muted" },
  in_call: { card: "animate-live border-amber bg-amber-bg", band: "bg-amber", text: "text-amber-text" },
  done: { card: "border-green bg-green-bg", band: "bg-green", text: "text-green-text" },
  no_answer: { card: "border-red bg-red-bg", band: "bg-red", text: "text-red-text" },
  removed: { card: "border-line bg-inset opacity-70", band: "bg-grey", text: "text-faint" },
};

export function ContactCard({ contact }: { contact: Contact }) {
  const t = useTranslations("contactLabels");
  const exclusions = useTranslations("exclusions");
  const style = STYLE[contact.status];
  const key = messageKey(contactLabelKey(contact));
  const label = t.has(key) ? t(key) : humanize(contactLabelKey(contact));
  const reason =
    contact.status === "removed" && contact.removedReason
      ? exclusions.has(messageKey(contact.removedReason))
        ? exclusions(messageKey(contact.removedReason))
        : humanize(contact.removedReason)
      : null;

  const body = (
    <>
      <span aria-hidden="true" className={`absolute inset-y-0 left-0 w-1 ${style.band}`} />
      <div lang="hi" className="truncate text-small font-semibold text-ink">
        {contact.farmerName ?? t("unknown")}
      </div>
      <div className={`font-mono text-micro ${style.text}`}>···{contact.last4}</div>
      <div className={`truncate text-tiny ${style.text}`}>{label}</div>
    </>
  );
  const className = `relative flex h-[84px] flex-col justify-between overflow-hidden rounded-btn border py-2 pl-[11px] pr-1.5 ${style.card}`;
  const title = reason ? `${label} · ${reason}` : label;

  return (
    <li>
      {contact.callId ? (
        <Link href={`/calls/${contact.callId}`} className={`${className} hover:border-ink`} title={title}>
          {body}
        </Link>
      ) : (
        <div className={className} title={title}>
          {body}
        </div>
      )}
    </li>
  );
}
