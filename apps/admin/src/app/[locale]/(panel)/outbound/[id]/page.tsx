import { CampaignBoard } from "@/components/outbound/campaign-board";
import { Forbidden } from "@/components/ui/forbidden";
import { can } from "@/lib/rbac";
import { getCampaign } from "@/server/api";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/** One campaign: fetched once here, then followed live by the board. */
export default async function CampaignPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const session = await currentSession();
  if (!session || !can(session, "campaigns.view")) return <Forbidden />;

  const campaign = await getCampaign(session, id);

  return (
    <CampaignBoard
      campaign={campaign}
      canControl={can(session, "campaigns.control")}
      canApprove={can(session, "campaigns.approve")}
      canCreate={can(session, "campaigns.create")}
    />
  );
}
