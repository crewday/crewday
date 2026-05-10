export type SiteLink = {
  label: string;
  href: string;
};

export const siteCopy = {
  skipLink: "Skip to content",
  brand: "crew.day",
  header: {
    accountLabel: "Account",
  },
  footer: {
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
