import {
  AlarmSmoke,
  Baby,
  Bell,
  Biohazard,
  BrushCleaning,
  Building2,
  Car,
  ChefHat,
  CookingPot,
  Droplets,
  Fan,
  FireExtinguisher,
  Flame,
  Heater,
  Package,
  Refrigerator,
  ShieldCheck,
  Snowflake,
  Sprout,
  Sun,
  ThermometerSun,
  Utensils,
  Waves,
  WavesLadder,
  WashingMachine,
  Wrench,
  Zap,
  type LucideIcon,
} from "lucide-react";

// §14 "Icons": data fields store a Lucide icon name (PascalCase)
// and the web resolves them through this whitelist. Unknown names
// fall back to the generic Package glyph so stale data never breaks
// the render.
export const ASSET_ICON_REGISTRY = {
  AlarmSmoke,
  Baby,
  Bell,
  Biohazard,
  BrushCleaning,
  Building2,
  Car,
  ChefHat,
  CookingPot,
  Droplets,
  Fan,
  FireExtinguisher,
  Flame,
  Heater,
  Refrigerator,
  ShieldCheck,
  Snowflake,
  Sprout,
  Sun,
  ThermometerSun,
  Utensils,
  Waves,
  WavesLadder,
  WashingMachine,
  Wrench,
  Zap,
} satisfies Record<string, LucideIcon>;

export type AssetIconName = keyof typeof ASSET_ICON_REGISTRY;

export const ASSET_ICON_NAMES: readonly AssetIconName[] = Object.freeze(
  (Object.keys(ASSET_ICON_REGISTRY) as AssetIconName[]).sort(),
);

export function isAssetIconName(name: string | null | undefined): name is AssetIconName {
  return Boolean(name && name in ASSET_ICON_REGISTRY);
}

export function AssetIcon({
  name,
  size = 16,
  className,
}: {
  name: string | null | undefined;
  size?: number;
  className?: string;
}) {
  const Icon = isAssetIconName(name) ? ASSET_ICON_REGISTRY[name] : Package;
  return (
    <span className={"asset-icon" + (className ? " " + className : "")} aria-hidden="true">
      <Icon size={size} strokeWidth={1.75} />
    </span>
  );
}
