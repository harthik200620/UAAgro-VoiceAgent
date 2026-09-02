import { getTranslations } from "next-intl/server";

import { Card } from "./card";

/** What a page renders when the session lacks the capability it needs. Every page checks; the sidebar merely hides. */
export async function Forbidden() {
  const t = await getTranslations("errors");
  return (
    <Card className="max-w-[640px] p-6" role="alert">
      <h1 className="text-md font-semibold">{t("forbiddenTitle")}</h1>
      <p className="mt-2 text-ui text-muted">{t("forbidden")}</p>
    </Card>
  );
}
