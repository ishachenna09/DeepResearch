import { ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import JobDetail from "./pages/JobDetail";
import Login from "./pages/Login";
import Register from "./pages/Register";
import Research from "./pages/Research";
import ProtectedRoute from "./components/ProtectedRoute";
import Layout from "./components/layout/Layout";

/** Every authenticated page shares one shell; only the inner page changes. */
function Shell({ children }: { children: ReactNode }) {
  return (
    <ProtectedRoute>
      <Layout>{children}</Layout>
    </ProtectedRoute>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/register" element={<Register />} />
      <Route
        path="/"
        element={
          <Shell>
            <Research />
          </Shell>
        }
      />
      <Route
        path="/jobs/:jobId"
        element={
          <Shell>
            <JobDetail />
          </Shell>
        }
      />
      <Route
        path="/system"
        element={
          <Shell>
            <Dashboard />
          </Shell>
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
