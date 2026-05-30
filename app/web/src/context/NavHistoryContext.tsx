import {
  createContext,
  use,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useLocation, useNavigationType } from "react-router-dom";

// Tracks in-app navigations since the page first loaded. The header
// back button uses this to choose between real browser back and the
// static parent map (cold-load deep links). Without it, every
// /w/<slug>/task/:id back button goes to /w/<slug>/today regardless
// of where the user came from, see `routeParents.ts`.
//
// Counts:
//   PUSH    → append (Link click, navigate(to))
//   POP     → move to a known entry (browser back/forward, navigate(-1))
//   REPLACE → replace current entry (Navigate replace, navigate(to, {replace: true}))
//
// Query/hash-only same-path entries stay in the local stack so the
// header can skip them instead of walking through tabs or filters.
interface NavHistoryValue {
  canGoBack: boolean;
  backTarget: string | null;
}

interface NavEntry {
  key: string;
  pathname: string;
  href: string;
}

interface NavStack {
  entries: NavEntry[];
  index: number;
}

const Ctx = createContext<NavHistoryValue>({ canGoBack: false, backTarget: null });

export function NavHistoryProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const navType = useNavigationType();
  const [stack, setStack] = useState<NavStack>(() => ({
    entries: [entryFromLocation(location)],
    index: 0,
  }));

  useEffect(() => {
    const entry = entryFromLocation(location);
    setStack((current) => {
      if (navType === "PUSH") {
        return {
          entries: [...current.entries.slice(0, current.index + 1), entry],
          index: current.index + 1,
        };
      }

      if (navType === "REPLACE") {
        const entries = [...current.entries];
        entries[current.index] = entry;
        return { entries, index: current.index };
      }

      const index = current.entries.findIndex(
        (item) => item.key === entry.key && item.href === entry.href,
      );
      if (index === current.index) return current;
      if (index === -1) {
        const entries = [...current.entries];
        const currentEntry = entries[current.index];
        if (currentEntry?.pathname !== entry.pathname) return current;
        entries[current.index] = entry;
        return { entries, index: current.index };
      }
      return { entries: current.entries, index };
    });
  }, [location, navType]);

  const backTarget = useMemo(() => previousPathnameTarget(stack), [stack]);
  const value = useMemo(
    () => ({ canGoBack: stack.index > 0, backTarget }),
    [backTarget, stack.index],
  );

  return (
    <Ctx.Provider value={value}>
      {children}
    </Ctx.Provider>
  );
}

export function useNavHistory(): NavHistoryValue {
  return use(Ctx);
}

function entryFromLocation(location: ReturnType<typeof useLocation>): NavEntry {
  return {
    key: location.key,
    pathname: location.pathname,
    href: location.pathname + location.search + location.hash,
  };
}

function previousPathnameTarget(stack: NavStack): string | null {
  const current = stack.entries[stack.index];
  if (!current) return null;

  for (let index = stack.index - 1; index >= 0; index -= 1) {
    if (stack.entries[index]?.pathname !== current.pathname) {
      return stack.entries[index]?.href ?? null;
    }
  }
  return null;
}
