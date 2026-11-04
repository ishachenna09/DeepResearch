import axios from "axios";
import { useAuthStore } from "../store/auth";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1",
  withCredentials: true, // refresh token lives in an HttpOnly cookie
});

// Attach access token to every request
api.interceptors.request.use((config) => {
  const token = useAuthStore.getState().accessToken;
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// On 401 → silently refresh once, then retry
let refreshing: Promise<string> | null = null;

api.interceptors.response.use(
  (res) => res,
  async (error) => {
    const original = error.config;
    const isAuthCall = original?.url?.startsWith("/auth/");
    if (error.response?.status === 401 && !original._retried && !isAuthCall) {
      original._retried = true;
      try {
        refreshing ??= axios
          .post(
            `${api.defaults.baseURL}/auth/refresh`,
            {},
            { withCredentials: true },
          )
          .then((r) => {
            useAuthStore.getState().setAccessToken(r.data.access_token);
            return r.data.access_token as string;
          })
          .finally(() => (refreshing = null));
        const newToken = await refreshing;
        original.headers.Authorization = `Bearer ${newToken}`;
        return api(original);
      } catch {
        useAuthStore.getState().clear();
        window.location.href = "/login";
      }
    }
    return Promise.reject(error);
  },
);

export default api;
