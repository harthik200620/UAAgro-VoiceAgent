"use client";

import { useTranslations } from "next-intl";
import { useActionState, useState } from "react";

import { addCentre, type AddCentreState } from "@/app/actions/centres";
import { Button } from "@/components/ui/button";
import { Field, inputClass } from "@/components/ui/field";
import { Icon } from "@/components/ui/icon";

/**
 * "Add a centre", as drawn: name, district and block, pincode and state,
 * a typed location, the manager and their number, opening hours. The code
 * comes back from the API (`NKSK-<DIST>-<n>`) and is shown on success.
 */
export function AddCentreForm() {
  const t = useTranslations("centres.add");
  const [state, action, pending] = useActionState<AddCentreState, FormData>(addCentre, { status: "idle" });
  // Cancel remounts the form; the fields are uncontrolled and reset with it.
  const [formKey, setFormKey] = useState(0);

  return (
    <form key={formKey} action={action} className="flex flex-col gap-3.5">
      <div className="flex items-center gap-2 font-semibold">
        <Icon name="pin" />
        {t("title")}
      </div>
      <Field label={t("name")}>
        <input name="name" required maxLength={160} className={inputClass} placeholder={t("namePlaceholder")} />
      </Field>
      <div className="flex gap-2.5">
        <Field label={t("district")} className="w-1/2">
          <input name="district" required className={inputClass} />
        </Field>
        <Field label={t("block")} className="w-1/2">
          <input name="block" className={inputClass} />
        </Field>
      </div>
      <div className="flex gap-2.5">
        <Field label={t("pincode")} className="w-2/5">
          <input name="pincode" inputMode="numeric" pattern="[0-9]{6}" className={inputClass} />
        </Field>
        <Field label={t("state")} className="w-3/5">
          <input name="state" defaultValue="Uttar Pradesh" className={inputClass} />
        </Field>
      </div>
      <Field label={t("location")} hint={t("locationHint")}>
        <input name="location" placeholder="27.87, 81.50" className={`${inputClass} font-mono`} />
      </Field>
      <div className="flex gap-2.5">
        <Field label={t("manager")} className="w-1/2">
          <input name="managerName" placeholder={t("managerPlaceholder")} className={inputClass} />
        </Field>
        <Field label={t("managerNumber")} className="w-1/2">
          <input name="managerNumber" type="tel" placeholder="+91" className={`${inputClass} font-mono`} />
        </Field>
      </div>
      <div className="flex gap-2.5">
        <Field label={t("opens")} className="w-1/2">
          <input name="openTime" type="time" required defaultValue="08:00" className={inputClass} />
        </Field>
        <Field label={t("closes")} className="w-1/2">
          <input name="closeTime" type="time" required defaultValue="19:00" className={inputClass} />
        </Field>
      </div>

      {state.status === "added" ? (
        <p role="status" className="text-ui text-green-text">
          {t("added", { code: state.code })}
        </p>
      ) : null}
      {state.status === "error" ? (
        <p role="alert" className="text-ui text-red-text">
          {state.message}
        </p>
      ) : null}

      <div className="flex justify-end gap-2 pt-1">
        <Button variant="ghost" onClick={() => setFormKey((key) => key + 1)}>
          {t("cancel")}
        </Button>
        <Button type="submit" variant="primary" icon="plus" disabled={pending}>
          {t("submit")}
        </Button>
      </div>
    </form>
  );
}
