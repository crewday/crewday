import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { Link, MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { NavHistoryProvider, useNavHistory } from "@/context/NavHistoryContext";
import PageHeader from "./PageHeader";

function renderRoutes(initial = "/schedule"): ReactElement {
  return (
    <MemoryRouter initialEntries={[initial]}>
      <NavHistoryProvider>
        <Routes>
          <Route
            path="/schedule"
            element={
              <>
                <Link to="/task/task_1">Open task</Link>
                <LocationProbe />
              </>
            }
          />
          <Route
            path="/task/:id"
            element={
              <>
                <PageHeader title="Task" />
                <LocationProbe />
              </>
            }
          />
          <Route
            path="/me"
            element={
              <>
                <Link to="/history">History</Link>
                <LocationProbe />
              </>
            }
          />
          <Route
            path="/history"
            element={
              <>
                <PageHeader title="History" />
                <Link to="/history?tab=chats">Chats</Link>
                <Link to="/history?tab=expenses">Expenses</Link>
                <LocationProbe />
                <NavHistoryProbe />
              </>
            }
          />
        </Routes>
      </NavHistoryProvider>
    </MemoryRouter>
  );
}

function LocationProbe(): ReactElement {
  const location = useLocation();
  return <span data-testid="location">{location.pathname + location.search}</span>;
}

function NavHistoryProbe(): ReactElement {
  const { canGoBack } = useNavHistory();
  return <span data-testid="can-go-back">{canGoBack ? "yes" : "no"}</span>;
}

describe("PageHeader NavHistory back", () => {
  it("uses browser back for a cross-route detail flow", async () => {
    render(renderRoutes());

    fireEvent.click(screen.getByRole("link", { name: "Open task" }));
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/task/task_1");
    });

    fireEvent.click(screen.getByRole("button", { name: "Back" }));

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/schedule");
    });
  });

  it("skips query-only same-path entries before going back", async () => {
    render(renderRoutes("/me"));

    fireEvent.click(screen.getByRole("link", { name: "History" }));
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/history");
    });

    fireEvent.click(screen.getByRole("link", { name: "Chats" }));
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/history?tab=chats");
    });

    fireEvent.click(screen.getByRole("link", { name: "Expenses" }));
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/history?tab=expenses");
    });
    expect(screen.getByTestId("can-go-back")).toHaveTextContent("yes");

    fireEvent.click(screen.getByRole("button", { name: "Back" }));

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/me");
    });
  });

  it("uses the route parent link when there is no previous different path", () => {
    render(renderRoutes("/history?tab=expenses"));

    const backLink = screen.getByRole("link", { name: "Back to My profile" });

    expect(backLink).toHaveAttribute("href", "/me");
    expect(screen.getByTestId("can-go-back")).toHaveTextContent("no");
  });
});
