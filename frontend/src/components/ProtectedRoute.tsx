import { ReactNode, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "../store/auth";

export default function ProtectedRoute({ children }: { children: ReactNode }) {
  const accessToken = useAuthStore((s) => s.accessToken);
  const navigate = useNavigate();

  useEffect(() => {
    if (!accessToken) navigate("/login", { replace: true });
  }, [accessToken, navigate]);

  return accessToken ? <>{children}</> : null;
}
