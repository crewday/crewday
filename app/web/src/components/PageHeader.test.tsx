import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { BrowserRouter, Link, MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { NavHistoryProvider, useNavHistory } from "@/context/NavHistoryContext";
import PageHeader from "./PageHeader";

function renderRoutes(initial = "/w/acme/schedule"): ReactElement {
  return (
    <MemoryRouter initialEntries={[initial]}>
      <NavHistoryProvider>
        <Routes>
          <Route
            path="/w/acme/schedule"
            element={
              <>
                <Link to="/w/acme/task/task_1">Open task</Link>
                <LocationProbe />
              </>
            }
          />
          <Route
            path="/w/acme/task/:id"
            element={
              <>
                <PageHeader title="Task" />
                <LocationProbe />
              </>
            }
          />
          <Route
            path="/w/acme/me"
            element={
              <>
                <Link to="/w/acme/history">History</Link>
                <LocationProbe />
              </>
            }
          />
          <Route
            path="/w/acme/history"
            element={
              <>
                <PageHeader title="History" />
                <Link to="/w/acme/history?tab=chats">Chats</Link>
                <Link to="/w/acme/history?tab=expenses">Expenses</Link>
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

function renderBrowserRoutes(initial: string): ReactElement {
  window.history.replaceState(null, "", initial);

  return (
    <BrowserRouter>
      <NavHistoryProvider>
        <Routes>
          <Route
            path="/w/acme/employees"
            element={
              <>
                <Link to="/w/acme/employee/emp_1">View employee</Link>
                <LocationProbe />
              </>
            }
          />
          <Route
            path="/w/acme/employee/:id"
            element={
              <>
                <PageHeader title="Employee" />
                <a href="#shifts">Shifts</a>
                <a href="#payslips">Payslips</a>
                <LocationProbe />
                <NavHistoryProbe />
              </>
            }
          />
        </Routes>
      </NavHistoryProvider>
    </BrowserRouter>
  );
}

function LocationProbe(): ReactElement {
  const location = useLocation();
  return <span data-testid="location">{location.pathname + location.search + location.hash}</span>;
}

function NavHistoryProbe(): ReactElement {
  const { backTarget, canGoBack } = useNavHistory();
  return (
    <>
      <span data-testid="can-go-back">{canGoBack ? "yes" : "no"}</span>
      <span data-testid="back-target">{backTarget ?? ""}</span>
    </>
  );
}

describe("PageHeader NavHistory back", () => {
  it("uses browser back for a cross-route detail flow", async () => {
    render(renderRoutes());

    fireEvent.click(screen.getByRole("link", { name: "Open task" }));
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/w/acme/task/task_1");
    });

    fireEvent.click(screen.getByRole("button", { name: "Back" }));

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/w/acme/schedule");
    });
  });

  it("skips query-only same-path entries before going back", async () => {
    render(renderRoutes("/w/acme/me"));

    fireEvent.click(screen.getByRole("link", { name: "History" }));
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/w/acme/history");
    });

    fireEvent.click(screen.getByRole("link", { name: "Chats" }));
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/w/acme/history?tab=chats");
    });

    fireEvent.click(screen.getByRole("link", { name: "Expenses" }));
    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/w/acme/history?tab=expenses");
    });
    expect(screen.getByTestId("can-go-back")).toHaveTextContent("yes");

    fireEvent.click(screen.getByRole("button", { name: "Back" }));

    await waitFor(() => {
      expect(screen.getByTestId("location")).toHaveTextContent("/w/acme/me");
    });
  });

  it("skips hash-only same-route browser entries before going back", async () => {
    try {
      const initialHistoryLength = window.history.length;
      render(renderBrowserRoutes("/w/acme/employees"));

      fireEvent.click(screen.getByRole("link", { name: "View employee" }));
      await waitFor(() => {
        expect(screen.getByTestId("location")).toHaveTextContent("/w/acme/employee/emp_1");
      });
      await waitFor(() => {
        expect(screen.getByTestId("back-target")).toHaveTextContent("/w/acme/employees");
        expect(screen.getByRole("button", { name: "Back" })).toBeInTheDocument();
      });

      fireEvent.click(screen.getByRole("link", { name: "Shifts" }));
      await waitFor(() => {
        expect(screen.getByTestId("location")).toHaveTextContent("/w/acme/employee/emp_1#shifts");
      });
      expect(window.location.pathname + window.location.hash).toBe("/w/acme/employee/emp_1#shifts");

      fireEvent.click(screen.getByRole("link", { name: "Payslips" }));
      await waitFor(() => {
        expect(screen.getByTestId("location")).toHaveTextContent("/w/acme/employee/emp_1#payslips");
      });
      expect(window.location.pathname + window.location.hash).toBe("/w/acme/employee/emp_1#payslips");
      expect(window.history.length).toBeGreaterThanOrEqual(initialHistoryLength + 3);
      expect(screen.getByTestId("back-target")).toHaveTextContent("/w/acme/employees");

      fireEvent.click(screen.getByRole("button", { name: "Back" }));

      await waitFor(() => {
        expect(screen.getByTestId("location")).toHaveTextContent("/w/acme/employees");
      });
      expect(window.location.pathname + window.location.hash).toBe("/w/acme/employees");
    } finally {
      window.history.replaceState(null, "", "/");
    }
  });

  it("uses the route parent link when there is no previous different path", () => {
    render(renderRoutes("/w/acme/history?tab=expenses"));

    const backLink = screen.getByRole("link", { name: "Back to My profile" });

    expect(backLink).toHaveAttribute("href", "/w/acme/me");
    expect(screen.getByTestId("can-go-back")).toHaveTextContent("no");
  });
});
