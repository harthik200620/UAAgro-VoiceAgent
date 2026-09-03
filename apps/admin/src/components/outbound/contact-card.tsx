import { clsx } from "clsx";
import { useTranslations } from "next-intl";

import { Link } from "@/i18n/routing";
import type { Contact, ContactStatus } from "@/lib/contract";
import { contactLabelKey, humanize, messageKey } from "@/lib/tones";

/**
 * One farmer on the wall. Paper while waiting, an amber outline while the
 * phone rings, amber and pulsing during the call, green when done, red when
 * not reached, muted grey when the gate removed them. The 4px band on the
 * left carries the same state for a reader who cannot see the fill.
 *
 * A card with a call behind it opens the call; in simulator mode a ringing
 * card also carries the link that answers it from a browser tab, which is
 * how a demo runs without a telephony account.
 */
const STYLE: Record<ContactStatus, { card: string; band: string; text: string }> = {
  waiting: { card: "border-line bg-surface", band: "bg-grey", text: "text-muted" },
  ringing: { card: "border-amber bg-surface", band: "bg-amber", text: "text-amber-text" },
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
  const name = contact.farmerName ?? t("unknown");

  return (
    <li
      title={reason ? `${label} · ${reason}` : label}
      className={clsx(
        "relative flex h-[84px] flex-col justify-between overflow-hidden rounded-btn border py-2 pl-[11px] pr-1.5",
        style.card,
        contact.callId && "hover:border-ink",
      )}
    >
      <span aria-hidden="true" className={`absolute inset-y-0 left-0 w-1 ${style.band}`} />
      <div className="truncate text-small font-semibold text-ink">
        {contact.callId ? (
          // The link is stretched over the whole card; the answer link below sits above it.
          <Link href={`/calls/${contact.callId}`} className="after:absolute after:inset-0 after:content-['']">
            {name}
          </Link>
        ) : (
          name
        )}
      </div>
      <div className={`font-mono text-micro ${style.text}`}>···{contact.last4}</div>
      <div className="flex items-center justify-between gap-1">
        <span className={`truncate text-tiny ${style.text}`}>{label}</span>
        {contact.answerUrl ? (
          <a
            href={contact.answerUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="relative z-10 shrink-0 text-tiny font-semibold text-ink underline underline-offset-2"
          >
            {t("answer")}
          </a>
        ) : null}
      </div>
    </li>
  );
}
