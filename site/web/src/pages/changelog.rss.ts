import rss from "@astrojs/rss";
import type { APIContext } from "astro";

import { changelogCopy } from "@/content/en/pages";

export function GET(context: APIContext): Promise<Response> {
  return rss({
    title: changelogCopy.title,
    description: changelogCopy.description,
    site: context.site ?? "https://crew.day",
    items: changelogCopy.sections.map((section) => {
      const anchor = section.eyebrow.toLowerCase().replace(/[^a-z0-9]+/gu, "-").replace(/^-|-$/gu, "");
      // @astrojs/rss derives each item's <guid> from its link, so the
      // per-section anchor is what keeps the guids unique.
      return {
        title: `${section.eyebrow} — ${section.heading}`,
        description: section.body,
        link: `/changelog#${anchor}`,
      };
    }),
  });
}
