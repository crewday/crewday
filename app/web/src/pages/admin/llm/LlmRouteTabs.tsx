import PageTabs from "@/components/PageTabs";

interface LlmRouteTabsProps {
  activeKey: "graph" | "usage";
}

export default function LlmRouteTabs({ activeKey }: LlmRouteTabsProps) {
  return (
    <PageTabs
      ariaLabel="LLM admin sections"
      activeKey={activeKey}
      tabs={[
        { key: "graph", label: "Graph", to: "/admin/llm/graph" },
        { key: "usage", label: "Usage", to: "/admin/llm/usage" },
      ]}
    />
  );
}
