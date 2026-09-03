"use client";

import { useTranslations } from "next-intl";
import { useState, useTransition, type ReactNode } from "react";

import { publishScript, saveScript } from "@/app/actions/flows";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Dialog } from "@/components/ui/dialog";
import { useRouter } from "@/i18n/routing";
import type { FlowScript, FlowType, InboundScript, OutboundScript } from "@/lib/contract";
import { isOutboundScript } from "@/lib/flows";

import { PreviewPanel } from "./preview-panel";
import { PromptEditor } from "./prompt-editor";
import { LockedText, ScriptStep, ScriptText } from "./script-step";
import { TestCallDialog } from "./test-call-dialog";
import { VersionsCard, type VersionRow } from "./versions-card";

type WorkbenchFlow = {
  id: string;
  name: string;
  flowType: FlowType;
  version: number;
  isPublished: boolean;
  publishedAt: string | null;
};

/** The helpline's persona, for an inbound flow; outbound flows carry none to edit. */
export type EditablePrompt = { text: string; defaultText: string | null };

/**
 * The script editor and its two side cards, sharing one piece of state: the
 * words as they are now -- and, for the helpline, the persona behind them.
 * Nothing changes on the phones until Publish, and Publish is confirmed;
 * editing a live version makes a draft, so the live one is never touched in
 * place.
 */
export function ScriptWorkbench({
  flow,
  script: initial,
  prompt,
  versions,
  nextVersion,
  canPublish,
  children,
}: {
  flow: WorkbenchFlow;
  script: FlowScript;
  prompt: EditablePrompt | null;
  versions: VersionRow[];
  nextVersion: number;
  canPublish: boolean;
  /** Server-rendered extras under the steps. */
  children?: ReactNode;
}) {
  const t = useTranslations("flows.editor");
  const router = useRouter();
  const [script, setScript] = useState<FlowScript>(initial);
  const [promptText, setPromptText] = useState(prompt?.text ?? "");
  const [testing, setTesting] = useState(false);
  const [confirmingPublish, setConfirmingPublish] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const scriptDirty = JSON.stringify(script) !== JSON.stringify(initial);
  const promptDirty = prompt !== null && promptText !== prompt.text;
  const dirty = scriptDirty || promptDirty;
  const promptToSave = promptDirty ? promptText : null;
  const publishVersion = flow.isPublished ? nextVersion : flow.version;

  const open = (id: string) => {
    router.push(`/flows/${id}`);
    router.refresh();
  };

  const save = () =>
    startTransition(async () => {
      setError(null);
      const result = await saveScript(flow.id, script, flow.isPublished, promptToSave);
      if (result.ok) open(result.value.id);
      else setError(result.message);
    });

  const publish = () =>
    startTransition(async () => {
      setError(null);
      setConfirmingPublish(false);
      const result = await publishScript(flow.id, dirty ? script : null, flow.isPublished, promptToSave);
      if (result.ok) open(result.value.id);
      else setError(result.message);
    });

  const state = flow.isPublished ? "live" : flow.publishedAt ? "retired" : "draft";
  const previewText = isOutboundScript(script) ? script.message : script.greetingKnown;

  return (
    <>
      <Card className="flex min-w-[520px] flex-1 flex-col gap-6 px-6.5 pb-6 pt-5.5">
        {/* The title keeps a full line; the actions drop below it rather than squeeze it. */}
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 grow basis-[420px]">
            <div className="flex flex-wrap items-center gap-2.5">
              <span className="text-xl font-semibold">{flow.name}</span>
              <Chip tone={state === "live" ? "green" : "grey"}>
                v{flow.version} · {t(`state.${state}`)}
              </Chip>
            </div>
            <div className="mt-1 text-body text-muted">{t("explainer")}</div>
          </div>
          <div className="flex shrink-0 gap-2">
            <Button icon="phone" onClick={() => setTesting(true)}>
              {t("testOnPhone")}
            </Button>
            {dirty ? (
              <Button icon="check" disabled={pending} onClick={save}>
                {flow.isPublished ? t("saveAsDraft", { version: nextVersion }) : t("saveDraft")}
              </Button>
            ) : null}
            {canPublish && (dirty || !flow.isPublished) ? (
              <Button variant="primary" icon="check" disabled={pending} onClick={() => setConfirmingPublish(true)}>
                {t("publishAs", { version: publishVersion })}
              </Button>
            ) : null}
          </div>
        </div>

        {error ? (
          <p role="alert" className="text-ui text-red-text">
            {error}
          </p>
        ) : null}

        {isOutboundScript(script) ? (
          <OutboundSteps script={script} onChange={setScript} />
        ) : (
          <InboundSteps script={script} onChange={setScript} />
        )}

        {prompt ? (
          <PromptEditor
            value={promptText}
            defaultValue={prompt.defaultText}
            disabled={pending}
            onChange={setPromptText}
          />
        ) : null}

        {children}
      </Card>

      {/* Beside the editor only on a wide screen; below it otherwise, so the steps never get narrow. */}
      <div className="flex w-full shrink-0 flex-col gap-4 2xl:w-[280px]">
        <PreviewPanel flowId={flow.id} text={previewText} step={isOutboundScript(script) ? 3 : 1} />
        <VersionsCard versions={versions} currentId={flow.id} />
      </div>

      <TestCallDialog flowId={flow.id} open={testing} onClose={() => setTesting(false)} />

      <Dialog
        open={confirmingPublish}
        onClose={() => setConfirmingPublish(false)}
        title={t("publishAs", { version: publishVersion })}
      >
        <p className="text-ui text-muted">{t("publishQuestion", { type: flow.flowType })}</p>
        {promptDirty ? <p className="text-ui text-muted">{t("publishPromptNote")}</p> : null}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setConfirmingPublish(false)}>
            {t("cancel")}
          </Button>
          <Button variant="primary" icon="check" disabled={pending} onClick={publish}>
            {t("publish")}
          </Button>
        </div>
      </Dialog>
    </>
  );
}

function OutboundSteps({
  script,
  onChange,
}: {
  script: OutboundScript;
  onChange: (script: OutboundScript) => void;
}) {
  const t = useTranslations("flows.steps");
  const set = (key: keyof OutboundScript) => (value: string) => onChange({ ...script, [key]: value });

  return (
    <>
      <ScriptStep number={1} title={t("opening")} lockedLabel={t("lockedByLaw")}>
        <LockedText value={script.opening} />
      </ScriptStep>
      <ScriptStep number={2} title={t("askTime")} hint={t("askTimeHint")}>
        <ScriptText value={script.askTime} onChange={set("askTime")} label={t("askTime")} />
      </ScriptStep>
      <ScriptStep number={3} title={t("message")} hint={t("messageHint")}>
        <ScriptText value={script.message} onChange={set("message")} label={t("message")} />
      </ScriptStep>
      <ScriptStep number={4} title={t("thenAsk")}>
        <ScriptText value={script.thenAsk} onChange={set("thenAsk")} label={t("thenAsk")} />
      </ScriptStep>
      <ScriptStep number={5} title={t("buttons")}>
        <div className="flex flex-col gap-3.5">
          <Key digit="1" description={t("key1")}>
            <ScriptText value={script.onPress1} onChange={set("onPress1")} label={t("key1")} />
          </Key>
          <Key digit="2" description={t("key2")}>
            <p className="text-body leading-[1.55]">
              {t.rich("key2Detail", { b: (chunks) => <b>{chunks}</b> })}
            </p>
          </Key>
          <Key digit="9" description={t("key9")}>
            <LockedText value={script.optOut} />
          </Key>
        </div>
      </ScriptStep>
      <ScriptStep number={6} title={t("closing")}>
        <ScriptText value={script.closing} onChange={set("closing")} label={t("closing")} />
      </ScriptStep>
    </>
  );
}

function InboundSteps({
  script,
  onChange,
}: {
  script: InboundScript;
  onChange: (script: InboundScript) => void;
}) {
  const t = useTranslations("flows.inboundSteps");
  const set = (key: keyof InboundScript) => (value: string) => onChange({ ...script, [key]: value });

  return (
    <>
      <ScriptStep number={1} title={t("greetingKnown")} hint={t("greetingKnownHint")}>
        <ScriptText value={script.greetingKnown} onChange={set("greetingKnown")} label={t("greetingKnown")} />
      </ScriptStep>
      <ScriptStep number={2} title={t("greetingUnknown")} hint={t("greetingUnknownHint")}>
        <ScriptText value={script.greetingUnknown} onChange={set("greetingUnknown")} label={t("greetingUnknown")} />
      </ScriptStep>
      <ScriptStep number={3} title={t("closing")} hint={t("closingHint")}>
        <ScriptText value={script.closing} onChange={set("closing")} label={t("closing")} />
      </ScriptStep>
    </>
  );
}

function Key({ digit, description, children }: { digit: string; description: string; children: ReactNode }) {
  return (
    <div className="flex items-start gap-3">
      <span className="flex h-8.5 w-8.5 shrink-0 items-center justify-center rounded-btn border border-line bg-surface font-mono font-medium">
        {digit}
      </span>
      <div className="flex flex-1 flex-col gap-1.5">
        <div className="text-body text-muted">{description}</div>
        {children}
      </div>
    </div>
  );
}
