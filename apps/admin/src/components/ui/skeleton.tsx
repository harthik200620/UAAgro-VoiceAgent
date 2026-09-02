/**
 * What a screen looks like for the half second before its data arrives:
 * the same paper, the same card shapes, no text. Shared by every `loading.tsx`.
 */
function Skeleton({ className }: { className: string }) {
  return <div aria-hidden="true" className={`animate-pulse rounded-panel bg-inset ${className}`} />;
}

export function PageSkeleton() {
  return (
    <div className="flex flex-col gap-5.5" aria-busy="true">
      <div className="flex flex-col gap-2">
        <Skeleton className="h-10 w-48" />
        <Skeleton className="h-4 w-96" />
      </div>
      <div className="flex gap-4">
        <Skeleton className="h-28 flex-1 rounded-card" />
        <Skeleton className="h-28 flex-1 rounded-card" />
        <Skeleton className="h-28 flex-1 rounded-card" />
      </div>
      <Skeleton className="h-96 rounded-card" />
    </div>
  );
}
