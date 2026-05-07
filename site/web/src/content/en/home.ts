import { appLinks } from "@/config/appLinks";

export const homeCopy = {
  title: "crew.day",
  description:
    "crew.day is your home organization operating system, from one house to fifty hotels, with an LLM agent running the day-to-day.",
  masthead: {
    ariaLabel: "Public site",
    homeAriaLabel: "crew.day home",
    navAriaLabel: "Landing",
    dateline: "Proudly open source · Self hostable",
    github: { label: "GitHub", href: "https://github.com/crewday/crewday" },
    navLinks: [
      { label: "Why", href: "#why" },
      { label: "Demo", href: "#try-it" },
      { label: "Who it's for", href: "#audiences" },
      { label: "Pricing", href: "#pricing" },
    ],
  },
  hero: {
    eyebrow: "Your home organization operating system",
    headlineLead: "From a single house to 50 hotels.",
    headlineEmphasis: "From a single part time maid to 500.",
    headline: "Run your houses like a hotel. Without the front desk.",
    subhead:
      "crew.day is the operations back-office for owners and small property-management outfits — designed so an LLM agent runs the day-to-day and the humans live in the house.",
    primaryCta: { label: "Try the demo", href: "#try-it" },
    secondaryCta: appLinks.signup,
  },
  devicePreview: {
    ariaLabel: "crew.day today preview on a pocket device",
    heading: "Today · Villa Mar",
    cards: [
      {
        time: "08:30",
        title: "Turnover · Luz",
        note: "Agent drafted airport run",
        active: true,
      },
      {
        time: "11:00",
        title: "Check-in prep",
        note: "Evidence requested",
        active: false,
      },
    ],
    chat: {
      label: "Agent",
      body: "Shall I ask Carla to bring extra sheets?",
    },
  },
  quote: {
    text: "A hotel of one to fifty rooms, minus the front desk. Guest stays flow in from Airbnb and VRBO; turnovers auto-generate; the cook sees what to prepare tomorrow; the driver sees the airport run; the head of house sees everything.",
    cite: "working mental model · docs/specs §00",
  },
  features: [
    {
      illustration: "agent",
      numeral: "I",
      figureLabel: "Fig. 2 — one bell, three doors",
      eyebrow: "Ask anywhere",
      heading: "A vast back-office. The easiest to use.",
      body: [
        "Every schedule, payslip, warranty and bedsheet lives inside crew.day. On top of it: a concierge who already knows how to use the whole system, waiting for your word.",
        "Every button is a CLI command; every CLI command is a tool the agent can call; every action is audited. You just ask; the agent does.",
      ],
    },
    {
      illustration: "operations",
      numeral: "II",
      figureLabel: "Fig. 3 — ledger",
      eyebrow: "Places, people, tasks",
      heading: "A ledger for who does what, where.",
      body: [
        "Properties, staff, stays, tasks, payroll: all modelled around the ways a small outfit actually works. Owners and managers see everything; workers see today; clients see the slice that is theirs.",
        "Turnovers auto-generate from bookings. Evidence photos close tasks. Hours roll up to payslips. The boring parts stay boring.",
      ],
    },
    {
      illustration: "selfHost",
      numeral: "III",
      figureLabel: "Fig. 4 — what's on hand",
      eyebrow: "Self-hostable · single binary",
      heading: "Your data can stay on your own box.",
      body: [
        "crew.day is designed for a single-binary deployment path: SQLite by default, Postgres when you need it, and operational control for teams that want their own system.",
        "Source, specs and issues are public. Self-host free, managed hosting when you want someone else to keep the lights on.",
      ],
    },
  ],
  tryIt: {
    label: "Interactive demo picker",
  },
  sections: {
    whyAriaLabel: "Why crew.day",
    audiencesAriaLabel: "Who crew.day is for",
    pricingAriaLabel: "Pricing",
    footerAriaLabel: "Footer",
  },
  audiences: [
    {
      tag: "For owners",
      title: "One villa, many lives.",
      body: "Your cleaner sees today. Your cook sees tomorrow. You see everything, when you want, and nothing when you do not.",
      href: "/for-owners",
      cta: "Read the long form →",
    },
    {
      tag: "For agencies",
      title: "Small, not shrink-wrapped.",
      body: "Staff across properties. Per-client views. Payslips and invoices that add up. Permissions that bend to how you are actually run.",
      href: "/for-agencies",
      cta: "Read the long form →",
    },
    {
      tag: "For housekeepers",
      title: "Today, then home.",
      body: "A phone-shaped list of what to do. Complete with a photo. Hours logged without a clock-in ritual.",
      href: "/for-housekeepers",
      cta: "Read the long form →",
    },
  ],
  pricing: {
    label: "Pricing",
    copy: "Self-host — free. Managed pricing — contact us via the suggestion box.",
    cta: { label: "Open the suggestion box →", href: "/suggest" },
  },
  imprint: {
    heading: "crew.day — the almanac",
    body: "A quiet operations surface for houses. Source code, specs and issues are public; the product itself is self-hostable out of one binary. Made in France, with a bias toward silence.",
    rss: { label: "RSS changelog", href: "/changelog.rss" },
    bottomLeft: "© MMXXVI crew.day · printed in moss ink on warm paper",
    bottomRight: "v0.1 · spec-site §01",
  },
} as const;
