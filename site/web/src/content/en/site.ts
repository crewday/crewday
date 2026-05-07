export type SiteLink = {
  label: string;
  href: string;
};

export const siteCopy = {
  skipLink: "Skip to content",
  brand: "crew.day",
  header: {
    navLabel: "Main navigation",
    accountLabel: "Account",
    navLinks: [
      { label: "For owners", href: "/for-owners" },
      { label: "For agencies", href: "/for-agencies" },
      { label: "For housekeepers", href: "/for-housekeepers" },
      { label: "Why crew.day", href: "/why-crewday" },
      { label: "Pricing", href: "/pricing" },
    ],
  },
  footer: {
    label: "Site footer",
    summary:
      "A public front door for the crew.day operations back-office. Static pages, no tracking pixels, and no marketing cookies.",
    columns: [
      {
        heading: "Product",
        links: [
          { label: "Why crew.day", href: "/why-crewday" },
          { label: "Pricing", href: "/pricing" },
          { label: "Changelog", href: "/changelog" },
        ],
      },
      {
        heading: "Audiences",
        links: [
          { label: "Owners", href: "/for-owners" },
          { label: "Agencies", href: "/for-agencies" },
          { label: "Housekeepers", href: "/for-housekeepers" },
        ],
      },
      {
        heading: "Legal",
        links: [
          { label: "Privacy", href: "/legal/privacy" },
          { label: "Terms", href: "/legal/terms" },
        ],
      },
    ],
  },
} as const;
