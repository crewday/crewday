export async function registerWorkspaceServiceWorker(workspaceSlug: string): Promise<void> {
  if (import.meta.env.DEV) return;
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;

  const slug = encodeURIComponent(workspaceSlug);
  await navigator.serviceWorker.register("/sw.js", {
    scope: `/w/${slug}/`,
  });
}
