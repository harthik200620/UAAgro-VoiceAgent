import { getTranslations } from "next-intl/server";

/**
 * §9 and §15.1: a permanent banner counting unapproved rows.
 *
 * Permanent, with no dismiss control. That is the whole design: a banner an
 * operator can close is closed on day one, and the queue behind it is never
 * looked at again. The failure that produces is quiet and expensive -- an
 * agronomy team believing its content is live while the agent refuses every
 * dosage question and escalates instead.
 *
 * The wording says what the agent will actually do, not that rows are pending.
 * "12 recommendations awaiting approval" reads like a backlog; "the agent will
 * never speak these until an agronomist signs them off" reads like a system
 * that is currently unable to answer, which is the truth.
 */
export async function UnapprovedBanner({ count }: { count: number }) {
  const t = await getTranslations("advisory");

  if (count === 0) {
    return (
      <p className="mb-4 rounded border border-ok/30 bg-ok/5 px-3 py-2 text-sm text-ok">
        {t("allApproved")}
      </p>
    );
  }

  return (
    <p
      role="status"
      className="mb-4 rounded border border-warn/40 bg-warn/10 px-3 py-2 text-sm font-medium text-warn"
    >
      {t("unapprovedBanner", { count })}
    </p>
  );
}
