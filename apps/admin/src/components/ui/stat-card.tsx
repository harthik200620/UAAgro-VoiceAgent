import { Card } from "./card";

/** One of the numbers across the top of a screen: a label, a serif numeral, and a line under it. */
export function StatCard({
  label,
  value,
  unit,
  note,
}: {
  label: string;
  value: string;
  unit?: string;
  note: string;
}) {
  return (
    <Card className="flex flex-1 flex-col gap-1.5 px-5.5 pb-4 pt-4.5">
      <div className="text-small font-medium tracking-[.01em] text-muted">{label}</div>
      <div className="font-serif text-num-lg">
        {value}
        {unit ? <span className="ml-1.5 text-xl text-muted">{unit}</span> : null}
      </div>
      <div className="text-small text-muted">{note}</div>
    </Card>
  );
}
