import { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import api from "../../lib/api";
import { useAuthStore } from "../../store/auth";

export default function Layout({ children }: { children: ReactNode }) {
  const { user, clear } = useAuthStore();
  const navigate = useNavigate();

  const logout = async () => {
    try {
      await api.post("/auth/logout");
    } finally {
      clear();
      navigate("/login");
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="flex items-center justify-between border-b border-slate-800 px-6 py-3">
        <div className="flex items-center gap-2">
          <span className="rounded bg-indigo-600 px-2 py-1 text-sm font-bold">SIQ</span>
          <span className="font-semibold">SupportIQ</span>
        </div>
        <div className="flex items-center gap-4 text-sm">
          {user && (
            <span className="text-slate-400">
              {user.full_name} · <span className="uppercase text-indigo-400">{user.role}</span>
            </span>
          )}
          <button
            onClick={logout}
            className="rounded border border-slate-700 px-3 py-1 hover:bg-slate-800"
          >
            Logout
          </button>
        </div>
      </header>
      <main className="mx-auto max-w-5xl p-6">{children}</main>
    </div>
  );
}
