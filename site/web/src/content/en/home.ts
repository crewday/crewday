import { appLinks } from "./site";

export const homeCopy = {
  title: "crew.day",
  description:
    "crew.day is an agent-first operations back-office for owners and small property-management teams.",
  hero: {
    eyebrow: "House operations",
    headline: "Run your houses like a hotel. Without the front desk.",
    subhead:
      "crew.day is the operations back-office for owners and small property-management outfits — designed so an LLM agent runs the day-to-day and the humans live in the house.",
    primaryCta: { label: "Try the demo", href: "#try-it" },
    secondaryCta: appLinks.signup,
  },
  featureIntro: {
    eyebrow: "What ships first",
    heading: "A small system for real houses, not a generic task board.",
  },
  features: [
    {
      illustration: "agent",
      eyebrow: "Agent-first",
      heading: "Every action has a command behind it.",
      body: "Every button is a CLI command; every CLI command is a tool the agent can call; every action is audited.",
    },
    {
      illustration: "operations",
      eyebrow: "Places, people, tasks",
      heading: "The model matches the way homes actually run.",
      body: "Properties, workers, owners, guests, tasks, stays, schedules, and payroll stay connected without flattening everyone into the same role.",
    },
    {
      illustration: "selfHost",
      eyebrow: "Self-hostable",
      heading: "Your data can stay on your own box.",
      body: "crew.day is designed for a single-binary deployment path: SQLite by default, Postgres when you need it, and operational control for teams that want their own system.",
    },
  ],
  tryIt: {
    label: "Interactive demo picker",
  },
} as const;
