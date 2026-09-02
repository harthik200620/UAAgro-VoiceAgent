import type { FlowScript, FlowType, FlowVersion, OutboundScript } from "./contract";

/**
 * Scripts as the Flows screen thinks of them: one name, many versions, one
 * of them live. The API returns versions; this folds them into groups so
 * the left-hand list can show "डीएपी ऑफ़र — v3 · live" once instead of
 * three times.
 */

type GroupState = "live" | "draft" | "retired";

export type ScriptGroup = {
  name: string;
  flowType: FlowType;
  /** Newest first. */
  versions: FlowVersion[];
  live: FlowVersion | null;
  newest: FlowVersion;
  state: GroupState;
};

export function isOutboundScript(script: FlowScript): script is OutboundScript {
  return "opening" in script;
}

export function groupScripts(flows: readonly FlowVersion[]): ScriptGroup[] {
  const byName = new Map<string, FlowVersion[]>();
  for (const flow of flows) {
    byName.set(flow.name, [...(byName.get(flow.name) ?? []), flow]);
  }

  const groups: ScriptGroup[] = [];
  for (const [name, members] of byName) {
    const versions = [...members].sort((a, b) => b.version - a.version);
    const newest = versions[0];
    if (!newest) continue;
    const live = versions.find((version) => version.isPublished) ?? null;
    const wasLive = versions.some((version) => version.publishedAt !== null);
    groups.push({
      name,
      flowType: newest.flowType,
      versions,
      live,
      newest,
      state: live ? "live" : wasLive ? "retired" : "draft",
    });
  }

  // What is on the phones first, then what is being worked on, then history.
  const rank: Record<GroupState, number> = { live: 0, draft: 1, retired: 2 };
  return groups.sort(
    (a, b) => rank[a.state] - rank[b.state] || b.newest.updatedAt.localeCompare(a.newest.updatedAt),
  );
}

/** The number the next draft will carry. */
export function nextVersion(versions: readonly FlowVersion[]): number {
  return versions.reduce((max, version) => Math.max(max, version.version), 0) + 1;
}
