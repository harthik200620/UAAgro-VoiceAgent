"use client";

import { useEffect, useRef, type ReactNode } from "react";

/**
 * A modal on the native `<dialog>`: focus is trapped, Escape closes, the
 * backdrop dims the page, and nothing here reimplements what the browser
 * already does correctly.
 */
export function Dialog({
  open,
  onClose,
  title,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    if (open && !element.open) element.showModal();
    if (!open && element.open) element.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      onClose={onClose}
      className="w-[440px] max-w-[calc(100vw-2rem)] rounded-card border border-line bg-surface p-6 text-ink shadow-card backdrop:bg-ink/30"
    >
      <h2 className="text-md font-semibold">{title}</h2>
      <div className="mt-3 flex flex-col gap-3.5">{children}</div>
    </dialog>
  );
}
