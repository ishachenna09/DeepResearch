import { ReactNode } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import api from "../../lib/api";
import { useAuthStore } from "../../store/auth";

const NAV = [
  { to: "/", label: "Research", end: true },
  { to: "/system", label: "System", end: false },
];

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
        <div className="flex items-center gap-6">
          <NavLink to="/" className="flex items-center gap-2">
            <span className="rounded bg-indigo-600 px-2 py-1 text-sm font-bold">DR</span>
            <span className="font-semibold">DeepResearch</span>
          </NavLink>
          <nav className="flex items-center gap-1 text-sm">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `rounded px-3 py-1 ${
                    isActive ? "bg-slate-800 text-slate-100" : "text-slate-400 hover:text-slate-200"
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
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
