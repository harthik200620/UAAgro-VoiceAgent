import { getTranslations } from "next-intl/server";

import { Card } from "@/components/ui/card";
import { Icon, type IconName } from "@/components/ui/icon";
import type { CallEvent } from "@/lib/contract";
import { formatClock } from "@/lib/format";

const ICONS: Record<string, IconName> = {
  dtmf: "keypad",
  whatsapp_sent: "whatsapp",
  transfer: "transfer",
  ended: "phone",
};

/** What happened on the call besides talking -- a key pressed, a message sent, a hand-over -- as a timeline. */
export async function EventsCard({ events }: { events: CallEvent[] }) {
  const t = await getTranslations("call.events");

  return (
    <Card className="flex flex-col gap-2.5 px-5.5 py-4">
      <div className="text-small font-medium text-muted">{t("title")}</div>
      {events.length === 0 ? (
        <p className="text-body text-muted">{t("empty")}</p>
      ) : (
        <ol className="flex flex-col gap-2">
          {events.map((event, index) => (
            <li key={`${event.type}-${index}`} className="flex items-start gap-2.5 text-body">
              <span className="w-11 shrink-0 font-mono text-meta text-faint">{formatClock(event.at)}</span>
              <Icon name={ICONS[event.type] ?? "clock"} size={14} className="mt-0.5 text-muted" />
              <span className="min-w-0 flex-1">{event.text}</span>
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}
