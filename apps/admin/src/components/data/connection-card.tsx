import { getTranslations } from "next-intl/server";
import type { ReactNode } from "react";

import { Card } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Icon } from "@/components/ui/icon";
import type { ConnectionInfo } from "@/lib/contract";
import { formatMs } from "@/lib/format";

import { ConnectionTestForm } from "./connection-test-form";

/** The database, as the super_admin sees it: where, as whom, how busy. Never the password. */
export async function ConnectionCard({ connection }: { connection: ConnectionInfo }) {
  const t = await getTranslations("data.connection");

  const rows: { label: string; value: ReactNode }[] = [
    { label: t("host"), value: <Mono>{connection.host}</Mono> },
    { label: t("port"), value: <Mono>{String(connection.port)}</Mono> },
    { label: t("database"), value: <Mono>{connection.database}</Mono> },
    {
      label: t("user"),
      value: (
        <>
          <Mono>{connection.user}</Mono>{" "}
          <span className="text-meta text-muted">· {t("rls")}</span>
        </>
      ),
    },
    {
      label: t("encryption"),
      value: connection.tls ? t("tlsOn") : <span className="text-red-text">{t("tlsOff")}</span>,
    },
    { label: t("server"), value: <Mono>{connection.serverVersion}</Mono> },
    {
      label: t("connections"),
      value: t("pool", { size: connection.poolSize, busy: connection.poolBusy }),
    },
  ];

  return (
    <Card className="flex flex-col gap-1.5 px-5 py-4.5">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="inline-flex items-center gap-2 font-semibold">
          <Icon name="data" />
          {t("title")}
        </span>
        <Chip tone="green">{t("connected", { latency: formatMs(connection.latencyMs) })}</Chip>
      </div>
      {rows.map((row) => (
        <div key={row.label} className="flex justify-between gap-3 border-b border-inset py-[7px] text-body">
          <span className="text-muted">{row.label}</span>
          <span className="text-right">{row.value}</span>
        </div>
      ))}
      <div className="pt-3">
        <ConnectionTestForm />
      </div>
      <p className="text-meta text-faint">{t("note")}</p>
    </Card>
  );
}

function Mono({ children }: { children: string }) {
  return <span className="font-mono text-small">{children}</span>;
}
