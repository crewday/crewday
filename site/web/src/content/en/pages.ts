import { appLinks } from "@/config/appLinks";

export type ContentSection = {
  eyebrow?: string;
  heading: string;
  body: string;
};

export type LinkAction = {
  label: string;
  href: string;
};

export type PersonaPageCopy = {
  title: string;
  description: string;
  eyebrow: string;
  heading: string;
  intro: string;
  primaryCta: LinkAction;
  secondaryCta: LinkAction;
  sections: readonly ContentSection[];
  ledger: readonly [string, string, string, string];
  tryIt: {
    label: string;
  };
};

export const personaPages = {
  owners: {
    title: "For villa owners | crew.day",
    description:
      "A house operations workspace for villa owners who need cleaners, stays, and property notes to move without becoming a full-time coordinator.",
    eyebrow: "For owners",
    heading: "Know what is happening at the house without running every handoff yourself.",
    intro:
      "crew.day gives a villa owner one place for upcoming stays, cleaner schedules, property notes, approvals, and the agent that turns plain-language requests into audited work.",
    primaryCta: { label: "Try the demo", href: "#try-it" },
    secondaryCta: appLinks.signup,
    sections: [
      {
        eyebrow: "Cleaner handoffs",
        heading: "Turn recurring house work into visible tasks.",
        body: "Cleaning, inspection, stocking, and maintenance work can be scheduled against the property and stay, so nobody has to reconstruct the plan from messages.",
      },
      {
        eyebrow: "Owner confidence",
        heading: "See the current state before you ask.",
        body: "The dashboard is built for quick answers: what is booked, what is due, who is on it, and what still needs approval.",
      },
      {
        eyebrow: "Agent help",
        heading: "Ask for the outcome, not the screen.",
        body: "The operations agent can call the same tools the interface uses, so requests become traceable commands instead of detached chat advice.",
      },
    ],
    ledger: ["Stay due", "Cleaner assigned", "Owner note", "Agent drafted"],
    tryIt: {
      label: "Owner demo picker",
    },
  },
  agencies: {
    title: "For property-management agencies | crew.day",
    description:
      "A compact operations back-office for small property-management agencies coordinating properties, staff, clients, stays, and payroll.",
    eyebrow: "For agencies",
    heading: "Run the day across properties without hiding the work in group chats.",
    intro:
      "crew.day keeps the agency view dense and practical: multiple places, multiple workers, owner clients, daily schedules, payroll, and an agent that can perform audited operations.",
    primaryCta: { label: "Try the demo", href: "#try-it" },
    secondaryCta: appLinks.signup,
    sections: [
      {
        eyebrow: "Scheduling",
        heading: "Plan staff across properties with the day in view.",
        body: "Managers can assign work around arrivals, departures, worker availability, and property-specific requirements without rebuilding the schedule from scratch.",
      },
      {
        eyebrow: "Client boundary",
        heading: "Owners get clarity without manager permissions.",
        body: "The owner-manager, worker, and client split is deliberate: each person sees the right surface for their role.",
      },
      {
        eyebrow: "Payroll",
        heading: "Hours and approvals stay close to the work.",
        body: "Completed tasks, shift records, and pay views share the same operating model, keeping payroll review grounded in what happened.",
      },
    ],
    ledger: ["Three arrivals", "Five shifts", "Payroll ready", "Client view"],
    tryIt: {
      label: "Agency demo picker",
    },
  },
  housekeepers: {
    title: "For housekeepers | crew.day",
    description:
      "A mobile-first work surface for housekeepers to see today's tasks, complete work with evidence, and review hours.",
    eyebrow: "For housekeepers",
    heading: "Start the shift, see the next task, and leave a clear record.",
    intro:
      "crew.day is designed for workers on phones, between rooms and properties. The worker view stays focused on the current day, the next task, proof of completion, and hours.",
    primaryCta: { label: "Try the demo", href: "#try-it" },
    secondaryCta: appLinks.signup,
    sections: [
      {
        eyebrow: "Today first",
        heading: "The work queue starts with what is due now.",
        body: "Housekeepers see the current shift and task list without needing manager dashboards or client-facing details.",
      },
      {
        eyebrow: "Evidence",
        heading: "Completion can include the proof the property needs.",
        body: "Photo-backed task completion keeps handoffs specific and reduces the after-the-fact back-and-forth.",
      },
      {
        eyebrow: "Hours",
        heading: "Review the record while the shift is still fresh.",
        body: "Workers can see hours and schedule context from the same system that assigned and completed the work.",
      },
    ],
    ledger: ["Shift open", "Next task", "Photo proof", "Hours saved"],
    tryIt: {
      label: "Housekeeper demo picker",
    },
  },
} as const satisfies Record<string, PersonaPageCopy>;

export const whyCrewdayCopy = {
  title: "Why crew.day | crew.day",
  description:
    "Why crew.day exists: an agent-first, self-hostable operations system for real house teams.",
  eyebrow: "Why crew.day",
  heading: "Most tools make the manager do the integration work.",
  intro:
    "Short-term rental operations are not just tickets. They are properties, stays, worker schedules, owner expectations, approvals, payroll, and the small bits of local knowledge that make a house run.",
  primaryCta: { label: "Try the demo", href: "/#try-it" },
  secondaryCta: appLinks.signup,
  sections: [
    {
      eyebrow: "The pain",
      heading: "Messages are fast until they become the system of record.",
      body: "Group chats are useful for the exception, but brittle as the source of truth. Tasks lose context, owners ask for status, and managers become the only people who know what is current.",
    },
    {
      eyebrow: "The bet",
      heading: "An agent can only help when the product exposes real tools.",
      body: "crew.day treats the interface, CLI, and agent surface as one audited operating layer. The agent is not a sidecar; it calls the same commands that power the app.",
    },
    {
      eyebrow: "The anti-goal",
      heading: "This is not a generic CRM with a property label.",
      body: "The v1 product stays narrow: homes, people, tasks, schedules, stays, payroll context, and the deployment choices needed by small operators.",
    },
  ],
} as const;

export const pricingCopy = {
  title: "Pricing | crew.day",
  description: "crew.day v1 pricing posture: self-host free; managed pricing is on the roadmap.",
  eyebrow: "Pricing",
  heading: "Self-host free. Managed pricing is still being shaped.",
  intro:
    "crew.day starts with a self-hostable path for teams that want their data on their own infrastructure. Managed SaaS pricing will be published when that service is ready.",
  primaryCta: appLinks.signup,
  secondaryCta: { label: "Read the roadmap", href: "/changelog" },
  sections: [
    {
      eyebrow: "Self-host",
      heading: "Free to run on your own box.",
      body: "The self-host path is designed around the single-binary deployment model, SQLite by default, and clear operational controls.",
    },
    {
      eyebrow: "Managed",
      heading: "Hosted service pricing is a roadmap stub.",
      body: "The managed version will include hosted infrastructure and operator-run services. Until then, pricing copy stays intentionally scoped instead of inventing plans that do not exist.",
    },
    {
      eyebrow: "Feedback",
      heading: "The suggestion box is the contact path in the roadmap.",
      body: "The public suggestion surface is the contact path while managed pricing is still being shaped, with authenticated submit and vote controls kept behind the app handshake.",
    },
  ],
} as const;

export const suggestCopy = {
  title: "Suggestion box | crew.day",
  description:
    "Read the public crew.day suggestion board, see grouped feedback themes, and sign in through the app to submit or vote.",
  eyebrow: "Suggestion box",
  heading: "A public board for the work people keep asking us to fix.",
  intro:
    "Browse the grouped themes openly. Submitting and voting stay tied to a signed-in crew.day workspace, so the board gets signal without anonymous spam.",
  loginCta: {
    label: "Log in to submit or vote",
    href: `${appLinks.login.href}?return=%2Ffeedback-redirect`,
  },
  formPanel: {
    ariaLabel: "Submit an idea",
    label: "Submit",
    heading: "Sign in through your workspace.",
    body: "Public visitors can read the board. Submissions and votes unlock after the app redirects you back with a short-lived feedback token.",
  },
  explainer: {
    ariaLabel: "How the suggestion box works",
  },
  board: {
    eyebrow: "Public board",
    heading: "Grouped themes, not private submissions.",
  },
  sections: [
    {
      eyebrow: "Read",
      heading: "Public themes stay visible.",
      body: "The board shows clustered ideas instead of raw submissions, so prospects and users can see what is active without exposing private workspace detail.",
    },
    {
      eyebrow: "Submit",
      heading: "Feedback starts in the app.",
      body: "Signed-in users arrive with a short-lived token from their workspace. The site sees only pseudonymous hashes, never email addresses or workspace names.",
    },
    {
      eyebrow: "Act",
      heading: "Operator replies land on the cluster.",
      body: "Each visible cluster can move from new to acknowledged, planned, in progress, shipped, or declined as the roadmap changes.",
    },
  ],
  clusters: [
    {
      title: "Make recurring turnovers easier to review before publishing.",
      meta: "42 submissions · acknowledged",
      body: "Managers want the agent to draft repeatable turnover plans and still leave a clear approval point before staff see the work.",
    },
    {
      title: "Give workers a cleaner view of proof photos and shift hours.",
      meta: "27 submissions · planned",
      body: "Housekeepers keep asking for fewer taps around evidence, completion notes, and the daily hour record.",
    },
    {
      title: "Expose managed-hosting pricing as soon as it exists.",
      meta: "18 submissions · new",
      body: "Small agencies want to compare self-hosting with a hosted option before they move operational data into the product.",
    },
  ],
} as const;

export const changelogCopy = {
  title: "Changelog | crew.day",
  description: "Public changelog for crew.day site and product milestones.",
  eyebrow: "Changelog",
  heading: "The public changelog starts with the site foundation.",
  intro:
    "This page is a static v1 stub until the MDX-backed changelog entries begin shipping with release notes.",
  sections: [
    {
      eyebrow: "Phase 0",
      heading: "Public-site scaffold",
      body: "The Astro site, static output, design-token gate, and isolated deployment path are in place.",
    },
    {
      eyebrow: "Phase 1",
      heading: "Landing pages and demo surface",
      body: "The landing page, persona pages, legal pages, and reserved demo anchors establish the public route map before the scenario picker and video-first demo frame land.",
    },
  ],
} as const;

export const legalPages = {
  terms: {
    title: "Terms of service | crew.day",
    description: "Generic v1 terms of service for crew.day's public site and future managed service.",
    eyebrow: "Legal",
    heading: "Terms of service",
    intro:
      "These v1 terms are a plain-language stub for the public site and future managed service. They will be replaced with reviewed terms before paid managed accounts launch.",
    sections: [
      {
        heading: "Use of the public site",
        body: "You may browse the marketing pages without an account. Do not interfere with the site, attempt unauthorized access, or use the site to harm other people or systems.",
      },
      {
        heading: "Accounts and the app",
        body: "Account creation and login happen on app.crew.day. The app may have additional terms when the managed service opens.",
      },
      {
        heading: "No production reliance before launch",
        body: "Roadmap pages and demo surfaces describe planned and early functionality. They are not a service-level promise for production operations.",
      },
      {
        heading: "Self-hosting",
        body: "Self-hosted deployments are operated by their administrators. Those administrators control their infrastructure, data retention, users, and backups.",
      },
    ],
  },
  privacy: {
    title: "Privacy policy | crew.day",
    description:
      "crew.day public-site privacy posture: no marketing cookies, no analytics pixels, and minimal data on future feedback surfaces.",
    eyebrow: "Legal",
    heading: "Privacy policy",
    intro:
      "The marketing site is intentionally quiet: static pages, self-hosted assets, no analytics pixels, and no cookies on the public marketing routes.",
    sections: [
      {
        heading: "Marketing pages",
        body: "The public marketing pages do not set cookies, load third-party analytics, embed tracking pixels, or call external marketing scripts.",
      },
      {
        heading: "Account links",
        body: "Sign-up and login links take you to app.crew.day. Account handling belongs to the app surface, not these static pages.",
      },
      {
        heading: "Future suggestion box",
        body: "The planned suggestion box uses one site-origin auth cookie after the app handshake. Submission text is redacted before storage, and public board pages never show raw submissions, emails, user identities, workspace identities, or precise personal timestamps.",
      },
      {
        heading: "Email",
        body: "Future notification email is opt-in only, used for operator-triggered updates, and never passed to the clustering service.",
      },
      {
        heading: "Infrastructure logs",
        body: "The site design avoids client IP logging on static routes, and the future API path hashes or truncates network identifiers as specified by the deployment security posture.",
      },
    ],
  },
} as const;
