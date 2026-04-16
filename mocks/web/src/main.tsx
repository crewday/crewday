import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { makeQueryClient } from "@/lib/queryClient";
import { RoleProvider } from "@/context/RoleContext";
import { ThemeProvider } from "@/context/ThemeContext";
import { SseProvider } from "@/context/SseContext";

import "@/styles/tokens.css";
import "@/styles/reset.css";
import "@/styles/globals.css";

const queryClient = makeQueryClient();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ThemeProvider>
          <RoleProvider>
            <SseProvider>
              <App />
            </SseProvider>
          </RoleProvider>
        </ThemeProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
