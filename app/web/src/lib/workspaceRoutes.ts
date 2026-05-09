interface LocationSuffix {
  search?: string;
  hash?: string;
}

function withPrefix(value: string, prefix: "?" | "#"): string {
  if (value === "") return "";
  return value.startsWith(prefix) ? value : prefix + value;
}

function splitRoutePath(routePath: string): { pathname: string; search: string; hash: string } {
  const hashIndex = routePath.indexOf("#");
  const beforeHash = hashIndex === -1 ? routePath : routePath.slice(0, hashIndex);
  const hash = hashIndex === -1 ? "" : routePath.slice(hashIndex);
  const searchIndex = beforeHash.indexOf("?");
  const pathname = searchIndex === -1 ? beforeHash : beforeHash.slice(0, searchIndex);
  const search = searchIndex === -1 ? "" : beforeHash.slice(searchIndex);
  return {
    pathname: pathname.startsWith("/") ? pathname : "/" + pathname,
    search,
    hash,
  };
}

export function workspaceRoute(
  workspaceSlug: string | null | undefined,
  routePath: string,
  suffix: LocationSuffix = {},
): string {
  const parsed = splitRoutePath(routePath || "/");
  const search = suffix.search === undefined ? parsed.search : withPrefix(suffix.search, "?");
  const hash = suffix.hash === undefined ? parsed.hash : withPrefix(suffix.hash, "#");
  if (!workspaceSlug) return parsed.pathname + search + hash;

  const slug = encodeURIComponent(workspaceSlug);
  const pathname = parsed.pathname === "/" ? "" : parsed.pathname;
  return `/w/${slug}${pathname}${search}${hash}`;
}

export function workspaceSlugFromRoutePath(pathname: string): string | null {
  const match = /^\/w\/([^/]+)(?:\/|$)/.exec(pathname);
  if (!match?.[1]) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

export function workspaceRelativePathname(pathname: string): string {
  const match = /^\/w\/[^/]+(?=\/|$)/.exec(pathname);
  if (!match) return pathname;
  const relative = pathname.slice(match[0].length);
  return relative || "/";
}

export function workspaceRouteForPathname(pathname: string, routePath: string): string {
  return workspaceRoute(workspaceSlugFromRoutePath(pathname), routePath);
}
