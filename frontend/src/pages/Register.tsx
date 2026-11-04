import { FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import api from "../lib/api";

export default function Register() {
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await api.post("/auth/register", {
        full_name: fullName,
        email,
        password,
      });
      navigate("/login");
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setError(typeof detail === "string" ? detail : "Registration failed");
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
        <h1 className="mb-6 text-2xl font-bold">Create your account</h1>
        {error && (
          <p className="mb-4 rounded bg-rose-950 px-3 py-2 text-sm text-rose-300">{error}</p>
        )}
        <label className="mb-1 block text-sm text-slate-400">Full name</label>
        <input
          value={fullName}
          onChange={(e) => setFullName(e.target.value)}
          required
          className="mb-4 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 outline-none focus:border-indigo-500"
        />
        <label className="mb-1 block text-sm text-slate-400">Email</label>
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
          className="mb-4 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 outline-none focus:border-indigo-500"
        />
        <label className="mb-1 block text-sm text-slate-400">Password (min 8 chars)</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          minLength={8}
          required
          className="mb-6 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 outline-none focus:border-indigo-500"
        />
        <button
          disabled={loading}
          className="w-full rounded bg-indigo-600 py-2 font-semibold hover:bg-indigo-500 disabled:opacity-50"
        >
          {loading ? "Creating…" : "Create account"}
        </button>
        <p className="mt-4 text-center text-sm text-slate-400">
          Already registered?{" "}
          <Link to="/login" className="text-indigo-400 hover:underline">
            Sign in
          </Link>
        </p>
      </form>
    </div>
  );
}
