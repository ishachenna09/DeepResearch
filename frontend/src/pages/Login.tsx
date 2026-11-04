import { FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import api from "../lib/api";
import { useAuthStore } from "../store/auth";

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const { setAccessToken, setUser } = useAuthStore();
  const navigate = useNavigate();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const { data } = await api.post("/auth/login", { email, password });
      setAccessToken(data.access_token);
      const me = await api.get("/auth/me");
      setUser(me.data);
      navigate("/");
    } catch (err: any) {
      setError(err.response?.data?.detail ?? "Login failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-950">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-xl border border-slate-800 bg-slate-900 p-8"
      >
        <h1 className="mb-6 text-2xl font-bold">Sign in to SupportIQ</h1>
        {error && (
          <p className="mb-4 rounded bg-rose-950 px-3 py-2 text-sm text-rose-300">{error}</p>
        )}
        <label className="mb-1 block text-sm text-slate-400">Email</label>
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
          className="mb-4 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 outline-none focus:border-indigo-500"
        />
        <label className="mb-1 block text-sm text-slate-400">Password</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          className="mb-6 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 outline-none focus:border-indigo-500"
        />
        <button
          disabled={loading}
          className="w-full rounded bg-indigo-600 py-2 font-semibold hover:bg-indigo-500 disabled:opacity-50"
        >
          {loading ? "Signing in…" : "Sign in"}
        </button>
        <p className="mt-4 text-center text-sm text-slate-400">
          No account?{" "}
          <Link to="/register" className="text-indigo-400 hover:underline">
            Register
          </Link>
        </p>
      </form>
    </div>
  );
}
