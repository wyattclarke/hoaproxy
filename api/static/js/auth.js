/**
 * Shared authentication utilities.
 * - Stores JWT in localStorage
 * - Injects Bearer header into fetch calls
 * - Provides helpers for login state and redirects
 */

const Auth = (() => {
  const TOKEN_KEY = "hoaware_token";
  const USER_KEY = "hoaware_user";

  function getToken() {
    return localStorage.getItem(TOKEN_KEY);
  }

  function setToken(token) {
    localStorage.setItem(TOKEN_KEY, token);
  }

  function clearToken() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  }

  function isLoggedIn() {
    return Boolean(getToken());
  }

  function getCachedUser() {
    try {
      return JSON.parse(localStorage.getItem(USER_KEY));
    } catch {
      return null;
    }
  }

  function setCachedUser(user) {
    localStorage.setItem(USER_KEY, JSON.stringify(user));
  }

  /**
   * Fetch wrapper that injects the Bearer header when a token exists.
   */
  async function fetchJson(path, options = {}) {
    const token = getToken();
    const isFormData = options.body instanceof FormData;
    const headers = {
      ...(isFormData ? {} : { "Content-Type": "application/json" }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    };
    const res = await fetch(path, { ...options, headers });
    if (res.status === 401) {
      clearToken();
      if (!window.location.pathname.startsWith("/login")) {
        window.location.href = "/login";
      }
      throw new Error("Session expired. Please log in again.");
    }
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (body.detail) detail = body.detail;
      } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  }

  function requireAuth() {
    if (!isLoggedIn()) {
      window.location.href = "/login";
      return false;
    }
    return true;
  }

  async function logout() {
    try {
      await fetchJson("/auth/logout", { method: "POST" });
    } catch (_) {
      // Ignore errors on logout
    }
    clearToken();
    window.location.href = "/";
  }

  /**
   * Render the auth-aware nav bar links with mobile hamburger toggle.
   * Call this from any page: Auth.renderNav(document.getElementById("navContainer"))
   */
  function renderNav(container) {
    if (!container) return;
    if (isLoggedIn()) {
      const user = getCachedUser();
      const name = (user && user.display_name) || (user && user.email) || "Account";
      container.innerHTML =
        '<a class="btn" href="/dashboard">Dashboard</a>' +
        '<a class="btn" href="/my-proxies">My Proxies</a>' +
        '<a class="btn" href="/proposals">Proposals</a>' +
        '<a class="btn" href="/legal">Legal</a>' +
        '<a class="btn" id="logoutBtn">Logout</a>';
      const logoutBtn = container.querySelector("#logoutBtn");
      if (logoutBtn) logoutBtn.addEventListener("click", logout);
    } else {
      container.innerHTML =
        '<a class="btn" href="/legal">Legal</a>' +
        '<a class="btn" href="/login">Login</a>' +
        '<a class="btn primary" href="/register">Register</a>';
    }
    _injectHamburger(container);
  }

  /**
   * Inject a hamburger button before the nav container for mobile.
   * Toggles .nav-open on the nav container when tapped.
   */
  function _injectHamburger(navEl) {
    if (!navEl || !navEl.parentElement) return;
    // Avoid duplicating
    if (navEl.parentElement.querySelector(".nav-hamburger")) return;
    const btn = document.createElement("button");
    btn.className = "nav-hamburger";
    btn.setAttribute("aria-label", "Toggle navigation");
    btn.innerHTML = "<span></span><span></span><span></span>";
    btn.addEventListener("click", function () {
      const open = navEl.classList.toggle("nav-open");
      btn.classList.toggle("open", open);
    });
    // Insert hamburger right before the nav container
    navEl.parentElement.insertBefore(btn, navEl);
  }

  return {
    getToken,
    setToken,
    clearToken,
    isLoggedIn,
    getCachedUser,
    setCachedUser,
    fetchJson,
    requireAuth,
    logout,
    renderNav,
  };
})();
