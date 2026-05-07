export type PersonaKey = "villa-owner" | "rental-manager" | "housekeeper";

export type DemoPersona = "owner" | "manager" | "worker";

export type IntentSlug =
  | "organise-cleaner"
  | "home-status"
  | "airbnb-stays"
  | "owner-agent"
  | "schedule-staff"
  | "todays-operations"
  | "payroll-glance"
  | "invite-client"
  | "todays-tasks"
  | "photo-completion"
  | "review-hours"
  | "worker-agent";

export type ScenarioIntent = {
  slug: IntentSlug;
  label: string;
  start: string;
  caption: string;
};

export type ScenarioPersona = {
  key: PersonaKey;
  label: string;
  shortLabel: string;
  scenarioKey: PersonaKey;
  as: DemoPersona;
  intents: readonly ScenarioIntent[];
};

export type ScenarioPickerCopy = {
  eyebrow: string;
  heading: string;
  body: string;
  personaLegend: string;
  intentLegend: string;
  lockedPersonaLabel: string;
  videoModeLabel: string;
  liveModeLabel: string;
  liveModeNotice: string;
  tryLiveLabel: string;
  tryLiveNotice: string;
  liveFrameTitle: string;
  videoFallback: string;
};

export const scenarioPickerCopy = {
  eyebrow: "Demo picker",
  heading: "Choose the day you want to inspect.",
  body: "Start with a recorded pass through the same fixture the live demo opens. The app iframe is only created when you ask for it.",
  personaLegend: "Choose a role",
  intentLegend: "Choose a job to try",
  lockedPersonaLabel: "Selected role",
  videoModeLabel: "Recorded demo",
  liveModeLabel: "Live demo workspace",
  liveModeNotice: "Picker changes now open the selected live demo.",
  tryLiveLabel: "Try it live",
  tryLiveNotice: "Opens a fresh demo workspace - you can break anything in it.",
  liveFrameTitle: "crew.day live demo",
  videoFallback: "Your browser does not support embedded demo video.",
} as const satisfies ScenarioPickerCopy;

export const scenarioPersonas = [
  {
    key: "villa-owner",
    label: "I own a villa",
    shortLabel: "Villa owner",
    scenarioKey: "villa-owner",
    as: "owner",
    intents: [
      {
        slug: "organise-cleaner",
        label: "Organise my cleaner",
        start: "/schedule",
        caption: "The owner checks the property schedule and cleaner handoff.",
      },
      {
        slug: "home-status",
        label: "See what's happening at home",
        start: "/dashboard",
        caption: "The dashboard shows stays, due work, and current house status.",
      },
      {
        slug: "airbnb-stays",
        label: "Manage incoming Airbnb stays",
        start: "/stays",
        caption: "Upcoming guest stays stay connected to the work they create.",
      },
      {
        slug: "owner-agent",
        label: "Chat with the agent about my property",
        start: "/dashboard",
        caption: "The owner asks the operations agent from the manager surface.",
      },
    ],
  },
  {
    key: "rental-manager",
    label: "I run a property-management agency",
    shortLabel: "Agency",
    scenarioKey: "rental-manager",
    as: "manager",
    intents: [
      {
        slug: "schedule-staff",
        label: "Schedule staff across properties",
        start: "/schedule",
        caption: "The manager balances worker assignments across properties.",
      },
      {
        slug: "todays-operations",
        label: "Track today's operations",
        start: "/dashboard",
        caption: "The operations dashboard keeps the day's exceptions visible.",
      },
      {
        slug: "payroll-glance",
        label: "See payroll at a glance",
        start: "/pay",
        caption: "Payroll review stays tied to completed shifts and task records.",
      },
      {
        slug: "invite-client",
        label: "Invite a new client",
        start: "/organizations",
        caption: "The agency adds an owner client without exposing manager tools.",
      },
    ],
  },
  {
    key: "housekeeper",
    label: "I work as a housekeeper",
    shortLabel: "Housekeeper",
    scenarioKey: "housekeeper",
    as: "worker",
    intents: [
      {
        slug: "todays-tasks",
        label: "See today's tasks",
        start: "/today",
        caption: "The worker view opens on the current shift and task queue.",
      },
      {
        slug: "photo-completion",
        label: "Complete a task with photo",
        start: "/today?focus=next-task",
        caption: "The housekeeper completes the next task with evidence attached.",
      },
      {
        slug: "review-hours",
        label: "Review my hours",
        start: "/schedule",
        caption: "The worker checks hours from the same schedule that assigned work.",
      },
      {
        slug: "worker-agent",
        label: "Ask the agent about my work",
        start: "/chat",
        caption: "The worker opens the full-screen agent entry for work questions.",
      },
    ],
  },
] as const satisfies readonly ScenarioPersona[];
