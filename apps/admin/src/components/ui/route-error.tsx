"use client";

import { useTranslations } from "next-intl";

import { Button } from "./button";
import { Card } from "./card";

/**
 * The card a screen turns into when its data could not be loaded. Shared by
 * every `error.tsx`. The message is whatever reached the boundary -- in
 * production Next replaces server-side detail with a generic line, which is
 * the right trade for a panel whose errors mention internal hosts.
 */
export function RouteError({ error, reset }: { error: Error; reset: () => void }) {
  const t = useTranslations("errors");
  return (
    <Card className="max-w-[640px] p-6" role="alert">
      <h2 className="text-md font-semibold">{t("title")}</h2>
      <p className="mt-2 text-ui text-muted">{error.message || t("generic")}</p>
      <div className="mt-4">
        <Button variant="primary" onClick={reset}>
          {t("retry")}
        </Button>
      </div>
    </Card>
  );
}
