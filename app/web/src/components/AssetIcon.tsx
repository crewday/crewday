import { Package, type LucideIcon } from "lucide-react";
import { useEffect, useState } from "react";
import {
  ASSET_ICON_LOADERS,
  ASSET_ICON_REGISTRY,
  isAssetIconName,
  type AssetIconName,
} from "@/components/AssetIcon.registry";

const loadedLazyIcons = new Map<AssetIconName, LucideIcon>();
type LoadedLazyIcon = { name: AssetIconName; icon: LucideIcon };

export function AssetIcon({
  name,
  size = 16,
  className,
}: {
  name: string | null | undefined;
  size?: number;
  className?: string;
}) {
  const iconName = isAssetIconName(name) ? name : null;
  const eagerIcon = iconName ? ASSET_ICON_REGISTRY[iconName] : undefined;
  const [lazyIcon, setLazyIcon] = useState<LoadedLazyIcon | null>(null);
  const cachedLazyIcon = iconName ? loadedLazyIcons.get(iconName) : undefined;
  const activeLazyIcon = lazyIcon?.name === iconName ? lazyIcon.icon : null;
  const Icon = eagerIcon ?? cachedLazyIcon ?? activeLazyIcon ?? Package;

  useEffect(() => {
    if (!iconName || eagerIcon || cachedLazyIcon) {
      return;
    }

    let cancelled = false;
    void ASSET_ICON_LOADERS[iconName]()
      .then((module) => {
        loadedLazyIcons.set(iconName, module.default);
        if (!cancelled) {
          setLazyIcon({ name: iconName, icon: module.default });
        }
      })
      .catch(() => {
        // Failed lazy icon imports fall back to the package icon.
      });

    return () => {
      cancelled = true;
    };
  }, [cachedLazyIcon, eagerIcon, iconName]);

  return (
    <span className={"asset-icon" + (className ? " " + className : "")} aria-hidden="true">
      <Icon size={size} strokeWidth={1.75} />
    </span>
  );
}
