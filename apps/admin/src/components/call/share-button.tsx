"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";

/** Copies this call's link. Nothing leaves the browser; the panel is behind sign-in either way. */
export function ShareButton() {
  const t = useTranslations("call");
  const [copied, setCopied] = useState(false);

  return (
    <Button
      icon="link"
      size="md"
      onClick={() => {
        void navigator.clipboard.writeText(window.location.href).then(() => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 2000);
        });
      }}
      aria-live="polite"
    >
      {copied ? t("copied") : t("share")}
    </Button>
  );
}
