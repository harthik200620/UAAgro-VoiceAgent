"use client";

import { clsx } from "clsx";
import { useTranslations } from "next-intl";
import { useState, useTransition } from "react";

import { restoreVersion } from "@/app/actions/flows";
import { Card } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { useRouter } from "@/i18n/routing";
import { formatDate, formatTime } from "@/lib/format";

export type VersionRow = {
  id: string;
  version: number;
  isPublished: boolean;
  publishedAt: string | null;
  publishedByName: string | null;
  changelog: string | null;
};

/**
 * Every version of this script, newest first. "Restore" makes a new draft
 * with an old version's words and opens it -- the old one stays as it was,
 * so history is never rewritten.
 */
export function VersionsCard({ versions, currentId }: { versions: VersionRow[]; currentId: string }) {
  const t = useTranslations("flows.versions");
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  const restore = (id: string) =>
    startTransition(async () => {
      setError(null);
      const result = await restoreVersion(id);
      if (result.ok) {
        router.push(`/flows/${result.value.id}`);
        router.refresh();
      } else setError(result.message);
    });

  return (
    <Card className="flex flex-col px-5 pb-1.5 pt-4">
      <div className="mb-1.5 font-semibold">{t("title")}</div>
      {versions.map((version) => (
        <div
          key={version.id}
          className="flex items-center justify-between gap-3 border-b border-inset py-2 text-body last:border-b-0"
        >
          <div className="min-w-0">
            <div className={clsx(version.id === currentId ? "font-semibold" : "font-medium")}>
              v{version.version}
              {version.id === currentId ? <span className="ml-1.5 text-label text-faint">{t("open")}</span> : null}
            </div>
            <div className="truncate text-label text-muted">
              {version.publishedAt
                ? t("publishedOn", {
                    date: formatDate(version.publishedAt),
                    time: formatTime(version.publishedAt),
                  })
                : t("draft")}
              {version.publishedByName ? ` · ${version.publishedByName}` : ""}
              {version.changelog ? ` · ${version.changelog}` : ""}
            </div>
          </div>
          {version.isPublished ? (
            <Chip tone="green">{t("live")}</Chip>
          ) : (
            <button
              type="button"
              disabled={pending}
              onClick={() => restore(version.id)}
              className="relative text-small text-muted after:absolute after:-inset-2.5 after:content-[''] hover:text-ink disabled:opacity-50"
            >
              {t("restore")}
            </button>
          )}
        </div>
      ))}
      {error ? (
        <p role="alert" className="py-2 text-small text-red-text">
          {error}
        </p>
      ) : null}
    </Card>
  );
}
