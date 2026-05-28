import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";
import ChatLog from "@/components/chat/ChatLog";

describe("ChatLog", () => {
  it("renders basic markdown for agent messages", () => {
    const { container } = render(
      <ChatLog
        messages={[
          {
            at: "2026-05-10T10:00:00Z",
            kind: "agent",
            body:
              "### **Communication** `plan` [runbook](https://example.com/runbook)\n\nFirst line\nsecond line\n\n- **Bring towels**\n- Check `linen`\n\n```sh\nnpm test\n```\n\n####### Not a heading\n#No heading\n\nSee [guide](https://example.com/guide).",
          },
        ]}
      />,
    );

    const heading = screen.getByRole("heading", {
      name: "Communication plan runbook",
    });
    expect(heading.tagName).toBe("H4");
    expect(heading).not.toHaveTextContent("#");
    expect(heading.querySelector("strong")).toHaveTextContent("Communication");
    expect(heading.querySelector("code")).toHaveTextContent("plan");
    expect(screen.getByRole("link", { name: "runbook" })).toHaveAttribute(
      "href",
      "https://example.com/runbook",
    );
    expect(screen.getByText("Bring towels")).toBeInTheDocument();
    expect(screen.getByText("Bring towels").tagName).toBe("STRONG");
    expect(screen.getByText("linen").tagName).toBe("CODE");
    expect(screen.getByText("npm test").closest("pre")).not.toBeNull();
    expect(screen.getByText("####### Not a heading")).toBeInTheDocument();
    expect(screen.getByText("#No heading")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "guide" })).toHaveAttribute(
      "href",
      "https://example.com/guide",
    );
    expect(screen.getByRole("link", { name: "guide" })).toHaveAttribute(
      "target",
      "_blank",
    );
    expect(screen.getByRole("link", { name: "guide" })).toHaveAttribute(
      "rel",
      "noopener noreferrer",
    );
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(container.querySelector(".chat-markdown__paragraph br")).not.toBeNull();
  });

  it("keeps same-origin and mail links in the current tab", () => {
    render(
      <ChatLog
        messages={[
          {
            at: "2026-05-10T10:00:00Z",
            kind: "agent",
            body: "Open [task](/w/crewday/tasks/abc) or [mail](mailto:ops@example.com).",
          },
        ]}
      />,
    );

    const taskLink = screen.getByRole("link", { name: "task" });
    expect(taskLink).toHaveAttribute("href", "/w/crewday/tasks/abc");
    expect(taskLink).not.toHaveAttribute("target");
    expect(taskLink).not.toHaveAttribute("rel");

    const mailLink = screen.getByRole("link", { name: "mail" });
    expect(mailLink).toHaveAttribute("href", "mailto:ops@example.com");
    expect(mailLink).not.toHaveAttribute("target");
    expect(mailLink).not.toHaveAttribute("rel");
  });

  it("renders property handoff links and navigates only after click", async () => {
    render(
      <MemoryRouter initialEntries={["/w/crewday/chat"]}>
        <ChatLog
          messages={[
            {
              at: "2026-05-10T10:00:00Z",
              kind: "agent",
              body: "I created Oak House.",
              links: [
                {
                  rel: "detail",
                  label: "Open property",
                  route: "property.detail",
                  href: "/w/crewday/property/prop_1",
                },
                {
                  rel: "related.list",
                  label: "View stays for property",
                  route: "stays.index",
                  href: "/w/crewday/stays?property_id=prop_1",
                },
                {
                  rel: "unsafe.create",
                  label: "Create stay now",
                  route: "stays.index",
                  href: "/w/crewday/api/v1/stays",
                },
              ],
              agent_links: {
                links: [],
                items: [
                  {
                    index: 0,
                    links: [
                      {
                        rel: "item.self",
                        label: "Open listed property",
                        route: "property.detail",
                        href: "/w/crewday/property/prop_2",
                      },
                    ],
                  },
                ],
              },
            },
          ]}
        />
        <LocationProbe />
      </MemoryRouter>,
    );

    const link = screen.getByRole("link", { name: "Open property" });
    const staysLink = screen.getByRole("link", { name: "View stays for property" });
    expect(link).toHaveAttribute("href", "/w/crewday/property/prop_1");
    expect(staysLink).toHaveAttribute("href", "/w/crewday/stays?property_id=prop_1");
    expect(screen.getByRole("link", { name: "Open listed property" })).toHaveAttribute(
      "href",
      "/w/crewday/property/prop_2",
    );
    expect(screen.queryByRole("link", { name: "Create stay now" })).toBeNull();
    expect(screen.getByTestId("location-probe")).toHaveTextContent("/w/crewday/chat");

    fireEvent.click(link);

    await waitFor(() => {
      expect(screen.getByTestId("location-probe")).toHaveTextContent(
        "/w/crewday/property/prop_1",
      );
    });

    fireEvent.click(staysLink);

    await waitFor(() => {
      expect(screen.getByTestId("location-probe")).toHaveTextContent(
        "/w/crewday/stays?property_id=prop_1",
      );
    });
  });

  it("rejects unsafe agent navigation links without hiding the message body", () => {
    render(
      <MemoryRouter>
        <ChatLog
          messages={[
            {
              at: "2026-05-10T10:00:00Z",
              kind: "agent",
              body: "The message still renders.",
              links: [
                { label: "Script", route: "property.detail", href: "javascript:alert(1)" },
                {
                  label: "External",
                  route: "property.detail",
                  href: "https://example.com/w/crewday/tasks",
                },
                {
                  label: "Protocol relative",
                  route: "property.detail",
                  href: "//example.com/w/crewday/tasks",
                },
                {
                  label: "API",
                  route: "property.detail",
                  href: "/w/crewday/api/v1/properties",
                },
                { label: "Bare API", route: "property.detail", href: "/api/v1/properties" },
                { label: "No slash", route: "property.detail", href: "property/prop_1" },
                {
                  label: "Unknown route",
                  route: "property.unknown",
                  href: "/w/crewday/property/prop_1",
                },
                {
                  label: "Mismatched route",
                  route: "employee.detail",
                  href: "/w/crewday/property/prop_1",
                },
                {
                  label: "Bad percent",
                  route: "property.detail",
                  href: "/w/crewday/property/%",
                },
                { label: "   ", route: "property.detail", href: "/w/crewday/property/prop_1" },
              ],
            },
          ]}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("The message still renders.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Script" })).toBeNull();
    expect(screen.queryByRole("link", { name: "External" })).toBeNull();
    expect(screen.queryByRole("link", { name: "API" })).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Agent suggested links" })).toBeNull();
  });

  it("keeps approval actions separate from navigation links", () => {
    const decisions: Array<[number, "approve" | "details"]> = [];
    render(
      <ChatLog
        messages={[
          {
            at: "2026-05-10T10:00:00Z",
            kind: "action",
            body: "Create stay at Oak House?",
            links: [
              {
                label: "Open property",
                route: "property.detail",
                href: "/w/crewday/property/prop_1",
              },
            ],
          },
        ]}
        onDecideAction={(idx, decision) => decisions.push([idx, decision])}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(decisions).toEqual([[0, "approve"]]);
    expect(screen.getByRole("button", { name: "Details" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open property" })).toBeNull();
  });

  it("does not inject raw HTML from agent messages", () => {
    const { container } = render(
      <ChatLog
        messages={[
          {
            at: "2026-05-10T10:00:00Z",
            kind: "agent",
            body:
              "<img src=x onerror=alert(1)>\n\n# <script>alert('xss')</script>\n\n[jump](javascript:alert(1))",
          },
        ]}
      />,
    );

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
    expect(screen.getByText(/<img src=x onerror=alert\(1\)>/u)).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "<script>alert('xss')</script>" }),
    ).toBeInTheDocument();
    expect(screen.getByText("jump")).toBeInTheDocument();
  });

  it("keeps heading parsing out of list and code contexts", () => {
    render(
      <ChatLog
        messages={[
          {
            at: "2026-05-10T10:00:00Z",
            kind: "agent",
            body:
              "### Closing marker ###\n\n- Keep list context\n# Not a heading inside list context\n\n```md\n## Not a heading in code\n```",
          },
        ]}
      />,
    );

    expect(screen.getByRole("heading", { name: "Closing marker" })).not.toHaveTextContent(
      "#",
    );
    expect(screen.getByText("# Not a heading inside list context")).toBeInTheDocument();
    expect(screen.getByText("## Not a heading in code").closest("pre")).not.toBeNull();
  });

  it("renders activity above the running indicator and final agent bubble", () => {
    const { rerender } = render(
      <ChatLog
        messages={[]}
        typing
        activity={{ typing: true, label: "Checking tasks" }}
      />,
    );

    expect(screen.getAllByText("Checking tasks")).toHaveLength(2);
    expect(screen.getByRole("status")).toHaveTextContent("Checking tasks");
    expect(screen.getByText("Agent is typing")).toBeInTheDocument();

    rerender(
      <ChatLog
        messages={[
          {
            at: "2026-05-10T10:00:00Z",
            kind: "agent",
            body: "Done.",
          },
        ]}
        activity={{ typing: false, label: "Checking tasks", status: "completed" }}
      />,
    );

    const activity = screen.getByText("Checking tasks");
    const reply = screen.getByText("Done.");
    expect(activity.compareDocumentPosition(reply) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull();
  });
});

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location-probe">{location.pathname + location.search}</span>;
}
