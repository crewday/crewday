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
  AirVent: () => import("lucide-react/dist/esm/icons/air-vent.js"),
  AlarmClock: () => import("lucide-react/dist/esm/icons/alarm-clock.js"),
  AlarmSmoke: loadEagerIcon("AlarmSmoke"),
  Armchair: () => import("lucide-react/dist/esm/icons/armchair.js"),
  Baby: loadEagerIcon("Baby"),
  BadgeCheck: () => import("lucide-react/dist/esm/icons/badge-check.js"),
  Bath: () => import("lucide-react/dist/esm/icons/bath.js"),
  Bed: () => import("lucide-react/dist/esm/icons/bed.js"),
  BedDouble: loadEagerIcon("BedDouble"),
  Bell: loadEagerIcon("Bell"),
  Bike: () => import("lucide-react/dist/esm/icons/bike.js"),
  Biohazard: loadEagerIcon("Biohazard"),
  Blinds: () => import("lucide-react/dist/esm/icons/blinds.js"),
  Bolt: () => import("lucide-react/dist/esm/icons/bolt.js"),
  Brush: () => import("lucide-react/dist/esm/icons/brush.js"),
  BrushCleaning: loadEagerIcon("BrushCleaning"),
  Bug: () => import("lucide-react/dist/esm/icons/bug.js"),
  Building2: loadEagerIcon("Building2"),
  Bus: () => import("lucide-react/dist/esm/icons/bus.js"),
  Cable: () => import("lucide-react/dist/esm/icons/cable.js"),
  Camera: loadEagerIcon("Camera"),
  Car: loadEagerIcon("Car"),
  CarFront: () => import("lucide-react/dist/esm/icons/car-front.js"),
  ChefHat: loadEagerIcon("ChefHat"),
  CircleParking: () => import("lucide-react/dist/esm/icons/circle-parking.js"),
  ClipboardCheck: loadEagerIcon("ClipboardCheck"),
  Clock: () => import("lucide-react/dist/esm/icons/clock.js"),
  Coffee: () => import("lucide-react/dist/esm/icons/coffee.js"),
  CookingPot: loadEagerIcon("CookingPot"),
  DoorClosed: () => import("lucide-react/dist/esm/icons/door-closed.js"),
  DoorOpen: () => import("lucide-react/dist/esm/icons/door-open.js"),
  Drill: () => import("lucide-react/dist/esm/icons/drill.js"),
  Droplets: loadEagerIcon("Droplets"),
  Dumbbell: () => import("lucide-react/dist/esm/icons/dumbbell.js"),
  Fan: loadEagerIcon("Fan"),
  Fence: () => import("lucide-react/dist/esm/icons/fence.js"),
  FireExtinguisher: loadEagerIcon("FireExtinguisher"),
  Flame: loadEagerIcon("Flame"),
  Flashlight: () => import("lucide-react/dist/esm/icons/flashlight.js"),
  Flower2: () => import("lucide-react/dist/esm/icons/flower-2.js"),
  Fuel: () => import("lucide-react/dist/esm/icons/fuel.js"),
  Gauge: loadEagerIcon("Gauge"),
  Hammer: () => import("lucide-react/dist/esm/icons/hammer.js"),
  HardHat: () => import("lucide-react/dist/esm/icons/hard-hat.js"),
  Heater: loadEagerIcon("Heater"),
  Home: () => import("lucide-react/dist/esm/icons/home.js"),
  House: loadEagerIcon("House"),
  KeyRound: loadEagerIcon("KeyRound"),
  Lamp: () => import("lucide-react/dist/esm/icons/lamp.js"),
  Landmark: () => import("lucide-react/dist/esm/icons/landmark.js"),
  Laptop: () => import("lucide-react/dist/esm/icons/laptop.js"),
  Leaf: () => import("lucide-react/dist/esm/icons/leaf.js"),
  Lightbulb: () => import("lucide-react/dist/esm/icons/lightbulb.js"),
  LockKeyhole: () => import("lucide-react/dist/esm/icons/lock-keyhole.js"),
  Luggage: () => import("lucide-react/dist/esm/icons/luggage.js"),
  Mail: () => import("lucide-react/dist/esm/icons/mail.js"),
  MapPin: () => import("lucide-react/dist/esm/icons/map-pin.js"),
  Microwave: () => import("lucide-react/dist/esm/icons/microwave.js"),
  Monitor: loadEagerIcon("Monitor"),
  Package: loadEagerIcon("Package"),
  PackageCheck: () => import("lucide-react/dist/esm/icons/package-check.js"),
  PaintBucket: () => import("lucide-react/dist/esm/icons/paint-bucket.js"),
  Paintbrush: () => import("lucide-react/dist/esm/icons/paintbrush.js"),
  ParkingMeter: () => import("lucide-react/dist/esm/icons/parking-meter.js"),
  Phone: () => import("lucide-react/dist/esm/icons/phone.js"),
  Plug: () => import("lucide-react/dist/esm/icons/plug.js"),
  PlugZap: () => import("lucide-react/dist/esm/icons/plug-zap.js"),
  Refrigerator: loadEagerIcon("Refrigerator"),
  Route: () => import("lucide-react/dist/esm/icons/route.js"),
  Router: () => import("lucide-react/dist/esm/icons/router.js"),
  Scissors: () => import("lucide-react/dist/esm/icons/scissors.js"),
  ShieldCheck: loadEagerIcon("ShieldCheck"),
  ShowerHead: () => import("lucide-react/dist/esm/icons/shower-head.js"),
  Snowflake: loadEagerIcon("Snowflake"),
  Sofa: () => import("lucide-react/dist/esm/icons/sofa.js"),
  Sparkles: loadEagerIcon("Sparkles"),
  SprayCan: () => import("lucide-react/dist/esm/icons/spray-can.js"),
  Sprout: loadEagerIcon("Sprout"),
  Sun: loadEagerIcon("Sun"),
  Thermometer: () => import("lucide-react/dist/esm/icons/thermometer.js"),
  ThermometerSun: loadEagerIcon("ThermometerSun"),
  Toilet: () => import("lucide-react/dist/esm/icons/toilet.js"),
  Trash2: loadEagerIcon("Trash2"),
  Trees: () => import("lucide-react/dist/esm/icons/trees.js"),
  Truck: () => import("lucide-react/dist/esm/icons/truck.js"),
  Tv: () => import("lucide-react/dist/esm/icons/tv.js"),
  Utensils: loadEagerIcon("Utensils"),
  Vault: () => import("lucide-react/dist/esm/icons/vault.js"),
  WashingMachine: loadEagerIcon("WashingMachine"),
  Waves: loadEagerIcon("Waves"),
  WavesLadder: loadEagerIcon("WavesLadder"),
  Wifi: () => import("lucide-react/dist/esm/icons/wifi.js"),
  Wind: () => import("lucide-react/dist/esm/icons/wind.js"),
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
