import { render, screen } from "@testing-library/react";
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
              "First line\nsecond line\n\n- **Bring towels**\n- Check `linen`\n\n```sh\nnpm test\n```\n\nSee [guide](https://example.com/guide).",
          },
        ]}
      />,
    );

    expect(screen.getByText("Bring towels")).toBeInTheDocument();
    expect(screen.getByText("Bring towels").tagName).toBe("STRONG");
    expect(screen.getByText("linen").tagName).toBe("CODE");
    expect(screen.getByText("npm test").closest("pre")).not.toBeNull();
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

  it("does not inject raw HTML from agent messages", () => {
    const { container } = render(
      <ChatLog
        messages={[
          {
            at: "2026-05-10T10:00:00Z",
            kind: "agent",
            body:
              "<img src=x onerror=alert(1)>\n\n<script>alert('xss')</script>\n\n[jump](javascript:alert(1))",
          },
        ]}
      />,
    );

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
    expect(screen.getByText(/<img src=x onerror=alert\(1\)>/u)).toBeInTheDocument();
    expect(screen.getByText("jump")).toBeInTheDocument();
  });
});
