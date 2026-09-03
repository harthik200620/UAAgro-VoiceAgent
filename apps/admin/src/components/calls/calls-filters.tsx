import { getTranslations } from "next-intl/server";

import { Button } from "@/components/ui/button";
import { Field, inputClass } from "@/components/ui/field";
import { Link } from "@/i18n/routing";
import type { CentreRow } from "@/lib/contract";
import { KNOWN_OUTCOMES } from "@/lib/tones";

export type CallFilters = {
  from?: string;
  to?: string;
  direction?: string;
  outcome?: string;
  centre?: string;
  q?: string;
  /** "1" when test calls are to be listed too. */
  test?: string;
};

/**
 * A GET form: submitting changes the URL, which is what makes "the failed
 * calls from Barabanki yesterday" something a manager can paste into a chat
 * rather than describe in words. No JavaScript is involved.
 */
export async function CallsFilters({
  values,
  centres,
}: {
  values: CallFilters;
  centres: CentreRow[];
}) {
  const t = await getTranslations("calls.filters");
  const outcomes = await getTranslations("outcomes");

  return (
    <form method="get" className="flex flex-wrap items-end gap-3">
      <Field label={t("from")} className="w-40">
        <input type="date" name="from" defaultValue={values.from ?? ""} className={inputClass} />
      </Field>
      <Field label={t("to")} className="w-40">
        <input type="date" name="to" defaultValue={values.to ?? ""} className={inputClass} />
      </Field>
      <Field label={t("direction")} className="w-36">
        <select name="direction" defaultValue={values.direction ?? ""} className={inputClass}>
          <option value="">{t("any")}</option>
          <option value="inbound">{t("inbound")}</option>
          <option value="outbound">{t("outbound")}</option>
        </select>
      </Field>
      <Field label={t("outcome")} className="w-44">
        <input
          type="text"
          name="outcome"
          list="call-outcomes"
          defaultValue={values.outcome ?? ""}
          placeholder={t("outcomePlaceholder")}
          className={inputClass}
        />
        <datalist id="call-outcomes">
          {KNOWN_OUTCOMES.map((outcome) => (
            <option key={outcome} value={outcome}>
              {outcomes.has(outcome) ? outcomes(outcome) : outcome}
            </option>
          ))}
        </datalist>
      </Field>
      {centres.length > 0 ? (
        <Field label={t("centre")} className="w-48">
          <select name="centre" defaultValue={values.centre ?? ""} className={inputClass}>
            <option value="">{t("any")}</option>
            {centres.map((centre) => (
              <option key={centre.id} value={centre.id}>
                {centre.district} · {centre.code}
              </option>
            ))}
          </select>
        </Field>
      ) : null}
      <Field label={t("search")} className="min-w-[220px] flex-1">
        <input
          type="search"
          name="q"
          defaultValue={values.q ?? ""}
          placeholder={t("searchPlaceholder")}
          className={inputClass}
        />
      </Field>
      <label className="flex items-center gap-2 pb-2.5 text-small text-muted">
        <input type="checkbox" name="test" value="1" defaultChecked={values.test === "1"} className="h-4 w-4" />
        {t("includeTest")}
      </label>
      <div className="flex items-center gap-2 pb-px">
        <Button type="submit" variant="primary" size="md">
          {t("apply")}
        </Button>
        <Link href="/calls" className="px-2 py-2 text-small text-muted hover:text-ink">
          {t("clear")}
        </Link>
      </div>
    </form>
  );
}
