import { getTranslations } from "next-intl/server";

import type { CampaignGate, GateCheck } from "@/server/api";

const CHECKS: readonly GateCheck[] = [
  "consent",
  "dnd",
  "internal_dnc",
  "caller_id_series",
  "dlt_registration",
  "calling_window",
  "frequency_cap",
  "duplicate_suppression",
];

/**
 * §13.1's gate, rendered so an operator can act on it.
 *
 * The spec's requirement is precise: "the UI shows exactly how many contacts
 * each check removed". The reason is behavioural rather than decorative. An
 * operator shown "1,200 contacts → 340 eligible" and nothing else concludes the
 * gate is broken and starts looking for a way around it. One shown "DND removed
 * 610, expired consent removed 190" goes and fixes the consent problem.
 *
 * So every check is listed, including the ones that removed nothing: a zero is
 * information -- the scrub ran and found nothing -- and an omitted row is
 * indistinguishable from a check that did not run.
 *
 * Campaign-level blocks are shown separately and above, because they are a
 * different kind of failure. A blocked campaign is not one with fewer eligible
 * contacts; it is one that must not run at all, and burying "wrong caller ID
 * series" in a list of contact counts invites an operator to read it as another
 * filter.
 */
export async function ComplianceGate({ gate }: { gate: CampaignGate }) {
  const t = await getTranslations("campaigns");
  const blocked = gate.blockedBy.length > 0;

  return (
    <section aria-labelledby="gate-heading" className="mb-6">
      <h2 id="gate-heading" className="mb-2 text-base font-semibold">
        {t("complianceGate")}
      </h2>

      {blocked ? (
        <div
          role="alert"
          className="mb-3 rounded border border-danger/40 bg-danger/10 px-3 py-2 text-sm"
        >
          <p className="font-medium text-danger">{t("blocked")}</p>
          <ul className="mt-1 list-inside list-disc text-danger">
            {gate.blockedBy.map((check) => (
              <li key={check}>{t(`checks.${check}`)}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <p className="mb-2 text-sm">
        {t("gateSummary", { total: gate.total, eligible: gate.eligible })}
      </p>

      <table className="grid-dense w-full max-w-md border-collapse text-sm">
        <tbody>
          {CHECKS.map((check) => {
            const removed = gate.removed[check] ?? 0;
            const isBlocking = gate.blockedBy.includes(check);
            return (
              <tr
                key={check}
                className="border-b border-slate-100 dark:border-slate-800"
              >
                <th scope="row" className="font-normal">
                  {t(`checks.${check}`)}
                </th>
                <td
                  className={
                    isBlocking
                      ? "text-right font-medium text-danger"
                      : removed > 0
                        ? "text-right text-warn"
                        : "text-right text-muted"
                  }
                >
                  {isBlocking ? "—" : removed}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <dl className="mt-3 flex gap-6 text-sm">
        <div>
          <dt className="text-muted">{t("estimatedCost")}</dt>
          {/* Shown before approval (§13.1). A marketing template is roughly
              7.5x a utility one, so this number is the difference between a
              campaign an operator approves and one they reconsider. */}
          <dd className="font-medium">₹{gate.estimatedCostRupees.toFixed(2)}</dd>
        </div>
        <div>
          <dt className="text-muted">{t("estimatedDuration")}</dt>
          <dd className="font-medium">{gate.estimatedMinutes} min</dd>
        </div>
      </dl>
    </section>
  );
}
