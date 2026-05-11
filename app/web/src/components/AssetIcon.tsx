import {
  AlarmSmoke,
  Baby,
  Bell,
  BedDouble,
  Biohazard,
  BrushCleaning,
  Building2,
  Camera,
  Car,
  ChefHat,
  ClipboardCheck,
  CookingPot,
  Droplets,
  Fan,
  FireExtinguisher,
  Flame,
  Gauge,
  Heater,
  House,
  KeyRound,
  Monitor,
  Package,
  Refrigerator,
  ShieldCheck,
  Snowflake,
  Sparkles,
  Sprout,
  Sun,
  ThermometerSun,
  Trash2,
  Utensils,
  Waves,
  WavesLadder,
  WashingMachine,
  Wrench,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useState } from "react";

type LazyIconModule = { default: LucideIcon };
type LazyIconLoader = () => Promise<LazyIconModule>;

// §14 "Icons": data fields store a Lucide icon name (PascalCase)
// and the web resolves them through this curated catalog. Unknown
// names fall back to the generic Package glyph so stale data never
// breaks the render.
const EAGER_ASSET_ICON_REGISTRY = {
  AlarmSmoke,
  Baby,
  Bell,
  BedDouble,
  Biohazard,
  BrushCleaning,
  Building2,
  Camera,
  Car,
  ChefHat,
  ClipboardCheck,
  CookingPot,
  Droplets,
  Fan,
  FireExtinguisher,
  Flame,
  Gauge,
  Heater,
  House,
  KeyRound,
  Monitor,
  Package,
  Refrigerator,
  ShieldCheck,
  Snowflake,
  Sparkles,
  Sprout,
  Sun,
  ThermometerSun,
  Trash2,
  Utensils,
  Waves,
  WavesLadder,
  WashingMachine,
  Wrench,
  Zap,
} satisfies Record<string, LucideIcon>;
type EagerAssetIconName = keyof typeof EAGER_ASSET_ICON_REGISTRY;

export const ASSET_ICON_LOADERS = {
  AirVent: () => import("lucide-react/dist/esm/icons/air-vent.mjs"),
  AlarmClock: () => import("lucide-react/dist/esm/icons/alarm-clock.mjs"),
  AlarmSmoke: loadEagerIcon("AlarmSmoke"),
  Armchair: () => import("lucide-react/dist/esm/icons/armchair.mjs"),
  Baby: loadEagerIcon("Baby"),
  BadgeCheck: () => import("lucide-react/dist/esm/icons/badge-check.mjs"),
  Bath: () => import("lucide-react/dist/esm/icons/bath.mjs"),
  Bed: () => import("lucide-react/dist/esm/icons/bed.mjs"),
  BedDouble: loadEagerIcon("BedDouble"),
  Bell: loadEagerIcon("Bell"),
  Bike: () => import("lucide-react/dist/esm/icons/bike.mjs"),
  Biohazard: loadEagerIcon("Biohazard"),
  Blinds: () => import("lucide-react/dist/esm/icons/blinds.mjs"),
  Bolt: () => import("lucide-react/dist/esm/icons/bolt.mjs"),
  Brush: () => import("lucide-react/dist/esm/icons/brush.mjs"),
  BrushCleaning: loadEagerIcon("BrushCleaning"),
  Bug: () => import("lucide-react/dist/esm/icons/bug.mjs"),
  Building2: loadEagerIcon("Building2"),
  Bus: () => import("lucide-react/dist/esm/icons/bus.mjs"),
  Cable: () => import("lucide-react/dist/esm/icons/cable.mjs"),
  Camera: loadEagerIcon("Camera"),
  Car: loadEagerIcon("Car"),
  CarFront: () => import("lucide-react/dist/esm/icons/car-front.mjs"),
  ChefHat: loadEagerIcon("ChefHat"),
  CircleParking: () => import("lucide-react/dist/esm/icons/circle-parking.mjs"),
  ClipboardCheck: loadEagerIcon("ClipboardCheck"),
  Clock: () => import("lucide-react/dist/esm/icons/clock.mjs"),
  Coffee: () => import("lucide-react/dist/esm/icons/coffee.mjs"),
  CookingPot: loadEagerIcon("CookingPot"),
  DoorClosed: () => import("lucide-react/dist/esm/icons/door-closed.mjs"),
  DoorOpen: () => import("lucide-react/dist/esm/icons/door-open.mjs"),
  Drill: () => import("lucide-react/dist/esm/icons/drill.mjs"),
  Droplets: loadEagerIcon("Droplets"),
  Dumbbell: () => import("lucide-react/dist/esm/icons/dumbbell.mjs"),
  Fan: loadEagerIcon("Fan"),
  Fence: () => import("lucide-react/dist/esm/icons/fence.mjs"),
  FireExtinguisher: loadEagerIcon("FireExtinguisher"),
  Flame: loadEagerIcon("Flame"),
  Flashlight: () => import("lucide-react/dist/esm/icons/flashlight.mjs"),
  Flower2: () => import("lucide-react/dist/esm/icons/flower-2.mjs"),
  Fuel: () => import("lucide-react/dist/esm/icons/fuel.mjs"),
  Gauge: loadEagerIcon("Gauge"),
  Hammer: () => import("lucide-react/dist/esm/icons/hammer.mjs"),
  HardHat: () => import("lucide-react/dist/esm/icons/hard-hat.mjs"),
  Heater: loadEagerIcon("Heater"),
  Home: () => import("lucide-react/dist/esm/icons/home.mjs"),
  House: loadEagerIcon("House"),
  KeyRound: loadEagerIcon("KeyRound"),
  Lamp: () => import("lucide-react/dist/esm/icons/lamp.mjs"),
  Landmark: () => import("lucide-react/dist/esm/icons/landmark.mjs"),
  Laptop: () => import("lucide-react/dist/esm/icons/laptop.mjs"),
  Leaf: () => import("lucide-react/dist/esm/icons/leaf.mjs"),
  Lightbulb: () => import("lucide-react/dist/esm/icons/lightbulb.mjs"),
  LockKeyhole: () => import("lucide-react/dist/esm/icons/lock-keyhole.mjs"),
  Luggage: () => import("lucide-react/dist/esm/icons/luggage.mjs"),
  Mail: () => import("lucide-react/dist/esm/icons/mail.mjs"),
  MapPin: () => import("lucide-react/dist/esm/icons/map-pin.mjs"),
  Microwave: () => import("lucide-react/dist/esm/icons/microwave.mjs"),
  Monitor: loadEagerIcon("Monitor"),
  Package: loadEagerIcon("Package"),
  PackageCheck: () => import("lucide-react/dist/esm/icons/package-check.mjs"),
  PaintBucket: () => import("lucide-react/dist/esm/icons/paint-bucket.mjs"),
  Paintbrush: () => import("lucide-react/dist/esm/icons/paintbrush.mjs"),
  ParkingMeter: () => import("lucide-react/dist/esm/icons/parking-meter.mjs"),
  Phone: () => import("lucide-react/dist/esm/icons/phone.mjs"),
  Plug: () => import("lucide-react/dist/esm/icons/plug.mjs"),
  PlugZap: () => import("lucide-react/dist/esm/icons/plug-zap.mjs"),
  Refrigerator: loadEagerIcon("Refrigerator"),
  Route: () => import("lucide-react/dist/esm/icons/route.mjs"),
  Router: () => import("lucide-react/dist/esm/icons/router.mjs"),
  Scissors: () => import("lucide-react/dist/esm/icons/scissors.mjs"),
  ShieldCheck: loadEagerIcon("ShieldCheck"),
  ShowerHead: () => import("lucide-react/dist/esm/icons/shower-head.mjs"),
  Snowflake: loadEagerIcon("Snowflake"),
  Sofa: () => import("lucide-react/dist/esm/icons/sofa.mjs"),
  Sparkles: loadEagerIcon("Sparkles"),
  SprayCan: () => import("lucide-react/dist/esm/icons/spray-can.mjs"),
  Sprout: loadEagerIcon("Sprout"),
  Sun: loadEagerIcon("Sun"),
  Thermometer: () => import("lucide-react/dist/esm/icons/thermometer.mjs"),
  ThermometerSun: loadEagerIcon("ThermometerSun"),
  Toilet: () => import("lucide-react/dist/esm/icons/toilet.mjs"),
  Trash2: loadEagerIcon("Trash2"),
  Trees: () => import("lucide-react/dist/esm/icons/trees.mjs"),
  Truck: () => import("lucide-react/dist/esm/icons/truck.mjs"),
  Tv: () => import("lucide-react/dist/esm/icons/tv.mjs"),
  Utensils: loadEagerIcon("Utensils"),
  Vault: () => import("lucide-react/dist/esm/icons/vault.mjs"),
  WashingMachine: loadEagerIcon("WashingMachine"),
  Waves: loadEagerIcon("Waves"),
  WavesLadder: loadEagerIcon("WavesLadder"),
  Wifi: () => import("lucide-react/dist/esm/icons/wifi.mjs"),
  Wind: () => import("lucide-react/dist/esm/icons/wind.mjs"),
  Wrench: loadEagerIcon("Wrench"),
  Zap: loadEagerIcon("Zap"),
} satisfies Record<string, LazyIconLoader>;

export const ASSET_ICON_REGISTRY = EAGER_ASSET_ICON_REGISTRY;

export type AssetIconName = keyof typeof ASSET_ICON_LOADERS;

const loadedLazyIcons = new Map<AssetIconName, LucideIcon>();

export const ASSET_ICON_NAMES: readonly AssetIconName[] = Object.freeze(
  (Object.keys(ASSET_ICON_LOADERS) as AssetIconName[]).sort(),
);

export function isAssetIconName(name: string | null | undefined): name is AssetIconName {
  return Boolean(name && hasOwn(ASSET_ICON_LOADERS, name));
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
  const iconName = isAssetIconName(name) ? name : null;
  const eagerIcon = iconName ? getEagerIcon(iconName) : undefined;
  const [lazyIcon, setLazyIcon] = useState<LucideIcon | null>(null);
  const cachedLazyIcon = iconName ? loadedLazyIcons.get(iconName) : undefined;
  const Icon = eagerIcon ?? cachedLazyIcon ?? lazyIcon ?? Package;

  useEffect(() => {
    if (!iconName || eagerIcon || cachedLazyIcon) {
      setLazyIcon(null);
      return;
    }

    let cancelled = false;
    setLazyIcon(null);
    void ASSET_ICON_LOADERS[iconName]()
      .then((module) => {
        loadedLazyIcons.set(iconName, module.default);
        if (!cancelled) {
          setLazyIcon(() => module.default);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setLazyIcon(null);
        }
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

function getEagerIcon(name: AssetIconName): LucideIcon | undefined {
  if (hasOwn(EAGER_ASSET_ICON_REGISTRY, name)) {
    return EAGER_ASSET_ICON_REGISTRY[name as EagerAssetIconName];
  }
  return undefined;
}

function loadEagerIcon(name: EagerAssetIconName): LazyIconLoader {
  return () => Promise.resolve({ default: EAGER_ASSET_ICON_REGISTRY[name] });
}

function hasOwn<T extends object>(object: T, key: PropertyKey): key is keyof T {
  return Object.prototype.hasOwnProperty.call(object, key);
}
